from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from visual_agent import codex_launcher
from visual_agent import task_review
from visual_agent.codex_rollout_telemetry import PACER_LAUNCH_OWNERSHIP_PREFIX
from visual_agent.pacer_events import list_pacer_events
from visual_agent.pacer_launch_context import (
    read_active_launch,
    read_launch_liveness,
    write_launch_liveness,
)

REAL_PRELOAD_PACER_MEMORY = codex_launcher._preload_pacer_memory
REAL_TASK_REVIEW_SUBPROCESS_RUN = task_review._SUBPROCESS_RUN


@pytest.fixture(autouse=True)
def _stable_python_runtime(monkeypatch):
    monkeypatch.setattr(
        codex_launcher,
        "resolve_python_runtime",
        lambda _root, **_kwargs: {
            "executable": "",
            "source": "unavailable",
            "available": False,
            "pytest_available": False,
            "probe_status": "not_found",
            "trusted_venv": False,
            "root": "",
            "bound_repo_root": str(_root),
        },
    )
    monkeypatch.setattr(codex_launcher, "_preload_pacer_memory", lambda **_kwargs: None)


def _stub_native_codex(monkeypatch, calls, *, returncode: int = 0) -> None:
    monkeypatch.setattr(codex_launcher.shutil, "which", lambda _name: r"C:\Tools\codex.exe")
    monkeypatch.setattr(codex_launcher, "capture_rollout_snapshot", lambda: "snapshot")
    monkeypatch.setattr(codex_launcher, "save_rollout_baseline", lambda **_kwargs: None)
    monkeypatch.setattr(codex_launcher, "_start_launch_watchdog", lambda **_kwargs: None)
    monkeypatch.setattr(
        codex_launcher,
        "aggregate_rollout_telemetry",
        lambda _snapshot, **_kwargs: {"status": "no_rollout", "attribution_confidence": "none"},
    )
    monkeypatch.setattr(
        codex_launcher.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or subprocess.CompletedProcess(command, returncode),
    )


def _assert_native_control(prompt: str, *, task: str | None = None) -> None:
    assert prompt.splitlines()[0] == codex_launcher.PACER_NATIVE_CONTROL_MARKER
    assert codex_launcher.PACER_SKILL_INVOCATION not in prompt
    assert "Do not read or load any Pacer SKILL.md or plugin skill." in prompt
    assert "Do not enumerate ALL_TOOLS" in prompt
    assert "mcp__pacer__begin_pacer_task" in prompt
    assert prompt.count("mcp__pacer__begin_pacer_task") == 1
    assert codex_launcher.PACER_BEGIN_TASK_TEMPLATE in prompt
    assert "the field is goal, never task" in prompt
    assert "mcp__pacer__get_pacer_memory" in prompt
    assert "mcp__pacer__complete_pacer_task" in prompt
    assert prompt.count("mcp__pacer__complete_pacer_task") == 1
    assert "batch independent reads" in prompt
    assert "do not repeat unchanged file/status/diff reads" in prompt
    assert "run each final acceptance command only through" in prompt
    assert "mcp__pacer__run_pacer_verification" not in prompt
    assert "mcp__pacer__get_pacer_runtime_telemetry" not in prompt
    assert "mcp__pacer__record_pacer_outcome" not in prompt
    assert codex_launcher.PACER_COMPLETE_TASK_TEMPLATE in prompt
    assert '"argv":["python","-m","pytest","-q"]' in prompt
    assert '"completion_evidence"' in prompt
    assert '"requirement_ids"' in prompt
    assert '"result_kind"' not in prompt
    assert '"files"' not in prompt
    assert "derives created/modified/deleted file facts" in prompt
    assert "Do not add git status/diff inspection steps" in prompt
    assert '"unresolved_items":[]' in prompt
    assert "Copy the original task text exactly into goal." in prompt
    assert "every locked task_contract requirement ID" in prompt
    assert "Do not send result_kind, kind, requirement, or files" in prompt
    assert "Treat the returned task_review as authoritative." in prompt
    assert "Use its user_report_markdown as the leading report block" in prompt
    assert "never upgrade or omit its limitations" in prompt
    assert "Read the Pacer skill" not in prompt
    assert "Get-Content" not in prompt
    if task is not None:
        assert f"{codex_launcher.PACER_USER_TASK_MARKER}\n{task}" in prompt
        assert prompt.endswith(task)


def _ownership_lines(prompt: str) -> list[str]:
    return [
        line
        for line in prompt.splitlines()
        if line.startswith(PACER_LAUNCH_OWNERSHIP_PREFIX)
    ]


def test_native_routing_injects_selected_model_and_provider(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    (workspace / "model_pool.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "routed",
                        "provider": "custom",
                        "model": "gpt-routed",
                        "capability": 0.8,
                        "cost": 0.4,
                        "agent_backend": "codex",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    arguments, decision = codex_launcher._apply_native_routing(
        ["exec", "Add checkout field"],
        repo_root=tmp_path,
        task="Add checkout field",
    )

    assert arguments[:4] == ["-c", "model_provider='custom'", "--model", "gpt-routed"]
    assert decision["request_evidence"]["policy_match"] is True
    assert decision["decision_id"]


def test_native_routing_preserves_explicit_user_model(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    (workspace / "model_pool.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "routed",
                        "provider": "custom",
                        "model": "gpt-routed",
                        "capability": 0.8,
                        "cost": 0.4,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    arguments, decision = codex_launcher._apply_native_routing(
        ["--model", "user-model", "exec", "task"],
        repo_root=tmp_path,
        task="task",
    )

    assert arguments == ["--model", "user-model", "exec", "task"]
    assert decision["verdict"] == "passthrough"
    assert decision["reason_codes"] == ["routing_user_override"]


def test_launch_codex_forwards_terminal_and_arguments(tmp_path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(codex_launcher.shutil, "which", lambda _name: r"C:\Tools\codex.exe")

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 23)

    monkeypatch.setattr(codex_launcher.subprocess, "run", fake_run)
    monkeypatch.setattr(codex_launcher, "capture_rollout_snapshot", lambda: "snapshot")
    monkeypatch.setattr(
        codex_launcher,
        "aggregate_rollout_telemetry",
        lambda _snapshot, **_kwargs: {"status": "no_rollout", "attribution_confidence": "none"},
    )

    result = codex_launcher.launch_codex(["resume", "--last"], cwd=tmp_path)

    assert result == 23
    assert calls[0][0][:-1] == [
        r"C:\Tools\codex.exe",
        "-c",
        "model_auto_compact_token_limit=96000",
        "-c",
        'model_auto_compact_token_limit_scope="total"',
        "-c",
        "mcp_servers.pacer.command="
        + json.dumps(os.path.abspath(codex_launcher.sys.executable), ensure_ascii=False),
        "-c",
        'mcp_servers.pacer.args=["-m", "visual_agent.mcp_server"]',
        "-c",
        "mcp_servers.pacer.env_vars="
        + json.dumps(
            [
                "PACER_LAUNCH_ID",
                "PACER_PRELAUNCH_TASK_REQUIRED",
                "PACER_PRELAUNCH_TASK_CONTRACT_DIGEST",
                "PACER_PRELAUNCH_SOURCE_BASELINE_DIGEST",
            ]
        ),
        "-c",
        "mcp_servers.pacer.required=true",
        "-c",
        "mcp_servers.pacer.startup_timeout_sec=30",
        "resume",
        "--last",
    ]
    _assert_native_control(calls[0][0][-1])
    assert "Continue the task already present" in calls[0][0][-1]
    assert calls[0][1]["cwd"] == str(tmp_path.resolve())
    assert calls[0][1]["check"] is False
    assert calls[0][1]["env"]["PACER_LAUNCH_ID"]
    assert _ownership_lines(calls[0][0][-1]) == [
        codex_launcher.rollout_ownership_marker(calls[0][1]["env"]["PACER_LAUNCH_ID"])
    ]
    assert len(list((tmp_path / ".agent-workspace" / "pacer_native" / "launches").glob("*.json"))) == 1


@pytest.mark.parametrize(
    "arguments",
    [
        ["-C", "project", "exec", "任务"],
        ["--cd", "project", "exec", "任务"],
        ["--cd=project", "exec", "任务"],
        ["-Cproject", "exec", "任务"],
    ],
)
def test_effective_repo_parser_supports_native_cd_forms(tmp_path, arguments) -> None:
    project = tmp_path / "project"
    project.mkdir()

    resolved, error = codex_launcher._effective_codex_repo_root(arguments, tmp_path)

    assert error == ""
    assert resolved == project.resolve()


def test_launch_binds_relative_sibling_without_changing_process_cwd(
    tmp_path,
    monkeypatch,
) -> None:
    process_cwd = tmp_path / "caller"
    repo = tmp_path / "source-project"
    process_cwd.mkdir()
    repo.mkdir()
    calls = []
    runtime_roots = []
    baseline_calls = []
    watchdog_calls = []
    _stub_native_codex(monkeypatch, calls)
    monkeypatch.setattr(
        codex_launcher,
        "resolve_python_runtime",
            lambda root, **_kwargs: runtime_roots.append(root)
        or {
            "available": False,
            "pytest_available": False,
            "probe_status": "not_found",
            "source": "unavailable",
            "bound_repo_root": str(root),
        },
    )
    monkeypatch.setattr(
        codex_launcher,
        "save_rollout_baseline",
        lambda **kwargs: baseline_calls.append(kwargs),
    )
    monkeypatch.setattr(
        codex_launcher,
        "_start_launch_watchdog",
        lambda **kwargs: watchdog_calls.append(kwargs),
    )

    relative_source = os.path.join("..", "source-project")
    assert codex_launcher.launch_codex(
        ["-C", relative_source, "exec", "检查项目"],
        cwd=process_cwd,
    ) == 0

    command, options = calls[0]
    cd_index = command.index("-C")
    assert command[cd_index : cd_index + 2] == ["-C", relative_source]
    assert options["cwd"] == str(process_cwd.resolve())
    assert runtime_roots == [repo.resolve()]
    assert baseline_calls[0]["workspace_root"] == repo.resolve() / ".agent-workspace"
    assert watchdog_calls[0]["workspace_root"] == repo.resolve() / ".agent-workspace"
    manifests = list((repo / ".agent-workspace" / "pacer_native" / "launches").glob("*.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["repo_root"] == str(repo.resolve())
    assert manifest["effective_repo_root"] == str(repo.resolve())
    assert manifest["process_cwd"] == str(process_cwd.resolve())
    assert manifest["rollout_ownership"] == {
        "scheme": "launch_marker_v1",
        "required": True,
    }
    active = read_active_launch(repo / ".agent-workspace", launch_id=manifest["launch_id"])
    assert active["process_cwd"] == str(process_cwd.resolve())
    assert active["effective_repo_root"] == str(repo.resolve())
    assert active["rollout_ownership"] == manifest["rollout_ownership"]
    assert not (process_cwd / ".agent-workspace").exists()
    assert _ownership_lines(command[-1]) == [
        codex_launcher.rollout_ownership_marker(manifest["launch_id"])
    ]


def test_launch_binds_absolute_inline_cd_for_memory_and_manifest(tmp_path, monkeypatch) -> None:
    process_cwd = tmp_path / "caller"
    repo = tmp_path / "absolute-source"
    process_cwd.mkdir()
    repo.mkdir()
    calls = []
    memory_calls = []
    _stub_native_codex(monkeypatch, calls)
    monkeypatch.setattr(
        codex_launcher,
        "_preload_pacer_memory",
        lambda **kwargs: memory_calls.append(kwargs)
        or {"memory_receipt": "absolute-receipt", "effective_memory": {"hit": False}},
    )
    cd_argument = f"--cd={repo}"

    assert codex_launcher.launch_codex(
        [cd_argument, "exec", "修复绝对路径项目"],
        cwd=process_cwd,
    ) == 0

    command, options = calls[0]
    assert cd_argument in command
    assert options["cwd"] == str(process_cwd.resolve())
    assert memory_calls[0]["repo_root"] == repo.resolve()
    assert memory_calls[0]["workspace_root"] == repo.resolve() / ".agent-workspace"
    manifest_path = next((repo / ".agent-workspace" / "pacer_native" / "launches").glob("*.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["process_cwd"] == str(process_cwd.resolve())
    assert manifest["effective_repo_root"] == str(repo.resolve())
    assert "修复绝对路径项目" not in manifest_path.read_text(encoding="utf-8")
    assert codex_launcher.PACER_BOOTSTRAP_MEMORY_MARKER in command[-1]
    assert _ownership_lines(command[-1]) == [
        codex_launcher.rollout_ownership_marker(manifest["launch_id"])
    ]


def test_missing_cd_target_degrades_pacer_but_preserves_native_codex_result(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    process_cwd = tmp_path / "caller"
    process_cwd.mkdir()
    missing = tmp_path / "missing-source"
    calls = []
    monkeypatch.delenv("PACER_LAUNCH_ID", raising=False)
    _stub_native_codex(monkeypatch, calls, returncode=19)

    result = codex_launcher.launch_codex(
        ["-C", str(missing), "exec", "原生处理不存在目录"],
        cwd=process_cwd,
    )

    assert result == 19
    command, options = calls[0]
    cd_index = command.index("-C")
    assert command[cd_index : cd_index + 2] == ["-C", str(missing)]
    assert options["cwd"] == str(process_cwd.resolve())
    launch_id = options["env"]["PACER_LAUNCH_ID"]
    assert _ownership_lines(command[-1]) == [codex_launcher.rollout_ownership_marker(launch_id)]
    assert not missing.exists()
    assert not (process_cwd / ".agent-workspace").exists()
    assert "effective repository binding: ValueError" in capsys.readouterr().err


def test_launch_codex_exports_managed_python_and_records_runtime(tmp_path, monkeypatch) -> None:
    interpreter = tmp_path / ".venv" / "Scripts" / "python.exe"
    runtime = {
        "executable": str(interpreter),
        "source": "project_venv",
        "available": True,
        "pytest_available": True,
        "probe_status": "ok",
        "trusted_venv": True,
        "root": str(tmp_path),
        "bound_repo_root": str(tmp_path),
    }
    calls = []
    monkeypatch.delenv("PACER_PYTHON", raising=False)
    monkeypatch.setattr(codex_launcher, "resolve_python_runtime", lambda _root, **_kwargs: runtime)
    monkeypatch.setattr(codex_launcher.shutil, "which", lambda _name: r"C:\Tools\codex.exe")
    monkeypatch.setattr(
        codex_launcher.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)) or subprocess.CompletedProcess(command, 0),
    )
    monkeypatch.setattr(codex_launcher, "capture_rollout_snapshot", lambda: "snapshot")
    monkeypatch.setattr(
        codex_launcher,
        "aggregate_rollout_telemetry",
        lambda _snapshot, **_kwargs: {"status": "no_rollout", "attribution_confidence": "none"},
    )

    assert codex_launcher.launch_codex(cwd=tmp_path) == 0

    environment = calls[0][1]["env"]
    assert environment["PACER_PYTHON"] == str(interpreter)
    assert environment["PATH"].split(codex_launcher.os.pathsep)[0] == str(interpreter.parent)
    manifest = next((tmp_path / ".agent-workspace" / "pacer_native" / "launches").glob("*.json"))
    assert json.loads(manifest.read_text(encoding="utf-8"))["runtime"]["python"] == runtime


def test_launch_codex_exports_its_installed_wheel_python(tmp_path, monkeypatch) -> None:
    interpreter = tmp_path / "wheel-a" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("fixture", encoding="utf-8")
    calls = []
    observed = {}
    monkeypatch.delenv("PACER_PYTHON", raising=False)
    monkeypatch.setattr(codex_launcher.sys, "executable", str(interpreter))
    monkeypatch.setattr(codex_launcher.shutil, "which", lambda name: r"C:\Tools\codex.exe")

    def resolve(root, **kwargs):
        observed.update({"root": root, **kwargs})
        return {
            "executable": str(interpreter),
            "source": "pacer_launcher",
            "available": True,
            "pytest_available": True,
            "probe_status": "ok",
            "trusted_venv": True,
            "root": str(interpreter.parent.parent),
            "bound_repo_root": str(tmp_path),
        }

    monkeypatch.setattr(codex_launcher, "resolve_python_runtime", resolve)
    monkeypatch.setattr(
        codex_launcher.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or subprocess.CompletedProcess(command, 0),
    )
    monkeypatch.setattr(codex_launcher, "capture_rollout_snapshot", lambda: "snapshot")
    monkeypatch.setattr(
        codex_launcher,
        "aggregate_rollout_telemetry",
        lambda _snapshot, **_kwargs: {"status": "no_rollout", "attribution_confidence": "none"},
    )

    assert codex_launcher.launch_codex(cwd=tmp_path) == 0

    assert observed["pacer_executable"] == str(interpreter)
    environment = calls[0][1]["env"]
    assert environment["PACER_PYTHON"] == str(interpreter)
    assert environment["PATH"].split(codex_launcher.os.pathsep)[0] == str(interpreter.parent)


def test_managed_pacer_launcher_python_runs_pytest_from_worker_path() -> None:
    if os.name != "nt":
        pytest.skip("Codex worker command resolution is exercised through PowerShell on Windows")
    interpreter = Path(sys.executable).resolve()
    environment = os.environ.copy()
    launch = {
        "runtime": {
            "python": {
                "executable": str(interpreter),
                "source": "pacer_launcher",
                "available": True,
                "pytest_available": True,
                "trusted_venv": True,
            }
        }
    }

    codex_launcher._apply_managed_python_environment(environment, launch)

    resolved = shutil.which("python", path=environment["PATH"])
    assert resolved is not None
    assert os.path.samefile(resolved, interpreter)
    powershell = shutil.which("powershell.exe")
    assert powershell is not None
    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "python -m pytest --version",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("pytest ")


def test_launcher_does_not_freeze_known_root_fallback_in_environment(tmp_path) -> None:
    interpreter = tmp_path / "pacer-runtime" / "Scripts" / "python.exe"
    environment = {"PATH": str(tmp_path / "system-bin")}
    launch = {
        "runtime": {
            "python": {
                "executable": str(interpreter),
                "source": "known_root_venv",
                "available": True,
                "pytest_available": True,
                "trusted_venv": True,
                "root": str(tmp_path / "pacer-runtime"),
                "bound_repo_root": str(tmp_path / "parent-project"),
            }
        }
    }

    codex_launcher._apply_managed_python_environment(environment, launch)

    assert "PACER_PYTHON" not in environment
    assert str(interpreter.parent) not in environment["PATH"].split(codex_launcher.os.pathsep)


def test_launch_record_atomic_writes_use_unique_temporary_files(tmp_path) -> None:
    path = tmp_path / "launch.json"
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def write(index: int) -> None:
        try:
            barrier.wait(timeout=2)
            codex_launcher._write_launch_record(path, {"writer": index})
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert errors == []
    assert json.loads(path.read_text(encoding="utf-8"))["writer"] in range(4)
    assert list(tmp_path.glob("*.tmp")) == []


def test_launch_record_detects_exec_after_global_options(tmp_path) -> None:
    path, payload = codex_launcher._start_launch_record(
        tmp_path,
        [
            "-c",
            "model=\"gpt-test\"",
            "-m",
            "gpt-test",
            "-s",
            "danger-full-access",
            "-a",
            "never",
            "exec",
            "任务",
        ],
    )

    assert path is not None
    assert payload is not None
    assert payload["mode"] == "exec"
    assert payload["liveness"]["stall_detection_enabled"] is True
    assert codex_launcher._codex_launch_mode(["-m", "gpt-test", "e", "任务"]) == "exec"


def test_launch_record_lock_wait_is_bounded_to_one_second(tmp_path, monkeypatch) -> None:
    observed: dict[str, object] = {}

    class FakeLock:
        def __init__(self, filename, **kwargs):
            observed["filename"] = filename
            observed.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(codex_launcher.portalocker, "Lock", FakeLock)
    codex_launcher._write_launch_record(tmp_path / "launch.json", {"status": "running"})

    assert observed["timeout"] == codex_launcher.LAUNCH_RECORD_LOCK_TIMEOUT_SECONDS == 1.0
    assert observed["check_interval"] == 0.01


def test_finish_forces_existing_running_liveness_to_terminal_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(codex_launcher, "recover_orphaned_launches", lambda _workspace: [])
    path, payload = codex_launcher._start_launch_record(tmp_path, ["exec"])
    assert path is not None
    assert payload is not None
    workspace = tmp_path / ".agent-workspace"
    launch_id = str(payload["launch_id"])
    write_launch_liveness(
        workspace,
        launch_id,
        {
            "state": "active",
            "monitoring": True,
            "lifecycle_status": "running",
        },
    )

    class StaleWatchdog:
        def stop(self, *, lifecycle_status: str):
            assert lifecycle_status == "failed"
            return {"state": "stalled", "monitoring": True, "lifecycle_status": "running"}

    monkeypatch.setattr(
        codex_launcher,
        "aggregate_rollout_telemetry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("telemetry failed")),
    )
    codex_launcher._finish_launch_record(
        path,
        payload,
        exit_code=7,
        status="failed",
        rollout_snapshot=object(),
        watchdog=StaleWatchdog(),
    )

    persisted = read_launch_liveness(workspace, launch_id)
    assert persisted["state"] == "stalled"
    assert persisted["monitoring"] is False
    assert persisted["lifecycle_status"] == "failed"
    assert persisted["stopped_at"]
    active = read_active_launch(workspace, launch_id=launch_id)
    assert active["status"] == "failed"
    assert active["liveness"]["monitoring"] is False
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["rollout_telemetry"]["status"] == "unavailable"


def test_terminal_context_wins_when_terminal_liveness_sidecar_write_fails(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(codex_launcher, "recover_orphaned_launches", lambda _workspace: [])
    path, payload = codex_launcher._start_launch_record(tmp_path, ["exec"])
    assert path is not None
    assert payload is not None
    workspace = tmp_path / ".agent-workspace"
    launch_id = str(payload["launch_id"])
    write_launch_liveness(
        workspace,
        launch_id,
        {
            "state": "active",
            "monitoring": True,
            "lifecycle_status": "running",
        },
    )
    monkeypatch.setattr(
        codex_launcher,
        "write_launch_liveness",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("sidecar unavailable")),
    )

    codex_launcher._finish_launch_record(path, payload, exit_code=7, status="failed")

    assert read_launch_liveness(workspace, launch_id)["monitoring"] is True
    active = read_active_launch(workspace, launch_id=launch_id)
    assert active["status"] == "failed"
    assert active["liveness"]["monitoring"] is False
    assert active["liveness"]["lifecycle_status"] == "failed"
    assert active["liveness"]["stopped_at"] == active["completed_at"]


def test_finish_isolates_manifest_recovery_active_and_event_failures(tmp_path, monkeypatch) -> None:
    from visual_agent import pacer_events

    monkeypatch.setattr(codex_launcher, "recover_orphaned_launches", lambda _workspace: [])
    path, payload = codex_launcher._start_launch_record(tmp_path, ["exec"])
    assert path is not None
    assert payload is not None
    payload["rollout_telemetry"] = {"current_context_usage": {"input_tokens": 1}}
    active_updates: list[dict[str, object]] = []
    events: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(
        codex_launcher,
        "_write_launch_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("manifest unavailable")),
    )
    monkeypatch.setattr(
        codex_launcher,
        "write_context_recovery_capsule",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("recovery unavailable")),
    )

    def fail_active_update(*_args, **kwargs):
        active_updates.append(kwargs)
        raise OSError("active state unavailable")

    monkeypatch.setattr(codex_launcher, "update_active_launch", fail_active_update)
    monkeypatch.setattr(
        pacer_events,
        "append_pacer_event",
        lambda _workspace, event_type, **kwargs: events.append((event_type, kwargs)),
    )

    codex_launcher._finish_launch_record(path, payload, exit_code=9, status="failed")

    assert active_updates[0]["status"] == "failed"
    assert active_updates[0]["exit_code"] == 9
    assert active_updates[0]["liveness"]["monitoring"] is False
    assert events[0][0] == "launch_finished"
    assert events[0][1]["data"]["status"] == "failed"


def test_manifest_lock_failure_warns_but_native_codex_still_runs(tmp_path, monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(codex_launcher.shutil, "which", lambda _name: r"C:\Tools\codex.exe")
    monkeypatch.setattr(codex_launcher, "recover_orphaned_launches", lambda _workspace: [])
    monkeypatch.setattr(
        codex_launcher,
        "_write_launch_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            codex_launcher.portalocker.exceptions.AlreadyLocked("busy")
        ),
    )
    monkeypatch.setattr(
        codex_launcher,
        "capture_rollout_snapshot",
        lambda: (_ for _ in ()).throw(OSError("snapshot unavailable")),
    )
    monkeypatch.setattr(
        codex_launcher.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)) or subprocess.CompletedProcess(command, 0),
    )

    assert codex_launcher.launch_codex(cwd=tmp_path) == 0

    assert len(calls) == 1
    assert "launch manifest lock: AlreadyLocked" in capsys.readouterr().err
    active = read_active_launch(tmp_path / ".agent-workspace")
    assert active["status"] == "completed"
    assert active["liveness"]["monitoring"] is False


def test_active_state_record_failure_warns_but_native_codex_still_runs(tmp_path, monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(codex_launcher.shutil, "which", lambda _name: r"C:\Tools\codex.exe")
    monkeypatch.setattr(codex_launcher, "recover_orphaned_launches", lambda _workspace: [])
    monkeypatch.setattr(
        codex_launcher,
        "initialize_active_launch",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("state unavailable")),
    )
    monkeypatch.setattr(
        codex_launcher,
        "capture_rollout_snapshot",
        lambda: (_ for _ in ()).throw(OSError("snapshot unavailable")),
    )
    monkeypatch.setattr(
        codex_launcher.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)) or subprocess.CompletedProcess(command, 0),
    )

    assert codex_launcher.launch_codex(cwd=tmp_path) == 0

    assert len(calls) == 1
    assert "active launch state: RuntimeError" in capsys.readouterr().err
    manifest = next((tmp_path / ".agent-workspace" / "pacer_native" / "launches").glob("*.json"))
    assert json.loads(manifest.read_text(encoding="utf-8"))["status"] == "completed"


def test_missing_launch_record_still_pins_mcp_to_this_failed_launch(tmp_path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(codex_launcher.shutil, "which", lambda _name: r"C:\Tools\codex.exe")
    monkeypatch.setattr(codex_launcher, "_start_launch_record", lambda *_args, **_kwargs: (None, None))
    monkeypatch.setattr(
        codex_launcher.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)) or subprocess.CompletedProcess(command, 0),
    )

    assert codex_launcher.launch_codex(["exec", "inspect project"], cwd=tmp_path) == 0

    assert len(calls) == 1
    launch_id = calls[0][1]["env"]["PACER_LAUNCH_ID"]
    assert codex_launcher.rollout_ownership_marker(launch_id) in calls[0][0][-1]
    assert not (tmp_path / ".agent-workspace").exists()


def test_launch_codex_bootstraps_minimal_workspace_on_first_project_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(codex_launcher.shutil, "which", lambda _name: r"C:\Tools\codex.exe")
    monkeypatch.setattr(
        codex_launcher.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0),
    )
    monkeypatch.setattr(codex_launcher, "capture_rollout_snapshot", lambda: "snapshot")
    monkeypatch.setattr(
        codex_launcher,
        "aggregate_rollout_telemetry",
        lambda _snapshot, **_kwargs: {"status": "no_rollout", "attribution_confidence": "none"},
    )

    assert not (tmp_path / ".agent-workspace").exists()
    assert codex_launcher.launch_codex(cwd=tmp_path) == 0

    manifests = list((tmp_path / ".agent-workspace" / "pacer_native" / "launches").glob("*.json"))
    assert len(manifests) == 1
    payload = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert payload["repo_root"] == str(tmp_path.resolve())
    assert payload["status"] == "completed"
    assert not (tmp_path / "pacer_native").exists()


def test_pacer_codex_args_preserves_explicit_auto_compact_override() -> None:
    arguments = ["-c", "model_auto_compact_token_limit=120000", "resume", "--last"]

    assert codex_launcher._pacer_codex_args(arguments) == arguments


def test_pacer_mcp_config_overrides_user_transport_and_forwards_launch_trust() -> None:
    configured = codex_launcher._inject_pacer_mcp_config(
        ["-c", 'mcp_servers.pacer.command="stale-python"', "exec", "修复错误"]
    )

    command_configs = [configured[index + 1] for index, value in enumerate(configured) if value == "-c"]
    expected_command = "mcp_servers.pacer.command=" + json.dumps(
        os.path.abspath(codex_launcher.sys.executable),
        ensure_ascii=False,
    )
    expected_environment = "mcp_servers.pacer.env_vars=" + json.dumps(
        [
            "PACER_LAUNCH_ID",
            "PACER_PRELAUNCH_TASK_REQUIRED",
            "PACER_PRELAUNCH_TASK_CONTRACT_DIGEST",
            "PACER_PRELAUNCH_SOURCE_BASELINE_DIGEST",
        ]
    )
    assert command_configs == [
        'mcp_servers.pacer.command="stale-python"',
        expected_command,
        'mcp_servers.pacer.args=["-m", "visual_agent.mcp_server"]',
        expected_environment,
        "mcp_servers.pacer.required=true",
        "mcp_servers.pacer.startup_timeout_sec=30",
    ]
    assert configured[-2:] == ["exec", "修复错误"]


def test_failed_prelaunch_registration_blocks_codex_and_returns_nonzero(
    tmp_path,
    monkeypatch,
) -> None:
    calls = []
    _stub_native_codex(monkeypatch, calls)
    monkeypatch.setenv("PACER_PRELAUNCH_TASK_CONTRACT_DIGEST", "a" * 64)
    monkeypatch.setenv("PACER_PRELAUNCH_SOURCE_BASELINE_DIGEST", "b" * 64)
    monkeypatch.setattr(
        codex_launcher,
        "_pre_register_pacer_task",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("registration failed")),
    )

    assert codex_launcher.launch_codex(["exec", "修复登录错误"], cwd=tmp_path) == 78

    assert calls == []
    active = read_active_launch(tmp_path / ".agent-workspace")
    assert active["status"] == "launch_failed"
    assert active["exit_code"] == 78
    assert active["prelaunch_task_registration"] == {
        "schema_version": 1,
        "status": "failed",
        "error_type": "RuntimeError",
    }


def test_effective_auto_compact_limit_reads_inline_long_config() -> None:
    arguments = ["--config=model_auto_compact_token_limit=123456", "resume", "--last"]

    assert codex_launcher._pacer_codex_args(arguments) == arguments
    assert codex_launcher._effective_auto_compact_limit(arguments) == 123456


def test_pacer_codex_args_can_disable_default_with_environment(monkeypatch) -> None:
    monkeypatch.setenv("PACER_AUTO_COMPACT_TOKEN_LIMIT", "0")

    assert codex_launcher._pacer_codex_args(["resume", "--last"]) == ["resume", "--last"]


def test_pacer_codex_args_uses_environment_limit(monkeypatch) -> None:
    monkeypatch.setenv("PACER_AUTO_COMPACT_TOKEN_LIMIT", "72000")

    assert codex_launcher._pacer_codex_args([])[:2] == [
        "-c",
        "model_auto_compact_token_limit=72000",
    ]


def test_prepare_pacer_exec_prepends_native_control_to_positional_prompt() -> None:
    arguments, prepend_stdin = codex_launcher._prepare_pacer_invocation(
        ["--model", "gpt-5.6-sol", "exec", "--json", "修复登录错误"]
    )

    assert prepend_stdin is False
    assert arguments[-3:-1] == ["exec", "--json"]
    _assert_native_control(arguments[-1], task="修复登录错误")


def test_prepare_pacer_exec_preserves_stdin_marker() -> None:
    arguments, prepend_stdin = codex_launcher._prepare_pacer_invocation(["exec", "--json", "-"])

    assert arguments[-3:] == ["exec", "--json", "-"]
    assert prepend_stdin is True


def test_launch_pacer_exec_prefixes_stdin_without_moving_marker(tmp_path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(codex_launcher.shutil, "which", lambda _name: r"C:\Tools\codex.exe")
    monkeypatch.setattr(codex_launcher.sys, "stdin", io.StringIO("检查当前项目"))
    monkeypatch.setattr(codex_launcher, "capture_rollout_snapshot", lambda: "snapshot")
    monkeypatch.setattr(
        codex_launcher,
        "aggregate_rollout_telemetry",
        lambda _snapshot, **_kwargs: {"status": "no_rollout", "attribution_confidence": "none"},
    )
    monkeypatch.setattr(
        codex_launcher.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)) or subprocess.CompletedProcess(command, 0),
    )

    assert codex_launcher.launch_codex(["exec", "-"], cwd=tmp_path) == 0

    command, options = calls[0]
    assert command[-2:] == ["exec", "-"]
    _assert_native_control(options["input"], task="检查当前项目")
    assert options["text"] is True
    assert options["encoding"] == "utf-8"
    assert options["errors"] == "replace"


def test_launch_preloads_memory_once_for_real_exec_task(tmp_path, monkeypatch) -> None:
    process_calls = []
    memory_calls = []
    _stub_native_codex(monkeypatch, process_calls)

    def fake_preload(**kwargs):
        memory_calls.append(kwargs)
        active = read_active_launch(kwargs["workspace_root"], launch_id=kwargs["launch_id"])
        assert active["status"] == "running"
        return {
            "schema_version": 2,
            "response_detail": "compact",
            "status": "memory_loaded",
            "memory_receipt": "receipt-exec-1",
            "effective_memory": {"hit": True, "formal_entries": 0, "native_history_entries": 1},
        }

    monkeypatch.setattr(codex_launcher, "_preload_pacer_memory", fake_preload)

    assert codex_launcher.launch_codex(
        ["-m", "gpt-5.6-sol", "exec", "--json", "修复登录错误"],
        cwd=tmp_path,
    ) == 0

    assert len(memory_calls) == 1
    assert memory_calls[0]["goal"] == "修复登录错误"
    assert memory_calls[0]["repo_root"] == tmp_path.resolve()
    assert memory_calls[0]["workspace_root"] == tmp_path.resolve() / ".agent-workspace"
    prompt = process_calls[0][0][-1]
    _assert_native_control(prompt, task="修复登录错误")
    assert prompt.splitlines().count(codex_launcher.PACER_BOOTSTRAP_MEMORY_MARKER) == 1
    assert "memory_receipt=receipt-exec-1" in prompt
    assert "Do not call mcp__pacer__get_pacer_memory again" in prompt
    assert '"memory_receipt":"receipt-exec-1"' in prompt
    manifest = next((tmp_path / ".agent-workspace" / "pacer_native" / "launches").glob("*.json"))
    persisted = json.loads(manifest.read_text(encoding="utf-8"))
    assert persisted["mode"] == "exec"
    assert persisted["liveness"]["stall_detection_enabled"] is True


def test_launch_pre_registers_task_evidence_before_starting_codex(tmp_path, monkeypatch) -> None:
    from visual_agent.pacer_launch_context import (
        PRELAUNCH_SOURCE_BASELINE_DIGEST_ENV,
        PRELAUNCH_TASK_CONTRACT_DIGEST_ENV,
        PRELAUNCH_TASK_REQUIRED_ENV,
        load_task_source_baseline,
        task_contract_digest,
        task_source_baseline_digest,
    )

    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    process_calls = []
    observed = {}
    monkeypatch.setattr(task_review, "_SUBPROCESS_RUN", REAL_TASK_REVIEW_SUBPROCESS_RUN)
    _stub_native_codex(monkeypatch, process_calls)

    def fake_run(command, **kwargs):
        launch_id = kwargs["env"]["PACER_LAUNCH_ID"]
        workspace = tmp_path / ".agent-workspace"
        active = read_active_launch(workspace, launch_id=launch_id)
        baseline = load_task_source_baseline(active, workspace_root=workspace)
        observed.update({"active": active, "baseline": baseline, "env": kwargs["env"]})
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(codex_launcher.subprocess, "run", fake_run)

    assert codex_launcher.launch_codex(["exec", "修复登录错误"], cwd=tmp_path) == 0

    active = observed["active"]
    baseline = observed["baseline"]
    environment = observed["env"]
    assert active["launch_goal"] == "修复登录错误"
    assert active["task_contract"]["requirements"]
    assert active["prelaunch_task_registration"]["status"] == "ready"
    assert baseline["entries"]["app.py"]
    assert environment[PRELAUNCH_TASK_REQUIRED_ENV] == "1"
    assert environment[PRELAUNCH_TASK_CONTRACT_DIGEST_ENV] == task_contract_digest(active["task_contract"])
    assert environment[PRELAUNCH_SOURCE_BASELINE_DIGEST_ENV] == task_source_baseline_digest(baseline)


def test_resume_reuses_pending_recovery_contract_and_source_baseline(tmp_path, monkeypatch) -> None:
    from visual_agent.pacer_launch_context import (
        PRELAUNCH_SOURCE_BASELINE_DIGEST_ENV,
        PRELAUNCH_TASK_CONTRACT_DIGEST_ENV,
        load_task_source_baseline,
    )

    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    expected_source_digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    calls = []
    observed = {}
    monkeypatch.setattr(task_review, "_SUBPROCESS_RUN", REAL_TASK_REVIEW_SUBPROCESS_RUN)
    monkeypatch.setattr(codex_launcher.shutil, "which", lambda _name: r"C:\Tools\codex.exe")
    snapshot = codex_launcher.RolloutSnapshot(tmp_path / "sessions", "2026-07-16T00:00:00+00:00", {})
    monkeypatch.setattr(codex_launcher, "capture_rollout_snapshot", lambda: snapshot)
    monkeypatch.setattr(codex_launcher, "save_rollout_baseline", lambda **_kwargs: None)
    monkeypatch.setattr(codex_launcher, "_start_launch_watchdog", lambda **_kwargs: None)
    monkeypatch.setattr(
        codex_launcher,
        "aggregate_rollout_telemetry",
        lambda *_args, **_kwargs: {
            "status": "captured",
            "current_context_usage": {"input_tokens": 96000},
        },
    )

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            source.write_text("value = 2\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 1)
        workspace = tmp_path / ".agent-workspace"
        active = read_active_launch(workspace, launch_id=kwargs["env"]["PACER_LAUNCH_ID"])
        observed.update(
            {
                "active": active,
                "baseline": load_task_source_baseline(active, workspace_root=workspace),
                "command": command,
                "environment": kwargs["env"],
            }
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(codex_launcher.subprocess, "run", fake_run)

    assert codex_launcher.launch_codex(["exec", "repair recovery"], cwd=tmp_path) == 1
    assert codex_launcher.launch_codex(
        ["exec", "resume", "session-1", "continue previous work"],
        cwd=tmp_path,
    ) == 0

    active = observed["active"]
    assert active["launch_goal"] == "repair recovery"
    assert active["prelaunch_task_registration"]["status"] == "recovered"
    assert active["recovery_source_launch_id"]
    assert observed["baseline"]["entries"]["app.py"] == expected_source_digest
    prompt = observed["command"][-1]
    _assert_native_control(prompt, task="repair recovery")
    assert "continue previous work" not in prompt
    assert observed["environment"][PRELAUNCH_TASK_CONTRACT_DIGEST_ENV] == active["task_contract_digest"]
    assert observed["environment"][PRELAUNCH_SOURCE_BASELINE_DIGEST_ENV] == active[
        "source_baseline_digest"
    ]


@pytest.mark.parametrize("tamper", ["contract", "baseline"])
def test_resume_blocks_tampered_recovery_evidence(tmp_path, monkeypatch, tamper) -> None:
    from visual_agent.pacer_launch_context import (
        read_active_launch,
        task_source_baseline_path,
        write_active_launch,
    )

    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(task_review, "_SUBPROCESS_RUN", REAL_TASK_REVIEW_SUBPROCESS_RUN)
    monkeypatch.setattr(codex_launcher.shutil, "which", lambda _name: r"C:\Tools\codex.exe")
    snapshot = codex_launcher.RolloutSnapshot(tmp_path / "sessions", "2026-07-16T00:00:00+00:00", {})
    monkeypatch.setattr(codex_launcher, "capture_rollout_snapshot", lambda: snapshot)
    monkeypatch.setattr(codex_launcher, "save_rollout_baseline", lambda **_kwargs: None)
    monkeypatch.setattr(codex_launcher, "_start_launch_watchdog", lambda **_kwargs: None)
    monkeypatch.setattr(
        codex_launcher,
        "aggregate_rollout_telemetry",
        lambda *_args, **_kwargs: {
            "status": "captured",
            "current_context_usage": {"input_tokens": 96000},
        },
    )
    monkeypatch.setattr(
        codex_launcher.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or subprocess.CompletedProcess(command, 1),
    )

    assert codex_launcher.launch_codex(["exec", "repair recovery"], cwd=tmp_path) == 1
    workspace = tmp_path / ".agent-workspace"
    source_launch = read_active_launch(workspace)
    if tamper == "contract":
        source_launch["task_contract"]["requirements"][0]["text"] = "tampered"
        write_active_launch(workspace, source_launch)
    else:
        baseline_path = task_source_baseline_path(workspace, source_launch["launch_id"])
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline["captured_at"] = "tampered"
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    assert codex_launcher.launch_codex(
        ["exec", "resume", "session-1", "continue previous work"],
        cwd=tmp_path,
    ) == 78
    assert len(calls) == 1


def test_launch_memory_preload_failure_warns_and_preserves_native_prompt(tmp_path, monkeypatch, capsys) -> None:
    process_calls = []
    _stub_native_codex(monkeypatch, process_calls)

    def fail_preload(**_kwargs):
        raise RuntimeError("memory unavailable")

    monkeypatch.setattr(codex_launcher, "_preload_pacer_memory", fail_preload)

    assert codex_launcher.launch_codex(["exec", "修复登录错误"], cwd=tmp_path) == 0

    _assert_native_control(process_calls[0][0][-1], task="修复登录错误")
    assert process_calls[0][0][-1].splitlines().count(codex_launcher.PACER_BOOTSTRAP_MEMORY_MARKER) == 0
    assert "memory preload: RuntimeError" in capsys.readouterr().err


def test_launch_preloads_stdin_task_without_moving_dash_marker(tmp_path, monkeypatch) -> None:
    process_calls = []
    memory_calls = []
    _stub_native_codex(monkeypatch, process_calls)
    monkeypatch.setattr(codex_launcher.sys, "stdin", io.StringIO("审查并修复并发错误"))
    monkeypatch.setattr(
        codex_launcher,
        "_preload_pacer_memory",
        lambda **kwargs: memory_calls.append(kwargs)
        or {"memory_receipt": "receipt-stdin-1", "effective_memory": {"hit": False}},
    )

    assert codex_launcher.launch_codex(["exec", "--json", "-"], cwd=tmp_path) == 0

    command, options = process_calls[0]
    assert command[-3:] == ["exec", "--json", "-"]
    assert len(memory_calls) == 1
    assert memory_calls[0]["goal"] == "审查并修复并发错误"
    _assert_native_control(options["input"], task="审查并修复并发错误")
    assert codex_launcher.PACER_BOOTSTRAP_MEMORY_MARKER in options["input"]
    assert "memory_receipt=receipt-stdin-1" in options["input"]


def test_memory_preload_binds_launch_id_and_restores_environment(tmp_path, monkeypatch) -> None:
    observed = []

    def fake_payload(args):
        observed.append((os.environ.get("PACER_LAUNCH_ID"), args))
        return {"memory_receipt": "receipt-env-1", "summary": "safe"}

    monkeypatch.setattr(codex_launcher, "_get_pacer_memory_payload", fake_payload)
    monkeypatch.delenv("PACER_LAUNCH_ID", raising=False)
    payload = REAL_PRELOAD_PACER_MEMORY(
        workspace_root=tmp_path / ".agent-workspace",
        repo_root=tmp_path,
        launch_id="launch-inner",
        goal="真实任务",
    )

    assert payload["memory_receipt"] == "receipt-env-1"
    assert observed == [
        (
            "launch-inner",
            {
                "workspace_root": str(tmp_path / ".agent-workspace"),
                "repo_root": str(tmp_path),
                "goal": "真实任务",
                "detail": "compact",
            },
        )
    ]
    assert "PACER_LAUNCH_ID" not in os.environ

    monkeypatch.setenv("PACER_LAUNCH_ID", "launch-outer")
    monkeypatch.setattr(
        codex_launcher,
        "_get_pacer_memory_payload",
        lambda _args: (_ for _ in ()).throw(RuntimeError("failed")),
    )
    with pytest.raises(RuntimeError, match="failed"):
        REAL_PRELOAD_PACER_MEMORY(
            workspace_root=tmp_path / ".agent-workspace",
            repo_root=tmp_path,
            launch_id="launch-failing",
            goal="失败任务",
        )
    assert os.environ["PACER_LAUNCH_ID"] == "launch-outer"


@pytest.mark.parametrize(
    ("argv", "stdin_text", "expected"),
    [
        (["自然语言任务"], None, "自然语言任务"),
        (["-c", "model=\"x\"", "exec", "执行任务"], None, "执行任务"),
        (["review", "审查任务"], None, "审查任务"),
        (["resume", "--last", "继续任务"], None, "继续任务"),
        (["resume", "session-name", "继续任务"], None, "继续任务"),
        (["exec", "resume", "--last", "-"], "stdin 继续任务", "stdin 继续任务"),
        (["fork", "--last", "分叉任务"], None, ""),
    ],
)
def test_task_text_is_extracted_only_from_supported_real_task_forms(argv, stdin_text, expected) -> None:
    assert codex_launcher._pacer_task_text(argv, stdin_text=stdin_text) == expected


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["review"],
        ["resume"],
        ["resume", "--last"],
        ["resume", "session-name"],
        ["plugin", "list"],
    ],
)
def test_no_task_and_management_launches_do_not_preload_memory(tmp_path, monkeypatch, argv) -> None:
    process_calls = []
    _stub_native_codex(monkeypatch, process_calls)

    def unexpected_preload(**_kwargs):
        raise AssertionError("plain or taskless Codex launch must not preload Pacer memory")

    monkeypatch.setattr(codex_launcher, "_preload_pacer_memory", unexpected_preload)

    assert codex_launcher.launch_codex(argv, cwd=tmp_path) == 0
    assert len(process_calls) == 1
    if not argv:
        _assert_native_control(process_calls[0][0][-1])
        assert "PACER_WAIT_FOR_TASK_V1" in process_calls[0][0][-1]
        assert "Wait for the user's task and do not call any tool yet." in process_calls[0][0][-1]


def test_empty_exec_stdin_does_not_preload_memory(tmp_path, monkeypatch) -> None:
    process_calls = []
    _stub_native_codex(monkeypatch, process_calls)
    monkeypatch.setattr(codex_launcher.sys, "stdin", io.StringIO(""))

    def unexpected_preload(**_kwargs):
        raise AssertionError("empty stdin is not a real task")

    monkeypatch.setattr(codex_launcher, "_preload_pacer_memory", unexpected_preload)

    assert codex_launcher.launch_codex(["exec", "-"], cwd=tmp_path) == 0
    assert process_calls[0][0][-2:] == ["exec", "-"]
    _assert_native_control(process_calls[0][1]["input"])
    assert "PACER_WAIT_FOR_TASK_V1" in process_calls[0][1]["input"]


def test_prepare_interactive_pacer_injects_control_without_changing_native_mode() -> None:
    arguments, prepend_stdin = codex_launcher._prepare_pacer_invocation([])

    _assert_native_control(arguments[-1])
    assert "PACER_WAIT_FOR_TASK_V1" in arguments[-1]
    assert "exec" not in arguments
    assert prepend_stdin is False


def test_prepare_pacer_skill_is_idempotent_and_management_commands_are_untouched() -> None:
    prompt = f"{codex_launcher.PACER_SKILL_INVOCATION}\n\n继续开发"
    activated, prepend_stdin = codex_launcher._activate_pacer_skill(["exec", prompt])
    reactivated, second_stdin = codex_launcher._activate_pacer_skill(activated)
    plugin_command, plugin_stdin = codex_launcher._activate_pacer_skill(["plugin", "list"])

    _assert_native_control(activated[-1], task="继续开发")
    assert reactivated == activated
    assert prepend_stdin is False
    assert second_stdin is False
    assert plugin_command == ["plugin", "list"]
    assert plugin_stdin is False


def test_prepare_pacer_resume_preserves_session_position_and_activates_followup() -> None:
    resumed, prepend_stdin = codex_launcher._activate_pacer_skill(
        ["resume", "session-name", "继续上次任务"]
    )
    picker, picker_stdin = codex_launcher._activate_pacer_skill(["resume"])

    assert resumed[:2] == ["resume", "session-name"]
    _assert_native_control(resumed[-1], task="继续上次任务")
    assert prepend_stdin is False
    assert picker == ["resume"]
    assert picker_stdin is False


def test_prompt_named_help_after_option_terminator_is_activated() -> None:
    arguments, prepend_stdin = codex_launcher._activate_pacer_skill(["exec", "--", "--help"])

    assert arguments[:2] == ["exec", "--"]
    _assert_native_control(arguments[-1], task="--help")
    assert prepend_stdin is False


def test_resume_prompt_named_last_after_option_terminator_does_not_replace_session() -> None:
    arguments, prepend_stdin = codex_launcher._activate_pacer_skill(
        ["resume", "session-name", "--", "--last"]
    )

    assert arguments[:3] == ["resume", "session-name", "--"]
    _assert_native_control(arguments[-1], task="--last")
    assert prepend_stdin is False


def test_multi_value_images_are_not_mistaken_for_exec_prompt() -> None:
    arguments, prepend_stdin = codex_launcher._activate_pacer_skill(
        ["exec", "-i", "first.png", "second.png"]
    )

    assert arguments == ["exec", "-i", "first.png", "second.png"]
    assert prepend_stdin is True


def test_attached_short_image_option_keeps_following_values_out_of_prompt() -> None:
    arguments, prepend_stdin = codex_launcher._activate_pacer_skill(
        ["exec", "-ifirst.png", "second.png"]
    )

    assert arguments == ["exec", "-ifirst.png", "second.png"]
    assert prepend_stdin is True


def test_exec_prompt_before_multi_value_images_is_activated_in_place() -> None:
    arguments, prepend_stdin = codex_launcher._activate_pacer_skill(
        ["exec", "检查截图", "-i", "first.png", "second.png", "--json"]
    )

    assert arguments[0] == "exec"
    _assert_native_control(arguments[1], task="检查截图")
    assert arguments[2:] == ["-i", "first.png", "second.png", "--json"]
    assert prepend_stdin is False


def test_interactive_skill_prompt_is_inserted_before_greedy_image_values() -> None:
    arguments, prepend_stdin = codex_launcher._activate_pacer_skill(
        ["-m", "gpt-5.6-sol", "--image", "first.png", "second.png"]
    )

    assert arguments[:2] == ["-m", "gpt-5.6-sol"]
    _assert_native_control(arguments[2])
    assert arguments[3:] == ["--image", "first.png", "second.png"]
    assert prepend_stdin is False


def test_native_control_idempotence_requires_exact_first_line_token() -> None:
    collision = f"{codex_launcher.PACER_NATIVE_CONTROL_MARKER}-extra task"
    exact = codex_launcher._pacer_control_prompt("继续开发")

    collision_arguments, _ = codex_launcher._activate_pacer_skill(["exec", collision])
    exact_arguments, _ = codex_launcher._activate_pacer_skill(["exec", exact])

    _assert_native_control(collision_arguments[-1], task=collision)
    assert exact_arguments == ["exec", exact]


@pytest.mark.parametrize("command", ["resume", "fork"])
def test_interactive_resume_and_fork_dash_is_a_literal_prompt(command) -> None:
    arguments, prepend_stdin = codex_launcher._activate_pacer_skill([command, "--last", "-"])

    assert arguments[:2] == [command, "--last"]
    _assert_native_control(arguments[-1], task="-")
    assert prepend_stdin is False


def test_exec_resume_dash_keeps_documented_stdin_semantics() -> None:
    arguments, prepend_stdin = codex_launcher._activate_pacer_skill(
        ["exec", "resume", "--last", "-"]
    )

    assert arguments == ["exec", "resume", "--last", "-"]
    assert prepend_stdin is True


def test_native_codex_command_bypasses_windows_cmd_shim(tmp_path, monkeypatch) -> None:
    npm = tmp_path / "npm"
    codex_cmd = npm / "codex.cmd"
    codex_js = npm / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    codex_js.parent.mkdir(parents=True)
    codex_cmd.write_text("shim", encoding="utf-8")
    codex_js.write_text("entry", encoding="utf-8")
    monkeypatch.setattr(codex_launcher.os, "name", "nt")
    monkeypatch.setattr(codex_launcher.shutil, "which", lambda name: r"C:\\node.exe" if name == "node" else None)

    command = codex_launcher._native_codex_command(codex_cmd, ["exec", "带 空格 的任务"])

    assert command == [r"C:\\node.exe", str(codex_js), "exec", "带 空格 的任务"]


def test_launch_codex_reports_missing_cli(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(codex_launcher.shutil, "which", lambda _name: None)

    assert codex_launcher.launch_codex(cwd=tmp_path) == 127
    assert "requires Codex CLI" in capsys.readouterr().out
    assert not (tmp_path / ".agent-workspace").exists()


def test_launch_codex_requires_mcp_runtime_before_creating_launch(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(codex_launcher.shutil, "which", lambda _name: r"C:\Tools\codex.exe")
    monkeypatch.setattr(codex_launcher, "_mcp_runtime_available", lambda: False)

    assert codex_launcher.launch_codex(["exec", "修复错误"], cwd=tmp_path) == 78

    assert "requires the MCP runtime" in capsys.readouterr().out
    assert not (tmp_path / ".agent-workspace").exists()


def test_launch_codex_writes_redacted_launch_manifest(tmp_path, monkeypatch) -> None:
    (tmp_path / ".agent-workspace").mkdir()
    calls = []
    monkeypatch.setattr(codex_launcher.shutil, "which", lambda _name: r"C:\Tools\codex.exe")

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(codex_launcher.subprocess, "run", fake_run)
    monkeypatch.setattr(codex_launcher, "capture_rollout_snapshot", lambda: "safe-snapshot")
    monkeypatch.setattr(
        codex_launcher,
        "aggregate_rollout_telemetry",
        lambda snapshot, **_kwargs: {
            "schema_version": 1,
            "status": "captured",
            "attribution_confidence": "high",
            "source_files": 1,
        },
    )

    assert codex_launcher.launch_codex(["sensitive prompt text"], cwd=tmp_path) == 0

    manifests = list((tmp_path / ".agent-workspace" / "pacer_native" / "launches").glob("*.json"))
    assert len(manifests) == 1
    payload = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["prompt_recorded"] is False
    assert payload["rollout_telemetry"]["status"] == "captured"
    assert payload["rollout_telemetry"]["context_control"] == {
        "auto_compact_token_limit": 96000,
        "scope": "total",
        "usage_semantics": "cumulative_session_usage_not_current_context_size",
    }
    assert "sensitive prompt text" not in manifests[0].read_text(encoding="utf-8")
    assert calls[0][1]["env"]["PACER_LAUNCH_ID"] == payload["launch_id"]


def test_rollout_snapshot_failure_does_not_block_codex(tmp_path, monkeypatch) -> None:
    (tmp_path / ".agent-workspace").mkdir()
    monkeypatch.setattr(codex_launcher.shutil, "which", lambda _name: r"C:\Tools\codex.exe")
    monkeypatch.setattr(
        codex_launcher.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0),
    )

    def fail_snapshot():
        raise PermissionError("sessions unavailable")

    monkeypatch.setattr(codex_launcher, "capture_rollout_snapshot", fail_snapshot)

    assert codex_launcher.launch_codex(cwd=tmp_path) == 0
    manifest = next((tmp_path / ".agent-workspace" / "pacer_native" / "launches").glob("*.json"))
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["rollout_telemetry"]["status"] == "unavailable"
    assert payload["rollout_telemetry"]["warnings"] == ["rollout snapshot unavailable: PermissionError"]


def test_failed_high_pressure_launch_writes_recovery_capsule(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(codex_launcher.shutil, "which", lambda _name: r"C:\Tools\codex.exe")
    monkeypatch.setattr(
        codex_launcher.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1),
    )
    snapshot = codex_launcher.RolloutSnapshot(tmp_path / "sessions", "2026-07-13T00:00:00+00:00", {})
    monkeypatch.setattr(codex_launcher, "capture_rollout_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        codex_launcher,
        "aggregate_rollout_telemetry",
        lambda *_args, **_kwargs: {
            "status": "captured",
            "usage": {"input_tokens": 500000},
            "current_context_usage": {"input_tokens": 96000, "total_tokens": 96100},
            "compactions": {"count": 0, "timestamps": []},
        },
    )
    assert codex_launcher.launch_codex(cwd=tmp_path) == 1
    capsules = list((tmp_path / ".agent-workspace" / "pacer_native" / "recovery").glob("*.json"))
    assert len(capsules) == 1
    capsule = json.loads(capsules[0].read_text(encoding="utf-8"))
    assert capsule["current_context_usage"]["input_tokens"] == 96000


class _FakeClock:
    def __init__(self) -> None:
        self.seconds = 0.0
        self.started = datetime(2026, 7, 13, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.seconds

    def utcnow(self) -> datetime:
        return self.started + timedelta(seconds=self.seconds)

    def advance(self, seconds: float) -> None:
        self.seconds += seconds


class _FakeActivityTracker:
    def __init__(self, observations) -> None:
        self.observations = list(observations)

    def poll(self):
        if self.observations:
            return self.observations.pop(0)
        return {"status": "captured", "activity_observed": False}


def test_watchdog_marks_stalled_once_and_resumes_without_process_control(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    clock = _FakeClock()
    warnings: list[str] = []
    tracker = _FakeActivityTracker(
        [
            {"status": "captured", "activity_observed": False, "attribution_confidence": "high"},
            {"status": "captured", "activity_observed": False, "attribution_confidence": "high"},
            {
                "status": "captured",
                "activity_observed": True,
                "attribution_confidence": "high",
                "observed_at": "2026-07-13T00:00:12+00:00",
                "source_files": 1,
            },
        ]
    )
    watchdog = codex_launcher.LaunchLivenessWatchdog(
        workspace_root=workspace,
        launch_id="launch-1",
        tracker=tracker,
        mode="exec",
        stall_timeout_seconds=10,
        idle_timeout_seconds=2,
        poll_interval_seconds=1,
        monotonic=clock.monotonic,
        utcnow=clock.utcnow,
        warning_callback=warnings.append,
    ).start(background=False)

    clock.advance(11)
    assert watchdog.check_once()["state"] == "stalled"
    clock.advance(1)
    assert watchdog.check_once()["state"] == "stalled"
    assert watchdog.check_once()["state"] == "active"

    transitions = [event["type"] for event in list_pacer_events(workspace)]
    assert transitions == ["launch_stalled", "launch_resumed"]
    assert len(warnings) == 1
    assert all(event["data"]["destructive_action"] is False for event in list_pacer_events(workspace))
    stopped = watchdog.stop(lifecycle_status="completed")
    assert stopped["monitoring"] is False
    assert stopped["lifecycle_status"] == "completed"
    assert read_launch_liveness(workspace, "launch-1")["state"] == "active"


def test_watchdog_thread_stops_cleanly_without_sleep_or_pid_probe(tmp_path) -> None:
    polled = threading.Event()

    class Tracker:
        def poll(self):
            polled.set()
            return {"status": "no_match", "activity_observed": False}

    watchdog = codex_launcher.LaunchLivenessWatchdog(
        workspace_root=tmp_path / ".agent-workspace",
        launch_id="launch-thread",
        tracker=Tracker(),
        mode="interactive",
        stall_timeout_seconds=60,
        poll_interval_seconds=0.01,
    ).start()

    assert polled.wait(timeout=1)
    watchdog.stop(lifecycle_status="interrupted")
    assert watchdog._thread is not None
    assert watchdog._thread.is_alive() is False
    assert read_launch_liveness(tmp_path / ".agent-workspace", "launch-thread")["monitoring"] is False


def test_interactive_watchdog_never_classifies_user_idle_as_stalled(tmp_path) -> None:
    clock = _FakeClock()
    watchdog = codex_launcher.LaunchLivenessWatchdog(
        workspace_root=tmp_path / ".agent-workspace",
        launch_id="launch-interactive",
        tracker=_FakeActivityTracker([{"status": "no_rollout", "activity_observed": False}]),
        mode="interactive",
        stall_timeout_seconds=1,
        monotonic=clock.monotonic,
        utcnow=clock.utcnow,
    ).start(background=False)
    clock.advance(3600)

    assert watchdog.check_once()["state"] == "idle"
    assert list_pacer_events(tmp_path / ".agent-workspace") == []
    watchdog.stop(lifecycle_status="completed")


def test_watchdog_observation_and_output_failures_are_unknown_not_stalled(tmp_path, monkeypatch) -> None:
    from visual_agent import pacer_events

    clock = _FakeClock()

    class BrokenTracker:
        def poll(self):
            raise PermissionError("rollout unavailable")

    monkeypatch.setattr(
        codex_launcher,
        "write_launch_liveness",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    monkeypatch.setattr(
        pacer_events,
        "append_pacer_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("event unavailable")),
    )

    def broken_warning(_message: str) -> None:
        raise OSError("terminal unavailable")

    watchdog = codex_launcher.LaunchLivenessWatchdog(
        workspace_root=tmp_path / ".agent-workspace",
        launch_id="launch-broken-io",
        tracker=BrokenTracker(),
        mode="exec",
        stall_timeout_seconds=1,
        monotonic=clock.monotonic,
        utcnow=clock.utcnow,
        warning_callback=broken_warning,
    ).start(background=False)
    clock.advance(2)

    degraded = watchdog.check_once()
    assert degraded["state"] == "unknown"
    assert degraded["observation_health"] == "degraded"
    assert degraded["inactivity_seconds"] == 0
    assert watchdog.stop(lifecycle_status="failed")["monitoring"] is False


def test_watchdog_excludes_unobservable_time_from_confirmed_inactivity(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    clock = _FakeClock()
    tracker = _FakeActivityTracker(
        [
            {"status": "captured", "activity_observed": False},
            {"status": "unavailable", "activity_observed": False, "observable": False},
            {"status": "captured", "activity_observed": False},
        ]
    )
    watchdog = codex_launcher.LaunchLivenessWatchdog(
        workspace_root=workspace,
        launch_id="launch-observation-gap",
        tracker=tracker,
        mode="exec",
        stall_timeout_seconds=10,
        monotonic=clock.monotonic,
        utcnow=clock.utcnow,
    ).start(background=False)

    clock.advance(5)
    assert watchdog.check_once()["inactivity_seconds"] == 5
    clock.advance(100)
    unknown = watchdog.check_once()
    assert unknown["state"] == "unknown"
    assert unknown["inactivity_seconds"] == 5
    assert list_pacer_events(workspace) == []
    clock.advance(4)
    recovered_observation = watchdog.check_once()
    assert recovered_observation["state"] == "idle"
    assert recovered_observation["inactivity_seconds"] == 9
    assert list_pacer_events(workspace) == []
    watchdog.stop(lifecycle_status="completed")


def test_degraded_observation_does_not_reopen_stalled_episode(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    clock = _FakeClock()
    tracker = _FakeActivityTracker(
        [
            {"status": "captured", "activity_observed": False},
            {"status": "unavailable", "activity_observed": False, "observable": False},
            {"status": "captured", "activity_observed": False},
            {"status": "captured", "activity_observed": True, "source_files": 1},
        ]
    )
    watchdog = codex_launcher.LaunchLivenessWatchdog(
        workspace_root=workspace,
        launch_id="launch-degraded-episode",
        tracker=tracker,
        mode="exec",
        stall_timeout_seconds=10,
        monotonic=clock.monotonic,
        utcnow=clock.utcnow,
    ).start(background=False)

    clock.advance(11)
    assert watchdog.check_once()["state"] == "stalled"
    clock.advance(100)
    assert watchdog.check_once()["state"] == "unknown"
    clock.advance(1)
    assert watchdog.check_once()["state"] == "stalled"
    assert [event["type"] for event in list_pacer_events(workspace)] == ["launch_stalled"]
    assert watchdog.check_once()["state"] == "active"
    assert [event["type"] for event in list_pacer_events(workspace)] == ["launch_stalled", "launch_resumed"]
    watchdog.stop(lifecycle_status="completed")


def test_stop_discards_late_poll_after_join_timeout(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    entered = threading.Event()
    release = threading.Event()

    class BlockingTracker:
        def poll(self):
            entered.set()
            assert release.wait(timeout=2)
            return {
                "status": "captured",
                "activity_observed": True,
                "observed_at": "2026-07-13T00:00:01+00:00",
                "source_files": 1,
            }

    watchdog = codex_launcher.LaunchLivenessWatchdog(
        workspace_root=workspace,
        launch_id="launch-late-poll",
        tracker=BlockingTracker(),
        mode="exec",
        stall_timeout_seconds=10,
        poll_interval_seconds=0.01,
        stop_join_timeout_seconds=0.01,
    ).start()
    assert entered.wait(timeout=1)

    stopped = watchdog.stop(lifecycle_status="completed")
    assert stopped["monitoring"] is False
    assert stopped["watchdog_thread_alive"] is True
    assert stopped["stop_join_timed_out"] is True
    assert list_pacer_events(workspace) == []

    release.set()
    assert watchdog._thread is not None
    watchdog._thread.join(timeout=1)
    assert watchdog._thread.is_alive() is False
    persisted = read_launch_liveness(workspace, "launch-late-poll")
    assert persisted["monitoring"] is False
    assert persisted["watchdog_thread_alive"] is False
    assert persisted["state"] == "idle"
    assert list_pacer_events(workspace) == []


def test_stall_timeout_environment_is_validated(monkeypatch) -> None:
    monkeypatch.setenv("PACER_STALL_TIMEOUT_SECONDS", "42")
    assert codex_launcher._effective_stall_timeout_seconds() == 42
    monkeypatch.setenv("PACER_STALL_TIMEOUT_SECONDS", "nan")
    assert codex_launcher._effective_stall_timeout_seconds() == codex_launcher.DEFAULT_STALL_TIMEOUT_SECONDS
