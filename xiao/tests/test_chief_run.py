from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from visual_agent.chief_dispatch import build_worker_command
from visual_agent.chief_plans_store import load_plan
from visual_agent.chief_run import _override_plan_agent, chief_run_to_markdown, mission_status_payload, run_chief_mission
from visual_agent.mission_progress import save_mission_progress
from visual_agent.missions import load_mission, load_rounds, save_mission
from visual_agent.notifications import NotificationConfig
from visual_agent.workspace import init_workspace


def write_verification_workflow(workspace, name: str, *, affects: str = "src/payment/") -> None:
    workspace.workflows_dir.joinpath(f"{name}.yaml").write_text(
        "schema_version: 1\n"
        f"name: {name}\n"
        "version: 1\n"
        "affects:\n"
        f"  - {affects}\n"
        "tags:\n"
        "  - verification\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_ocr\n"
        "    mock_text: ready\n",
        encoding="utf-8",
    )


def preview_payload(**_kwargs):
    return {
        "schema_version": 1,
        "status": "preview",
        "worker": {"track_id": "track_1_codex", "agent": "codex", "command": "codex exec ..."},
        "worktree": {"path": "worktree", "branch": "checkpoint/plan/track", "created": False},
        "verification": {"command": "checkpoint codex-check", "run_profile": "dry-run"},
    }


def verified_payload(**kwargs):
    if not kwargs.get("execute"):
        return preview_payload(**kwargs)
    attempt = {
        "verdict": "pass",
        "run_profile": "dry-run",
        "passed": 1,
        "inspection_only": 0,
        "failed": 0,
        "total": 1,
        "results": [{"name": "checkout", "status": "passed"}],
    }
    return {
        "schema_version": 1,
        "status": "verified",
        "worker_record": {"status": "completed"},
        "verification_attempts": [attempt],
        "latest_verification": attempt,
    }


def worker_failed_passing_check_payload(**kwargs):
    if not kwargs.get("execute"):
        return preview_payload(**kwargs)
    attempt = {
        "verdict": "pass",
        "run_profile": "supervised",
        "passed": 1,
        "inspection_only": 0,
        "failed": 0,
        "total": 1,
        "results": [{"name": "explicit-command", "status": "passed"}],
        "command_verification": {
            "command": "python -c pass",
            "verdict": "pass",
            "exit_code": 0,
        },
    }
    return {
        "schema_version": 1,
        "status": "worker_failed",
        "worker_record": {"status": "failed", "stderr_tail": "patch fragment without header"},
        "verification_attempts": [attempt],
        "latest_verification": attempt,
    }


def worker_failed_tests_pass_payload(**kwargs):
    if not kwargs.get("execute"):
        return preview_payload(**kwargs)
    attempt = {
        "verdict": "pass",
        "run_profile": "supervised",
        "passed": 1,
        "inspection_only": 0,
        "failed": 0,
        "total": 1,
        "results": [{"name": "explicit-command", "status": "passed"}],
        "command_verification": {
            "command": "python -c pass",
            "verdict": "pass",
            "exit_code": 0,
        },
    }
    return {
        "schema_version": 1,
        "status": "worker_failed_tests_pass",
        "worker_record": {"status": "failed", "stderr_tail": "patch fragment without header"},
        "verification_attempts": [attempt],
        "latest_verification": attempt,
        "warnings": [
            "Worker did not complete normally, but the current worktree changes passed the test command."
        ],
    }


def verified_blocked_payload(**kwargs):
    if not kwargs.get("execute"):
        return preview_payload(**kwargs)
    attempt = {
        "verdict": "pass",
        "run_profile": "supervised",
        "passed": 1,
        "inspection_only": 0,
        "failed": 0,
        "total": 1,
        "results": [{"name": "explicit-command", "status": "passed"}],
        "command_verification": {
            "command": "dart analyze",
            "verdict": "pass",
            "exit_code": 0,
        },
    }
    return {
        "schema_version": 1,
        "status": "verified_blocked",
        "worker_record": {"status": "completed"},
        "verification_attempts": [attempt],
        "latest_verification": attempt,
        "toolchain_violation": {
            "status": "violated",
            "expected_executable": r"D:\Projects\flutter_stable\bin\cache\dart-sdk\bin\dart.exe",
            "forbidden_path": r"D:\Projects\flutter_stable\bin\flutter.bat",
            "log_path": r"D:\logs\worker.log",
        },
    }


def repeated_failure_payload(**kwargs):
    if not kwargs.get("execute"):
        return preview_payload(**kwargs)
    attempt = {
        "verdict": "fail",
        "run_profile": "dry-run",
        "passed": 0,
        "inspection_only": 0,
        "failed": 1,
        "total": 1,
        "results": [
            {
                "name": "checkout",
                "status": "failed",
                "failed_step": "assert_total",
                "message": "expected total 128",
            }
        ],
    }
    return {
        "schema_version": 1,
        "status": "verification_failed",
        "worker_record": {"status": "completed"},
        "verification_attempts": [attempt, attempt],
        "latest_verification": attempt,
    }


def single_failure_payload(**kwargs):
    payload = repeated_failure_payload(**kwargs)
    if kwargs.get("execute"):
        payload["verification_attempts"] = payload["verification_attempts"][:1]
    return payload


def missing_verification_environment_payload(**kwargs):
    if not kwargs.get("execute"):
        return preview_payload(**kwargs)
    attempt = {
        "verdict": "fail",
        "run_profile": "supervised",
        "passed": 0,
        "inspection_only": 0,
        "failed": 1,
        "total": 1,
        "command_verification": {
            "command": "npm run eval:acceptance",
            "verdict": "fail",
            "exit_code": 1,
            "failure_kind": "verification_environment_missing",
            "classification_confidence": "heuristic",
            "output_tail": "QWEN_API_KEY missing",
        },
        "repair_brief": {
            "source": "test_command",
            "failure_kind": "verification_environment_missing",
            "classification_confidence": "heuristic",
        },
    }
    return {
        "schema_version": 1,
        "status": "verification_failed",
        "worker_record": {"status": "completed"},
        "verification_attempts": [attempt],
        "latest_verification": attempt,
    }


def ready_workspace(tmp_path, monkeypatch):
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])
    return workspace


def test_chief_run_dry_run_saves_preview_mission(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)

    payload = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=preview_payload,
    )

    assert payload["status"] == "preview"
    assert payload["stop_reason"] == "preview_only"
    mission = payload["mission"]
    assert mission["product"] == "Pacer"
    assert Path(payload["final_report_path"]).exists()
    state_path = workspace.root / "missions" / mission["mission_id"] / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["current_state"] == "REVIEW"
    assert state["context"]["history"][-1]["event"] == "chief_run_finished"
    assert "mandatory_record_path" not in payload
    assert not (tmp_path / "强制测试记录.md").exists()
    rounds = load_rounds(workspace.root, mission["mission_id"])
    assert rounds[0]["type"] == "dispatch_preview"


def test_chief_run_with_test_command_bootstraps_missing_workspace(tmp_path, monkeypatch) -> None:
    workspace_root = tmp_path / ".agent-workspace"
    captured: dict[str, object] = {}
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: [])

    def fake_dispatch(**kwargs):
        captured.update(kwargs)
        return preview_payload(**kwargs)

    payload = run_chief_mission(
        goal="给 utils 加 slugify，要求 slugify(' Hello, World! ') == 'hello-world'",
        workspace_root=workspace_root,
        repo_root=tmp_path,
        test_command="python -m pytest -q",
        dispatch_runner=fake_dispatch,
    )

    assert payload["status"] == "preview"
    assert workspace_root.exists()
    assert captured["test_command"].endswith("-m pytest -q")
    assert captured["allow_test_edits"] is False


def test_chief_run_resume_inherits_saved_allow_dirty(tmp_path, monkeypatch) -> None:
    workspace_root = tmp_path / ".agent-workspace"
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: [])

    first = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace_root,
        repo_root=tmp_path,
        test_command="python -m pytest -q",
        allow_dirty=True,
        dispatch_runner=preview_payload,
    )
    mission_id = first["mission"]["mission_id"]
    assert load_mission(workspace_root, mission_id)["allow_dirty"] is True

    captures: list[dict[str, object]] = []

    def fake_dispatch(**kwargs):
        captures.append(dict(kwargs))
        return preview_payload(**kwargs)

    resumed = run_chief_mission(
        workspace_root=workspace_root,
        repo_root=tmp_path,
        resume_mission_id=mission_id,
        execute=True,
        dry_run=False,
        dispatch_runner=fake_dispatch,
    )

    assert resumed["status"] == "stopped"
    assert captures
    assert all(item["allow_dirty"] is True for item in captures)
    assert load_mission(workspace_root, mission_id)["allow_dirty"] is True


def test_chief_run_records_required_verification_env(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    def fake_dispatch(**kwargs):
        captured.update(kwargs)
        return preview_payload(**kwargs)

    payload = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        test_command="npm run eval:acceptance",
        require_env=("QWEN_API_KEY",),
        dispatch_runner=fake_dispatch,
    )

    expected = [{"kind": "env_var", "name": "QWEN_API_KEY"}]
    assert payload["status"] == "preview"
    assert payload["mission"]["verification_env"] == expected
    assert captured["verification_env"] == expected


def test_chief_run_allows_test_edits_when_objective_requests_tests(tmp_path, monkeypatch) -> None:
    workspace_root = tmp_path / ".agent-workspace"
    captured: dict[str, object] = {}
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: [])

    def fake_dispatch(**kwargs):
        captured.update(kwargs)
        return preview_payload(**kwargs)

    payload = run_chief_mission(
        goal="给 utils 加 slugify 并写测试，要求 slugify(' Hello, World! ') == 'hello-world'",
        workspace_root=workspace_root,
        repo_root=tmp_path,
        test_command="python -m pytest -q",
        dispatch_runner=fake_dispatch,
    )

    assert payload["status"] == "preview"
    assert captured["allow_test_edits"] is True
    assert payload["plan"]["test_edit_policy"]["source"] == "objective"


def test_objective_test_edit_inference_handles_qualified_phrases() -> None:
    from visual_agent.chief_run import _objective_requests_test_edits, _plan_requests_test_edits

    # V5 cold-start regression: a qualifier between the verb and 测试 must match.
    assert _objective_requests_test_edits("创建 counting.py 并写 pytest 测试到 tests/test_counting.py")
    assert _objective_requests_test_edits("添加对应的单元测试")
    assert _objective_requests_test_edits("补齐必要实现与验收样本")
    assert _objective_requests_test_edits("更新 eval 验收脚本")
    assert _objective_requests_test_edits("新增验收用例覆盖登录失败")
    assert not _objective_requests_test_edits("审查 eval 脚本但不修改")
    assert not _objective_requests_test_edits("只改 docs/开发进度.md，保留所有验收配置不变")
    assert not _objective_requests_test_edits("不要修改测试、pyproject.toml、pytest.ini、conftest.py 或任何验收配置")
    assert _objective_requests_test_edits("write unit tests for the parser")
    assert _objective_requests_test_edits("add integration tests covering login")
    assert _objective_requests_test_edits("add coverage for Qwen markdown JSON extraction")
    assert _objective_requests_test_edits("increase parser coverage without external API calls")
    assert _objective_requests_test_edits("完善离线解析覆盖率")
    assert _plan_requests_test_edits(
        original_goal="继续开发，并补齐或更新相应测试",
        goal="构建旅游防骗知识库后端API",
        plan={"objective": "构建旅游防骗知识库后端API", "acceptance_criteria": []},
        grounding={"grounded_goal": "构建旅游防骗知识库后端API", "acceptance_hint": "运行后端单元测试"},
    )
    # Plain implementation goals must not unlock test edits.
    assert not _objective_requests_test_edits("修复登录页面的崩溃")
    assert not _objective_requests_test_edits("optimize the query planner")


def test_chief_run_report_marks_command_verification_mode() -> None:
    report = chief_run_to_markdown(
        {
            "status": "verified",
            "stop_reason": "verified",
            "mission": {"mission_id": "m1", "objective": "Add util", "plan_id": "p1"},
            "plan": {"status": "needs_workflow_coverage", "selected_workflows": ["checkout_verification"]},
            "rounds": [],
            "dispatch": {
                "preflight": {
                    "status": "warning",
                    "test_command": {"status": "resolved", "requested": "auto", "resolved": "npm test"},
                    "verification_env": {"status": "ok", "missing_env_vars": []},
                    "dependency": {
                        "package_manager": "npm",
                        "lockfile": "package-lock.json",
                        "deps_installed": False,
                        "cache_available": False,
                        "native_install_risk": False,
                        "estimated_install_minutes": 9,
                        "warnings": ["node_dependencies_not_installed"],
                    },
                    "verification_timeout": {"base_timeout_seconds": 900.0, "timeout_seconds": 2100.0, "reason": "missing_node_modules"},
                },
                "latest_verification": {
                    "verdict": "pass",
                    "command_verification": {
                        "command": "npm ci && npm test",
                        "verdict": "pass",
                        "exit_code": 0,
                    },
                }
            },
        }
    )

    assert "- Status: `command_gate`" in report
    assert "- Verification mode: `command`" in report
    assert "- Workflow coverage: workflow coverage 由显式测试命令接管" in report
    assert "- Selected workflows: not used (explicit test command)" in report
    assert "needs_workflow_coverage" not in report
    assert "Preflight" in report
    assert "| dependency | `warning` |" in report
    assert "reason=missing_node_modules" in report
    assert "- Command: `npm ci && npm test`" in report
    assert "Selected workflows: checkout_verification" not in report


def test_chief_run_saves_command_verification_mode_to_plan(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)

    payload = run_chief_mission(
        goal="Fix checkout",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        test_command="python -m pytest -q",
        dispatch_runner=preview_payload,
    )

    saved = load_plan(workspace.root, payload["plan"]["plan_id"])
    assert payload["plan"]["verification_mode"] == "command"
    assert saved is not None
    assert saved["verification_mode"] == "command"


def test_final_report_has_three_sections() -> None:
    report = chief_run_to_markdown(
        {
            "status": "verified",
            "stop_reason": "verified",
            "message": "任务已通过验收。",
            "mission": {"mission_id": "m1", "objective": "Fix checkout", "plan_id": "p1", "repo_root": r"D:\repo"},
            "plan": {"status": "ready"},
            "rounds": [],
            "progress": {"changed_product_file_count": 1, "changed_product_files": ["src/结算.py"]},
            "dispatch": {
                "worktree": {"path": r"D:\repo.checkpoint-worktrees\p1\track", "branch": "checkpoint/p1/track"},
                "latest_verification": {
                    "verdict": "pass",
                    "command_verification": {"command": "python -m pytest -q", "verdict": "pass", "exit_code": 0},
                },
            },
        }
    )

    conclusion = report.index("## 结论")
    evidence = report.index("## 证据")
    next_step = report.index("## 下一步")
    assert conclusion < evidence < next_step


def test_chief_run_report_explains_toolchain_violation_gate() -> None:
    report = chief_run_to_markdown(
        {
            "status": "stopped",
            "stop_reason": "worker_toolchain_violation",
            "mission": {"mission_id": "m1", "objective": "Fix discovery page", "plan_id": "p1"},
            "plan": {"status": "ready"},
            "rounds": [],
            "dispatch": {
                "latest_verification": {
                    "verdict": "pass",
                    "command_verification": {
                        "command": r"D:\Projects\flutter_stable\bin\cache\dart-sdk\bin\dart.exe analyze lib/main.dart",
                        "verdict": "pass",
                        "exit_code": 0,
                    },
                },
                "toolchain_violation": {
                    "expected_executable": r"D:\Projects\flutter_stable\bin\cache\dart-sdk\bin\dart.exe",
                    "forbidden_path": r"D:\Projects\flutter_stable\bin\dart.bat",
                    "log_path": r"D:\logs\worker.log",
                },
            },
        }
    )

    assert "Gate Decision" in report
    assert "- Verified: `false`" in report
    assert r"D:\Projects\flutter_stable\bin\cache\dart-sdk\bin\dart.exe" in report
    assert r"D:\Projects\flutter_stable\bin\dart.bat" in report


def test_agent_override_recomputes_codex_specific_track_config(tmp_path) -> None:
    plan = {
        "objective": "continue",
        "worker_tracks": [
            {
                "id": "track_1_mimo",
                "agent": "mimo",
                "track_kind": "implementation",
                "model": "opus",
                "sandbox": {},
                "approval": {"flag": "--permission-mode acceptEdits"},
                "command": "claude -p --model opus --permission-mode acceptEdits ...",
            }
        ],
        "acceptance_criteria": [],
        "selected_workflows": [],
    }

    assert _override_plan_agent(plan, "codex") is True
    track = plan["worker_tracks"][0]
    assert track["agent"] == "codex"
    assert track["id"] == "track_1_codex"
    assert track["model"] == ""
    assert track["sandbox"]["flag"] == "--sandbox workspace-write"
    assert track["approval"] == {}
    assert "command" not in track

    cmd = build_worker_command(
        plan=plan,
        track=track,
        worktree=tmp_path,
        verification_command="python -m visual_agent.cli codex-check",
    )
    argv = cmd["argv"]
    assert argv[:4] == ["codex", "--sandbox", "workspace-write", "exec"]
    assert "--model" not in argv
    assert "--permission-mode" not in argv
    assert "--sandbox" in argv and "workspace-write" in argv


def test_agent_override_normalizes_existing_codex_track(tmp_path) -> None:
    plan = {
        "objective": "continue",
        "worker_tracks": [
            {
                "id": "track_1_codex",
                "agent": "codex",
                "track_kind": "implementation",
                "model": "sonnet",
                "approval": {"flag": "--permission-mode default"},
            }
        ],
        "acceptance_criteria": [],
        "selected_workflows": [],
    }

    assert _override_plan_agent(plan, "codex") is True
    track = plan["worker_tracks"][0]
    assert track["model"] == ""
    assert track["approval"] == {}

    cmd = build_worker_command(
        plan=plan,
        track=track,
        worktree=tmp_path,
        verification_command="python -m visual_agent.cli codex-check",
    )
    assert "--model" not in cmd["argv"]
    assert "--permission-mode" not in cmd["argv"]


def test_agent_override_preserves_standard_tier_for_claude() -> None:
    plan = {
        "objective": "continue",
        "worker_tracks": [
            {
                "id": "track_1_claude_code",
                "agent": "claude-code",
                "track_kind": "implementation",
                "tier": "standard",
                "model": "opus",
            }
        ],
    }

    assert _override_plan_agent(plan, "claude-code") is True
    track = plan["worker_tracks"][0]
    assert track["model"] == "sonnet"
    assert track["tier"] == "standard"


def test_chief_run_blocks_vague_goal_before_dispatch(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)

    def should_not_dispatch(**_kwargs):
        raise AssertionError("dispatch should not run for vague goals")

    payload = run_chief_mission(
        goal="改一下",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=should_not_dispatch,
    )

    assert payload["status"] == "stopped"
    assert payload["stop_reason"] == "needs_clarification"
    mission = load_mission(workspace.root, payload["mission"]["mission_id"])
    assert mission is not None
    assert mission["status"] == "stopped"


def test_chief_run_dispatches_vague_goal_with_requirement_contract(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)

    payload = run_chief_mission(
        goal="改一下",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        requirement_contract={
            "source": "goal_intake",
            "input_goal": "改一下",
            "final_goal": "修复结算页 checkout 金额显示，总价必须等于行项目之和",
            "answers": ["保留现有优惠和税费展示"],
            "acceptance_hint": "checkout 工作流验收通过",
            "model_id": "codex:cli",
            "intake_policy": "selected_agent_cli",
        },
        dispatch_runner=preview_payload,
    )

    assert payload["status"] == "preview"
    assert payload["stop_reason"] == "preview_only"
    assert "checkout 金额显示" in payload["mission"]["objective"]
    assert payload["plan"]["clarity"]["ok"] is True
    assert any("用户补充：保留现有优惠和税费展示" in item for item in payload["plan"]["acceptance_criteria"])
    assert any("验收提示：checkout 工作流验收通过" in item for item in payload["plan"]["acceptance_criteria"])

    mission = load_mission(workspace.root, payload["mission"]["mission_id"])
    assert mission is not None
    assert mission["requirement_contract"]["input_goal"] == "改一下"
    assert mission["requirement_contract"]["final_goal"] == "修复结算页 checkout 金额显示，总价必须等于行项目之和"
    rounds = load_rounds(workspace.root, mission["mission_id"])
    assert any(round_.get("type") == "requirement_contract" for round_ in rounds)
    contract_round = next(round_ for round_ in rounds if round_.get("type") == "requirement_contract")
    assert contract_round["payload"]["intake_policy"] == "selected_agent_cli"
    assert contract_round["payload"]["model_id"] == "codex:cli"
    report = Path(payload["final_report_path"]).read_text(encoding="utf-8")
    assert "Requirement Contract" in report
    assert "policy=selected_agent_cli" in report
    assert "model=codex:cli" in report
    assert "原始目标：改一下" in report
    assert "收口目标：修复结算页 checkout 金额显示" in report


def test_chief_run_grounds_vague_goal_onto_plan_document(tmp_path, monkeypatch) -> None:
    """A vague goal that points at a repo plan gets resolved and dispatched,
    not bounced back as an error."""
    workspace = ready_workspace(tmp_path, monkeypatch)

    def fake_grounding(*, goal, repo_root):
        assert "开发计划" in goal
        return {
            "resolved": True,
            "source": "model",
            "plan_document": "docs/roadmap.md",
            "grounded_goal": "修复结算页 checkout 金额显示，总价必须等于行项目之和",
            "acceptance_hint": "checkout 工作流验收通过",
            "evidence": "- [ ] 修复结算页金额",
        }

    payload = run_chief_mission(
        goal="参照最新的开发计划，继续给我推进开发",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=preview_payload,
        grounding_runner=fake_grounding,
    )

    assert payload["status"] == "preview"
    mission = payload["mission"]
    assert "checkout" in mission["objective"]
    rounds = load_rounds(workspace.root, mission["mission_id"])
    grounding_rounds = [r for r in rounds if r.get("type") == "grounding"]
    assert grounding_rounds and grounding_rounds[0]["status"] == "resolved"
    assert grounding_rounds[0]["plan_document"] == "docs/roadmap.md"
    assert payload["plan"]["grounding"]["resolved"] is True
    report = Path(payload["final_report_path"]).read_text(encoding="utf-8")
    assert "计划审查" in report
    assert "落地为具体任务" in report


def test_chief_run_manual_livekit_validation_does_not_stop_on_coverage_gap(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)

    def should_not_dispatch(**_kwargs):
        raise AssertionError("manual validation preview should not dispatch a coding worker")

    payload = run_chief_mission(
        goal="进行 LiveKit 真机验证，包括弱网和户外噪声环境下的测试，确保语音交互稳定可用。",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=should_not_dispatch,
    )

    assert payload["status"] == "preview"
    assert payload["stop_reason"] == "manual_verification_required"
    rounds = load_rounds(workspace.root, payload["mission"]["mission_id"])
    assert any(round_.get("status") == "manual_acceptance_required" for round_ in rounds)


def test_chief_run_review_plan_goal_does_not_stop_on_coverage_gap(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)

    def fake_dispatch(**kwargs):
        return {
            "status": "preview" if kwargs.get("dry_run") else "verified",
            "latest_verification": {"verdict": "pass", "run_profile": "review_plan"},
            "review_plan_report": "## 产品判断\n\n可以审查。\n\n## 建议开发计划\n\n继续收口。",
        }

    payload = run_chief_mission(
        goal="对本项目进行审核并给出下一阶段开发计划",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        execute=True,
        dry_run=False,
        allow_coverage_gap=True,
        dispatch_runner=fake_dispatch,
    )

    assert payload["status"] == "verified"
    assert payload["stop_reason"] == "verified"
    assert "审查与开发计划" in Path(payload["final_report_path"]).read_text(encoding="utf-8")


def test_chief_run_review_plan_goal_is_not_rewritten_by_grounding(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)

    def should_not_ground(**_kwargs):
        raise AssertionError("review/plan report goals must not be rewritten into a plan document task")

    seen = {}

    def fake_dispatch(**kwargs):
        seen["preview_objective"] = kwargs.get("plan_id")
        return {
            "status": "preview" if kwargs.get("dry_run") else "verified",
            "latest_verification": {"verdict": "pass", "run_profile": "review_plan"},
            "review_plan_report": "## 产品判断\n\n保持原始审查目标。\n\n## 建议开发计划\n\n按报告推进。",
        }

    payload = run_chief_mission(
        goal="Review/audit the product under D:\\Projects and generate a development plan report",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        execute=True,
        dry_run=False,
        allow_coverage_gap=True,
        grounding_runner=should_not_ground,
        dispatch_runner=fake_dispatch,
    )

    assert payload["status"] == "verified"
    assert "Review/audit" in payload["mission"]["objective"]


def test_review_plan_goal_detection_distinguishes_plan_reference() -> None:
    from visual_agent.mission_intake import is_review_plan_goal

    assert is_review_plan_goal("对本项目进行审查并给出下一阶段开发计划")
    assert is_review_plan_goal("Review/audit the product and generate a development plan report")
    assert not is_review_plan_goal("参照最新的开发计划，继续给我推进开发")


def test_chief_run_unresolved_grounding_stops_with_advice(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)

    def fake_grounding(*, goal, repo_root):
        return {
            "resolved": False,
            "source": "model",
            "documents_reviewed": ["README.md"],
            "plan_document": "",
            "grounded_goal": "",
            "proposed_plan": ["先定验收命令", "实现登录页"],
            "questions": ["先做哪个功能？"],
        }

    def should_not_dispatch(**_kwargs):
        raise AssertionError("dispatch should not run when grounding is unresolved")

    payload = run_chief_mission(
        goal="按照计划开发",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=should_not_dispatch,
        grounding_runner=fake_grounding,
    )

    assert payload["status"] == "stopped"
    assert payload["stop_reason"] == "needs_clarification"
    assert payload["plan"]["grounding"]["proposed_plan"] == ["先定验收命令", "实现登录页"]
    report = Path(payload["final_report_path"]).read_text(encoding="utf-8")
    assert "计划审查" in report
    assert "建议的开发计划" in report
    assert "先做哪个功能？" in report
    rounds = load_rounds(workspace.root, payload["mission"]["mission_id"])
    assert any(r.get("type") == "grounding" and r.get("status") == "unresolved" for r in rounds)


def test_chief_run_execute_verified(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)

    payload = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        execute=True,
        dry_run=False,
        dispatch_runner=verified_payload,
    )

    assert payload["status"] == "verified"
    assert payload["stop_reason"] == "verified"
    assert Path(payload["mandatory_record_path"]).name == "强制测试记录.md"
    assert Path(payload["mandatory_record_path"]).parent == workspace.root / "missions" / payload["mission"]["mission_id"]
    assert "Fix checkout total display" in Path(payload["mandatory_record_path"]).read_text(encoding="utf-8")
    assert not (tmp_path / "强制测试记录.md").exists()
    rounds = load_rounds(workspace.root, payload["mission"]["mission_id"])
    assert rounds[-1]["status"] == "pass"
    assert payload["notification"]["status"] == "skipped"


def test_chief_run_worker_failure_is_not_verified_by_passing_command(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)

    payload = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        execute=True,
        dry_run=False,
        test_command="python -c pass",
        dispatch_runner=worker_failed_passing_check_payload,
    )

    assert payload["status"] == "stopped"
    assert payload["stop_reason"] == "worker_error"
    assert "编程助手" in payload["message"] or "没跑完" in payload["message"]
    rounds = load_rounds(workspace.root, payload["mission"]["mission_id"])
    assert rounds[-1]["status"] == "pass"


def test_chief_run_worker_failed_tests_pass_needs_manual_merge(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)

    payload = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        execute=True,
        dry_run=False,
        test_command="python -c pass",
        dispatch_runner=worker_failed_tests_pass_payload,
    )

    assert payload["status"] == "stopped"
    assert payload["stop_reason"] == "worker_failed_tests_pass"
    assert "不敢说完成" in payload["message"] or "隔离" in payload["message"]
    assert payload["progress"]["stage"] == "worker_failed_tests_pass"
    assert payload["progress"]["stage_label"] == "Worker failed; tests passed"
    assert payload["progress"]["needs_attention"] is True


def test_chief_run_verified_blocked_is_not_reported_as_stopped(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)

    payload = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        execute=True,
        dry_run=False,
        test_command="dart analyze",
        dispatch_runner=verified_blocked_payload,
    )

    assert payload["status"] == "verified_blocked"
    assert payload["stop_reason"] == "worker_toolchain_violation"
    assert "合并" in payload["message"]
    report = Path(payload["final_report_path"]).read_text(encoding="utf-8")
    assert "Verified: `false`" in report
    assert r"D:\Projects\flutter_stable\bin\flutter.bat" in report
    mission_status = mission_status_payload(workspace_root=workspace.root, mission_id=payload["mission"]["mission_id"])
    assert mission_status["status"] == "verified_blocked"
    assert "policy gate blocked" in mission_status["message"]


def test_chief_run_sends_terminal_notification_when_configured(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)
    sent: dict[str, object] = {}

    monkeypatch.setattr(
        "visual_agent.chief_run.load_notification_config",
        lambda: NotificationConfig(
            smtp_host="smtp.example.com",
            smtp_port=587,
            username="user@example.com",
            password="secret",
            sender="user@example.com",
            recipient="owner@example.com",
        ),
    )

    def fake_send(notification, *, dry_run=True, **_kwargs):
        sent["notification"] = notification
        sent["dry_run"] = dry_run
        return {"status": "sent", "subject": notification["subject"]}

    monkeypatch.setattr("visual_agent.chief_run.send_email_notification", fake_send)

    payload = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        execute=True,
        dry_run=False,
        dispatch_runner=verified_payload,
    )

    assert payload["notification"]["status"] == "sent"
    assert sent["dry_run"] is False
    assert sent["notification"]["event"] == "mission_verified"
    assert "Fix checkout total display" in sent["notification"]["body"]


def test_chief_run_execute_stops_on_repeated_failure(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)

    payload = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        execute=True,
        dry_run=False,
        max_rounds=2,
        dispatch_runner=repeated_failure_payload,
    )

    assert payload["status"] == "stopped"
    assert payload["stop_reason"] == "same_failure_repeated"
    rounds = load_rounds(workspace.root, payload["mission"]["mission_id"])
    assert rounds[-1]["failed_signature"] == "checkout|assert_total|expected total 128"


def test_chief_run_stops_on_missing_verification_environment(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)

    payload = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        execute=True,
        dry_run=False,
        test_command="npm run eval:acceptance",
        dispatch_runner=missing_verification_environment_payload,
    )

    assert payload["status"] == "stopped"
    assert payload["stop_reason"] == "verification_environment_missing"
    assert "验收环境" in payload["message"] or "环境" in payload["message"]
    report = Path(payload["final_report_path"]).read_text(encoding="utf-8")
    assert "verification_environment_missing" in report
    assert f"checkpoint mission resume --mission '{payload['mission']['mission_id']}' --execute" in report
    assert "mission start --resume" not in report
    assert "Classification confidence: `heuristic`" in report
    assert "建议人工确认" in report


def test_chief_run_surfaces_provider_5xx_instead_of_generic_worker_error() -> None:
    from visual_agent.chief_run import _message_for_stop, _stop_reason_from_dispatch

    dispatch = {
        "status": "worker_failed",
        "latest_verification": {"verdict": "pass"},
        "managed_runtime": {
            "retry": {
                "retry": True,
                "status": "scheduled",
                "failure_kind": "provider_5xx",
            }
        },
    }

    assert _stop_reason_from_dispatch(dispatch, {"max_rounds": 2}) == "provider_5xx"
    message = _message_for_stop("provider_5xx")
    assert "503" in message or "5xx" in message
    assert "agents doctor" not in message


def test_chief_run_does_not_call_verified_work_budget_exhausted() -> None:
    from visual_agent.chief_run import _stop_reason_from_dispatch

    dispatch = {
        "status": "managed_budget_exhausted",
        "latest_verification": {"verdict": "pass"},
        "worker_record": {"status": "completed", "exit_code": 0},
        "verification_attempts": [{"verdict": "pass"}, {"verdict": "pass"}],
    }
    assert _stop_reason_from_dispatch(dispatch, {"max_rounds": 2}) == "verified"

    dispatch_ok = {
        "status": "verified",
        "latest_verification": {"verdict": "pass"},
        "verification_attempts": [{"verdict": "pass"}],
    }
    assert _stop_reason_from_dispatch(dispatch_ok, {"max_rounds": 1}) == "verified"


def test_chief_run_distinguishes_unknown_usage_from_exhausted_budget() -> None:
    from visual_agent.chief_run import _message_for_stop, _stop_reason_from_dispatch

    dispatch = {
        "status": "managed_usage_unknown",
        "latest_verification": {"verdict": "fail"},
        "managed_runtime": {
            "budget_status": "usage_unknown",
            "budget": {"reason_codes": ["token_usage_unknown"]},
        },
    }

    assert _stop_reason_from_dispatch(dispatch, {"max_rounds": 2}) == "usage_unknown"
    assert "不是额度已经耗尽" in _message_for_stop("usage_unknown")


def test_chief_run_reports_post_merge_verification_failure() -> None:
    from visual_agent.chief_run import _stop_reason_from_dispatch

    dispatch = {
        "status": "merged_verification_failed",
        "latest_verification": {
            "verdict": "fail",
            "command_verification": {
                "command": "npm run eval:acceptance",
                "verdict": "fail",
                "exit_code": 1,
                "failure_kind": "verification_environment_missing",
            },
        },
    }

    assert _stop_reason_from_dispatch(dispatch, {"max_rounds": 2}) == "merged_verification_failed"
    report = chief_run_to_markdown(
        {
            "status": "stopped",
            "stop_reason": "merged_verification_failed",
            "mission": {"mission_id": "m1", "objective": "Fix checkout", "plan_id": "p1"},
            "plan": {"status": "ready"},
            "rounds": [],
            "dispatch": dispatch,
        }
    )
    assert "Failure kind: `verification_environment_missing`" in report


def test_chief_run_resume_executes_existing_preview_mission(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)
    preview = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=preview_payload,
    )
    mission_id = preview["mission"]["mission_id"]

    resumed = run_chief_mission(
        workspace_root=workspace.root,
        resume_mission_id=mission_id,
        execute=True,
        dry_run=False,
        dispatch_runner=verified_payload,
    )

    assert resumed["status"] == "verified"
    assert resumed["mission"]["mission_id"] == mission_id
    rounds = load_rounds(workspace.root, mission_id)
    assert [item["round"] for item in rounds] == [0, 1, 2]
    assert rounds[-1]["status"] == "pass"


def test_chief_run_resume_restores_repo_and_test_command(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)
    preview = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        test_command="python -c pass",
        dispatch_runner=preview_payload,
    )
    mission_id = preview["mission"]["mission_id"]
    calls = []

    def capture_dispatch(**kwargs):
        calls.append(kwargs)
        return verified_payload(**kwargs)

    wrong_repo = tmp_path / "wrong-repo"
    wrong_repo.mkdir()
    resumed = run_chief_mission(
        workspace_root=workspace.root,
        repo_root=wrong_repo,
        resume_mission_id=mission_id,
        execute=True,
        dry_run=False,
        dispatch_runner=capture_dispatch,
    )

    assert resumed["status"] == "verified"
    assert Path(resumed["mission"]["repo_root"]) == tmp_path.resolve()
    assert resumed["mission"]["test_command"] == "python -c pass"
    assert calls and all(call["test_command"] == "python -c pass" for call in calls)


def test_chief_run_preserves_absent_reasoning_override_for_track_resolution(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)
    calls = []

    def capture_dispatch(**kwargs):
        calls.append(kwargs)
        return preview_payload(**kwargs)

    result = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=capture_dispatch,
    )

    assert result["mission"]["reasoning_effort"] == "inherit"
    assert calls[0]["reasoning_effort"] is None


def test_chief_run_forwards_explicit_reasoning_override(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)
    calls = []

    def capture_dispatch(**kwargs):
        calls.append(kwargs)
        return preview_payload(**kwargs)

    run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        reasoning_effort="high",
        dispatch_runner=capture_dispatch,
    )

    assert calls[0]["reasoning_effort"] == "high"
    assert calls[0]["execution_policy"]["managed_budget"] == {
        "max_wall_seconds": 3600.0,
        "max_total_tokens": 120_000,
        "max_attempts": 3,
        "max_repair_rounds": 2,
        "max_same_failure_count": 2,
    }


def test_chief_run_resume_uses_remaining_round_budget(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)
    preview = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        max_rounds=2,
        dispatch_runner=preview_payload,
    )
    mission_id = preview["mission"]["mission_id"]

    first = run_chief_mission(
        workspace_root=workspace.root,
        resume_mission_id=mission_id,
        execute=True,
        dry_run=False,
        dispatch_runner=single_failure_payload,
    )
    second_calls = []

    def second_failure(**kwargs):
        second_calls.append(kwargs)
        return single_failure_payload(**kwargs)

    second = run_chief_mission(
        workspace_root=workspace.root,
        resume_mission_id=mission_id,
        execute=True,
        dry_run=False,
        dispatch_runner=second_failure,
    )

    assert first["stop_reason"] == "verification_failed"
    assert second["stop_reason"] == "budget_exhausted"
    execution_calls = [call for call in second_calls if call.get("execute")]
    assert execution_calls and execution_calls[0]["auto_repair_once"] is False
    rounds = load_rounds(workspace.root, mission_id)
    assert len([item for item in rounds if item.get("type") == "verification"]) == 2


def test_chief_run_resume_preserves_wall_clock_budget_start(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)
    preview = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        max_wall_minutes=1,
        dispatch_runner=preview_payload,
    )
    mission = load_mission(workspace.root, preview["mission"]["mission_id"])
    mission["budget_started_at"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    save_mission(workspace.root, mission)

    def should_not_dispatch(**_kwargs):
        raise AssertionError("expired mission budget must stop before dispatch")

    resumed = run_chief_mission(
        workspace_root=workspace.root,
        resume_mission_id=mission["mission_id"],
        execute=True,
        dry_run=False,
        dispatch_runner=should_not_dispatch,
    )

    assert resumed["status"] == "stopped"
    assert resumed["stop_reason"] == "budget_exhausted"


def test_mission_status_payload_reports_next_action(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)
    preview = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=preview_payload,
    )

    payload = mission_status_payload(workspace_root=workspace.root, mission_id=preview["mission"]["mission_id"])

    assert payload["status"] == "preview"
    assert "checkpoint mission resume --mission" in payload["message"]
    assert payload["rounds"][0]["type"] == "dispatch_preview"
    assert payload["progress"]["stage"] == "dispatch_ready"
    assert "changed_product_file_count" in payload["progress"]


def test_mission_status_payload_reports_stale_running_worker_activity(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)
    preview = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=preview_payload,
    )
    mission = preview["mission"]
    mission["status"] = "running"
    mission["stop_reason"] = ""
    save_mission(workspace.root, mission)
    save_mission_progress(workspace.root, mission["mission_id"], stage="worker_running")

    payload = mission_status_payload(workspace_root=workspace.root, mission_id=mission["mission_id"])

    assert payload["status"] == "running"
    assert payload["progress"]["stage"] == "worker_activity_stale"
    assert payload["progress"]["needs_attention"] is True
    assert "no live background worker" in payload["message"]
