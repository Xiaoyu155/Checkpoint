from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from visual_agent.pacer_launch_context import initialize_active_launch, read_active_launch
from visual_agent.subprocess_window import hidden_subprocess_kwargs


_CHILD_UPDATE = r"""
import sys
import time
from pathlib import Path

import visual_agent.pacer_launch_context as launch_context

workspace = Path(sys.argv[1])
field = sys.argv[2]
value = sys.argv[3]
attempted = Path(sys.argv[4])
ready = Path(sys.argv[5])
release = Path(sys.argv[6])
target = launch_context.launch_context_path(workspace, "launch-1").resolve()
original_read_json = launch_context._read_json
armed = True


def controlled_read(path: Path) -> dict:
    global armed
    payload = original_read_json(path)
    if armed and Path(path).resolve() == target:
        armed = False
        ready.write_text("ready", encoding="utf-8")
        deadline = time.monotonic() + 5.0
        while not release.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("parent did not release child update")
            time.sleep(0.01)
    return payload


attempted.write_text("attempted", encoding="utf-8")
launch_context._read_json = controlled_read
launch_context.update_active_launch(
    workspace,
    expected_launch_id="launch-1",
    **{field: value},
)
"""


def _wait_for(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path.name}")
        time.sleep(0.01)


def test_cross_process_updates_are_serialized_without_lost_fields(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    workspace = root / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-1.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={
            "launch_id": "launch-1",
            "repo_root": str(root),
            "runtime": {"python": {"executable": sys.executable, "available": True}},
        },
    )

    markers = tmp_path / "markers"
    markers.mkdir()
    first_attempted = markers / "first-attempted"
    first_ready = markers / "first-ready"
    first_release = markers / "first-release"
    second_attempted = markers / "second-attempted"
    second_ready = markers / "second-ready"
    second_release = markers / "second-release"
    env = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (source_root, env.get("PYTHONPATH", "")) if item
    )

    def start(field: str, value: str, attempted: Path, ready: Path, release: Path) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CHILD_UPDATE,
                str(workspace),
                field,
                value,
                str(attempted),
                str(ready),
                str(release),
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **hidden_subprocess_kwargs(),
        )

    first: subprocess.Popen[str] | None = None
    second: subprocess.Popen[str] | None = None
    outputs: list[tuple[int, str, str]] = []
    try:
        first = start("first_process_field", "first", first_attempted, first_ready, first_release)
        _wait_for(first_ready)
        second = start("second_process_field", "second", second_attempted, second_ready, second_release)
        _wait_for(second_attempted)

        time.sleep(0.1)
        assert not second_ready.exists(), "the second process entered the transaction before the first released it"

        first_release.write_text("release", encoding="utf-8")
        _wait_for(second_ready)
        second_release.write_text("release", encoding="utf-8")
    finally:
        first_release.write_text("release", encoding="utf-8")
        second_release.write_text("release", encoding="utf-8")
        for process in (first, second):
            if process is not None:
                stdout, stderr = process.communicate(timeout=10)
                outputs.append((int(process.returncode or 0), stdout, stderr))

    assert outputs == [(0, "", ""), (0, "", "")]
    active = read_active_launch(workspace, launch_id="launch-1")
    assert active["first_process_field"] == "first"
    assert active["second_process_field"] == "second"
