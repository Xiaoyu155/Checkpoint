from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from visual_agent.dashboard import _bind_dashboard_server, _launch_snapshot

from .helpers import run_cli


pytestmark = [pytest.mark.e2e]


def test_mission_http_pipeline_end_to_end(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is required for this test")

    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = repo / ".agent-workspace"

    init_result = run_cli("init", "--root", str(workspace), "--overwrite", "--no-demo", cwd=repo)
    assert init_result.returncode == 0, init_result.stdout + init_result.stderr

    workflow = workspace / "workflows" / "checkout.yaml"
    workflow.write_text(
        "schema_version: 1\n"
        "name: checkout\n"
        "version: 1\n"
        "affects:\n"
        "  - src/payment/\n"
        "tags:\n"
        "  - verification\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_ocr\n"
        "    mock_text: ready\n",
        encoding="utf-8",
    )

    target = repo / "src" / "payment" / "checkout.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def total():\n    return 100\n", encoding="utf-8")
    git(repo, "init")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "add", "src/payment/checkout.py")
    git(repo, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
    target.write_text("def total():\n    return 128\n", encoding="utf-8")

    server = _bind_dashboard_server("127.0.0.1", 0, workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        base_url = f"http://127.0.0.1:{port}"

        invalid = post_json(
            f"{base_url}/api/mission/start",
            {"goal": "Fix checkout total display"},
            expect_status=400,
        )
        assert invalid["error_code"] == "spec_validation_failed"

        payload = {
            "repo_root": str(repo),
            "goal": "Fix checkout total display",
            "test_command": "",
            "agent": "codex",
            "execute": False,
            "merge_policy": "manual",
            "spec": {
                "schema_version": 1,
                "goal": "Fix checkout total display",
                "scope": [{"repo_root": str(repo), "agent": "codex", "mode": "preview"}],
                "plan": ["Fix checkout total display"],
                "test": ["auto-detect verification command"],
                "risk": ["Preview-only mission created through the dashboard HTTP API."],
                "rollback": ["Reopen the mission from state.json if it needs another pass."],
            },
        }
        start = post_json(f"{base_url}/api/mission/start", payload)
        assert start["ok"] is True
        assert Path(start["state_path"]).exists()

        launch = wait_for_launch(start["launch_id"])
        assert launch["state"] == "done"
        assert launch["status"] in {"preview", "stopped", "blocked"}
        mission_id = launch["mission_id"]
        assert mission_id

        state_path = wait_for_path(workspace / "missions" / mission_id / "state.json")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["mission_id"] == mission_id
        assert state["current_state"] == "REVIEW"
        assert state["context"]["spec"]["plan"] == ["Fix checkout total display"]
        assert state["context"]["history"][-1]["event"] == "chief_run_finished"

        detail = get_json(f"{base_url}/api/mission?id={mission_id}")
        assert detail["mission"]["mission_id"] == mission_id
        assert detail["mission"]["status"] in {"preview", "stopped", "blocked"}
        assert detail["rounds"]

        time.sleep(2.2)
        data = get_json(f"{base_url}/api/data")
        assert any(item["mission_id"] == mission_id for item in data["missions"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def wait_for_launch(launch_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        matches = [item for item in _launch_snapshot() if item.get("launch_id") == launch_id]
        if matches and matches[0].get("state") in {"done", "error"}:
            return matches[0]
        time.sleep(0.2)
    raise AssertionError("mission launch did not finish in time")


def wait_for_path(path: Path, timeout: float = 20.0) -> Path:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return path
        time.sleep(0.2)
    raise AssertionError(f"expected path to exist: {path}")


def post_json(url: str, payload: dict[str, object], *, expect_status: int = 200) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8-sig")
            assert response.status == expect_status
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8-sig"))
        assert exc.code == expect_status
        return body


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
