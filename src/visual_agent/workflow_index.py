from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from time import time
from typing import Any

from .security import contains_secret_text
from .validation import validate_workflow
from .workflow import parse_workflow_file
from .workflow_quality import score_workflow_quality


INDEX_FILE = "workflow_index.json"
DEFAULT_PUBLIC_WORKFLOW_CATALOG_URL = "https://visualagent.local/workflows"


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
    if getattr(workflow_ref, "quality_score", None) is not None:
        index[name]["quality_score"] = float(getattr(workflow_ref, "quality_score"))
    if getattr(workflow_ref, "published_at", None) is not None:
        index[name]["published_at"] = float(getattr(workflow_ref, "published_at"))
    if getattr(workflow_ref, "published_url", ""):
        index[name]["published_url"] = str(getattr(workflow_ref, "published_url"))
    _write_index(index_path, index)
    return index_path


def mark_workflow_public(workspace: Path, workflow_ref: Any) -> Path:
    path = Path(str(getattr(workflow_ref, "path", "")))
    if path.exists():
        _mark_yaml_public(path)
    index_path = update_workflow_index(workspace, workflow_ref)
    index = _load_index(index_path)
    name = str(getattr(workflow_ref, "name", "") or Path(str(getattr(workflow_ref, "path", ""))).stem)
    if name in index and isinstance(index[name], dict):
        index[name]["visibility"] = "public"
        index[name]["license"] = index[name].get("license") or "cc-by-4.0"
        index[name]["updated_at"] = time()
    _write_index(index_path, index)
    return index_path


def mark_workflow_private(workspace: Path, workflow_ref: Any) -> Path:
    path = Path(str(getattr(workflow_ref, "path", "")))
    if path.exists():
        _mark_yaml_private(path)
    index_path = update_workflow_index(workspace, workflow_ref)
    index = _load_index(index_path)
    name = str(getattr(workflow_ref, "name", "") or Path(str(getattr(workflow_ref, "path", ""))).stem)
    if name in index and isinstance(index[name], dict):
        index[name]["visibility"] = "private"
        index[name]["updated_at"] = time()
    _write_index(index_path, index)
    return index_path


def list_workflows(workspace: Path, *, visibility: str | None = None) -> list[dict[str, Any]]:
    index = _load_index(workspace / INDEX_FILE)
    items = [dict(item) for item in index.values() if isinstance(item, dict)]
    if visibility:
        items = [item for item in items if str(item.get("visibility") or "") == visibility]
    return sorted(items, key=lambda item: str(item.get("name") or ""))


def list_public_workflows(workspace: Path) -> list[dict[str, Any]]:
    return list_workflows(workspace, visibility="public")


def search_workflows(workspace: Path, query: str, *, visibility: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    needle = query.strip().lower()
    if not needle:
        return list_workflows(workspace, visibility=visibility)[:limit]
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in list_workflows(workspace, visibility=visibility):
        haystack = " ".join(
            [
                str(item.get("name") or ""),
                str(item.get("description") or ""),
                " ".join(str(tag) for tag in item.get("tags", []) if str(tag)),
            ]
        ).lower()
        score = _match_score(needle, haystack)
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("name") or "")))
    return [item for _, item in scored[:limit]]


def load_workflow_index(workspace: Path) -> dict[str, Any]:
    return _load_index(workspace / INDEX_FILE)


def publish_workflow(
    workspace: Path,
    workflow_ref: Any,
    *,
    min_quality_score: float = 0.6,
    catalog_url_base: str = DEFAULT_PUBLIC_WORKFLOW_CATALOG_URL,
) -> dict[str, Any]:
    workflow_path = Path(str(getattr(workflow_ref, "path", "")))
    workflow = parse_workflow_file(workflow_path)
    validation = validate_workflow(workflow, strict=True)
    if not validation.valid:
        return {
            "status": "blocked",
            "reason": "workflow_validation_failed",
            "workflow": workflow.name,
            "issues": [
                {"level": issue.level, "step_id": issue.step_id, "message": issue.message}
                for issue in validation.issues
            ],
        }
    workflow_text = workflow_path.read_text(encoding="utf-8")
    if contains_secret_text(workflow_text):
        return {
            "status": "blocked",
            "reason": "secret_text_detected",
            "workflow": workflow.name,
        }
    quality = score_workflow_quality(workflow_text)
    if quality.total_score < float(min_quality_score):
        return {
            "status": "blocked",
            "reason": "quality_below_threshold",
            "workflow": workflow.name,
            "quality_score": round(quality.total_score, 3),
            "min_quality_score": float(min_quality_score),
        }
    if not str(workflow.visibility or "").strip():
        return {
            "status": "blocked",
            "reason": "missing_visibility",
            "workflow": workflow.name,
        }
    if workflow.visibility != "public":
        return {
            "status": "blocked",
            "reason": "workflow_not_public",
            "workflow": workflow.name,
        }
    if not workflow.license:
        return {
            "status": "blocked",
            "reason": "missing_license",
            "workflow": workflow.name,
        }

    published_at = time()
    published_url = f"{catalog_url_base.rstrip('/')}/{workflow.name}"
    public_ref = replace(
        workflow_ref,
        visibility="public",
        quality_score=quality.total_score,
        published_at=published_at,
        published_url=published_url,
    )
    index_path = mark_workflow_public(workspace, public_ref)
    return {
        "status": "published",
        "id": workflow.name,
        "name": workflow.name,
        "version": workflow.version,
        "quality_score": int(round(quality.total_score * 100)),
        "url": published_url,
        "index_path": str(index_path),
        "workflow": {
            "name": workflow.name,
            "visibility": workflow.visibility,
            "license": workflow.license,
            "tags": list(workflow.tags),
        },
    }


def withdraw_workflow(
    workspace: Path,
    workflow_ref: Any,
) -> dict[str, Any]:
    index_path = mark_workflow_private(workspace, workflow_ref)
    name = str(getattr(workflow_ref, "name", "") or Path(str(getattr(workflow_ref, "path", ""))).stem)
    index = _load_index(index_path)
    return {
        "status": "withdrawn",
        "workflow": name,
        "visibility": "private",
        "index_path": str(index_path),
        "workflow_entry": index.get(name, {}),
    }


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


def _mark_yaml_public(path: Path) -> None:
    try:
        import yaml

        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return
        payload["visibility"] = "public"
        payload["license"] = payload.get("license") or "cc-by-4.0"
        path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).rstrip() + "\n", encoding="utf-8")
    except Exception:
        return


def _mark_yaml_private(path: Path) -> None:
    try:
        import yaml

        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return
        payload["visibility"] = "private"
        path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).rstrip() + "\n", encoding="utf-8")
    except Exception:
        return


def _match_score(needle: str, haystack: str) -> int:
    if needle in haystack:
        return 100 + len(needle)
    words = [word for word in needle.split() if word]
    if words:
        hits = sum(1 for word in words if word in haystack)
        if hits:
            return hits * 10
    compact_needle = "".join(needle.split())
    for token in haystack.split():
        if _edit_distance(compact_needle, token) <= 2:
            return 5
    return 0


def _edit_distance(left: str, right: str) -> int:
    if abs(len(left) - len(right)) > 2:
        return 3
    previous = list(range(len(right) + 1))
    for i, lchar in enumerate(left, 1):
        current = [i]
        for j, rchar in enumerate(right, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (0 if lchar == rchar else 1)))
        previous = current
    return previous[-1]
