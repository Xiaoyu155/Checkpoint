import json
from pathlib import Path

import pytest

from visual_agent.dag_scheduler import ContractDrivenDAGScheduler
from visual_agent.gemini4_vps_client import Gemini4Response
from visual_agent.model_workflow import build_plan_from_gemini4_text, plan_with_gemini4
from visual_agent.wish_intake import resolve_wish


def test_vague_page_wish_asks_and_does_not_touch_files(tmp_path: Path) -> None:
    page = tmp_path / "page.py"
    page.write_text("color = 'red'\n", encoding="utf-8")
    before = page.read_bytes()

    decision = resolve_wish("帮我改一下那个页面", tmp_path)

    assert decision.proceed is False
    assert decision.status == "awaiting_input"
    assert "哪个文件" in decision.question
    assert page.read_bytes() == before


def test_gemini4_plan_carries_a_specific_exam_and_no_placeholder_files(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(
        "import unittest\nimport calc\n\nclass TestDivide(unittest.TestCase):\n"
        "    def test_zero_divisor(self):\n        with self.assertRaises(ZeroDivisionError):\n"
        "            calc.divide(1, 0)\n",
        encoding="utf-8",
    )
    text = """
    {"tasks":[{"role":"coder","title":"add divide","target_files":["calc.py"],
      "assertion_cmd":"python -m unittest tests.test_calc",
      "question":"divide by zero raises ZeroDivisionError"}]}
    """
    plan = build_plan_from_gemini4_text("给计算器模块加一个除法函数，除数为0时报错", tmp_path, text)
    assert plan.tasks[0].target_files == ["calc.py"]
    assert plan.tasks[0].verification_command == 'python -m unittest discover -s "tests" -p "test_calc.py"'
    assert "ZeroDivisionError" in plan.tasks[0].acceptance_exam[0]["question"]
    assert "impl_core.py" not in plan.tasks[0].target_files
    with pytest.raises(ValueError):
        build_plan_from_gemini4_text("x", tmp_path, '{"tasks":[{"target_files":["impl_core.py"],"assertion_cmd":"python -c \\"print(1)\\""}]}')


def test_planner_rejects_acceptance_call_missing_required_target_argument(tmp_path: Path) -> None:
    module_dir = tmp_path / "feasi_test"
    module_dir.mkdir()
    (module_dir / "fee_calc.py").write_text(
        "def calc_maker_taker_fee(notional, is_maker):\n    return notional\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    payload = {
        "tasks": [{
            "role": "coder",
            "title": "minimum notional guard",
            "target_files": ["feasi_test/fee_calc.py"],
            "assertion_cmd": "python -m pytest tests/test_fee_calc_minimum_notional_acceptance.py -q",
            "question": "Does the function reject values below 10?",
            "acceptance_test": {
                "path": "tests/test_fee_calc_minimum_notional_acceptance.py",
                "source": (
                    "from decimal import Decimal\n"
                    "import pytest\n"
                    "from feasi_test.fee_calc import calc_maker_taker_fee\n\n"
                    "def test_below_minimum_raises():\n"
                    "    with pytest.raises(ValueError):\n"
                    "        calc_maker_taker_fee(Decimal('9.99'))\n"
                ),
            },
        }],
    }
    with pytest.raises(ValueError, match="omits required argument.*is_maker"):
        build_plan_from_gemini4_text("enforce minimum notional", tmp_path, json.dumps(payload))


def test_explicit_existing_pytest_command_is_not_replaced_by_generated_exam(tmp_path: Path) -> None:
    (tmp_path / "feasi_test").mkdir()
    (tmp_path / "feasi_test" / "fee_calc.py").write_text(
        "def calc_maker_taker_fee(notional, is_maker):\n    return notional\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_fee_calc.py").write_text(
        "def test_existing_fee_contract():\n    assert True\n",
        encoding="utf-8",
    )
    payload = {
        "tasks": [{
            "role": "coder",
            "title": "enforce minimum notional",
            "target_files": ["feasi_test/fee_calc.py", "tests/test_fee_calc.py"],
            "assertion_cmd": "python -m pytest tests/test_generated_exam.py -q",
            "question": "Does the fee contract hold?",
            "acceptance_test": {
                "path": "tests/test_generated_exam.py",
                "source": (
                    "from decimal import Decimal\n"
                    "from feasi_test.fee_calc import calc_maker_taker_fee\n\n"
                    "def test_exam():\n"
                    "    assert calc_maker_taker_fee(Decimal('10')) == Decimal('0.0020')\n"
                ),
            },
        }],
    }
    goal = '只验证、不写码；please run exactly `python -m pytest "tests/test_fee_calc.py" -q`.'
    plan = build_plan_from_gemini4_text(goal, tmp_path, json.dumps(payload))

    assert plan.tasks[0].verification_command == 'python -m pytest "tests/test_fee_calc.py" -q'
    assert plan.tasks[0].acceptance_test == {}
    assert plan.tasks[0].role == "verifier"
    assert plan.tasks[0].backend == "local"


def test_planner_failure_does_not_become_a_local_plan(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "visual_agent.gemini4_vps_client.invoke_gemini4_vps",
        lambda *_args, **_kwargs: Gemini4Response(False, "", error="gateway down"),
    )
    with pytest.raises(RuntimeError):
        plan_with_gemini4("给计算器模块加一个除法函数，除数为0时报错", tmp_path)


def test_generic_planner_receives_local_repo_map_and_ignores_secret_paths(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "greetings.py").write_text("def greet(name):\n    return f'Hello {name}'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_greetings.py").write_text(
        "import unittest\nimport greetings\n\nclass TestGreetings(unittest.TestCase):\n"
        "    def test_greet(self):\n        self.assertEqual(greetings.greet('Ada'), 'Hello Ada')\n",
        encoding="utf-8",
    )
    (tmp_path / "gemini_keys.json").write_text('{"api_key":"never include"}', encoding="utf-8")
    captured: dict[str, str] = {}
    response_text = json.dumps({
        "tasks": [{
            "role": "coder",
            "title": "Improve greeting",
            "target_files": ["greetings.py"],
            "assertion_cmd": "python -m unittest tests.test_greetings",
            "question": "greet returns a personalized message",
        }]
    })

    def planner(prompt: str, **_kwargs):
        captured["prompt"] = prompt
        return Gemini4Response(True, response_text)

    monkeypatch.setattr("visual_agent.gemini4_vps_client.invoke_gemini4_vps", planner)
    plan = plan_with_gemini4("improve greeting behavior", tmp_path)

    assert plan.tasks[0].target_files == ["greetings.py"]
    assert "greetings.py" in captured["prompt"]
    assert "def greet" in captured["prompt"]
    assert "gemini_keys.json" not in captured["prompt"]
    assert "never include" not in captured["prompt"]


def test_plan_rejects_inline_python_verification_code(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    unsafe = json.dumps({
        "tasks": [{
            "role": "coder",
            "title": "Add divide",
            "target_files": ["calc.py"],
            "assertion_cmd": "python -c \"import os; os.remove('important.py')\"",
            "question": "divide behaves correctly",
        }]
    })

    with pytest.raises(ValueError, match="unsafe acceptance command"):
        build_plan_from_gemini4_text("add divide", tmp_path, unsafe)


def test_plan_requires_locked_test_source_for_new_test_targets(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    response = json.dumps({
        "tasks": [{
            "role": "coder",
            "title": "Add divide",
            "target_files": ["calc.py", "tests/test_calc.py"],
            "assertion_cmd": "python -m unittest tests/test_calc.py",
            "question": "divide handles zero",
        }]
    })

    with pytest.raises(ValueError, match="without an acceptance_test source"):
        build_plan_from_gemini4_text("add divide", tmp_path, response)


def test_unittest_acceptance_command_rejects_pytest_style_test_source(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    response = json.dumps({
        "tasks": [{
            "role": "coder",
            "title": "Add divide",
            "target_files": ["calc.py"],
            "assertion_cmd": "python -m unittest tests/test_calc.py",
            "question": "divide works",
            "acceptance_test": {
                "path": "tests/test_calc.py",
                "source": "import calc\ndef test_divide():\n    assert calc.divide(4, 2) == 2\n",
            },
        }]
    })

    with pytest.raises(ValueError, match="unittest.TestCase"):
        build_plan_from_gemini4_text("add divide", tmp_path, response)


def test_gemini38_replans_once_after_unsafe_acceptance_command(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    invalid = json.dumps({
        "tasks": [{
            "role": "coder",
            "title": "Add divide",
            "target_files": ["calc.py"],
            "assertion_cmd": "python -c \"print(1)\"",
            "question": "divide handles zero",
        }]
    })
    corrected = json.dumps({
        "tasks": [{
            "role": "coder",
            "title": "Add divide",
            "target_files": ["calc.py"],
            "assertion_cmd": "python -m unittest tests.test_calc",
            "question": "divide handles zero",
            "acceptance_test": {
                "path": "tests/test_calc.py",
                "source": "import unittest\nimport calc\n\nclass TestCalc(unittest.TestCase):\n    def test_divide(self):\n        self.assertEqual(calc.divide(4, 2), 2)\n",
            },
        }]
    })
    prompts: list[str] = []

    def fallback(prompt: str, **_kwargs):
        prompts.append(prompt)
        response = invalid if len(prompts) == 1 else corrected
        return type("Response", (), {"success": True, "response": response, "error": None})()

    monkeypatch.setattr(
        "visual_agent.gemini4_vps_client.invoke_gemini4_vps",
        lambda *_args, **_kwargs: Gemini4Response(False, "", error="gateway down"),
    )
    monkeypatch.setattr("visual_agent.model_workflow._invoke_gemini38_json", fallback)

    plan = plan_with_gemini4("给 calc.py 添加除法", tmp_path, allow_gemini38_fallback=True)

    assert plan.planner_backend == "gemini38"
    assert plan.tasks[0].verification_command == 'python -m unittest discover -s "tests" -p "test_calc.py"'
    assert len(prompts) == 2
    assert "unsafe acceptance command" in prompts[1]


def test_planner_authored_acceptance_test_runs_red_before_worker(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    payload = json.dumps({
        "tasks": [{
            "role": "coder",
            "title": "Add divide",
            "target_files": ["calc.py"],
            "assertion_cmd": "python -m unittest tests/test_calc.py",
            "question": "divide computes a quotient",
            "acceptance_test": {
                "path": "tests/test_calc.py",
                "source": (
                    "import unittest\nimport calc\n\n"
                    "class TestCalc(unittest.TestCase):\n"
                    "    def test_divide(self):\n"
                    "        self.assertEqual(calc.divide(6, 3), 2)\n"
                ),
            },
        }]
    })
    plan = build_plan_from_gemini4_text("add divide to calc.py", tmp_path, payload)
    assert not (tmp_path / "tests" / "test_calc.py").exists()
    assert plan.tasks[0].verification_command == 'python -m unittest discover -s "tests" -p "test_calc.py"'

    from visual_agent.acceptance_discrimination import probe_workspace_command
    from visual_agent.subagent_orchestrator import SubagentOrchestrator

    SubagentOrchestrator(
        plan,
        workspace_root=tmp_path,
        probe_base_discrimination=True,
        enforce_test_tamper_guard=True,
    )
    assert (tmp_path / "tests" / "test_calc.py").is_file()
    probe = probe_workspace_command(
        command=plan.tasks[0].verification_command,
        workspace_root=tmp_path,
        timeout_seconds=120.0,
    )

    assert probe["status"] == "failed_on_base", probe


def test_placeholder_pipeline_requires_real_files(tmp_path: Path) -> None:
    scheduler = ContractDrivenDAGScheduler(tmp_path)
    with pytest.raises(ValueError):
        scheduler.plan_l3_pipeline("build a feature")
