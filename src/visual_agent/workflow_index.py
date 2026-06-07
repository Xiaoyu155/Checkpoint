from __future__ import annotations

import json
from pathlib import Path
from time import time
from typing import Any


INDEX_FILE = "workflow_index.json"


def update_workflow_index(workspace: Path, workflow_ref: Any) -> Path:
    index_path = workspace / INDEX_FILE
    index = _load_index(index_path)
    name = str(getattr(workflow_ref, "name", "") or Path(str(getattr(workflow_ref, "path", ""))).stem)
    index[name] = {
        "name": name,
        "description": str(getattr(workflow_ref, "description", "") or ""),
        "tags": [str(item) for item in getattr(workflow_ref, "tags", ())],
        "visibility": str(getattr(workflow_ref, "visibility", "private") or "private"),
        "author": str(getattr(workflow_ref, "author", "") or ""),
        "license": str(getattr(workflow_ref, "license", "") or ""),
        "path": str(getattr(workflow_ref, "relative_path", "") or getattr(workflow_ref, "path", "")),
        "updated_at": time(),
    }
    _write_index(index_path, index)
    return index_path


def mark_workflow_public(workspace: Path, workflow_ref: Any) -> Path:
    index_path = update_workflow_index(workspace, workflow_ref)
    index = _load_index(index_path)
    name = str(getattr(workflow_ref, "name", "") or Path(str(getattr(workflow_ref, "path", ""))).stem)
    if name in index and isinstance(index[name], dict):
        index[name]["visibility"] = "public"
        index[name]["updated_at"] = time()
    _write_index(index_path, index)
    return index_path


def list_public_workflows(workspace: Path) -> list[dict[str, Any]]:
    index = _load_index(workspace / INDEX_FILE)
    return [
        dict(item)
        for item in index.values()
        if isinstance(item, dict) and item.get("visibility") == "public"
    ]


def load_workflow_index(workspace: Path) -> dict[str, Any]:
    return _load_index(workspace / INDEX_FILE)


def _load_index(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_index(path: Path, index: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
