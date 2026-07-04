from __future__ import annotations

from pathlib import Path

from visual_agent.chief_run import mission_status_payload, run_chief_mission
from visual_agent.missions import load_mission, load_rounds
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
    assert mission["product"] == "DevPacer"
    assert Path(payload["final_report_path"]).exists()
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
    assert captured["test_command"] == "python -m pytest -q"
    assert captured["allow_test_edits"] is False


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
    from visual_agent.chief_run import _objective_requests_test_edits

    # V5 cold-start regression: a qualifier between the verb and 测试 must match.
    assert _objective_requests_test_edits("创建 counting.py 并写 pytest 测试到 tests/test_counting.py")
    assert _objective_requests_test_edits("添加对应的单元测试")
    assert _objective_requests_test_edits("write unit tests for the parser")
    assert _objective_requests_test_edits("add integration tests covering login")
    # Plain implementation goals must not unlock test edits.
    assert not _objective_requests_test_edits("修复登录页面的崩溃")
    assert not _objective_requests_test_edits("optimize the query planner")


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
    assert "Fix checkout total display" in Path(payload["mandatory_record_path"]).read_text(encoding="utf-8")
    rounds = load_rounds(workspace.root, payload["mission"]["mission_id"])
    assert rounds[-1]["status"] == "pass"


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
    assert "--resume" in payload["message"]
    assert payload["rounds"][0]["type"] == "dispatch_preview"
