from __future__ import annotations

import sys
import shutil
import subprocess

import pytest

from visual_agent.task_review import (
    audit_task_completion,
    build_task_contract,
    capture_task_source_baseline,
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
    assert review["trust"] == "yes"
    assert review["goal_binding"] == {"pinned": True, "matches": True}
    assert review["user_report"]["completed"] == ["修复登录错误并增加失败状态处理"]
    assert review["user_report"]["can_trust"] == "yes"
    assert "实际完成：" in review["user_report_markdown"]
    assert "可信结论：可信" in review["user_report_markdown"]


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
    contract = build_task_contract(goal)
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
    assert review["trust"] == "yes"


def test_task_contract_classifies_read_only_test_run_and_documentation() -> None:
    read_only = build_task_contract("审查这个项目，给出意见")
    test_run = build_task_contract("运行现有测试")
    documentation = build_task_contract("更新 README")

    assert (read_only["intent"], read_only["requires_source_change"]) == ("read_only", False)
    assert (test_run["intent"], test_run["requires_source_change"]) == ("test_run", False)
    assert documentation["requirements"][0]["required_artifact_role"] == "documentation"


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
