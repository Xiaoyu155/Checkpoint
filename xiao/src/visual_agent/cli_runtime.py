from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .auth_state import auth_state_probe_to_markdown, build_auth_state_import_plan, import_auth_state, inspect_storage_state, probe_storage_state
from .model_credentials import (
    build_model_api_probe_plan,
    inspect_model_credentials,
    model_api_probe_plan_to_markdown,
    model_api_probe_result_to_markdown,
    model_credentials_to_markdown,
    run_model_api_probe,
)
from .models import to_jsonable
from .preflight import inspect_environment


RUNTIME_COMMANDS = {
    "env-check",
    "real-acceptance-readiness",
    "auth-state-plan",
    "auth-state-import",
    "auth-state-inspect",
    "auth-state-probe",
    "model-credentials-inspect",
    "model-api-probe-plan",
    "context-snapshot",
    "show-status",
    "stats",
    "export-runs",
    "usage-status",
    "usage",
    "usage-timeline",
    "journey",
    "worktrees",
    "activate",
    "save-task-context",
    "summarize-latest-failure",
}


def handle_runtime_command(args: Any) -> int:
    if args.command == "env-check":
        environment = inspect_environment(
            Path(args.workspace_root).resolve(),
            host=args.host,
            port=args.port,
            dist_dir=args.dist_dir,
            max_age_minutes=args.max_age_minutes,
        )
        from .visual_status import write_environment_status_file

        write_environment_status_file(Path(args.workspace_root).resolve(), environment)
        if args.format == "markdown":
            print(environment_to_markdown(environment))
        else:
            print(json.dumps(to_jsonable(environment), ensure_ascii=False, indent=2))
        return 0 if environment.get("ok") else 1
    if args.command == "real-acceptance-readiness":
        from .real_acceptance import (
            build_real_acceptance_readiness,
            real_acceptance_readiness_to_jsonable,
            real_acceptance_readiness_to_markdown,
        )

        payload = build_real_acceptance_readiness(workspace_root=args.workspace_root)
        if args.format == "markdown":
            print(real_acceptance_readiness_to_markdown(payload))
        else:
            print(json.dumps(real_acceptance_readiness_to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0 if payload.get("ready") else 1
    if args.command == "auth-state-plan":
        result = build_auth_state_import_plan(
            args.source,
            name=args.name,
            workspace_root=args.workspace_root,
            overwrite=args.overwrite,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["source_exists"] and result["safe_target"] and (args.overwrite or not result["target_exists"]) else 1
    if args.command == "auth-state-import":
        result = import_auth_state(
            args.source,
            name=args.name,
            workspace_root=args.workspace_root,
            overwrite=args.overwrite,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "auth-state-inspect":
        result = inspect_storage_state(args.path)
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "auth-state-probe":
        result = probe_storage_state(
            args.path,
            url=args.url,
            allowed_domain=args.allowed_domain,
            headed=args.headed,
            timeout_ms=args.timeout_ms,
        )
        if args.format == "markdown":
            print(auth_state_probe_to_markdown(result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "ready" else 1
    if args.command == "model-credentials-inspect":
        result = inspect_model_credentials(source=args.source, preferred_provider=args.preferred)
        if args.format == "markdown":
            print(model_credentials_to_markdown(result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["preferred_available"] else 1
    if args.command == "model-api-probe-plan":
        if args.run:
            result = run_model_api_probe(
                source=args.source,
                preferred_provider=args.preferred,
                base_url=args.base_url,
                endpoint=args.endpoint,
                model=args.model,
                timeout_seconds=args.timeout_seconds,
                max_completion_tokens=args.max_completion_tokens,
            )
        else:
            result = build_model_api_probe_plan(
                source=args.source,
                preferred_provider=args.preferred,
                base_url=args.base_url,
                endpoint=args.endpoint,
                model=args.model,
            )
        if args.format == "markdown":
            print(model_api_probe_result_to_markdown(result) if args.run else model_api_probe_plan_to_markdown(result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("ready") and (not args.run or result.get("status") == "success") else 1
    if args.command == "context-snapshot":
        from .session import workspace_session_snapshot_text

        workspace_root = Path(args.workspace_root).resolve()
        text = workspace_session_snapshot_text(workspace_root)
        if args.format == "markdown":
            print(text)
        else:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "workspace": str(workspace_root),
                        "format": "text",
                        "snapshot": text,
                        "token_estimate": len(text) // 4,
                        "within_budget": len(text) <= 2000,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0
    if args.command == "show-status":
        from .visual_status import read_status_file, visual_status_to_dict

        workspace_root = Path(args.workspace_root).resolve()
        project_root = workspace_root.parent
        status_path = project_root / ".visual-agent-status.md"
        status = read_status_file(project_root)
        if args.format == "json":
            print(json.dumps(visual_status_to_dict(status), ensure_ascii=False, indent=2))
        else:
            if status_path.exists():
                print(status_path.read_text(encoding="utf-8"))
            else:
                print(f"No .visual-agent-status.md found at {status_path}")
        return 0 if status is not None else 1
    if args.command == "stats":
        from .visual_status import local_stats

        payload = local_stats(Path(args.workspace_root).resolve())
        if args.format == "json":
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        else:
            print(stats_to_markdown(payload))
        return 0
    if args.command == "export-runs":
        from .visual_status import export_run_history

        output = export_run_history(Path(args.workspace_root).resolve(), Path(args.output).resolve(), fmt=args.format)
        print(json.dumps({"status": "success", "output": str(output), "format": args.format}, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"usage-status", "usage"}:
        from .cloud import build_remote_workflow_request, cloud_config_status, cloud_run_quota_status
        from .licensing import check_feature, get_license
        from .session import load_agent_session

        workspace_root = Path(args.workspace_root).resolve()
        session = load_agent_session(workspace_root)
        license_ = get_license()
        payload = {
            "schema_version": 1,
            "workspace": str(workspace_root),
            "license": {
                "tier": license_.tier,
                "seats": license_.seats,
                "expires_at": license_.expires_at,
                "source": license_.source,
                "key_present": license_.key_present,
            },
            "usage": {
                "runs_this_month": session.runs_this_month if session else 0,
                "cloud_runs_used": session.cloud_runs_used if session else 0,
                "usage_reset_date": session.usage_reset_date if session else "",
                "cloud_run_quota": cloud_run_quota_status(workspace_root),
            },
            "feature_access": {
                "cloud_run": check_feature("cloud_run"),
                "team_workspace": check_feature("team_workspace"),
                "workflow_history_unlimited": check_feature("workflow_history_unlimited"),
            },
            "cloud_config": cloud_config_status(),
            "remote_request_preview": build_remote_workflow_request(
                "example_workflow",
                workspace_root,
                run_profile="dry-run",
            ),
        }
        if args.format == "markdown":
            print(usage_status_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0
    if args.command == "usage-timeline":
        from .usage_timeline import collect_usage_timeline, discover_workspace_roots, usage_timeline_to_markdown

        roots: list[Any]
        if args.workspace_root:
            roots = [Path(item).expanduser().resolve() for item in args.workspace_root]
        else:
            roots = list(discover_workspace_roots(args.base or Path.cwd()))
        payload = collect_usage_timeline(roots, days=args.days, limit=args.limit)
        if args.format == "markdown":
            print(usage_timeline_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0
    if args.command == "worktrees":
        from .worktree_gc import reap_worktrees, worktree_report_to_markdown

        payload = reap_worktrees(
            Path(args.repo_root).expanduser().resolve(),
            keep_days=args.keep_days,
            keep_last=args.keep_last,
            dry_run=not args.reap,
        )
        if args.format == "markdown":
            print(worktree_report_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 1 if payload.get("failed") else 0
    if args.command == "journey":
        from .mission_journey import build_latest_mission_journey, build_mission_journey, mission_journey_report

        workspace_root = Path(args.workspace_root).expanduser().resolve()
        if args.mission_id:
            journey = build_mission_journey(workspace_root=workspace_root, mission_id=args.mission_id)
        else:
            journey = build_latest_mission_journey(workspace_root)
        if args.format == "markdown":
            print(mission_journey_report(journey))
        else:
            print(json.dumps(to_jsonable(journey), ensure_ascii=False, indent=2))
        return 0 if journey.get("phases") else 1
    if args.command == "activate":
        from .licensing import activate_license, default_license_path

        license_path = Path(args.license_file).expanduser().resolve() if args.license_file else default_license_path()
        license_ = activate_license(args.key, tier=args.tier, seats=args.seats, path=license_path)
        payload = {
            "schema_version": 1,
            "status": "success",
            "license_file": str(license_path),
            "license": {
                "tier": license_.tier,
                "seats": license_.seats,
                "source": license_.source,
                "key_present": license_.key_present,
            },
            "message": "License written locally.",
        }
        if args.format == "markdown":
            print(activate_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0
    if args.command == "save-task-context":
        from .session import save_task_context, session_to_snapshot_text

        session = save_task_context(
            Path(args.workspace_root).resolve(),
            task=args.task,
            analyzed_files=[str(item) for item in args.files],
            root_cause=args.root_cause,
            plan=args.plan,
            tried=[str(item) for item in args.tried],
        )
        snapshot = session_to_snapshot_text(session)
        if args.format == "markdown":
            print(snapshot)
        else:
            print(
                json.dumps(
                    {
                        "status": "saved",
                        "task": session.ai_task_context.task if session.ai_task_context else "",
                        "snapshot": snapshot,
                        "token_estimate": len(snapshot) // 4,
                        "within_budget": len(snapshot) <= 2000,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0
    if args.command == "summarize-latest-failure":
        from .failure_summary import build_failure_summary

        payload = build_failure_summary(Path(args.workspace_root).resolve())
        if args.format == "markdown":
            if payload.get("status") == "no_failure":
                print(payload["message"])
            else:
                print(payload.get("suggested_next_prompt", ""))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0
    raise ValueError(f"Unsupported runtime command: {args.command}")


def usage_status_to_markdown(payload: dict[str, Any]) -> str:
    license_ = payload.get("license") if isinstance(payload.get("license"), dict) else {}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    feature_access = payload.get("feature_access") if isinstance(payload.get("feature_access"), dict) else {}
    cloud_config = payload.get("cloud_config") if isinstance(payload.get("cloud_config"), dict) else {}
    lines = [
        "# Checkpoint Usage",
        "",
        f"- Workspace: `{payload.get('workspace')}`",
        f"- License tier: `{license_.get('tier') or 'free'}`",
        f"- License source: `{license_.get('source') or 'default'}`",
        f"- License key present: `{bool(license_.get('key_present', False))}`",
        f"- Local runs this month: `{usage.get('runs_this_month', 0)}`",
        f"- Cloud runs used: `{usage.get('cloud_runs_used', 0)}`",
        f"- Reset month: `{usage.get('usage_reset_date') or ''}`",
        "",
        "## Cloud Config",
        f"- Ready: `{bool(cloud_config.get('available', False))}`",
        f"- Endpoint: `{cloud_config.get('endpoint') or ''}`",
        f"- API key present: `{bool(cloud_config.get('api_key_present', False))}`",
        f"- Org: `{cloud_config.get('org') or ''}`",
        f"- Network probe: `{cloud_config.get('network_probe') or 'not_run'}`",
    ]
    quota = usage.get("cloud_run_quota") if isinstance(usage.get("cloud_run_quota"), dict) else {}
    if quota:
        limit = "unlimited" if quota.get("limit") is None else quota.get("limit")
        remaining = "unlimited" if quota.get("remaining") is None else quota.get("remaining")
        lines.extend([f"- Cloud run limit: `{limit}`", f"- Cloud run remaining: `{remaining}`"])
    blockers = cloud_config.get("blockers") if isinstance(cloud_config.get("blockers"), list) else []
    if blockers:
        lines.append(f"- Blockers: {', '.join(str(item) for item in blockers)}")
    lines.extend(["", "## Feature Access"])
    for name in ("cloud_run", "team_workspace", "workflow_history_unlimited"):
        lines.append(f"- {name}: `{bool(feature_access.get(name, False))}`")
    return "\n".join(lines)


def environment_to_markdown(payload: dict[str, Any]) -> str:
    port_check = payload.get("port_check") if isinstance(payload.get("port_check"), dict) else {}
    build_checks = payload.get("build_checks") if isinstance(payload.get("build_checks"), list) else []
    lines = [
        "# Checkpoint Environment",
        "",
        f"- Project root: `{payload.get('project_root')}`",
        f"- Project type: `{payload.get('project_type') or 'unknown'}`",
        f"- Status: `{payload.get('status') or ('OK' if payload.get('ok') else 'WARN')}`",
        f"- Host: `{payload.get('host') or ''}`",
        f"- Port: `{payload.get('port') or ''}`",
        "",
        "## Port",
        f"- OK: `{bool(port_check.get('ok', True))}`",
        f"- Message: {port_check.get('message') or ''}",
        f"- Suggestion: {port_check.get('suggestion') or ''}",
        "",
        "## Builds",
    ]
    if build_checks:
        for check in build_checks:
            if isinstance(check, dict):
                lines.append(f"- `{check.get('path')}` `{check.get('status')}` age={check.get('age_minutes')} {check.get('message') or ''}")
    else:
        lines.append("- No build outputs checked.")
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    recommendations = payload.get("recommendations") if isinstance(payload.get("recommendations"), list) else []
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
    if recommendations:
        lines.extend(["", "## Recommendations", ""])
        lines.extend(f"- {item}" for item in recommendations)
    return "\n".join(lines).rstrip() + "\n"


def activate_to_markdown(payload: dict[str, Any]) -> str:
    license_ = payload.get("license") if isinstance(payload.get("license"), dict) else {}
    lines = [
        "# License Activated",
        "",
        f"- License file: `{payload.get('license_file')}`",
        f"- Tier: `{license_.get('tier') or 'pro'}`",
        f"- Seats: `{license_.get('seats', 1)}`",
        f"- Key present: `{bool(license_.get('key_present', False))}`",
        "",
        str(payload.get("message") or ""),
    ]
    return "\n".join(line for line in lines if line is not None).rstrip() + "\n"


def stats_to_markdown(payload: dict[str, Any]) -> str:
    slowest = payload.get("slowest_workflow") if isinstance(payload.get("slowest_workflow"), dict) else None
    lines = [
        "# Checkpoint Stats",
        "",
        f"- Workspace: `{payload.get('workspace')}`",
        f"- Total runs: `{payload.get('total_runs', 0)}`",
        f"- Passed runs: `{payload.get('passed_runs', 0)}`",
        f"- Failed runs: `{payload.get('failed_runs', 0)}`",
        f"- Pass rate: `{float(payload.get('pass_rate') or 0.0) * 100:.1f}%`",
    ]
    if slowest:
        lines.append(f"- Slowest workflow: `{slowest.get('workflow_name')}` ({slowest.get('duration_ms')} ms)")
    failures = payload.get("most_failed_steps") if isinstance(payload.get("most_failed_steps"), list) else []
    if failures:
        lines.extend(["", "## Most Failed Steps"])
        for item in failures:
            if isinstance(item, dict):
                lines.append(f"- `{item.get('step')}`: `{item.get('count')}`")
    return "\n".join(lines)
