from __future__ import annotations

import json
import sys
import shutil
import subprocess

import pytest

from visual_agent.task_review import (
    audit_task_completion,
    build_task_contract,
    capture_task_source_baseline,
    derive_task_completion_evidence,
    derive_task_source_changes,
    task_contract_allows_compile_only,
    task_review_error,
)


def _steps() -> list[dict[str, object]]:
    return [
        {
            "name": "focused-tests",
            "argv": [sys.executable, "-m", "pytest", "tests/test_login.py", "-q"],
        }
    ]


def _evidence(*, requirement: str = "修复登录错误", path: str = "src/login.py") -> dict[str, object]:
    requirement_id = str(build_task_contract(requirement)["requirements"][0]["id"])
    return {
        "result_kind": "change",
        "claims": [
            {
                "kind": "change",
                "requirement_ids": [requirement_id],
                "requirement": requirement,
                "result": "修复登录错误并增加失败状态处理",
                "files": [{"path": path, "state": "modified"}],
                "verification_steps": ["focused-tests"],
            }
        ],
        "unresolved_items": [],
        "known_risks": [],
    }


def _verification(*, status: str = "passed", step_status: str = "passed") -> dict[str, object]:
    return {
        "status": status,
        "records": [
            {
                "name": "focused-tests",
                "status": step_status,
                "exit_code": 0 if step_status == "passed" else 1,
                "elapsed_seconds": 0.25,
            }
        ],
    }


def _repo(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "src" / "login.py"
    source.parent.mkdir(parents=True)
    source.write_text("def login():\n    return False\n", encoding="utf-8")
    baseline = capture_task_source_baseline(repo)
    source.write_text("def login():\n    return True\n", encoding="utf-8")
    return repo, baseline


def _codes(review: dict[str, object]) -> set[str]:
    return {
        str(item.get("code") or "")
        for item in review.get("errors") or []
        if isinstance(item, dict)
    }


def test_task_review_approves_goal_bound_file_and_passing_step(tmp_path) -> None:
    repo, baseline = _repo(tmp_path)
    review = audit_task_completion(
        launch_goal="修复登录错误",
        submitted_goal="修复登录错误",
        summary="登录失败状态已经修复并通过聚焦测试",
        completion_evidence=_evidence(),
        requested_steps=_steps(),
        repo_root=repo,
        source_baseline=baseline,
        verification=_verification(),
    )

    assert review["valid"] is True
    assert review["verdict"] == "approved"
    assert review["trust"] == "with_limits"
    assert review["evidence_integrity"] == "verified"
    assert review["acceptance_adequacy"] == "insufficient"
    assert review["product_verdict"] == "indeterminate"
    assert review["goal_binding"] == {"pinned": True, "matches": True}
    assert review["user_report"]["completed"] == ["修复登录错误并增加失败状态处理"]
    assert review["user_report"]["can_trust"] == "with_limits"
    assert "实际完成：" in review["user_report_markdown"]
    assert "验收标准充分性：不足" in review["user_report_markdown"]
    assert "产品结论：无法判定" in review["user_report_markdown"]
    assert "可信结论：有限可信" in review["user_report_markdown"]


def test_task_review_rejects_goal_replacement_before_verification(tmp_path) -> None:
    repo, baseline = _repo(tmp_path)
    review = audit_task_completion(
        launch_goal="修复登录错误",
        submitted_goal="新增支付页面",
        summary="新增了支付页面",
        completion_evidence=_evidence(requirement="新增支付页面"),
        requested_steps=_steps(),
        repo_root=repo,
        source_baseline=baseline,
    )

    assert review["valid"] is False
    assert "goal_mismatch" in _codes(review)
    assert review["user_report"]["can_trust"] == "no"


def test_task_review_rejects_completion_without_a_pinned_launch_goal(tmp_path) -> None:
    repo, baseline = _repo(tmp_path)
    review = audit_task_completion(
        launch_goal="",
        submitted_goal="修复登录错误",
        summary="登录失败状态已经修复",
        completion_evidence=_evidence(),
        requested_steps=_steps(),
        repo_root=repo,
        source_baseline=baseline,
    )

    assert review["valid"] is False
    assert "launch_goal_unavailable" in _codes(review)


def test_task_review_rejects_generic_summary_unresolved_work_and_unknown_step(tmp_path) -> None:
    repo, baseline = _repo(tmp_path)
    evidence = _evidence()
    evidence["unresolved_items"] = ["登录超时仍未处理"]
    evidence["claims"][0]["verification_steps"] = ["invented-tests"]
    review = audit_task_completion(
        launch_goal="修复登录错误",
        submitted_goal="修复登录错误",
        summary="已完成",
        completion_evidence=evidence,
        requested_steps=_steps(),
        repo_root=repo,
        source_baseline=baseline,
    )

    assert review["valid"] is False
    assert {
        "summary_generic",
        "unresolved_items_present",
        "unknown_verification_step",
    }.issubset(_codes(review))
    assert review["user_report"]["not_completed"] == ["登录超时仍未处理"]


def test_task_review_rejects_outside_or_missing_file_evidence(tmp_path) -> None:
    repo, baseline = _repo(tmp_path)
    outside = audit_task_completion(
        launch_goal="修复登录错误",
        submitted_goal="修复登录错误",
        summary="登录错误修复有文件证据",
        completion_evidence=_evidence(path="../other/login.py"),
        requested_steps=_steps(),
        repo_root=repo,
        source_baseline=baseline,
    )
    missing = audit_task_completion(
        launch_goal="修复登录错误",
        submitted_goal="修复登录错误",
        summary="登录错误修复有文件证据",
        completion_evidence=_evidence(path="src/missing.py"),
        requested_steps=_steps(),
        repo_root=repo,
        source_baseline=baseline,
    )

    assert "file_path_outside_repo" in _codes(outside)
    assert "evidence_file_missing" in _codes(missing)


def test_task_review_rejects_unrelated_claim_and_failed_verification(tmp_path) -> None:
    repo, baseline = _repo(tmp_path)
    evidence = _evidence(requirement="优化支付结算")
    evidence["claims"][0]["result"] = "优化支付结算"
    review = audit_task_completion(
        launch_goal="修复登录错误",
        submitted_goal="修复登录错误",
        summary="支付结算已经优化",
        completion_evidence=evidence,
        requested_steps=_steps(),
        repo_root=repo,
        source_baseline=baseline,
        verification=_verification(status="failed", step_status="failed"),
    )

    assert review["valid"] is False
    assert {
        "unknown_requirement_id",
        "goal_items_uncovered",
        "verification_batch_not_passed",
        "verification_step_not_passed",
    }.issubset(_codes(review))


def test_task_review_rejects_preexisting_unchanged_file_as_task_output(tmp_path) -> None:
    repo = tmp_path / "repo"
    source = repo / "src" / "login.py"
    source.parent.mkdir(parents=True)
    source.write_text("def login():\n    return True\n", encoding="utf-8")
    baseline = capture_task_source_baseline(repo)

    review = audit_task_completion(
        launch_goal="修复登录错误",
        submitted_goal="修复登录错误",
        summary="登录错误已经修复",
        completion_evidence=_evidence(),
        requested_steps=_steps(),
        repo_root=repo,
        source_baseline=baseline,
        verification=_verification(),
    )

    assert review["valid"] is False
    assert {"file_change_not_attributed", "source_change_not_proven"}.issubset(_codes(review))


def test_task_review_rejects_change_goal_disguised_as_read_only_review(tmp_path) -> None:
    repo, baseline = _repo(tmp_path)
    evidence = {
        "result_kind": "review",
        "claims": [
            {
                "kind": "review",
                "requirement_ids": [build_task_contract("修复登录错误")["requirements"][0]["id"]],
                "requirement": "修复登录错误",
                "result": "审查了登录错误",
                "files": [],
                "verification_steps": ["focused-tests"],
            }
        ],
        "unresolved_items": [],
        "known_risks": [],
    }

    review = audit_task_completion(
        launch_goal="修复登录错误",
        submitted_goal="修复登录错误",
        summary="完成登录错误审查",
        completion_evidence=evidence,
        requested_steps=_steps(),
        repo_root=repo,
        source_baseline=baseline,
        verification=_verification(),
    )

    assert review["valid"] is False
    assert {"result_kind_conflicts_goal", "source_change_not_proven"}.issubset(_codes(review))


def test_task_review_rejects_product_fix_claimed_as_test_only(tmp_path) -> None:
    repo = tmp_path / "repo"
    test_file = repo / "tests" / "test_login.py"
    test_file.parent.mkdir(parents=True)
    baseline = capture_task_source_baseline(repo)
    test_file.write_text("def test_login():\n    assert True\n", encoding="utf-8")
    evidence = {
        "result_kind": "test",
        "claims": [
            {
                "kind": "test",
                "requirement_ids": [build_task_contract("修复登录错误")["requirements"][0]["id"]],
                "requirement": "修复登录错误",
                "result": "增加登录回归测试",
                "files": [{"path": "tests/test_login.py", "state": "created"}],
                "verification_steps": ["focused-tests"],
            }
        ],
        "unresolved_items": [],
        "known_risks": [],
    }

    review = audit_task_completion(
        launch_goal="修复登录错误",
        submitted_goal="修复登录错误",
        summary="增加登录回归测试",
        completion_evidence=evidence,
        requested_steps=_steps(),
        repo_root=repo,
        source_baseline=baseline,
        verification=_verification(),
    )

    assert review["valid"] is False
    assert {"result_kind_conflicts_goal", "implementation_change_not_proven"}.issubset(
        _codes(review)
    )


def test_task_review_rejects_one_weak_token_as_full_goal_coverage(tmp_path) -> None:
    repo, baseline = _repo(tmp_path)
    evidence = _evidence(requirement="登录")

    review = audit_task_completion(
        launch_goal="修复登录错误并增加超时测试",
        submitted_goal="修复登录错误并增加超时测试",
        summary="修复登录错误并增加超时测试",
        completion_evidence=evidence,
        requested_steps=_steps(),
        repo_root=repo,
        source_baseline=baseline,
    )

    assert review["valid"] is False
    assert {"unknown_requirement_id", "goal_items_uncovered"}.issubset(_codes(review))


def test_task_review_uses_file_path_role_instead_of_claim_kind(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    baseline = capture_task_source_baseline(repo)
    test_file = repo / "tests" / "test_login.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_login():\n    assert True\n", encoding="utf-8")
    goal = "修复登录错误"
    evidence = _evidence(requirement=goal, path="tests/test_login.py")
    evidence["claims"][0]["files"][0]["state"] = "created"

    review = audit_task_completion(
        launch_goal=goal,
        submitted_goal=goal,
        summary="增加了登录测试并声称完成产品修复",
        completion_evidence=evidence,
        requested_steps=_steps(),
        repo_root=repo,
        source_baseline=baseline,
        verification=_verification(),
    )

    assert review["valid"] is False
    assert {"required_artifact_role_missing", "implementation_change_not_proven"}.issubset(
        _codes(review)
    )


def test_task_review_defaults_unknown_development_wording_to_implementation(tmp_path) -> None:
    repo, baseline = _repo(tmp_path)
    goal = "支持暗色模式"
    requirement_id = build_task_contract(goal)["requirements"][0]["id"]
    evidence = {
        "result_kind": "review",
        "claims": [
            {
                "kind": "review",
                "requirement_ids": [requirement_id],
                "requirement": goal,
                "result": "审查了暗色模式",
                "files": [],
                "verification_steps": ["focused-tests"],
            }
        ],
        "unresolved_items": [],
        "known_risks": [],
    }

    review = audit_task_completion(
        launch_goal=goal,
        submitted_goal=goal,
        summary="仅审查暗色模式，没有修改产品",
        completion_evidence=evidence,
        requested_steps=_steps(),
        repo_root=repo,
        source_baseline=baseline,
        verification=_verification(),
    )

    assert build_task_contract(goal)["intent"] == "implementation"
    assert review["valid"] is False
    assert {"result_kind_conflicts_goal", "source_change_not_proven"}.issubset(_codes(review))


def test_task_review_requires_every_locked_parallel_requirement_id(tmp_path) -> None:
    repo, baseline = _repo(tmp_path)
    goal = "fix login bug and add timeout tests"
    contract = build_task_contract(goal, repo_root=repo)
    assert len(contract["requirements"]) == 2
    first = contract["requirements"][0]
    evidence = _evidence(requirement=str(first["text"]))

    review = audit_task_completion(
        launch_goal=goal,
        submitted_goal=goal,
        summary="fixed the login bug but omitted the requested timeout tests",
        completion_evidence=evidence,
        requested_steps=_steps(),
        repo_root=repo,
        task_contract=contract,
        source_baseline=baseline,
        verification=_verification(),
    )

    assert review["valid"] is False
    assert "goal_items_uncovered" in _codes(review)
    error = next(item for item in review["errors"] if item["code"] == "goal_items_uncovered")
    assert error["requirement_ids"] == [contract["requirements"][1]["id"]]


def test_task_review_rejects_modified_task_contract(tmp_path) -> None:
    repo, baseline = _repo(tmp_path)
    goal = "修复登录错误"
    contract = build_task_contract(goal)
    contract["requirements"] = []

    review = audit_task_completion(
        launch_goal=goal,
        submitted_goal=goal,
        summary="登录失败状态已经修复并通过聚焦测试",
        completion_evidence=_evidence(),
        requested_steps=_steps(),
        repo_root=repo,
        task_contract=contract,
        source_baseline=baseline,
        verification=_verification(),
    )

    assert review["valid"] is False
    assert "task_contract_mismatch" in _codes(review)


def test_task_review_rejects_unrelated_green_focused_test(tmp_path) -> None:
    repo, baseline = _repo(tmp_path)
    unrelated_steps = [
        {
            "name": "unrelated-tests",
            "argv": [sys.executable, "-m", "pytest", "tests/test_payments.py", "-q"],
        }
    ]
    evidence = _evidence()
    evidence["claims"][0]["verification_steps"] = ["unrelated-tests"]
    verification = {
        "status": "passed",
        "records": [
            {
                "name": "unrelated-tests",
                "status": "passed",
                "exit_code": 0,
                "elapsed_seconds": 0.1,
            }
        ],
    }

    review = audit_task_completion(
        launch_goal="修复登录错误",
        submitted_goal="修复登录错误",
        summary="登录失败状态已经修复",
        completion_evidence=evidence,
        requested_steps=unrelated_steps,
        repo_root=repo,
        source_baseline=baseline,
        verification=verification,
    )

    assert review["valid"] is False
    assert "claim_without_relevant_acceptance" in _codes(review)


def test_task_review_accepts_test_run_requirement_with_a_real_test_step(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    baseline = capture_task_source_baseline(repo)
    goal = "run the focused tests"
    contract = build_task_contract(goal, repo_root=repo)
    requirement = contract["requirements"][0]
    steps = [
        {
            "name": "dogfood-tests",
            "argv": [sys.executable, "-m", "pytest", "tests/test_dogfood_provider_check.py", "-q"],
        }
    ]
    review = audit_task_completion(
        launch_goal=goal,
        submitted_goal=goal,
        summary="the focused Dogfood tests passed",
        completion_evidence={
            "result_kind": "test",
            "claims": [
                {
                    "kind": "test",
                    "requirement_ids": [requirement["id"]],
                    "requirement": requirement["text"],
                    "result": "the focused Dogfood tests passed",
                    "files": [],
                    "verification_steps": ["dogfood-tests"],
                }
            ],
            "unresolved_items": [],
            "known_risks": [],
        },
        requested_steps=steps,
        repo_root=repo,
        task_contract=contract,
        source_baseline=baseline,
        verification={
            "status": "passed",
            "records": [
                {
                    "name": "dogfood-tests",
                    "status": "passed",
                    "exit_code": 0,
                    "elapsed_seconds": 0.1,
                }
            ],
        },
    )

    assert review["valid"] is True, review["errors"]
    assert "claim_without_relevant_acceptance" not in _codes(review)


def test_task_review_does_not_use_self_reported_result_to_make_test_relevant(tmp_path) -> None:
    repo, baseline = _repo(tmp_path)
    payments_test = repo / "tests" / "test_payments.py"
    payments_test.parent.mkdir()
    payments_test.write_text("def test_payments():\n    assert True\n", encoding="utf-8")
    steps = [
        {
            "name": "payments-tests",
            "argv": [sys.executable, "-m", "pytest", "tests/test_payments.py", "-q"],
        }
    ]
    evidence = _evidence()
    evidence["claims"][0]["result"] = "修复登录错误并检查 payments"
    evidence["claims"][0]["files"].append(
        {"path": "tests/test_payments.py", "state": "created"}
    )
    evidence["claims"][0]["verification_steps"] = ["payments-tests"]

    review = audit_task_completion(
        launch_goal="修复登录错误",
        submitted_goal="修复登录错误",
        summary="登录修复声称由 payments 测试验收",
        completion_evidence=evidence,
        requested_steps=steps,
        repo_root=repo,
        source_baseline=baseline,
        verification={
            "status": "passed",
            "records": [
                {
                    "name": "payments-tests",
                    "status": "passed",
                    "exit_code": 0,
                    "elapsed_seconds": 0.1,
                }
            ],
        },
    )

    assert review["valid"] is False
    assert "claim_without_relevant_acceptance" in _codes(review)


def test_task_review_accepts_mixed_implementation_and_test_requirements(tmp_path) -> None:
    repo = tmp_path / "repo"
    source = repo / "src" / "login.py"
    source.parent.mkdir(parents=True)
    source.write_text("def login():\n    return False\n", encoding="utf-8")
    baseline = capture_task_source_baseline(repo)
    source.write_text("def login():\n    return True\n", encoding="utf-8")
    test_file = repo / "tests" / "test_timeout.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_timeout():\n    assert True\n", encoding="utf-8")
    goal = "修复登录错误并增加超时测试"
    contract = build_task_contract(goal)
    implementation, tests = contract["requirements"]
    requested_steps = [
        *_steps(),
        {
            "name": "timeout-tests",
            "argv": [sys.executable, "-m", "pytest", "tests/test_timeout.py", "-q"],
        },
    ]
    evidence = {
        "result_kind": "change",
        "claims": [
            {
                "kind": "change",
                "requirement_ids": [implementation["id"]],
                "requirement": implementation["text"],
                "result": "修复登录错误并保留失败状态",
                "files": [{"path": "src/login.py", "state": "modified"}],
                "verification_steps": ["focused-tests"],
            },
            {
                "kind": "test",
                "requirement_ids": [tests["id"]],
                "requirement": tests["text"],
                "result": "增加超时测试并覆盖失败路径",
                "files": [{"path": "tests/test_timeout.py", "state": "created"}],
                "verification_steps": ["timeout-tests"],
            },
        ],
        "unresolved_items": [],
        "known_risks": [],
    }

    review = audit_task_completion(
        launch_goal=goal,
        submitted_goal=goal,
        summary="登录修复和超时回归测试均已完成",
        completion_evidence=evidence,
        requested_steps=requested_steps,
        repo_root=repo,
        task_contract=contract,
        source_baseline=baseline,
        verification={
            "status": "passed",
            "records": [
                *_verification()["records"],
                {
                    "name": "timeout-tests",
                    "status": "passed",
                    "exit_code": 0,
                    "elapsed_seconds": 0.1,
                },
            ],
        },
    )

    assert review["valid"] is True
    assert review["trust"] == "with_limits"
    assert review["product_verdict"] == "indeterminate"


def test_task_contract_classifies_read_only_test_run_and_documentation() -> None:
    read_only = build_task_contract("审查这个项目，给出意见")
    test_run = build_task_contract("运行现有测试")
    documentation = build_task_contract("更新 README")

    assert (read_only["intent"], read_only["requires_source_change"]) == ("read_only", False)
    assert (test_run["intent"], test_run["requires_source_change"]) == ("test_run", False)
    assert documentation["requirements"][0]["required_artifact_role"] == "documentation"


def test_single_completion_policy_is_locked_in_task_contract() -> None:
    contract = build_task_contract(
        "Read-only audit. Use exactly one Pacer completion call. Do not retry."
    )

    assert contract["completion_policy"] == {
        "schema_version": 1,
        "max_attempts": 1,
        "retry_on_rejection": False,
    }


def test_task_contract_keeps_documentation_scope_with_compileall_acceptance() -> None:
    goal = (
        "更新 README.md，增加 Usage 小节，写明 python app.py 启动命令，只修改该文档"
        "并使用 python -m compileall -q app.py 验证。"
    )

    contract = build_task_contract(goal)

    assert [item["required_artifact_role"] for item in contract["requirements"]] == [
        "documentation",
        "documentation",
    ]


def test_task_contract_inherits_documentation_role_for_adjacent_section_clause() -> None:
    goal = (
        "更新 README.md，增加“运行方式”小节，说明使用 python app.py 启动；"
        "不要修改 app.py，并使用 python -m compileall -q app.py 验证示例代码可编译。"
    )

    contract = build_task_contract(goal)

    assert [item["required_artifact_role"] for item in contract["requirements"]] == [
        "documentation",
        "documentation",
        "",
    ]
    assert contract["protected_paths"] == ["app.py"]
    assert task_contract_allows_compile_only(contract) is True


def test_task_contract_does_not_inherit_documentation_role_for_implementation_clause() -> None:
    contract = build_task_contract("更新 README.md，并修复 app.py 的返回值。")

    assert [item["required_artifact_role"] for item in contract["requirements"]] == [
        "documentation",
        "implementation",
    ]


def test_task_contract_keeps_negated_implementation_constraint_read_only() -> None:
    goal = (
        "新增 tests/test_validator.py，为 validator.py 的 is_even 覆盖偶数和奇数，并使用 "
        "python -m unittest discover -s tests -v 验证；不要修改 validator.py。"
    )

    contract = build_task_contract(goal)

    assert [item["required_artifact_role"] for item in contract["requirements"]] == ["test", ""]
    assert contract["requirements"][1]["intent"] == "read_only"
    assert contract["requirements"][1]["requires_source_change"] is False


def test_task_review_accepts_documentation_change_with_requested_compileall(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    readme = repo / "README.md"
    readme.write_text("# Sample App\n", encoding="utf-8")
    (repo / "app.py").write_text("print('sample')\n", encoding="utf-8")
    baseline = capture_task_source_baseline(repo)
    readme.write_text("# Sample App\n\n## Usage\n\n```bash\npython app.py\n```\n", encoding="utf-8")
    goal = (
        "更新 README.md，增加“运行方式”小节，说明使用 python app.py 启动；"
        "不要修改 app.py，并使用 python -m compileall -q app.py 验证示例代码可编译。"
    )
    contract = build_task_contract(goal)
    step = {
        "name": "compile-app",
        "argv": [sys.executable, "-m", "compileall", "-q", "app.py"],
    }
    results = [
        "README.md 已更新。",
        "README.md 已增加运行方式小节，并写明 python app.py 启动命令。",
        "app.py 未修改，并由 compileall 验证示例代码可编译。",
    ]
    evidence = derive_task_completion_evidence(
        completion_evidence={
            "claims": [
                {
                    "requirement_ids": [requirement["id"]],
                    "result": result,
                    "verification_steps": ["compile-app"],
                }
                for requirement, result in zip(contract["requirements"], results, strict=True)
            ],
            "unresolved_items": [],
            "known_risks": [],
        },
        repo_root=repo,
        task_contract=contract,
        source_baseline=baseline,
    )

    review = audit_task_completion(
        launch_goal=goal,
        submitted_goal=goal,
        summary="README 已增加 Usage 小节和 python app.py 启动命令。",
        completion_evidence=evidence,
        requested_steps=[step],
        repo_root=repo,
        task_contract=contract,
        source_baseline=baseline,
        verification={
            "status": "passed",
            "records": [
                {
                    "name": "compile-app",
                    "status": "passed",
                    "exit_code": 0,
                    "elapsed_seconds": 0.1,
                }
            ],
        },
    )

    assert review["valid"] is True
    assert review["trust"] == "yes"
    assert review["warnings"] == []


@pytest.mark.parametrize(
    "goal",
    [
        "修复 calculator.py 中 add 函数的错误，使 tests/test_calculator.py 通过",
        "修复登录逻辑，让现有测试通过",
        "Fix the parser so that tests/test_parser.py passes",
    ],
)
def test_task_contract_treats_passing_tests_as_implementation_acceptance(goal) -> None:
    requirement = build_task_contract(goal)["requirements"][0]

    assert requirement["intent"] == "implementation"
    assert requirement["requires_source_change"] is True
    assert requirement["required_artifact_role"] == "implementation"


def test_task_review_accepts_implementation_fix_verified_by_unchanged_existing_test(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "calculator.py"
    source.write_text(
        "def add(left: int, right: int) -> int:\n    return left - right\n",
        encoding="utf-8",
    )
    tests = repo / "tests" / "test_calculator.py"
    tests.parent.mkdir()
    tests.write_text(
        "from calculator import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    baseline = capture_task_source_baseline(repo)
    source.write_text(
        "def add(left: int, right: int) -> int:\n    return left + right\n",
        encoding="utf-8",
    )
    goal = (
        "修复 calculator.py 中 add 函数的错误，使 tests/test_calculator.py 通过；"
        "只修改 calculator.py，并使用 python -m unittest discover -s tests -v 验证。"
    )
    contract = build_task_contract(goal)
    implementation, test_run = contract["requirements"]
    steps = [
        {
            "name": "tests",
            "argv": ["python", "-m", "unittest", "discover", "-s", "tests", "-v"],
        }
    ]
    evidence = {
        "result_kind": "change",
        "claims": [
            {
                "kind": "change",
                "requirement_ids": [implementation["id"]],
                "requirement": implementation["text"],
                "result": "add 现在返回 left + right，满足正整数相加行为并由指定 unittest 测试验证。",
                "files": [{"path": "calculator.py", "state": "modified"}],
                "verification_steps": ["tests"],
            },
            {
                "kind": "change",
                "requirement_ids": [test_run["id"]],
                "requirement": test_run["text"],
                "result": "变更清单仅包含 calculator.py；指定 unittest discover 命令作为原子验收步骤执行。",
                "files": [{"path": "calculator.py", "state": "modified"}],
                "verification_steps": ["tests"],
            },
        ],
        "unresolved_items": [],
        "known_risks": [],
    }
    review = audit_task_completion(
        launch_goal=goal,
        submitted_goal=goal,
        summary="已修复 add 的运算错误，并使用指定命令完成验证。",
        completion_evidence=evidence,
        requested_steps=steps,
        repo_root=repo,
        task_contract=contract,
        source_baseline=baseline,
        verification={
            "status": "passed",
            "records": [
                {
                    "name": "tests",
                    "status": "passed",
                    "exit_code": 0,
                    "elapsed_seconds": 0.1,
                }
            ],
        },
    )

    assert review["valid"] is True
    assert review["trust"] == "yes"
    assert review["warnings"] == []
    assert review["user_report"]["can_trust"] == "yes"


def test_task_review_keeps_low_overlap_warning_for_unrelated_result(tmp_path) -> None:
    repo, baseline = _repo(tmp_path)
    evidence = _evidence()
    evidence["claims"][0]["result"] = "fix payment checkout"
    review = audit_task_completion(
        launch_goal="修复登录错误",
        submitted_goal="修复登录错误",
        summary="登录失败状态已经修复",
        completion_evidence=evidence,
        requested_steps=_steps(),
        repo_root=repo,
        source_baseline=baseline,
        verification=_verification(),
    )

    assert review["valid"] is True
    assert review["trust"] == "with_limits"
    assert "claim_result_low_overlap" in {
        str(item.get("code") or "") for item in review["warnings"]
    }


@pytest.mark.parametrize(
    "goal",
    [
        "修复登录错误与增加超时测试",
        "修复登录错误和增加超时测试",
        "fix login bug & add timeout tests",
    ],
)
def test_task_contract_splits_common_parallel_connectors(goal) -> None:
    contract = build_task_contract(goal)

    assert len(contract["requirements"]) == 2
    assert [item["required_artifact_role"] for item in contract["requirements"]] == [
        "implementation",
        "test",
    ]


def test_task_contract_rejects_silent_goal_or_requirement_truncation() -> None:
    with pytest.raises(ValueError, match="2000 character"):
        build_task_contract("修复" + "很" * 2000)
    with pytest.raises(ValueError, match="more than 20"):
        build_task_contract(" and ".join(f"fix item {index}" for index in range(21)))


def test_task_contract_keeps_explicit_numbered_requirements_atomic() -> None:
    goal = """Implement the missing local Dogfood relay preflight in Pacer.

Requirements:
1. Add a product module and CLI command, and keep the implementation narrowly scoped.
2. Reuse the installed Codex CLI and do not hand-roll Responses streaming.
3. Accept provider configuration but never accept a raw key.
4. Launch Codex in an empty directory and produce a bounded receipt.
5. Fail closed on timeout, missing credentials, nonzero exit, or marker mismatch.
6. Make the runner injectable and add focused tests for all failures.
7. Wire the command through existing quality CLI patterns and do not commit.
8. Run focused tests and do not run process-kill probes.

Before editing, inspect AGENTS.md and existing Dogfood conventions."""

    contract = build_task_contract(goal)
    requirement_texts = [item["text"] for item in contract["requirements"]]

    assert len(requirement_texts) == 11
    assert requirement_texts[1].startswith("Add a product module and CLI command")
    assert requirement_texts[2].startswith("Reuse the installed Codex CLI")
    assert requirement_texts[8].startswith("Run focused tests")


def test_task_contract_does_not_treat_latest_as_test_run() -> None:
    contract = build_task_contract("restyle latest dashboard")

    assert contract["intent"] == "implementation"
    assert contract["requires_source_change"] is True


def test_git_baseline_does_not_attribute_preexisting_dirty_file_to_task(tmp_path) -> None:
    if shutil.which("git") is None:
        return
    repo = tmp_path / "repo"
    source = repo / "src" / "login.py"
    source.parent.mkdir(parents=True)
    source.write_text("def login():\n    return False\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "src/login.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Pacer Test",
            "-c",
            "user.email=pacer@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        check=True,
    )
    source.write_text("def login():\n    return 'user change'\n", encoding="utf-8")
    baseline = capture_task_source_baseline(repo)

    preexisting = audit_task_completion(
        launch_goal="修复登录错误",
        submitted_goal="修复登录错误",
        summary="登录错误已经修复",
        completion_evidence=_evidence(),
        requested_steps=_steps(),
        repo_root=repo,
        source_baseline=baseline,
        verification=_verification(),
    )
    source.write_text("def login():\n    return 'pacer change'\n", encoding="utf-8")
    changed_after_launch = audit_task_completion(
        launch_goal="修复登录错误",
        submitted_goal="修复登录错误",
        summary="登录错误已经修复",
        completion_evidence=_evidence(),
        requested_steps=_steps(),
        repo_root=repo,
        source_baseline=baseline,
        verification=_verification(),
    )

    assert baseline["kind"] == "git"
    assert "file_change_not_attributed" in _codes(preexisting)
    assert changed_after_launch["valid"] is True


def test_git_baseline_attributes_clean_file_in_nested_project_root(tmp_path) -> None:
    if shutil.which("git") is None:
        return
    git_root = tmp_path / "monorepo"
    repo = git_root / "project"
    source = repo / "src" / "login.py"
    source.parent.mkdir(parents=True)
    source.write_text("def login():\n    return False\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(git_root)], check=True)
    subprocess.run(["git", "-C", str(git_root), "add", "project/src/login.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(git_root),
            "-c",
            "user.name=Pacer Test",
            "-c",
            "user.email=pacer@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        check=True,
    )
    baseline = capture_task_source_baseline(repo)
    source.write_text("def login():\n    return True\n", encoding="utf-8")

    review = audit_task_completion(
        launch_goal="修复登录错误",
        submitted_goal="修复登录错误",
        summary="登录错误已经修复",
        completion_evidence=_evidence(),
        requested_steps=_steps(),
        repo_root=repo,
        source_baseline=baseline,
        verification=_verification(),
    )

    assert baseline["git_prefix"] == "project/"
    assert review["valid"] is True


def test_server_derives_filesystem_created_modified_and_deleted_changes(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    modified = repo / "app.py"
    deleted = repo / "old.py"
    modified.write_text("value = 1\n", encoding="utf-8")
    deleted.write_text("legacy = True\n", encoding="utf-8")
    baseline = capture_task_source_baseline(repo)

    modified.write_text("value = 2\n", encoding="utf-8")
    deleted.unlink()
    (repo / "new.py").write_text("created = True\n", encoding="utf-8")

    payload = derive_task_source_changes(repo_root=repo, source_baseline=baseline)

    assert payload["complete"] is True
    assert {(item["path"], item["state"]) for item in payload["changes"]} == {
        ("app.py", "modified"),
        ("new.py", "created"),
        ("old.py", "deleted"),
    }


def test_server_derives_git_rename_untracked_and_dirty_baseline_delta(tmp_path) -> None:
    if shutil.which("git") is None:
        return
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "original.py").write_text("original = True\n", encoding="utf-8")
    (repo / "dirty.py").write_text("value = 'head'\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Pacer Test",
            "-c",
            "user.email=pacer@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        check=True,
    )
    (repo / "dirty.py").write_text("value = 'user'\n", encoding="utf-8")
    (repo / "scratch.py").write_text("value = 'user'\n", encoding="utf-8")
    baseline = capture_task_source_baseline(repo)

    (repo / "dirty.py").write_text("value = 'pacer'\n", encoding="utf-8")
    (repo / "scratch.py").write_text("value = 'pacer'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "mv", "original.py", "renamed.py"], check=True)
    (repo / "new.py").write_text("created = True\n", encoding="utf-8")

    payload = derive_task_source_changes(repo_root=repo, source_baseline=baseline)
    changes = {item["path"]: item for item in payload["changes"]}

    assert payload["complete"] is True
    assert changes["dirty.py"]["state"] == "modified"
    assert changes["scratch.py"]["state"] == "modified"
    assert changes["new.py"]["state"] == "created"
    assert changes["original.py"] == {
        "path": "original.py",
        "state": "deleted",
        "artifact_role": "implementation",
        "renamed_to": "renamed.py",
    }
    assert changes["renamed.py"] == {
        "path": "renamed.py",
        "state": "created",
        "artifact_role": "implementation",
        "renamed_from": "original.py",
    }


def test_server_omits_unchanged_preexisting_dirty_file(tmp_path) -> None:
    if shutil.which("git") is None:
        return
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("value = 'head'\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Pacer Test",
            "-c",
            "user.email=pacer@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        check=True,
    )
    source.write_text("value = 'user'\n", encoding="utf-8")
    baseline = capture_task_source_baseline(repo)

    payload = derive_task_source_changes(repo_root=repo, source_baseline=baseline)

    assert payload == {"complete": True, "changes": [], "errors": []}


def test_server_builds_canonical_evidence_from_minimal_claim(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    goal = "修复 app.py 的结果错误"
    contract = build_task_contract(goal)
    baseline = capture_task_source_baseline(repo)
    source.write_text("value = 2\n", encoding="utf-8")
    requirement_id = contract["requirements"][0]["id"]

    evidence = derive_task_completion_evidence(
        completion_evidence={
            "claims": [
                {
                    "requirement_ids": [requirement_id],
                    "result": "app.py 现在返回正确结果",
                    "verification_steps": ["focused-tests"],
                }
            ],
            "unresolved_items": [],
            "known_risks": [],
        },
        repo_root=repo,
        task_contract=contract,
        source_baseline=baseline,
    )

    assert evidence["evidence_origin"] == "server_derived"
    assert evidence["result_kind"] == "change"
    assert evidence["claims"][0]["requirement"] == contract["requirements"][0]["text"]
    assert evidence["claims"][0]["files"] == [{"path": "app.py", "state": "modified"}]
    assert evidence["legacy_fields_ignored"] == []


def test_server_rejects_out_of_scope_and_protected_path_changes(tmp_path) -> None:
    repo = tmp_path / "repo"
    tests_dir = repo / "tests"
    tests_dir.mkdir(parents=True)
    validator = repo / "validator.py"
    validator.write_text("def is_even(value): return True\n", encoding="utf-8")
    goal = "新增 tests/test_validator.py 覆盖 is_even；不要修改 validator.py。"
    contract = build_task_contract(goal)
    baseline = capture_task_source_baseline(repo)
    validator.write_text("def is_even(value): return value % 2 == 0\n", encoding="utf-8")
    (tests_dir / "test_validator.py").write_text("def test_even(): assert True\n", encoding="utf-8")

    evidence = derive_task_completion_evidence(
        completion_evidence={"claims": [], "unresolved_items": [], "known_risks": []},
        repo_root=repo,
        task_contract=contract,
        source_baseline=baseline,
    )
    codes = {item["code"] for item in evidence["source_change_issues"]}

    assert contract["protected_paths"] == ["validator.py"]
    assert {"source_change_out_of_scope", "protected_path_changed"}.issubset(codes)


def test_server_derives_changes_committed_after_launch(tmp_path) -> None:
    if shutil.which("git") is None:
        return
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    commit = [
        "git",
        "-C",
        str(repo),
        "-c",
        "user.name=Pacer Test",
        "-c",
        "user.email=pacer@example.invalid",
        "commit",
        "-qm",
    ]
    subprocess.run([*commit, "baseline"], check=True)
    baseline = capture_task_source_baseline(repo)
    source.write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    subprocess.run([*commit, "task change"], check=True)

    payload = derive_task_source_changes(repo_root=repo, source_baseline=baseline)

    assert payload == {
        "complete": True,
        "changes": [{"path": "app.py", "state": "modified", "artifact_role": "implementation"}],
        "errors": [],
    }


def test_completion_rejection_returns_structured_correction_without_rescan() -> None:
    review = {
        "errors": [
            {
                "code": "protected_path_changed",
                "message": "protected file changed",
                "paths": ["validator.py"],
            }
        ],
        "task_contract": {
            "requirements": [{"id": "R01-test", "text": "add validator tests"}]
        },
        "source_changes": [
            {"path": "validator.py", "state": "modified", "artifact_role": "implementation"}
        ],
    }

    message = task_review_error(review, retryable=False, attempt=3, max_attempts=3)
    payload = json.loads(message.removeprefix("completion audit rejected: "))

    assert payload["kind"] == "pacer_completion_correction"
    assert payload["retryable"] is False
    assert payload["completion_control"] == {"attempt": 3, "max_attempts": 3}
    assert payload["errors"][0]["code"] == "protected_path_changed"
    assert "Restore" in payload["errors"][0]["correction"]
    assert payload["server_derived_changes"][0]["path"] == "validator.py"
    assert payload["required_claim_fields"] == [
        "requirement_ids",
        "result",
        "verification_steps",
    ]


def test_repository_acceptance_manifest_is_digest_locked_and_protected(tmp_path) -> None:
    repo = tmp_path / "repo"
    manifest = repo / ".pacer" / "acceptance.json"
    manifest.parent.mkdir(parents=True)
    manifest_payload = {
        "schema_version": 1,
        "observable_outcomes": ["validator returns the expected value"],
        "required_artifact_roles": ["implementation"],
        "verification": {"required_commands": ["python -m pytest -q"]},
        "boundary_cases": ["zero"],
    }
    manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
    goal = "修复 validator.py；使用 python -m pytest -q 验证。"
    contract = build_task_contract(goal, repo_root=repo)
    baseline = capture_task_source_baseline(repo)

    assert ".pacer/acceptance.json" in contract["protected_paths"]
    original_digest = contract["acceptance_contract"]["digest"]

    manifest_payload["observable_outcomes"] = ["different outcome"]
    manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
    rebuilt = build_task_contract(goal, repo_root=repo)
    evidence = derive_task_completion_evidence(
        completion_evidence={"claims": [], "unresolved_items": [], "known_risks": []},
        repo_root=repo,
        task_contract=contract,
        source_baseline=baseline,
    )

    assert rebuilt["acceptance_contract"]["digest"] != original_digest
    assert any(
        issue["code"] == "protected_path_changed"
        and ".pacer/acceptance.json" in issue.get("paths", [])
        for issue in evidence["source_change_issues"]
    )
