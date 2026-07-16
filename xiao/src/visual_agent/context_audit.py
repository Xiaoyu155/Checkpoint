from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def append_context_parse_audit(
    path: str | Path,
    *,
    task_description: str,
    generation: Any,
    changed_files: list[str] | tuple[str, ...] = (),
) -> Path:
    audit_path = Path(path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    model = generation.semantic_model
    quality = generation.quality_score
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "task": task_description,
        "status": generation.status,
        "framework": model.framework,
        "confidence": model.confidence,
        "method": generation.generation_method,
        "fields": [field.name for field in model.form_fields],
        "submit_actions": [action.text for action in model.submit_actions],
        "success_states": [state.value for state in model.success_states],
        "unmatched_data_displays": list(_unmatched_data_displays(model)),
        "warnings": list(generation.warnings),
        "quality_score": quality.total_score,
        "workflow_name": generation.workflow_name,
        "workflow_path": generation.workflow_path,
        "changed_files": list(changed_files),
    }
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return audit_path


def _unmatched_data_displays(model: Any) -> tuple[str, ...]:
    from .context_ingestion import summarize_data_displays

    return summarize_data_displays(model).unmatched
