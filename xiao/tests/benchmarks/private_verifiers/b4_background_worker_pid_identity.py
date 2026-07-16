from __future__ import annotations

import sys
from pathlib import Path


def _project_root(target_root: Path) -> Path:
    for candidate in (target_root, target_root / "xiao"):
        if (candidate / "src" / "visual_agent" / "chief_background.py").is_file():
            return candidate
    raise AssertionError(f"Could not locate the xiao project under {target_root}")


def _identity(token: str, create_time: float) -> dict[str, object]:
    return {
        "token": token,
        "pid": 4242,
        "create_time": create_time,
        "started_at": f"2026-07-10T00:00:0{int(create_time) % 10}+00:00",
        "executable": f"C:/tools/{token}/python.exe",
        "command_line": ["python", "-m", "visual_agent.cli", "chief-background-worker"],
    }


def _record(identity: dict[str, object] | None, *, pid: int = 4242) -> dict[str, object]:
    record: dict[str, object] = {
        "status": "running",
        "pid": pid,
        "worker_pid": pid,
        "started_at": "2026-07-10T00:00:01+00:00",
    }
    if identity is not None:
        record.update(
            {
                "identity": identity,
                "process_identity": identity,
                "worker_identity": identity,
                "pid_identity": identity,
                "process_create_time": identity["create_time"],
                "worker_create_time": identity["create_time"],
                "process_started_at": identity["started_at"],
                "worker_started_at": identity["started_at"],
                "process_executable": identity["executable"],
                "worker_executable": identity["executable"],
            }
        )
    return record


def _probe(identity: dict[str, object], *, alive: bool = True) -> dict[str, object]:
    return {
        "pid": 4242,
        "alive": alive,
        "exit_code": None,
        "identity": identity,
        "process_identity": identity,
        "worker_identity": identity,
        "create_time": identity["create_time"],
        "started_at": identity["started_at"],
        "executable": identity["executable"],
        "command_line": identity["command_line"],
    }


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: b4_background_worker_pid_identity.py <target-root>", file=sys.stderr)
        return 2

    project = _project_root(Path(argv[0]).expanduser().resolve())
    sys.path.insert(0, str(project / "src"))

    from visual_agent import chief_background

    expected = _identity("mission-worker", 1.0)
    recycled = _identity("unrelated-process", 2.0)

    def inspect(record: dict[str, object], process: dict[str, object]) -> dict[str, object]:
        chief_background.load_mission = lambda *_args, **_kwargs: {"budget_policy": {"max_wall_minutes": 0}}
        chief_background.load_background_record = lambda *_args, **_kwargs: dict(record)
        return chief_background.inspect_background_state(
            workspace_root=project / ".benchmark-private-workspace",
            mission_id="b4-private",
            update=False,
            process_probe=lambda _pid: dict(process),
        )

    matching = inspect(_record(expected), _probe(expected))
    assert matching.get("alive") is True and matching.get("process_state") == "running", (
        "a live process with matching persisted identity was not reported as running"
    )

    mismatched = inspect(_record(expected), _probe(recycled))
    assert mismatched.get("alive") is not True and mismatched.get("process_state") != "running", (
        "a recycled PID with mismatched identity was reported as the mission worker"
    )

    missing = inspect(_record(expected, pid=0), {"pid": 0, "alive": False, "exit_code": None})
    assert missing.get("alive") is not True and missing.get("process_state") != "running", (
        "a missing PID was reported as running"
    )

    legacy = inspect(_record(None), _probe(expected))
    assert legacy.get("alive") is not True and legacy.get("process_state") != "running", (
        "a legacy record without identity metadata was trusted as a live worker"
    )

    print("B4_PRIVATE_VERIFIER_OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except AssertionError as exc:
        print(f"B4_PRIVATE_VERIFIER_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
