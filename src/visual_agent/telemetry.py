from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .workflow import Workflow, WorkflowRunResult
from .visual_status import first_failed_step, root_cause_guess


TELEMETRY_ENV = "VISUAL_AGENT_TELEMETRY"
TELEMETRY_FILE = "telemetry.jsonl"
VA_VERSION = "0.1.0"


def enabled() -> bool:
    return os.environ.get(TELEMETRY_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def record_run(workspace_root: Path, workflow: Workflow, result: WorkflowRunResult, *, project_type: str | None = None) -> Path | None:
    if not enabled():
        return None
    payload = workflow_run_event(workflow, result, project_type=project_type)
    path = workspace_root / TELEMETRY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    return path


def workflow_run_event(workflow: Workflow, result: WorkflowRunResult, *, project_type: str | None = None) -> dict[str, Any]:
    failed = first_failed_step(result)
    duration_ms = int(sum(float(step.metadata.get("elapsed_seconds") or 0.0) for step in result.steps if isinstance(step.metadata, dict)) * 1000)
    event = {
        "event": "workflow_run",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "step_count": len(result.steps),
        "step_types": [step.action for step in result.steps],
        "tags": list(workflow.tags),
        "visibility": workflow.visibility,
        "passed": failed is None,
        "duration_ms": duration_ms,
        "project_type": project_type or "unknown",
        "va_version": VA_VERSION,
    }
    if failed is not None:
        event["root_cause_guess"] = root_cause_guess(failed)
    return event
