from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from .console import build_report_detail
from .models import to_jsonable
from .security import scrub_secrets
from .workspace import Workspace, load_workspace_report_index, open_workspace, run_workspace_workflow, workspace_report_access_payload
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
        audit_log: str | Path = "",
    ):
        super().__init__(server_address, CloudRunRequestHandler)
        self.workspace_root = Path(workspace_root).resolve()
        self.default_run_profile = default_run_profile
        self.api_key = str(api_key or "")
        self.required_org = str(required_org or "")
        self.audit_log = Path(audit_log).resolve() if audit_log else None
        self.runs: dict[str, dict[str, Any]] = {}


class CloudRunRequestHandler(BaseHTTPRequestHandler):
    server: CloudRunHTTPServer

    def do_GET(self) -> None:
        started_at = perf_counter()
        parsed = urlparse(self.path)
        if parsed.path == "/v1/health":
            self.write_json({"status": "ok", "workspace": str(self.server.workspace_root)})
            return
        if not self.require_authorized(started_at=started_at):
            return
        if parsed.path == "/v1/runs":
            query = parse_qs(parsed.query)
            payload = list_cloud_run_reports(
                self.server,
                limit=bounded_int(first_query_value(query, "limit"), default=20, minimum=1, maximum=100),
                offset=bounded_int(first_query_value(query, "offset"), default=0, minimum=0, maximum=100000),
                status=first_query_value(query, "status"),
                workflow=first_query_value(query, "workflow"),
                failed_only=parse_bool_query(first_query_value(query, "failed_only")),
            )
            self.write_json(payload)
            self.audit_request(
                "run_list",
                payload,
                started_at=started_at,
                query_keys=sorted(query),
            )
            return
        report_request = parse_report_download_path(parsed.path)
        if report_request is not None:
            run_id = report_request
            query = parse_qs(parsed.query)
            payload = cloud_run_report_file(self.server, run_id, fmt=first_query_value(query, "format") or "json")
            http_status = int(payload.get("http_status") or 200)
            if isinstance(payload.get("content"), bytes):
                self.write_content(
                    payload["content"],
                    content_type=str(payload.get("content_type") or "application/octet-stream"),
                    status=http_status,
                    filename=str(payload.get("filename") or ""),
                )
                self.audit_request(
                    "report_download",
                    payload,
                    started_at=started_at,
                    http_status=http_status,
                    run_id=run_id,
                    query_keys=sorted(query),
                    report_format=first_query_value(query, "format") or "json",
                )
                return
            self.write_json(payload, status=http_status)
            self.audit_request(
                "report_download",
                payload,
                started_at=started_at,
                http_status=http_status,
                run_id=run_id,
                query_keys=sorted(query),
                report_format=first_query_value(query, "format") or "json",
            )
            return
        if parsed.path.startswith("/v1/run/"):
            run_id = parsed.path.removeprefix("/v1/run/").strip("/")
            payload = cloud_run_report_detail(self.server, run_id)
            if payload.get("status") == "not_found":
                self.write_json({"status": "not_found", "run_id": run_id, "message": "Run id was not found."}, status=404)
                self.audit_request("run_detail", payload, started_at=started_at, http_status=404, run_id=run_id)
                return
            if payload.get("status") == "upgrade_required":
                self.write_json(payload, status=403)
                self.audit_request("run_detail", payload, started_at=started_at, http_status=403, run_id=run_id)
                return
            self.write_json(payload)
            self.audit_request("run_detail", payload, started_at=started_at, run_id=run_id)
            return
        self.write_json({"status": "not_found", "message": "Unknown endpoint."}, status=404)
        self.audit_request("unknown", {"status": "not_found"}, started_at=started_at, http_status=404)

    def do_POST(self) -> None:
        started_at = perf_counter()
        if self.path != "/v1/run":
            self.write_json({"status": "not_found", "message": "Unknown endpoint."}, status=404)
            self.audit_request("unknown", {"status": "not_found"}, started_at=started_at, http_status=404)
            return
        if not self.require_authorized(started_at=started_at):
            return
        body: dict[str, Any] = {}
        try:
            body = self.read_json()
            payload = execute_cloud_run_request(self.server, body)
        except Exception as exc:
            payload = {"status": "failed", "message": str(scrub_secrets(str(exc)))[:500]}
        http_status = 200 if payload.get("status") in {"success", "failed"} else 400
        self.write_json(payload, status=http_status)
        self.audit_request(
            "run_create",
            payload,
            started_at=started_at,
            http_status=http_status,
            workflow_name=str(body.get("workflow_name") or body.get("workflow") or payload.get("workflow_name") or ""),
            run_profile=str(body.get("run_profile") or payload.get("run_profile") or self.server.default_run_profile),
            workspace_provided=bool(body.get("workspace")),
            workflow_yaml_provided=bool(str(body.get("workflow_yaml") or "").strip()),
        )

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

    def write_content(self, data: bytes, *, content_type: str, status: int = 200, filename: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def require_authorized(self, *, started_at: float | None = None) -> bool:
        headers = {key.lower(): value for key, value in self.headers.items()}
        failure = cloud_server_auth_failure(
            headers=headers,
            api_key=self.server.api_key,
            required_org=self.server.required_org,
        )
        if failure is None:
            return True
        http_status = 401 if failure["reason"] == "unauthorized" else 403
        self.write_json(failure, status=http_status)
        self.audit_request(
            "auth",
            failure,
            started_at=started_at,
            http_status=http_status,
            org=str(headers.get("x-visual-agent-org") or ""),
        )
        return False

    def audit_request(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        started_at: float | None = None,
        http_status: int = 200,
        run_id: str = "",
        org: str = "",
        **extra: Any,
    ) -> None:
        parsed = urlparse(self.path)
        headers = {key.lower(): value for key, value in self.headers.items()}
        append_cloud_server_audit(
            self.server,
            {
                "method": self.command,
                "path": parsed.path,
                "query_keys": extra.pop("query_keys", sorted(parse_qs(parsed.query))),
                "endpoint": endpoint,
                "status": payload.get("status") or "unknown",
                "http_status": int(http_status),
                "reason": payload.get("reason") or "",
                "run_id": run_id or str(payload.get("run_id") or ""),
                "workflow_name": extra.pop("workflow_name", payload.get("workflow_name", "")),
                "run_profile": extra.pop("run_profile", payload.get("run_profile", "")),
                "org": org or str(headers.get("x-visual-agent-org") or ""),
                "duration_ms": round((perf_counter() - started_at) * 1000, 2) if started_at is not None else None,
                "remote_addr": self.client_address[0] if self.client_address else "",
                **extra,
            },
        )

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


def append_cloud_server_audit(server: CloudRunHTTPServer, event: dict[str, Any]) -> None:
    if server.audit_log is None:
        return
    audit_event = {
        "schema_version": 1,
        "ts": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    try:
        server.audit_log.parent.mkdir(parents=True, exist_ok=True)
        with server.audit_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(scrub_secrets(audit_event), ensure_ascii=False, default=str) + "\n")
    except OSError:
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


def list_cloud_run_reports(
    server: CloudRunHTTPServer,
    *,
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
    workflow: str | None = None,
    failed_only: bool = False,
) -> dict[str, Any]:
    workspace = open_workspace(server.workspace_root)
    index = load_workspace_report_index(
        workspace,
        rebuild=True,
        status=normalize_filter_value(status),
        workflow=normalize_filter_value(workflow),
        failed_only=bool(failed_only),
    )
    entries = index.get("entries") if isinstance(index.get("entries"), list) else []
    offset = max(0, int(offset))
    limit = max(1, int(limit))
    selected = entries[offset : offset + limit]
    next_offset = offset + len(selected)
    return {
        "schema_version": 1,
        "status": "success",
        "workspace": str(workspace.root),
        "total_reports": index.get("total_reports", len(entries)),
        "returned_reports": len(selected),
        "offset": offset,
        "limit": limit,
        "next_offset": next_offset if next_offset < len(entries) else None,
        "has_more": next_offset < len(entries),
        "filters": {
            "status": normalize_filter_value(status),
            "workflow": normalize_filter_value(workflow),
            "failed_only": bool(failed_only),
        },
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


def cloud_run_report_file(server: CloudRunHTTPServer, run_id: str, *, fmt: str = "json") -> dict[str, Any]:
    workspace = open_workspace(server.workspace_root)
    normalized_format = str(fmt or "json").strip().lower()
    if normalized_format not in {"json", "markdown"}:
        return {
            "schema_version": 1,
            "status": "failed",
            "reason": "unsupported_format",
            "message": "Report format must be json or markdown.",
            "http_status": 400,
        }
    suffix = ".json" if normalized_format == "json" else ".md"
    report_path = safe_report_file_path(workspace.reports_dir, run_id, suffix=suffix)
    if report_path is None or not report_path.exists():
        return {
            "schema_version": 1,
            "status": "not_found",
            "run_id": run_id,
            "message": "Run report file was not found.",
            "http_status": 404,
        }
    access = workspace_report_access_payload(workspace, report_path)
    if not access["allowed"]:
        return {
            "schema_version": 1,
            "status": "upgrade_required",
            "run_id": run_id,
            "history_access": scrub_secrets(access),
            "message": access.get("message"),
            "http_status": 403,
        }
    if normalized_format == "json":
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        content = json.dumps(scrub_secrets(payload), ensure_ascii=False, default=str, indent=2).encode("utf-8")
        content_type = "application/json; charset=utf-8"
    else:
        content = str(scrub_secrets(report_path.read_text(encoding="utf-8"))).encode("utf-8")
        content_type = "text/markdown; charset=utf-8"
    return {
        "schema_version": 1,
        "status": "success",
        "run_id": run_id,
        "format": normalized_format,
        "filename": report_path.name,
        "content_type": content_type,
        "content": content,
        "http_status": 200,
    }


def parse_report_download_path(path: str) -> str | None:
    parts = path.strip("/").split("/")
    if len(parts) == 4 and parts[0] == "v1" and parts[1] == "run" and parts[3] == "report":
        return parts[2]
    return None


def safe_report_file_path(reports_dir: Path, run_id: str, *, suffix: str) -> Path | None:
    filename = f"{run_id}{suffix}"
    if Path(filename).name != filename:
        return None
    root = reports_dir.resolve()
    path = (root / filename).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def first_query_value(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0] if values else None


def bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def parse_bool_query(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def normalize_filter_value(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


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
    audit_log: str | Path = "",
) -> CloudRunHTTPServer:
    return CloudRunHTTPServer(
        (host, int(port)),
        workspace_root,
        default_run_profile=run_profile,
        api_key=api_key,
        required_org=required_org,
        audit_log=audit_log,
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
    audit_log: str | Path = "",
) -> None:
    resolved_api_key = api_key or str(os.environ.get(api_key_env) or "")
    server = create_cloud_server(
        workspace_root=workspace_root,
        host=host,
        port=port,
        run_profile=run_profile,
        api_key=resolved_api_key,
        required_org=required_org,
        audit_log=audit_log,
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
                    "audit_log_enabled": bool(audit_log),
                    "audit_log": str(Path(audit_log).resolve()) if audit_log else "",
                }
            ),
            ensure_ascii=False,
        )
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
