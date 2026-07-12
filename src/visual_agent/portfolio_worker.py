"""Multi-project DevPacer queue worker.

The project queue worker intentionally keeps one worker per workspace. This
module adds the product-level layer above it: run several project queues at the
same time while preserving each project's existing mission lock and safety
rules.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import monotonic
from typing import Any

from .chief_queue import MissionRunner, run_mission_queue_worker


def run_portfolio_mission_worker(
    *,
    project_roots: list[str | Path] | tuple[str | Path, ...],
    workspace_name: str = ".agent-workspace",
    max_workers: int = 2,
    max_items_per_project: int | None = None,
    poll_seconds: float = 0.5,
    watch: bool = False,
    max_seconds: float | None = None,
    mission_runner: MissionRunner | None = None,
) -> dict[str, Any]:
    projects = [_project_entry(path, workspace_name=workspace_name) for path in project_roots]
    if not projects:
        return {
            "schema_version": 1,
            "product": "DevPacer",
            "status": "blocked",
            "reason": "No project roots were provided.",
            "project_count": 0,
            "processed_items": 0,
            "projects": [],
        }

    started = monotonic()
    worker_count = max(1, min(int(max_workers), len(projects)))
    item_limit = _project_item_limit(max_items_per_project, watch=watch)
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="pacer-portfolio") as executor:
        futures = {
            executor.submit(
                _run_project_worker,
                project,
                item_limit=item_limit,
                poll_seconds=poll_seconds,
                watch=watch,
                max_seconds=max_seconds,
                mission_runner=mission_runner,
            ): project
            for project in projects
        }
        for future in as_completed(futures):
            project = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001
                results.append(
                    {
                        **project,
                        "status": "failed",
                        "processed_items": 0,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

    results.sort(key=lambda item: str(item.get("project_root") or ""))
    processed = sum(int(item.get("processed_items") or 0) for item in results)
    failed = [item for item in results if str(item.get("status") or "") in {"failed", "blocked"}]
    if failed:
        status = "partial_failed" if processed else "failed"
    elif any(str(item.get("status") or "") == "max_seconds_reached" for item in results):
        status = "max_seconds_reached"
    elif any(str(item.get("status") or "") == "max_items_reached" for item in results):
        status = "max_items_reached"
    else:
        status = "completed" if processed else "idle"
    return {
        "schema_version": 1,
        "product": "DevPacer",
        "verification_engine": "Checkpoint",
        "status": status,
        "project_count": len(results),
        "max_workers": worker_count,
        "max_items_per_project": item_limit,
        "watch": bool(watch),
        "max_seconds": max_seconds,
        "processed_items": processed,
        "elapsed_seconds": round(monotonic() - started, 6),
        "projects": results,
    }


def portfolio_mission_worker_to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "## DevPacer Portfolio Worker",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- Projects: {payload.get('project_count', 0)}",
        f"- Max workers: {payload.get('max_workers', 0)}",
        f"- Processed items: {payload.get('processed_items', 0)}",
        f"- Watch: `{bool(payload.get('watch'))}`",
        f"- Max seconds: `{payload.get('max_seconds') if payload.get('max_seconds') is not None else ''}`",
        "",
        "| project | status | processed | workspace |",
        "| --- | --- | --- | --- |",
    ]
    for item in payload.get("projects", []) if isinstance(payload.get("projects"), list) else []:
        if not isinstance(item, dict):
            continue
        lines.append(
            "| "
            + " | ".join(
                _cell(value)
                for value in (
                    item.get("project_root"),
                    item.get("status"),
                    item.get("processed_items"),
                    item.get("workspace_root"),
                )
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _run_project_worker(
    project: dict[str, Any],
    *,
    item_limit: int | None,
    poll_seconds: float,
    watch: bool,
    max_seconds: float | None,
    mission_runner: MissionRunner | None,
) -> dict[str, Any]:
    result = run_mission_queue_worker(
        workspace_root=project["workspace_root"],
        run_once=not watch and int(item_limit or 1) <= 1,
        watch=watch,
        poll_seconds=poll_seconds,
        max_items=item_limit,
        max_seconds=max_seconds,
        mission_runner=mission_runner,
    )
    return {
        **project,
        "status": result.get("status"),
        "processed_items": int(result.get("processed_items") or 0),
        "idle_polls": int(result.get("idle_polls") or 0),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "runs": result.get("runs") if isinstance(result.get("runs"), list) else [],
    }


def _project_item_limit(value: int | None, *, watch: bool) -> int | None:
    if value is None:
        return None if watch else 1
    return max(1, int(value))


def _project_entry(project_root: str | Path, *, workspace_name: str) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    workspace = Path(workspace_name).expanduser()
    if not workspace.is_absolute():
        workspace = project / workspace
    return {
        "project_root": str(project),
        "workspace_root": str(workspace.resolve()),
    }


def _cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")
