from __future__ import annotations

import os
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .auth import bearer_token, generate_api_key, verify_api_key
from .context import CloudRequestContext, resolve_cloud_context
from .marketplace import (
    delete_catalog_workflow,
    get_catalog_workflow,
    list_catalog_workflows,
    publish_catalog_workflow,
    search_catalog_workflows,
    sync_workspace_public_workflows,
)
from .models import RunRequest, RunStatus, run_result_from_cloud_payload
from visual_agent.security import scrub_secrets
from .worker import execute_workflow_run


def create_app(*, workspace_root: str | Path = ".agent-workspace", audit_log: str | Path = ""):
    try:
        from fastapi import FastAPI, Header, HTTPException
    except ImportError as exc:
        raise RuntimeError("Install cloud API dependencies with `pip install -e .[cloud]`.") from exc

    app = FastAPI(title="Checkpoint Cloud API", version="0.1.0")
    runs_by_scope: dict[str, dict[str, dict[str, Any]]] = {}
    root = Path(workspace_root).resolve()
    audit_path = Path(audit_log).resolve() if audit_log else None
    default_org = str(os.environ.get("VISUAL_AGENT_CLOUD_ORG") or "").strip()
    default_user_id = str(os.environ.get("VISUAL_AGENT_CLOUD_USER") or "").strip()

    def require_auth(authorization: str = "") -> None:
        expected = os.environ.get("VISUAL_AGENT_CLOUD_API_KEY_SHA256", "")
        salt = os.environ.get("VISUAL_AGENT_CLOUD_API_KEY_SALT", "")
        if expected and not verify_api_key(bearer_token(authorization), expected_sha256=expected, salt=salt):
            raise HTTPException(status_code=401, detail="Missing or invalid bearer token.")

    def audit_event(event: dict[str, Any]) -> None:
        if audit_path is None:
            return
        payload = {
            "schema_version": 1,
            "ts": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        try:
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            with audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(scrub_secrets(payload), ensure_ascii=False, default=str) + "\n")
        except OSError:
            return

    def request_context(
        *,
        org: str = "",
        user_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> CloudRequestContext:
        headers: dict[str, str] = {}
        if org:
            headers["x-visual-agent-org"] = org
        if user_id:
            headers["x-visual-agent-user"] = user_id
        return resolve_cloud_context(
            headers=headers,
            payload=payload,
            default_org=default_org,
            default_user_id=default_user_id,
        )

    def scope_runs(context: CloudRequestContext) -> dict[str, dict[str, Any]]:
        return runs_by_scope.setdefault(context.scope_key, {})

    @app.get("/v1/health")
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        payload = {"status": "ok", "workspace": str(root)}
        audit_event({"method": "GET", "path": "/api/health", "status": "ok", "http_status": 200, "user_id": ""})
        return payload

    @app.post("/api/auth/token")
    def issue_token() -> dict[str, Any]:
        key = generate_api_key()
        payload = {
            "status": "success",
            "token": key.token,
            "salt": key.salt,
            "sha256": key.sha256,
            "message": "Store token securely and configure sha256/salt on the API service.",
        }
        audit_event({"method": "POST", "path": "/api/auth/token", "status": "success", "http_status": 200, "user_id": ""})
        return payload

    @app.get("/api/workflows")
    def list_workflows(
        x_visual_agent_org: str = Header(default="", alias="X-Visual-Agent-Org"),
        x_visual_agent_user: str = Header(default="", alias="X-Visual-Agent-User"),
        visibility: str = "all",
        category: str = "",
        tag: list[str] | None = None,
        limit: int = 50,
        cursor: str = "",
    ) -> dict[str, Any]:
        context = request_context(org=x_visual_agent_org, user_id=x_visual_agent_user)
        sync_workspace_public_workflows(root, org=context.org)
        items = list_catalog_workflows(root, org=context.org, visibility=visibility, category=category, tags=tag, limit=limit, cursor=cursor)
        payload = {"schema_version": 1, "workflows": items, "next_cursor": None}
        audit_event(
            {
                "method": "GET",
                "path": "/api/workflows",
                "status": "success",
                "http_status": 200,
                "org": context.org,
                "user_id": context.user_id,
            }
        )
        return payload

    @app.get("/api/workflows/search")
    def search_public_workflows(
        q: str,
        x_visual_agent_org: str = Header(default="", alias="X-Visual-Agent-Org"),
        x_visual_agent_user: str = Header(default="", alias="X-Visual-Agent-User"),
        visibility: str = "all",
        limit: int = 20,
    ) -> dict[str, Any]:
        context = request_context(org=x_visual_agent_org, user_id=x_visual_agent_user)
        sync_workspace_public_workflows(root, org=context.org)
        items = search_catalog_workflows(root, q, org=context.org, visibility=visibility, limit=max(0, min(int(limit), 50)))
        payload = {"schema_version": 1, "query": q, "workflows": items, "next_cursor": None}
        audit_event(
            {
                "method": "GET",
                "path": "/api/workflows/search",
                "status": "success",
                "http_status": 200,
                "org": context.org,
                "user_id": context.user_id,
            }
        )
        return payload

    @app.get("/api/workflows/{workflow_id}")
    def get_public_workflow(
        workflow_id: str,
        x_visual_agent_org: str = Header(default="", alias="X-Visual-Agent-Org"),
        x_visual_agent_user: str = Header(default="", alias="X-Visual-Agent-User"),
    ) -> dict[str, Any]:
        context = request_context(org=x_visual_agent_org, user_id=x_visual_agent_user)
        sync_workspace_public_workflows(root, org=context.org)
        workflow = get_catalog_workflow(root, workflow_id, org=context.org)
        if workflow is None:
            raise HTTPException(status_code=404, detail="Workflow id was not found.")
        payload = {"schema_version": 1, "workflow": workflow}
        audit_event(
            {
                "method": "GET",
                "path": f"/api/workflows/{workflow_id}",
                "status": "success",
                "http_status": 200,
                "org": context.org,
                "user_id": context.user_id,
            }
        )
        return payload

    @app.get("/api/workflows/{workflow_id}/download")
    def download_public_workflow(
        workflow_id: str,
        x_visual_agent_org: str = Header(default="", alias="X-Visual-Agent-Org"),
        x_visual_agent_user: str = Header(default="", alias="X-Visual-Agent-User"),
    ) -> dict[str, Any]:
        context = request_context(org=x_visual_agent_org, user_id=x_visual_agent_user)
        sync_workspace_public_workflows(root, org=context.org)
        workflow = get_catalog_workflow(root, workflow_id, org=context.org)
        if workflow is None:
            raise HTTPException(status_code=404, detail="Workflow id was not found.")
        payload = {
            "schema_version": 1,
            "workflow_id": str(workflow.get("id") or workflow_id),
            "name": str(workflow.get("name") or ""),
            "workflow_yaml": str(workflow.get("workflow_yaml") or ""),
        }
        audit_event(
            {
                "method": "GET",
                "path": f"/api/workflows/{workflow_id}/download",
                "status": "success",
                "http_status": 200,
                "org": context.org,
                "user_id": context.user_id,
            }
        )
        return payload

    @app.delete("/api/workflows/{workflow_id}")
    def delete_public_workflow(
        workflow_id: str,
        authorization: str = Header(default=""),
        x_visual_agent_org: str = Header(default="", alias="X-Visual-Agent-Org"),
        x_visual_agent_user: str = Header(default="", alias="X-Visual-Agent-User"),
    ) -> dict[str, Any]:
        require_auth(authorization)
        context = request_context(org=x_visual_agent_org, user_id=x_visual_agent_user)
        result = delete_catalog_workflow(root, workflow_id, org=context.org)
        status = 200 if result.get("status") == "deleted" else 404 if result.get("reason") == "workflow_not_found" else 400
        audit_event(
            {
                "method": "DELETE",
                "path": f"/api/workflows/{workflow_id}",
                "status": result.get("status") or "blocked",
                "http_status": status,
                "org": context.org,
                "user_id": context.user_id,
            }
        )
        if result.get("status") == "deleted":
            return {
                "status": "deleted",
                "workflow_id": str(result.get("workflow_id") or workflow_id),
                "workflow": result.get("workflow"),
            }
        raise HTTPException(status_code=status, detail="Workflow id was not found." if status == 404 else "Unable to delete workflow.")

    @app.post("/api/workflows/publish")
    def publish_public_workflow(
        payload: dict[str, Any],
        authorization: str = Header(default=""),
        x_visual_agent_org: str = Header(default="", alias="X-Visual-Agent-Org"),
        x_visual_agent_user: str = Header(default="", alias="X-Visual-Agent-User"),
    ) -> dict[str, Any]:
        require_auth(authorization)
        context = request_context(org=x_visual_agent_org, user_id=x_visual_agent_user, payload=payload)
        result = publish_catalog_workflow(
            root,
            payload,
            org=context.org,
            user_id=context.user_id,
            min_quality_score=float(payload.get("min_quality_score") or 0.6) if payload.get("min_quality_score") is not None else 0.6,
            catalog_url_base=str(payload.get("catalog_url_base") or "https://visualagent.local/workflows"),
        )
        status = 200 if result.get("status") == "published" else 400
        audit_event(
            {
                "method": "POST",
                "path": "/api/workflows/publish",
                "status": result.get("status") or "blocked",
                "http_status": status,
                "org": context.org,
                "user_id": context.user_id,
                "workflow_name": str(payload.get("name") or payload.get("workflow_name") or ""),
            }
        )
        if result.get("status") == "published":
            workflow = result.get("workflow") if isinstance(result.get("workflow"), dict) else {}
            return {
                "status": "published",
                "id": result.get("id"),
                "name": result.get("name"),
                "version": result.get("version"),
                "visibility": str(workflow.get("visibility") or ""),
                "org": context.org,
                "user_id": context.user_id,
                "quality_score": result.get("quality_score"),
                "url": result.get("url"),
            }
        return {
            "status": "blocked",
            "reason": result.get("reason"),
            "workflow": result.get("workflow"),
            "issues": result.get("issues"),
            "quality_score": result.get("quality_score"),
            "min_quality_score": result.get("min_quality_score"),
        }

    @app.post("/api/runs")
    def create_run(
        payload: dict[str, Any],
        authorization: str = Header(default=""),
        x_visual_agent_org: str = Header(default="", alias="X-Visual-Agent-Org"),
        x_visual_agent_user: str = Header(default="", alias="X-Visual-Agent-User"),
    ) -> dict[str, Any]:
        require_auth(authorization)
        context = request_context(org=x_visual_agent_org, user_id=x_visual_agent_user, payload=payload)
        request = RunRequest.from_payload({**payload, "org": context.org, "user_id": context.user_id})
        run_payload = execute_workflow_run(
            {
                **request.to_dict(),
                "workspace": request.workspace or str(root),
                "workflow_name": request.workflow_name,
                "org": context.org,
                "user_id": context.user_id,
            },
            workspace_root=root,
        )
        result = run_result_from_cloud_payload(run_payload)
        response = {
            **result.to_dict(),
            "schema_version": 1,
            "run_id": result.id,
            "artifact_url": f"/api/runs/{result.id}/artifacts",
            "org": context.org,
            "user_id": context.user_id,
        }
        scope_runs(context)[result.id] = response
        audit_event(
            {
                "method": "POST",
                "path": "/api/runs",
                "status": response.get("status") or "unknown",
                "http_status": 200,
                "run_id": result.id,
                "workflow_name": result.workflow_name,
                "workflow_source": request.workflow_source,
                "workflow_id": request.workflow_id,
                "org": context.org,
                "user_id": context.user_id,
                "workspace": str(request.workspace or root),
            }
        )
        return response

    @app.get("/api/runs/{run_id}")
    def get_run(
        run_id: str,
        authorization: str = Header(default=""),
        x_visual_agent_org: str = Header(default="", alias="X-Visual-Agent-Org"),
        x_visual_agent_user: str = Header(default="", alias="X-Visual-Agent-User"),
    ) -> dict[str, Any]:
        require_auth(authorization)
        context = request_context(org=x_visual_agent_org, user_id=x_visual_agent_user)
        payload = scope_runs(context).get(run_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="Run id was not found.")
        response = RunStatus(
            id=run_id,
            status=payload.get("status", "failed"),
            workflow_name=str(payload.get("workflow_name") or ""),
            workflow_source=str(payload.get("workflow_source") or "workspace"),
            workflow_id=str(payload.get("workflow_id") or ""),
            message=str(payload.get("message") or ""),
            report_url=str(payload.get("report_url") or ""),
            artifact_url=f"/api/runs/{run_id}/artifacts",
            org=str(payload.get("org") or context.org),
            user_id=str(payload.get("user_id") or context.user_id),
        ).to_dict()
        audit_event(
            {
                "method": "GET",
                "path": f"/api/runs/{run_id}",
                "status": response.get("status") or "unknown",
                "http_status": 200,
                "run_id": run_id,
                "workflow_name": response.get("workflow_name") or "",
                "workflow_source": response.get("workflow_source") or "",
                "workflow_id": response.get("workflow_id") or "",
                "org": context.org,
                "user_id": context.user_id,
            }
        )
        return response

    @app.get("/api/runs/{run_id}/artifacts")
    def get_artifacts(
        run_id: str,
        authorization: str = Header(default=""),
        x_visual_agent_org: str = Header(default="", alias="X-Visual-Agent-Org"),
        x_visual_agent_user: str = Header(default="", alias="X-Visual-Agent-User"),
    ) -> dict[str, Any]:
        require_auth(authorization)
        context = request_context(org=x_visual_agent_org, user_id=x_visual_agent_user)
        payload = scope_runs(context).get(run_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="Run id was not found.")
        artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
        response = {"schema_version": 1, "status": "success", "run_id": run_id, "artifacts": artifacts}
        audit_event(
            {
                "method": "GET",
                "path": f"/api/runs/{run_id}/artifacts",
                "status": "success",
                "http_status": 200,
                "run_id": run_id,
                "workflow_name": str(payload.get("workflow_name") or ""),
                "org": context.org,
                "user_id": context.user_id,
            }
        )
        return response

    return app


try:
    app = create_app(
        workspace_root=os.environ.get("VISUAL_AGENT_WORKSPACE_ROOT", ".agent-workspace"),
        audit_log=os.environ.get("VISUAL_AGENT_CLOUD_AUDIT_LOG", ""),
    )
except RuntimeError:
    app = None

