from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import to_jsonable
from .security import scrub_secrets
from .workspace import Workspace, open_workspace, run_workspace_workflow
from .workflow import parse_workflow_file


class CloudRunHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], workspace_root: str | Path, default_run_profile: str = "dry-run"):
        super().__init__(server_address, CloudRunRequestHandler)
        self.workspace_root = Path(workspace_root).resolve()
        self.default_run_profile = default_run_profile
        self.runs: dict[str, dict[str, Any]] = {}


class CloudRunRequestHandler(BaseHTTPRequestHandler):
    server: CloudRunHTTPServer

    def do_GET(self) -> None:
        if self.path == "/v1/health":
            self.write_json({"status": "ok", "workspace": str(self.server.workspace_root)})
            return
        if self.path.startswith("/v1/run/"):
            run_id = self.path.removeprefix("/v1/run/").strip("/")
            payload = self.server.runs.get(run_id)
            if payload is None:
                self.write_json({"status": "not_found", "run_id": run_id, "message": "Run id was not found."}, status=404)
                return
            self.write_json(payload)
            return
        self.write_json({"status": "not_found", "message": "Unknown endpoint."}, status=404)

    def do_POST(self) -> None:
        if self.path != "/v1/run":
            self.write_json({"status": "not_found", "message": "Unknown endpoint."}, status=404)
            return
        try:
            body = self.read_json()
            payload = execute_cloud_run_request(self.server, body)
        except Exception as exc:
            payload = {"status": "failed", "message": str(scrub_secrets(str(exc)))[:500]}
        self.write_json(payload, status=200 if payload.get("status") in {"success", "failed"} else 400)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else "{}"
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def write_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        data = json.dumps(scrub_secrets(payload), ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def execute_cloud_run_request(server: CloudRunHTTPServer, request: dict[str, Any]) -> dict[str, Any]:
    workspace = open_workspace(request.get("workspace") or server.workspace_root)
    workflow_name = materialize_request_workflow(workspace, request)
    run_profile = str(request.get("run_profile") or server.default_run_profile or "dry-run")
    inputs = request.get("inputs")
    if not isinstance(inputs, dict) or "provided" in inputs:
        inputs = {}
    result = run_workspace_workflow(
        workspace,
        workflow_name,
        inputs=inputs,
        dry_run=run_profile == "dry-run",
        run_profile=run_profile,
        export_report=True,
    )
    failed_steps = [step for step in result.steps if getattr(step.status, "value", str(step.status)) == "failed"]
    status = "failed" if failed_steps else "success"
    payload = {
        "schema_version": 1,
        "status": status,
        "run_id": result.run_id,
        "workflow_name": result.workflow_name,
        "run_profile": result.run_profile,
        "report_url": f"/v1/run/{result.run_id}",
        "steps_passed": sum(1 for step in result.steps if getattr(step.status, "value", str(step.status)) in {"success", "dry_run"}),
        "steps_total": len(result.steps),
        "message": "Workflow completed." if status == "success" else "Workflow failed.",
        "failed_step": failed_steps[0].id if failed_steps else "",
    }
    server.runs[result.run_id] = payload
    return payload


def materialize_request_workflow(workspace: Workspace, request: dict[str, Any]) -> str:
    workflow_yaml = request.get("workflow_yaml")
    if isinstance(workflow_yaml, str) and workflow_yaml.strip():
        workflow_id = uuid4().hex[:8]
        path = workspace.workflows_dir / f"cloud_request_{workflow_id}.yaml"
        path.write_text(workflow_yaml.rstrip() + "\n", encoding="utf-8")
        return parse_workflow_file(path).name
    workflow_name = str(request.get("workflow_name") or request.get("workflow") or "").strip()
    if not workflow_name:
        raise ValueError("Request requires workflow_name or workflow_yaml.")
    return workflow_name


def create_cloud_server(
    *,
    workspace_root: str | Path,
    host: str = "127.0.0.1",
    port: int = 7890,
    run_profile: str = "dry-run",
) -> CloudRunHTTPServer:
    return CloudRunHTTPServer((host, int(port)), workspace_root, default_run_profile=run_profile)


def serve_cloud_server(
    *,
    workspace_root: str | Path,
    host: str = "127.0.0.1",
    port: int = 7890,
    run_profile: str = "dry-run",
) -> None:
    server = create_cloud_server(workspace_root=workspace_root, host=host, port=port, run_profile=run_profile)
    print(
        json.dumps(
            to_jsonable({"status": "listening", "host": host, "port": port, "workspace": str(Path(workspace_root).resolve())}),
            ensure_ascii=False,
        )
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
