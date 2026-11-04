from pathlib import Path

from visual_agent.acceptance_discrimination import judge_red_to_green
from visual_agent.anti_slop import detect_slop_in_code
from visual_agent.tamper_guard import TamperGuard
from visual_agent.task_review import _verification_step_covers_claim


def test_old_green_suite_is_not_completion() -> None:
    verdict = judge_red_to_green(base_passed=True, current_passed=True)
    assert verdict["verdict"] == "rejected"
    assert verdict["reason"] == "no red-to-green discrimination"
    assert judge_red_to_green(base_passed=False, current_passed=True)["verdict"] == "verified"


def test_whole_suite_command_does_not_cover_a_new_claim() -> None:
    step = {"argv": ["python", "-m", "pytest", "tests/"]}
    assert _verification_step_covers_claim(step, locked_requirement="add divide", files=["calc.py"]) is False
    specific = {"argv": ["python", "-m", "pytest", "tests/test_calc.py", "-q"]}
    assert _verification_step_covers_claim(specific, locked_requirement="calc divide", files=["calc.py"]) is True


def test_empty_test_and_hardcoded_true_are_blocking() -> None:
    findings = detect_slop_in_code("def test_check():\n    pass\n", "test_check.py", is_test_file=True)
    assert any(item.severity == "error" and item.category == "empty_test" for item in findings)
    product = detect_slop_in_code("def check():\n    return True\n", "new_feature.py", is_test_file=False)
    assert any(item.severity == "error" and item.category == "fake_return" for item in product)


def test_tamper_snapshot_survives_a_new_guard(tmp_path: Path) -> None:
    exam = tmp_path / "test_exam.py"
    exam.write_text("def test_one():\n    assert True\n", encoding="utf-8")
    first = TamperGuard(tmp_path)
    first.lock_files(["test_exam.py"])
    second = TamperGuard(tmp_path)
    second.load_persisted()
    assert second.protected_files["test_exam.py"] == first.protected_files["test_exam.py"]
    assert second.backup_blobs["test_exam.py"] == exam.read_bytes()
