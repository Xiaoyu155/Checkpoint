from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .env import env_get, env_present
from .licensing import FeatureGatedError, get_license, monthly_feature_limit, require_feature
from .security import scrub_secrets

CloudWorkflowClient = Callable[[str, Path], dict[str, Any]]
CloudTransport = Callable[[dict[str, Any]], dict[str, Any]]
HttpOpener = Callable[[Request, float], Any]
MarketplaceTransport = Callable[[str], dict[str, Any]]


def cloud_config_status() -> dict[str, Any]:
    endpoint = str(env_get("VISUAL_AGENT_CLOUD_ENDPOINT") or "").strip()
    api_key_present = env_present("VISUAL_AGENT_CLOUD_API_KEY")
    org = str(env_get("VISUAL_AGENT_CLOUD_ORG") or "").strip()
    user_id = str(env_get("VISUAL_AGENT_CLOUD_USER") or "").strip()
    blockers: list[str] = []
    if not endpoint:
        blockers.append("missing_endpoint")
    if not api_key_present:
        blockers.append("missing_api_key")
    return {
        "schema_version": 1,
        "available": not blockers,
        "endpoint": endpoint,
        "api_key_present": api_key_present,
        "org": org,
        "user_id": user_id,
        "blockers": blockers,
        "network_probe": "not_run",
    }


def build_remote_workflow_request(
    workflow_name: str,
    workspace_root: Path,
    *,
    run_profile: str = "dry-run",
    inputs: dict[str, Any] | None = None,
    inputs_file: str | None = None,
    workflow_yaml: str = "",
    workflow_source: str = "workspace",
    workflow_id: str = "",
) -> dict[str, Any]:
    config = cloud_config_status()
    workspace_root = Path(workspace_root)
    return {
        "schema_version": 1,
        "status": "ready" if config["available"] else "blocked",
        "workflow_name": str(workflow_name),
        "workspace": str(workspace_root),
        "run_profile": str(run_profile),
        "cloud_config": config,
        "inputs": summarize_remote_inputs(inputs),
        "inputs_file": str(inputs_file or ""),
        "workflow_yaml_provided": bool(str(workflow_yaml or "").strip()),
        "workflow_yaml": str(workflow_yaml or ""),
        "workflow_source": str(workflow_source or "workspace"),
        "workflow_id": str(workflow_id or ""),
        "network_probe": "not_run",
    }


def request_ready_for_injected_transport(request: dict[str, Any]) -> dict[str, Any]:
    """Mark a request ready when the caller supplied an explicit transport."""
    updated = dict(request)
    config = dict(updated.get("cloud_config") or {})
    config.update(
        {
            "available": True,
            "endpoint": config.get("endpoint") or "<injected-transport>",
            "api_key_present": True,
            "blockers": [],
        }
    )
    updated["status"] = "ready"
    updated["cloud_config"] = config
    return updated


def summarize_remote_inputs(inputs: dict[str, Any] | None) -> dict[str, Any]:
    if not inputs:
        return {"provided": False, "field_count": 0, "fields": []}
    cleaned = scrub_secrets(inputs)
    return {
        "provided": True,
        "field_count": len(inputs),
        "fields": sorted(str(key) for key in inputs),
        "redacted": cleaned,
    }


def filter_remote_workflow_response(response: dict[str, Any]) -> dict[str, Any]:
    status = str(response.get("status") or "unknown")
    if status not in {"success", "failed", "queued", "running", "blocked", "upgrade_required", "unknown"}:
        status = "unknown"
    run_id = str(response.get("run_id") or response.get("id") or "")
    payload = {
        "schema_version": 1,
        "remote_schema_version": str(response.get("schema_version") or ""),
        "status": status,
        "run_id": run_id,
        "report_url": str(response.get("report_url") or ""),
        "message": str(scrub_secrets(response.get("message") or ""))[:500],
    }
    if response.get("workflow_source"):
        payload["workflow_source"] = str(response.get("workflow_source") or "")
    if response.get("workflow_id"):
        payload["workflow_id"] = str(response.get("workflow_id") or "")
    if response.get("artifact_url"):
        payload["artifact_url"] = str(response.get("artifact_url") or "")
    return payload


def build_http_cloud_transport(
    *,
    endpoint: str,
    api_key: str,
    org: str = "",
    user_id: str = "",
    timeout_seconds: float = 30.0,
    max_retries: int = 0,
    retry_backoff_seconds: float = 0.0,
    opener: HttpOpener | None = None,
) -> CloudTransport:
    opener = opener or (lambda request, timeout: urlopen(request, timeout=timeout))
    endpoint = endpoint.strip()
    api_key = api_key.strip()
    org = org.strip()
    user_id = user_id.strip()

    def transport(request: dict[str, Any]) -> dict[str, Any]:
        import json

        body = json.dumps(request).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "visual-agent-cloud-run/1",
        }
        if org:
            headers["X-Visual-Agent-Org"] = org
        if user_id:
            headers["X-Visual-Agent-User"] = user_id
        attempts = max(0, int(max_retries)) + 1
        last_result: dict[str, Any] | None = None
        for attempt in range(attempts):
            http_request = Request(endpoint, data=body, headers=headers, method="POST")
            try:
                with opener(http_request, float(timeout_seconds)) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                    result = parse_http_cloud_response(raw, status_code=getattr(response, "status", 200))
            except HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                result = parse_http_cloud_error(exc.code, raw)
            result["attempts"] = attempt + 1
            last_result = result
            if not should_retry_http_result(result) or attempt == attempts - 1:
                return result
            if retry_backoff_seconds > 0:
                time.sleep(float(retry_backoff_seconds) * (2**attempt))
        return last_result or {"status": "failed", "message": "Cloud workflow request did not complete.", "attempts": attempts}

    return transport


def parse_http_cloud_response(raw: str, *, status_code: int = 200) -> dict[str, Any]:
    import json

    if status_code >= 400:
        return parse_http_cloud_error(status_code, raw)
    if not raw.strip():
        return {"status": "unknown", "message": "Cloud workflow response was empty."}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"status": "failed", "message": "Cloud workflow response was not valid JSON."}
    if not isinstance(parsed, dict):
        return {"status": "failed", "message": "Cloud workflow response was not a JSON object."}
    return parsed


def parse_http_cloud_error(status_code: int, raw: str) -> dict[str, Any]:
    status = "blocked" if status_code in {401, 403} else "failed"
    message = f"Cloud workflow HTTP {status_code}."
    body = str(scrub_secrets(raw or "")).strip()
    if body:
        message = f"{message} {body[:300]}"
    return {"status": status, "message": message, "http_status": status_code}


def should_retry_http_result(result: dict[str, Any]) -> bool:
    status_code = int(result.get("http_status") or 0)
    return status_code == 429 or status_code >= 500


def http_cloud_transport_from_env(
    *,
    timeout_seconds: float = 30.0,
    max_retries: int = 0,
    retry_backoff_seconds: float = 0.0,
    opener: HttpOpener | None = None,
) -> CloudTransport | None:
    endpoint = str(env_get("VISUAL_AGENT_CLOUD_ENDPOINT") or "").strip()
    api_key = str(env_get("VISUAL_AGENT_CLOUD_API_KEY") or "").strip()
    org = str(env_get("VISUAL_AGENT_CLOUD_ORG") or "").strip()
    user_id = str(env_get("VISUAL_AGENT_CLOUD_USER") or "").strip()
    if not endpoint or not api_key:
        return None
    return build_http_cloud_transport(
        endpoint=endpoint,
        api_key=api_key,
        org=org,
        user_id=user_id,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
        opener=opener,
    )


def marketplace_config_status() -> dict[str, Any]:
    endpoint = str(env_get("VISUAL_AGENT_CLOUD_MARKETPLACE_ENDPOINT") or "").strip()
    api_key_present = env_present("VISUAL_AGENT_CLOUD_MARKETPLACE_API_KEY")
    org = str(env_get("VISUAL_AGENT_CLOUD_MARKETPLACE_ORG") or "").strip()
    user_id = str(env_get("VISUAL_AGENT_CLOUD_MARKETPLACE_USER") or "").strip()
    blockers: list[str] = []
    if not endpoint:
        blockers.append("missing_endpoint")
    return {
        "schema_version": 1,
        "available": not blockers,
        "endpoint": endpoint,
        "api_key_present": api_key_present,
        "org": org,
        "user_id": user_id,
        "blockers": blockers,
        "network_probe": "not_run",
    }


def build_http_marketplace_transport(
    *,
    endpoint: str,
    api_key: str = "",
    org: str = "",
    user_id: str = "",
    timeout_seconds: float = 30.0,
    opener: HttpOpener | None = None,
) -> MarketplaceTransport:
    opener = opener or (lambda request, timeout: urlopen(request, timeout=timeout))
    endpoint = endpoint.rstrip("/")
    api_key = api_key.strip()
    org = org.strip()
    user_id = user_id.strip()

    def transport(path: str) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "visual-agent-cloud-marketplace/1",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if org:
            headers["X-Visual-Agent-Org"] = org
        if user_id:
            headers["X-Visual-Agent-User"] = user_id
        request = Request(f"{endpoint}{path}", headers=headers, method="GET")
        try:
            with opener(request, float(timeout_seconds)) as response:
                raw = response.read().decode("utf-8", errors="replace")
                payload = parse_http_cloud_response(raw, status_code=getattr(response, "status", 200))
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            payload = parse_http_cloud_error(exc.code, raw)
        payload["http_status"] = int(payload.get("http_status") or 200)
        return payload

    return transport


def http_marketplace_transport_from_env(
    *,
    timeout_seconds: float = 30.0,
    opener: HttpOpener | None = None,
) -> MarketplaceTransport | None:
    endpoint = str(env_get("VISUAL_AGENT_CLOUD_MARKETPLACE_ENDPOINT") or "").strip()
    if not endpoint:
        return None
    return build_http_marketplace_transport(
        endpoint=endpoint,
        api_key=str(env_get("VISUAL_AGENT_CLOUD_MARKETPLACE_API_KEY") or "").strip(),
        org=str(env_get("VISUAL_AGENT_CLOUD_MARKETPLACE_ORG") or "").strip(),
        user_id=str(env_get("VISUAL_AGENT_CLOUD_MARKETPLACE_USER") or "").strip(),
        timeout_seconds=timeout_seconds,
        opener=opener,
    )


def fetch_marketplace_workflow(
    workflow_id: str,
    *,
    transport: MarketplaceTransport | None = None,
) -> dict[str, Any]:
    transport = transport or http_marketplace_transport_from_env()
    if transport is None:
        return {"status": "blocked", "message": "Marketplace client transport is not configured."}
    workflow_id = str(workflow_id or "").strip()
    if not workflow_id:
        return {"status": "blocked", "message": "Workflow id is required."}
    payload = transport(f"/api/workflows/{quote(workflow_id, safe='')}")
    workflow = payload.get("workflow") if isinstance(payload.get("workflow"), dict) else {}
    if not workflow:
        workflow = {
            "id": str(payload.get("workflow_id") or workflow_id),
            "name": str(payload.get("name") or workflow_id),
            "workflow_yaml": str(payload.get("workflow_yaml") or ""),
        }
    if not str(workflow.get("workflow_yaml") or "").strip() and not str(payload.get("workflow_yaml") or "").strip():
        if payload.get("status") not in {"unknown", "failed"}:
            return payload
    return {
        "schema_version": 1,
        "status": "success" if isinstance(workflow, dict) and workflow else "failed",
        "workflow_id": workflow_id,
        "workflow": workflow,
        "transport_response": payload,
    }


def save_marketplace_workflow(
    workspace_root: Path,
    workflow_id: str,
    *,
    transport: MarketplaceTransport | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    from .workspace import init_workspace, open_workspace

    workspace_path = Path(workspace_root)
    workspace = open_workspace(workspace_path) if workspace_path.exists() else init_workspace(workspace_path, with_demo=False, overwrite=False)
    result = fetch_marketplace_workflow(workflow_id, transport=transport)
    if result.get("status") != "success":
        return result
    workflow = result.get("workflow") if isinstance(result.get("workflow"), dict) else {}
    workflow_yaml = str(workflow.get("workflow_yaml") or "")
    if not workflow_yaml.strip():
        return {"status": "failed", "message": "Marketplace workflow payload did not include workflow_yaml."}
    name = str(workflow.get("name") or workflow_id).strip() or workflow_id
    path = workspace.workflows_dir / f"{name}.yaml"
    if path.exists() and not overwrite:
        return {
            "status": "blocked",
            "reason": "workflow_exists",
            "path": str(path),
            "workflow_id": workflow_id,
            "workflow_name": name,
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(workflow_yaml.rstrip() + "\n", encoding="utf-8")
    return {
        "status": "success",
        "workflow_id": workflow_id,
        "workflow_name": name,
        "path": str(path),
        "workflow": workflow,
    }


def remote_client_from_env(
    *,
    transport: CloudTransport | None = None,
    allow_injected_transport_without_config: bool = False,
    run_profile: str = "dry-run",
    inputs: dict[str, Any] | None = None,
    inputs_file: str | None = None,
    workflow_yaml: str = "",
    workflow_source: str = "workspace",
    workflow_id: str = "",
) -> CloudWorkflowClient:
    def client(workflow_name: str, workspace_root: Path) -> dict[str, Any]:
        request = build_remote_workflow_request(
            workflow_name,
            workspace_root,
            run_profile=run_profile,
            inputs=inputs,
            inputs_file=inputs_file,
            workflow_yaml=workflow_yaml,
            workflow_source=workflow_source,
            workflow_id=workflow_id,
        )
        if allow_injected_transport_without_config and transport is not None and request["status"] != "ready":
            request = request_ready_for_injected_transport(request)
        if request["status"] != "ready":
            return {
                "status": "blocked",
                "message": "Cloud workflow execution is not configured.",
                "request": request,
            }
        if transport is None:
            return {
                "status": "blocked",
                "message": "Cloud workflow client transport is not enabled.",
                "request": request,
            }
        try:
            response = transport(request)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            return {
                "schema_version": 1,
                "status": "failed",
                "message": str(scrub_secrets(str(exc)))[:500],
                "request": request,
            }
        return {**filter_remote_workflow_response(response), "request": request}

    return client


def execute_remote_workflow_plan(
    workflow_name: str,
    workspace_root: Path,
    *,
    run_profile: str = "dry-run",
    inputs: dict[str, Any] | None = None,
    inputs_file: str | None = None,
    workflow_yaml: str = "",
    workflow_source: str = "workspace",
    workflow_id: str = "",
    execute: bool = False,
    transport: CloudTransport | None = None,
) -> dict[str, Any]:
    workspace_root = Path(workspace_root)
    request = build_remote_workflow_request(
        workflow_name,
        workspace_root,
        run_profile=run_profile,
        inputs=inputs,
        inputs_file=inputs_file,
        workflow_yaml=workflow_yaml,
        workflow_source=workflow_source,
        workflow_id=workflow_id,
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "workspace": str(workspace_root),
        "workflow_name": str(workflow_name),
        "execution_requested": bool(execute),
        "network_sent": False,
        "request": request,
    }
    if execute and transport is not None and request["status"] != "ready":
        request = request_ready_for_injected_transport(request)
        payload["request"] = request
    if not execute:
        payload["adapter_diagnostic"] = remote_client_from_env(
            run_profile=run_profile,
            inputs=inputs,
            inputs_file=inputs_file,
            workflow_yaml=workflow_yaml,
            workflow_source=workflow_source,
            workflow_id=workflow_id,
        )(workflow_name, workspace_root)
        return payload

    client = remote_client_from_env(
        transport=transport,
        allow_injected_transport_without_config=bool(transport is not None),
        run_profile=run_profile,
        inputs=inputs,
        inputs_file=inputs_file,
        workflow_yaml=workflow_yaml,
        workflow_source=workflow_source,
        workflow_id=workflow_id,
    )
    result = run_remote_workflow(workflow_name, workspace_root, client=client)
    payload["result"] = result
    payload["network_sent"] = bool(
        transport is not None
        and request.get("status") == "ready"
        and result.get("status") not in {"upgrade_required", "quota_exceeded"}
    )
    return payload


def run_remote_workflow(
    workflow_name: str,
    workspace_root: Path,
    *,
    client: CloudWorkflowClient | None = None,
) -> dict[str, Any]:
    """
    Execute a cloud workflow through an injected or configured client.

    Feature gating happens before any remote transport is called, so free-tier
    callers get a structured upgrade response without network traffic.
    """
    workspace_root = Path(workspace_root)
    try:
        require_feature("cloud_run")
    except FeatureGatedError as exc:
        return {
            "schema_version": 1,
            "status": "upgrade_required",
            "feature": exc.feature,
            "required_tier": exc.required_tier,
            "current_tier": exc.current_tier,
            "message": str(scrub_secrets(str(exc)))[:500],
            "workflow_name": workflow_name,
            "workspace": str(workspace_root),
            "usage_recorded": False,
        }
    quota_status = cloud_run_quota_status(workspace_root)
    if not quota_status["allowed"]:
        return {
            "schema_version": 1,
            "status": "upgrade_required",
            "feature": "cloud_run",
            "reason": "quota_exceeded",
            "required_tier": "pro",
            "current_tier": quota_status["tier"],
            "message": (
                f"Cloud run monthly quota exceeded ({quota_status['used']}/{quota_status['limit']}). "
                "Upgrade to pro for unlimited cloud runs."
            ),
            "quota": quota_status,
            "workflow_name": workflow_name,
            "workspace": str(workspace_root),
            "usage_recorded": False,
        }
    if client is not None:
        result = dict(client(workflow_name, workspace_root))
        result.setdefault("schema_version", 1)
        result.setdefault("workflow_name", workflow_name)
        result.setdefault("workspace", str(workspace_root))
        if result.get("status") == "success":
            from .session import record_cloud_run_usage

            record_cloud_run_usage(workspace_root)
            result["usage_recorded"] = True
        else:
            result["usage_recorded"] = False
        return result
    raise NotImplementedError(
        "Cloud runs are not yet available. "
        f"Workflow '{workflow_name}' can still be run locally from {workspace_root}."
    )


def cloud_run_quota_status(workspace_root: Path) -> dict[str, Any]:
    license_ = get_license()
    limit = monthly_feature_limit("cloud_run", license_)
    from .session import load_agent_session

    session = load_agent_session(Path(workspace_root))
    current_month = datetime.now().strftime("%Y-%m")
    used = int(session.cloud_runs_used if session and session.usage_reset_date == current_month else 0)
    remaining = None if limit is None else max(0, int(limit) - used)
    return {
        "feature": "cloud_run",
        "tier": license_.tier,
        "limit": limit,
        "used": used,
        "remaining": remaining,
        "allowed": limit is None or used < int(limit),
        "reset_month": current_month,
    }
