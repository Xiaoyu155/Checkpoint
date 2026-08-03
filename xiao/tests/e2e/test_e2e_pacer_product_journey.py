from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from visual_agent.chief_dispatch import dispatch_chief_plan
from visual_agent.chief_run import mission_status_payload, run_chief_mission
from visual_agent.interactive_agent import run_interactive_agent
from visual_agent.pacer_host import build_host_dashboard
from visual_agent.pacer_management import handle_pacer_management
from visual_agent.workspace import init_workspace


pytestmark = pytest.mark.e2e


def test_two_mission_journey_routes_remembers_manages_and_accepts(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    workspace = init_workspace(repo / ".agent-workspace", with_demo=False)
    (repo / "app.py").write_text("def greeting():\n    return 'todo'\n", encoding="utf-8")
    (repo / "test_app.py").write_text(
        "from app import greeting\n\n"
        "def test_greeting():\n"
        "    assert greeting() == 'hello'\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".gitignore", "app.py", "test_app.py")
    _git(repo, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "baseline")

    monkeypatch.setattr(
        "visual_agent.chief_dispatch._codex_user_defaults",
        lambda: {
            "provider": "test-relay",
            "model": "gpt-test",
            "reasoning_effort": "high",
            "sandbox": "workspace-write",
            "approval": "never",
        },
    )
    monkeypatch.setattr(
        "visual_agent.provider_liveness.probe_worker_agent_liveness",
        lambda *_args, **_kwargs: {"ok": True, "agent": "codex", "stop_reason": ""},
    )
    monkeypatch.setattr(
        "visual_agent.agent_backends.has_recent_quota_failure",
        lambda *_args, **_kwargs: False,
    )

    prompts: list[str] = []

    def worker_runner(argv, cwd, timeout_seconds, log_path, **kwargs):
        del argv, timeout_seconds
        prompts.append(str(kwargs.get("stdin_text") or ""))
        Path(cwd, "app.py").write_text(
            "def greeting():\n    return 'hello'\n",
            encoding="utf-8",
        )
        output = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": f"journey-{len(prompts)}"}),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 20, "cached_input_tokens": 5, "output_tokens": 4},
                    }
                ),
            ]
        )
        Path(log_path).write_text(output + "\n", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": output, "stderr_tail": ""}

    def dispatch_runner(**kwargs):
        return dispatch_chief_plan(**kwargs, command_runner=worker_runner)

    common = {
        "workspace_root": workspace.root,
        "repo_root": repo,
        "agents": ("codex",),
        "test_command": f'"{sys.executable}" -m pytest -q',
        "execute": True,
        "dry_run": False,
        "allow_dirty": False,
        "max_rounds": 2,
        "max_repair_rounds": 0,
        "dispatch_runner": dispatch_runner,
        "ground_vague_goals": False,
    }
    first = run_chief_mission(
        goal="Fix greeting to return hello and pass test_app.py",
        **common,
    )
    second = run_chief_mission(
        goal="Continue greeting work from the previous verified greeting mission and keep test_app.py passing",
        **common,
    )

    first_id = str(first["mission"]["mission_id"])
    second_id = str(second["mission"]["mission_id"])
    first_memory_id = f"mission:{first_id}"
    phases = {item["id"]: item for item in second["journey"]["phases"]}

    assert first["status"] == "verified"
    assert second["status"] == "verified"
    assert first["journey"]["status"] == "verified_pending_delivery"
    assert phases["routing"]["status"] == "passed"
    assert phases["memory"]["status"] == "passed"
    assert phases["memory"]["details"]["memory_ids"] == [first_memory_id]
    assert phases["managed"]["status"] == "passed"
    assert phases["acceptance"]["status"] == "passed"
    assert phases["delivery"]["status"] == "ready"
    assert second["journey"]["continuity_status"] == "connected_pending_delivery"
    assert second["journey"]["can_claim_verified"] is True
    assert second["journey"]["can_claim_delivered"] is False
    assert first_memory_id in prompts[1]
    assert (workspace.root / "missions" / second_id / "journey.json").is_file()

    status = mission_status_payload(workspace_root=workspace.root, mission_id=second_id)
    assert status["journey"]["status"] == "verified_pending_delivery"
    assert status["journey"]["can_claim_verified"] is True

    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {
            "installed": True,
            "authenticated": True,
            "auth_method": "api_key",
            "status": "authenticated",
        },
    )
    monkeypatch.setattr("visual_agent.pacer_support.load_codex_user_defaults", lambda: {})
    assert (
        handle_pacer_management(
            [
                "status",
                "--workspace-root",
                str(workspace.root),
                "--repo-root",
                str(repo),
                "--json",
            ]
        )
        == 0
    )
    status_view = json.loads(capsys.readouterr().out)
    assert status_view["mission_journey"]["mission_id"] == second_id
    assert status_view["mission_journey"]["status"] == "verified_pending_delivery"

    monkeypatch.setattr(
        "visual_agent.pacer_host.probe_worker_agent_liveness",
        lambda *_args, **_kwargs: {"ok": True, "agent": "codex", "stop_reason": ""},
    )
    host_view = build_host_dashboard(
        workspace_root=workspace.root,
        repo_root=repo,
        auto_resume=False,
    )
    assert host_view["latest_journey"]["mission_id"] == second_id
    assert host_view["latest_journey"]["can_claim_verified"] is True

    outputs: list[str] = []
    inputs = iter(["/状态", "/退出"])
    assert (
        run_interactive_agent(
            repo_root=repo,
            workspace_root=workspace.root,
            input_func=lambda _prompt: next(inputs),
            output_func=outputs.append,
        )
        == 0
    )
    interaction = "\n".join(outputs)
    assert "闭环：" in interaction
    assert "本地记忆 已完成" in interaction
    assert "强验收 已完成" in interaction


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
