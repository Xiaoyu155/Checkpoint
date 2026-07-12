from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .env import env_get
from .models import to_jsonable


CLOUD_COMMANDS = {"cloud-run-plan", "cloud-run", "cloud-pull-workflow", "cloud-server"}


def handle_cloud_command(args: Any) -> int:
    if args.command == "cloud-run-plan":
        return handle_cloud_run_plan(args)
    if args.command == "cloud-run":
        return handle_cloud_run(args)
    if args.command == "cloud-pull-workflow":
        return handle_cloud_pull_workflow(args)
    if args.command == "cloud-server":
        return handle_cloud_server(args)
    raise ValueError(f"Unsupported cloud command: {args.command}")


def handle_cloud_run_plan(args: Any) -> int:
    from .cloud import build_remote_workflow_request, fetch_marketplace_workflow, remote_client_from_env

    workspace_root = Path(args.workspace_root).resolve()
    workflow_yaml = ""
    workflow_name = args.workflow
    workflow_source = "workspace"
    if getattr(args, "workflow_id", None):
        workflow_source = "marketplace"
        transport = build_marketplace_transport(args)
        marketplace = fetch_marketplace_workflow(args.workflow_id, transport=transport)
        if marketplace.get("status") != "success":
            payload = {
                "schema_version": 1,
                "workspace": str(workspace_root),
                "workflow_name": workflow_name,
                "workflow_id": args.workflow_id,
                "workflow_source": workflow_source,
                "request": {"status": "blocked", "message": marketplace.get("message") or "Marketplace lookup failed."},
            }
            print_payload(payload, args.format, markdown=cloud_run_plan_to_markdown)
            return 1
        workflow = marketplace.get("workflow") if isinstance(marketplace.get("workflow"), dict) else {}
        workflow_yaml = str(workflow.get("workflow_yaml") or "")
        workflow_name = str(workflow.get("name") or workflow_name)
    request = build_remote_workflow_request(
        workflow_name,
        workspace_root,
        run_profile=args.run_profile,
        inputs=None,
        inputs_file=args.inputs_file,
        workflow_yaml=workflow_yaml,
        workflow_source=workflow_source,
        workflow_id=getattr(args, "workflow_id", None) or "",
    )
    diagnostic = remote_client_from_env(
        run_profile=args.run_profile,
        inputs_file=args.inputs_file,
        workflow_yaml=workflow_yaml,
        workflow_source=workflow_source,
        workflow_id=getattr(args, "workflow_id", None) or "",
    )(
        workflow_name, workspace_root
    )
    payload = {
        "schema_version": 1,
        "workspace": str(workspace_root),
        "workflow_name": workflow_name,
        "workflow_id": getattr(args, "workflow_id", None) or "",
        "workflow_source": workflow_source,
        "request": request,
        "adapter_diagnostic": diagnostic,
    }
    print_payload(payload, args.format, markdown=cloud_run_plan_to_markdown)
    return 0


def handle_cloud_run(args: Any) -> int:
    from .cloud import execute_remote_workflow_plan, fetch_marketplace_workflow, http_cloud_transport_from_env

    transport = None
    workflow_yaml = ""
    workflow_name = args.workflow
    workflow_source = "workspace"
    if getattr(args, "workflow_id", None):
        workflow_source = "marketplace"
        marketplace = fetch_marketplace_workflow(args.workflow_id, transport=build_marketplace_transport(args))
        if marketplace.get("status") != "success":
            payload = {
                "schema_version": 1,
                "workspace": str(Path(args.workspace_root).resolve()),
                "workflow_name": workflow_name,
                "workflow_id": args.workflow_id,
                "workflow_source": workflow_source,
                "execution_requested": bool(args.execute),
                "network_sent": False,
                "request": {"status": "blocked", "message": marketplace.get("message") or "Marketplace lookup failed."},
                "result": {"status": "failed", "message": marketplace.get("message") or "Marketplace lookup failed."},
            }
            print_payload(payload, args.format, markdown=cloud_run_to_markdown)
            return 1
        workflow = marketplace.get("workflow") if isinstance(marketplace.get("workflow"), dict) else {}
        workflow_yaml = str(workflow.get("workflow_yaml") or "")
        workflow_name = str(workflow.get("name") or workflow_name)
    if args.execute and args.transport == "http":
        transport = http_cloud_transport_from_env(
            timeout_seconds=args.timeout_seconds,
            max_retries=args.max_retries,
            retry_backoff_seconds=args.retry_backoff_seconds,
        )
    payload = execute_remote_workflow_plan(
        workflow_name,
        Path(args.workspace_root).resolve(),
        run_profile=args.run_profile,
        inputs=None,
        inputs_file=args.inputs_file,
        workflow_yaml=workflow_yaml,
        workflow_source=workflow_source,
        workflow_id=getattr(args, "workflow_id", None) or "",
        execute=args.execute,
        transport=transport,
    )
    payload["workflow_id"] = getattr(args, "workflow_id", None) or ""
    payload["workflow_source"] = workflow_source
    payload["transport"] = args.transport
    if args.execute and isinstance(payload.get("result"), dict):
        try:
            from .visual_status import append_cloud_run_history

            append_cloud_run_history(Path(args.workspace_root).resolve(), payload["result"])
        except Exception:
            pass
    print_payload(payload, args.format, markdown=cloud_run_to_markdown)
    return 0


def handle_cloud_pull_workflow(args: Any) -> int:
    from .cloud import save_marketplace_workflow

    result = save_marketplace_workflow(
        Path(args.workspace_root).resolve(),
        args.workflow_id,
        transport=build_marketplace_transport(args),
        overwrite=args.overwrite,
    )
    if args.format == "markdown":
        if result.get("status") == "success":
            print(f"Downloaded `{result.get('workflow_name')}` to `{result.get('path')}`.")
        else:
            print(f"{result.get('status')}: {result.get('message') or result.get('reason') or 'download failed'}")
    else:
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "success" else 1


def handle_cloud_server(args: Any) -> int:
    from .cloud_server import serve_cloud_server

    serve_cloud_server(
        workspace_root=args.workspace_root,
        host=args.host,
        port=args.port,
        run_profile=args.run_profile,
        api_key=args.api_key,
        api_key_env=args.api_key_env,
        required_org=args.required_org,
        audit_log=args.audit_log,
        retention_max_reports=args.retention_max_reports,
        retention_days=args.retention_days,
    )
    return 0


def build_marketplace_transport(args: Any) -> Any:
    if not getattr(args, "marketplace_endpoint", ""):
        return None
    from .cloud import build_http_marketplace_transport

    return build_http_marketplace_transport(
        endpoint=args.marketplace_endpoint,
        api_key=str(args.marketplace_api_key or ""),
        org=str(args.marketplace_org or ""),
        user_id=str(env_get("VISUAL_AGENT_CLOUD_MARKETPLACE_USER") or ""),
    )


def print_payload(payload: dict[str, Any], fmt: str, *, markdown: Any) -> None:
    if fmt == "markdown":
        print(markdown(payload))
    else:
        print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))


def cloud_run_plan_to_markdown(payload: dict[str, Any]) -> str:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    config = request.get("cloud_config") if isinstance(request.get("cloud_config"), dict) else {}
    diagnostic = payload.get("adapter_diagnostic") if isinstance(payload.get("adapter_diagnostic"), dict) else {}
    lines = [
        "# Cloud Run Plan",
        "",
        f"- Workspace: `{payload.get('workspace')}`",
        f"- Workflow: `{payload.get('workflow_name')}`",
        f"- Workflow source: `{payload.get('workflow_source') or request.get('workflow_source') or 'workspace'}`",
        f"- Workflow id: `{payload.get('workflow_id') or request.get('workflow_id') or ''}`",
        f"- Request status: `{request.get('status') or 'blocked'}`",
        f"- Run profile: `{request.get('run_profile') or ''}`",
        f"- Inputs file: `{request.get('inputs_file') or ''}`",
        f"- Network probe: `{request.get('network_probe') or 'not_run'}`",
        "",
        "## Cloud Config",
        f"- Ready: `{bool(config.get('available', False))}`",
        f"- Endpoint: `{config.get('endpoint') or ''}`",
        f"- API key present: `{bool(config.get('api_key_present', False))}`",
        f"- Org: `{config.get('org') or ''}`",
    ]
    blockers = config.get("blockers") if isinstance(config.get("blockers"), list) else []
    if blockers:
        lines.append(f"- Blockers: {', '.join(str(item) for item in blockers)}")
    lines.extend(
        [
            "",
            "## Adapter Diagnostic",
            f"- Status: `{diagnostic.get('status') or 'blocked'}`",
            f"- Message: {diagnostic.get('message') or ''}",
        ]
    )
    return "\n".join(lines)


def cloud_run_to_markdown(payload: dict[str, Any]) -> str:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    config = request.get("cloud_config") if isinstance(request.get("cloud_config"), dict) else {}
    diagnostic = payload.get("adapter_diagnostic") if isinstance(payload.get("adapter_diagnostic"), dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    lines = [
        "# Cloud Run",
        "",
        f"- Workspace: `{payload.get('workspace')}`",
        f"- Workflow: `{payload.get('workflow_name')}`",
        f"- Workflow source: `{payload.get('workflow_source') or request.get('workflow_source') or 'workspace'}`",
        f"- Workflow id: `{payload.get('workflow_id') or request.get('workflow_id') or ''}`",
        f"- Execution requested: `{bool(payload.get('execution_requested', False))}`",
        f"- Transport: `{payload.get('transport') or 'none'}`",
        f"- Network sent: `{bool(payload.get('network_sent', False))}`",
        f"- Request status: `{request.get('status') or 'blocked'}`",
        f"- Run profile: `{request.get('run_profile') or ''}`",
        f"- Inputs file: `{request.get('inputs_file') or ''}`",
        "",
        "## Cloud Config",
        f"- Ready: `{bool(config.get('available', False))}`",
        f"- Endpoint: `{config.get('endpoint') or ''}`",
        f"- API key present: `{bool(config.get('api_key_present', False))}`",
        f"- Org: `{config.get('org') or ''}`",
    ]
    blockers = config.get("blockers") if isinstance(config.get("blockers"), list) else []
    if blockers:
        lines.append(f"- Blockers: {', '.join(str(item) for item in blockers)}")
    if diagnostic:
        lines.extend(
            [
                "",
                "## Adapter Diagnostic",
                f"- Status: `{diagnostic.get('status') or 'blocked'}`",
                f"- Message: {diagnostic.get('message') or ''}",
            ]
        )
    if result:
        lines.extend(
            [
                "",
                "## Execution Result",
                f"- Status: `{result.get('status') or 'blocked'}`",
                f"- Run id: `{result.get('run_id') or ''}`",
                f"- Workflow source: `{result.get('workflow_source') or payload.get('workflow_source') or request.get('workflow_source') or 'workspace'}`",
                f"- Workflow id: `{result.get('workflow_id') or payload.get('workflow_id') or request.get('workflow_id') or ''}`",
                f"- Report URL: `{result.get('report_url') or ''}`",
                f"- Usage recorded: `{bool(result.get('usage_recorded', False))}`",
                f"- Message: {result.get('message') or ''}",
            ]
        )
    return "\n".join(lines)
