from __future__ import annotations

import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from .console import build_report_detail
from .models import to_jsonable
from .security import scrub_secrets
from .workspace import Workspace, load_workspace_report_index, open_workspace, run_workspace_workflow
from .workflow import parse_workflow_file


class CloudRunHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        workspace_root: str | Path,
        default_run_profile: str = "dry-run",
        *,
        api_key: str = "",
        required_org: str = "",
    ):
        super().__init__(server_address, CloudRunRequestHandler)
        self.workspace_root = Path(workspace_root).resolve()
        self.default_run_profile = default_run_profile
        self.api_key = str(api_key or "")
        self.required_org = str(required_org or "")
        self.runs: dict[str, dict[str, Any]] = {}


class CloudRunRequestHandler(BaseHTTPRequestHandler):
    server: CloudRunHTTPServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/v1/health":
            self.write_json({"status": "ok", "workspace": str(self.server.workspace_root)})
            return
        if not self.require_authorized():
            return
        if parsed.path == "/v1/runs":
            query = parse_qs(parsed.query)
            payload = list_cloud_run_reports(self.server, limit=bounded_int(first_query_value(query, "limit"), default=20, minimum=1, maximum=100))
            self.write_json(payload)
            return
        if parsed.path.startswith("/v1/run/"):
            run_id = parsed.path.removeprefix("/v1/run/").strip("/")
            payload = cloud_run_report_detail(self.server, run_id)
            if payload.get("status") == "not_found":
                self.write_json({"status": "not_found", "run_id": run_id, "message": "Run id was not found."}, status=404)
                return
            if payload.get("status") == "upgrade_required":
                self.write_json(payload, status=403)
                return
            self.write_json(payload)
            return
        self.write_json({"status": "not_found", "message": "Unknown endpoint."}, status=404)

    def do_POST(self) -> None:
        if self.path != "/v1/run":
            self.write_json({"status": "not_found", "message": "Unknown endpoint."}, status=404)
            return
        if not self.require_authorized():
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
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def require_authorized(self) -> bool:
        failure = cloud_server_auth_failure(
            headers={key.lower(): value for key, value in self.headers.items()},
            api_key=self.server.api_key,
            required_org=self.server.required_org,
        )
        if failure is None:
            return True
        self.write_json(failure, status=401 if failure["reason"] == "unauthorized" else 403)
        return False

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def cloud_server_auth_failure(
    *,
    headers: dict[str, str],
    api_key: str = "",
    required_org: str = "",
) -> dict[str, Any] | None:
    if api_key:
        auth = str(headers.get("authorization") or "")
        prefix = "Bearer "
        token = auth[len(prefix) :].strip() if auth.startswith(prefix) else ""
        if not token or not secrets.compare_digest(token, api_key):
            return {
                "schema_version": 1,
                "status": "unauthorized",
                "reason": "unauthorized",
                "message": "Missing or invalid bearer token.",
            }
    if required_org:
        org = str(headers.get("x-visual-agent-org") or "")
        if org != required_org:
            return {
                "schema_version": 1,
                "status": "forbidden",
                "reason": "org_forbidden",
                "message": "Request org is not allowed.",
            }
    return None


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


def list_cloud_run_reports(server: CloudRunHTTPServer, *, limit: int = 20) -> dict[str, Any]:
    workspace = open_workspace(server.workspace_root)
    index = load_workspace_report_index(workspace, rebuild=True)
    entries = index.get("entries") if isinstance(index.get("entries"), list) else []
    selected = entries[:limit]
    return {
        "schema_version": 1,
        "status": "success",
        "workspace": str(workspace.root),
        "total_reports": index.get("total_reports", len(entries)),
        "returned_reports": len(selected),
        "history_access": index.get("history_access") if isinstance(index.get("history_access"), dict) else {},
        "reports": selected,
    }


def cloud_run_report_detail(server: CloudRunHTTPServer, run_id: str) -> dict[str, Any]:
    workspace = open_workspace(server.workspace_root)
    summary = server.runs.get(run_id)
    try:
        detail = build_report_detail(workspace, run_id)
    except FileNotFoundError:
        return summary or {"schema_version": 1, "status": "not_found", "run_id": run_id}
    if detail.get("status") == "upgrade_required":
        return detail
    return {
        "schema_version": 1,
        "status": detail.get("status") or (summary or {}).get("status") or "unknown",
        "run_id": detail.get("run_id") or run_id,
        "workflow_name": detail.get("workflow_name") or (summary or {}).get("workflow_name") or "",
        "run_profile": detail.get("run_profile") or (summary or {}).get("run_profile") or "",
        "report": detail,
        "summary": summary or {},
    }


def first_query_value(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0] if values else None


def bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


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
    api_key: str = "",
    required_org: str = "",
) -> CloudRunHTTPServer:
    return CloudRunHTTPServer(
        (host, int(port)),
        workspace_root,
        default_run_profile=run_profile,
        api_key=api_key,
        required_org=required_org,
    )


def serve_cloud_server(
    *,
    workspace_root: str | Path,
    host: str = "127.0.0.1",
    port: int = 7890,
    run_profile: str = "dry-run",
    api_key: str = "",
    api_key_env: str = "VISUAL_AGENT_CLOUD_SERVER_API_KEY",
    required_org: str = "",
) -> None:
    resolved_api_key = api_key or str(os.environ.get(api_key_env) or "")
    server = create_cloud_server(
        workspace_root=workspace_root,
        host=host,
        port=port,
        run_profile=run_profile,
        api_key=resolved_api_key,
        required_org=required_org,
    )
    print(
        json.dumps(
            to_jsonable(
                {
                    "status": "listening",
                    "host": host,
                    "port": port,
                    "workspace": str(Path(workspace_root).resolve()),
                    "auth_required": bool(resolved_api_key),
                    "org_required": bool(required_org),
                }
            ),
            ensure_ascii=False,
        )
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
