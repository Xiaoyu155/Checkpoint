from __future__ import annotations

import json
from threading import Thread
from urllib.request import Request, urlopen

from visual_agent.cloud import build_http_cloud_transport
from visual_agent.cloud_server import create_cloud_server
from visual_agent.workspace import init_workspace


def test_cloud_server_health_endpoint(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    server = create_cloud_server(workspace_root=workspace.root, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1/health"
        with urlopen(url, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert payload["status"] == "ok"
    assert payload["workspace"] == str(workspace.root)


def test_cloud_server_run_endpoint_executes_workspace_workflow(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    (workspace.fixtures_dir / "ready.html").write_text("<p>Ready</p>", encoding="utf-8")
    (workspace.workflows_dir / "ready.yaml").write_text(
        """
schema_version: 1
name: ready
version: 1
steps:
  - id: observe
    action: observe_html
    path: fixtures/ready.html
  - id: assert_ready
    action: assert_text
    text: Ready
""".strip(),
        encoding="utf-8",
    )
    server = create_cloud_server(workspace_root=workspace.root, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/run"
        body = json.dumps({"workflow_name": "ready", "workspace": str(workspace.root), "run_profile": "dry-run"}).encode("utf-8")
        request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        with urlopen(f"http://127.0.0.1:{server.server_port}/v1/run/{payload['run_id']}", timeout=5) as response:
            run_payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert payload["status"] == "success"
    assert payload["workflow_name"] == "ready"
    assert payload["steps_passed"] == 2
    assert payload["steps_total"] == 2
    assert run_payload["run_id"] == payload["run_id"]


def test_http_cloud_transport_can_call_local_cloud_server(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    (workspace.fixtures_dir / "ready.html").write_text("<p>Ready</p>", encoding="utf-8")
    (workspace.workflows_dir / "ready.yaml").write_text(
        """
schema_version: 1
name: ready
version: 1
steps:
  - id: observe
    action: observe_html
    path: fixtures/ready.html
  - id: assert_ready
    action: assert_text
    text: Ready
""".strip(),
        encoding="utf-8",
    )
    server = create_cloud_server(workspace_root=workspace.root, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        transport = build_http_cloud_transport(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1/run",
            api_key="local-test-key",
            timeout_seconds=10,
        )
        payload = transport({"workflow_name": "ready", "workspace": str(workspace.root), "run_profile": "dry-run"})
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert payload["status"] == "success"
    assert payload["run_id"]
    assert payload["report_url"].startswith("/v1/run/")
