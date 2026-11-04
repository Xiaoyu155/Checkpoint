import json
from pathlib import Path
from unittest.mock import MagicMock

from visual_agent.subagent_orchestrator import SubagentOrchestrator, SubagentPlan, SubagentTask


def _plan(
    tmp_path: Path,
    *,
    require_red_baseline: bool | None = None,
    probe_base_discrimination: bool | None = None,
    **task_kwargs,
) -> tuple[SubagentOrchestrator, SubagentTask]:
    fields = {
        "target_files": ["visual_agent/renderer.py"],
        "verification_command": "python -c \"assert False\"",
        "max_retries": 5,
    }
    fields.update(task_kwargs)
    task = SubagentTask(
        id="t1",
        title="edit",
        role="coder",
        backend="workbuddy",
        instructions="change the file",
        **fields,
    )
    plan = SubagentPlan(goal="wish", tasks=[task], repo_root=str(tmp_path))
    return SubagentOrchestrator(
        plan,
        workspace_root=tmp_path,
        enforce_test_tamper_guard=False,
        require_red_baseline=require_red_baseline,
        probe_base_discrimination=probe_base_discrimination,
    ), task


def test_green_baseline_never_calls_a_coder(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "visual_agent.subagent_orchestrator.probe_workspace_command",
        lambda **_kwargs: {"failed_on_base": False, "status": "passed_on_base"},
    )
    calls = []
    monkeypatch.setattr("visual_agent.subagent_orchestrator.invoke_workbuddy", lambda *_a, **_k: calls.append("workbuddy"))
    monkeypatch.setattr("visual_agent.subagent_orchestrator.invoke_grok", lambda *_a, **_k: calls.append("grok"))
    orchestrator, task = _plan(
        tmp_path,
        require_red_baseline=True,
        probe_base_discrimination=False,
    )
    summary = orchestrator.run()
    assert summary["success"] is False
    assert task.status == "rejected"
    assert calls == []


def test_same_failure_stops_after_two_coder_calls(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "visual_agent.subagent_orchestrator.probe_workspace_command",
        lambda **_kwargs: {"status": "failed_on_base"},
    )
    calls = []

    def coder(*_args, **_kwargs):
        calls.append(_kwargs.get("cwd"))
        return MagicMock(success=True, response="no patch", error=None)

    monkeypatch.setattr("visual_agent.subagent_orchestrator.invoke_workbuddy", coder)
    monkeypatch.setattr("visual_agent.subagent_orchestrator.invoke_grok", coder)
    orchestrator, task = _plan(tmp_path, target_files=["app.py"])
    orchestrator._is_backend_live = lambda backend: backend in {"workbuddy", "grok"}
    orchestrator._run_verification_command = lambda _cmd: (False, "AssertionError: case_1")
    summary = orchestrator.run()
    assert summary["success"] is False
    assert len(calls) == 2
    assert task.status == "failed"


def test_pacer_edit_without_dogfood_is_not_completed(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "visual_agent" / "renderer.py"
    target.parent.mkdir(parents=True)
    target.write_text("color = 'red'\n", encoding="utf-8")
    monkeypatch.setattr(
        "visual_agent.subagent_orchestrator.probe_workspace_command",
        lambda **_kwargs: {"status": "failed_on_base"},
    )
    monkeypatch.setattr(
        "visual_agent.dogfood_local.run_local_dogfood",
        lambda *_a, **_k: {"installed_artifact_verified": False},
    )

    def coder(task):
        target.write_text("color = 'blue'\n", encoding="utf-8")
        task.patched_files = ["visual_agent/renderer.py"]
        return "edited", None

    orchestrator, task = _plan(tmp_path)
    orchestrator._dispatch_to_backend = coder
    orchestrator._run_verification_command = lambda _cmd: (True, "passed")
    summary = orchestrator.run()
    assert summary["success"] is False
    assert task.status != "completed"
    assert target.read_text(encoding="utf-8") == "color = 'red'\n"
    assert summary["dogfood_evidence"]["installed_artifact_verified"] is False
    assert summary["dogfood_evidence"]["task_reports"][0]["task_id"] == task.id


def test_successful_dogfood_receipt_survives_task_finalization(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "visual_agent" / "renderer.py"
    target.parent.mkdir(parents=True)
    target.write_text("color = 'red'\n", encoding="utf-8")
    monkeypatch.setattr("visual_agent.subagent_orchestrator.probe_workspace_command",
                        lambda **_kwargs: {"status": "failed_on_base"})
    monkeypatch.setattr("visual_agent.dogfood_local.run_local_dogfood",
                        lambda *_a, **_k: {"installed_artifact_verified": True, "wheel_sha256": "fixture-digest"})
    orchestrator, task = _plan(tmp_path)

    def coder(_task):
        target.write_text("color = 'blue'\n", encoding="utf-8")
        return "edited", None

    orchestrator._dispatch_to_backend = coder
    orchestrator._run_verification_command = lambda _cmd: (True, "passed")
    summary = orchestrator.run()
    assert summary["success"] is True
    assert task.status == "completed"
    assert summary["dogfood_evidence"]["wheel_sha256"] == "fixture-digest"
    assert summary["dogfood_evidence"]["task_reports"][0]["patched_files"] == ["visual_agent/renderer.py"]


def test_gemini4_calls_use_a_new_session_and_no_terminal_history(tmp_path: Path, monkeypatch) -> None:
    orchestrator, _task = _plan(tmp_path)
    first = json.loads(orchestrator._gemini4_request("第一句"))
    second = json.loads(orchestrator._gemini4_request("第二句"))
    for payload in (first, second):
        assert set(payload) == {"profile_summary", "latest_utterance", "session_id"}
        assert "terminal_history" not in payload
    assert first["session_id"] != second["session_id"]
    assert first["latest_utterance"] == "第一句"
