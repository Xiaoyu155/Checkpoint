from __future__ import annotations

from typing import Any

from .mcp_common import require_str, require_workspace


def list_benchmarks_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .benchmarks import list_public_benchmarks

    workspace = require_workspace(args)
    return {"workspace": str(workspace.root), **list_public_benchmarks(category=str(args.get("category") or "") or None)}


def build_benchmark_plan_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .benchmarks import build_benchmark_plan

    workspace = require_workspace(args)
    return {
        "workspace": str(workspace.root),
        **build_benchmark_plan(
            category=str(args.get("category") or "") or None,
            benchmark_id=str(args.get("benchmark_id") or "") or None,
        ),
    }


def build_benchmark_draft_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .benchmarks import build_benchmark_workflow_draft

    workspace = require_workspace(args)
    output = str(args.get("output") or "") or None
    output_path = (workspace.root / output).resolve() if output else None
    return {
        "workspace": str(workspace.root),
        **build_benchmark_workflow_draft(
            scenario_id=require_str(args, "scenario_id"),
            workspace_root=workspace.root,
            output_path=output_path,
            dry_run=not bool(args.get("save", False)),
            overwrite=bool(args.get("overwrite", False)),
        ),
    }
