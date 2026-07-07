from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from time import time
from typing import Any

from visual_agent.security import contains_secret_text
from visual_agent.validation import validate_workflow
from visual_agent.workflow import parse_workflow_file, workflow_from_dict
from visual_agent.workflow_quality import score_workflow_quality
from visual_agent.workspace import discover_workflows, open_workspace
from visual_agent.versioning import UnsupportedSchemaVersionError, migrate_catalog_payload


CATALOG_DIR = "cloud_marketplace"
CATALOG_ORGS_DIR = "orgs"
CATALOG_FILE = "catalog.json"
DEFAULT_CATALOG_URL_BASE = "https://visualagent.local/workflows"


@dataclass(frozen=True)
class CatalogWorkflow:
    id: str
    name: str
    description: str
    tags: tuple[str, ...]
    visibility: str
    author: str
    license: str
    version: int
    quality_score: int
    downloads: int
    created_at: float
    updated_at: float
    workflow_yaml: str = ""
    workflow_path: str = ""
    published_url: str = ""
    source: str = "workspace"
    org: str = ""
    owner_user_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["tags"] = list(self.tags)
        return payload


def catalog_root(workspace_root: str | Path, *, org: str = "") -> Path:
    root = Path(workspace_root).resolve() / CATALOG_DIR
    scope = _catalog_scope_name(org)
    return root if scope == "default" else root / CATALOG_ORGS_DIR / scope


def catalog_path(workspace_root: str | Path, *, org: str = "") -> Path:
    return catalog_root(workspace_root, org=org) / CATALOG_FILE


def load_catalog(workspace_root: str | Path, *, org: str = "") -> dict[str, Any]:
    path = catalog_path(workspace_root, org=org)
    if not path.exists():
        return {"schema_version": 1, "org": org, "next_id": 1, "workflows": [], "withdrawn_workflows": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": 1, "org": org, "next_id": 1, "workflows": [], "withdrawn_workflows": []}
    if not isinstance(payload, dict):
        return {"schema_version": 1, "org": org, "next_id": 1, "workflows": [], "withdrawn_workflows": []}
    try:
        return migrate_catalog_payload(payload, org=org)
    except UnsupportedSchemaVersionError as exc:
        return {
            "schema_version": 1,
            "org": org,
            "next_id": 1,
            "workflows": [],
            "withdrawn_workflows": [],
            "status": "upgrade_required",
            "reason": "unsupported_catalog_schema",
            "migration_hint": exc.migration_hint,
        }


def save_catalog(workspace_root: str | Path, catalog: dict[str, Any], *, org: str = "") -> Path:
    path = catalog_path(workspace_root, org=org)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = migrate_catalog_payload(catalog, org=org)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def sync_workspace_public_workflows(workspace_root: str | Path, *, org: str = "") -> Path:
    workspace = open_workspace(workspace_root)
    catalog = load_catalog(workspace.root, org=org)
    workflows = _workflow_map(catalog)
    withdrawn = _withdrawn_workflow_names(catalog)
    for ref in discover_workflows(workspace, include_slow=True):
        if str(ref.visibility or "") != "public":
            continue
        if ref.name in withdrawn:
            continue
        workflow_path = Path(ref.path)
        workflow = parse_workflow_file(workflow_path)
        workflow_yaml = workflow_path.read_text(encoding="utf-8")
        quality = score_workflow_quality(workflow_path.read_text(encoding="utf-8"))
        existing = workflows.get(workflow.name)
        created_at = float(existing.get("created_at") or time()) if isinstance(existing, dict) else time()
        published_url = str(existing.get("published_url") or f"{DEFAULT_CATALOG_URL_BASE.rstrip('/')}/{workflow.name}") if isinstance(existing, dict) else f"{DEFAULT_CATALOG_URL_BASE.rstrip('/')}/{workflow.name}"
        workflows[workflow.name] = CatalogWorkflow(
            id=str(existing.get("id") or workflow.name) if isinstance(existing, dict) else workflow.name,
            name=workflow.name,
            description=workflow.description,
            tags=tuple(str(item) for item in workflow.tags),
            visibility="public",
            author=workflow.author,
            license=workflow.license,
            version=workflow.version,
            quality_score=int(round(quality.total_score * 100)),
            downloads=int(existing.get("downloads") or 0) + 1 if isinstance(existing, dict) else 0,
            created_at=created_at,
            updated_at=time(),
            workflow_yaml=workflow_yaml,
            workflow_path=ref.relative_path,
            published_url=published_url,
            source="workspace",
            org=str(org or ""),
        ).to_dict()
    catalog["workflows"] = sorted(workflows.values(), key=lambda item: str(item.get("name") or ""))
    catalog["next_id"] = max(int(catalog.get("next_id") or 1), len(catalog["workflows"]) + 1)
    catalog["withdrawn_workflows"] = sorted(withdrawn)
    return save_catalog(workspace.root, catalog, org=org)


def list_catalog_workflows(
    workspace_root: str | Path,
    *,
    org: str = "",
    visibility: str | None = None,
    category: str = "",
    tags: list[str] | None = None,
    limit: int = 50,
    cursor: str = "",
) -> list[dict[str, Any]]:
    catalog = load_catalog(workspace_root, org=org)
    items = [item for item in catalog.get("workflows", []) if isinstance(item, dict)]
    if visibility and visibility not in {"all", "*"}:
        items = [item for item in items if str(item.get("visibility") or "") == visibility]
    if category:
        items = [item for item in items if category in {str(tag) for tag in item.get("tags", []) if str(tag)}]
    if tags:
        wanted = {str(tag) for tag in tags if str(tag)}
        if wanted:
            items = [item for item in items if wanted.intersection({str(tag) for tag in item.get("tags", []) if str(tag)})]
    items = sorted(items, key=lambda item: (-float(item.get("updated_at") or 0), str(item.get("name") or "")))
    start = int(cursor or 0) if str(cursor or "").isdigit() else 0
    return [_catalog_public_projection(item) for item in items[start : start + max(0, min(int(limit), 100))]]


def search_catalog_workflows(
    workspace_root: str | Path,
    query: str,
    *,
    org: str = "",
    visibility: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    needle = query.strip().lower()
    items = list_catalog_workflows(workspace_root, org=org, visibility=visibility, limit=1000)
    if not needle:
        return [_catalog_public_projection(item) for item in items[:limit]]
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in items:
        haystack = " ".join(
            [
                str(item.get("name") or ""),
                str(item.get("description") or ""),
                " ".join(str(tag) for tag in item.get("tags", []) if str(tag)),
            ]
        ).lower()
        score = 0
        if needle in haystack:
            score = 100 + len(needle)
        else:
            hits = sum(1 for word in needle.split() if word and word in haystack)
            if hits:
                score = hits * 10
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("name") or "")))
    return [dict(_catalog_public_projection(item), score=score) for score, item in scored[:limit]]


def get_catalog_workflow(workspace_root: str | Path, workflow_id: str, *, org: str = "") -> dict[str, Any] | None:
    catalog = load_catalog(workspace_root, org=org)
    items = [item for item in catalog.get("workflows", []) if isinstance(item, dict)]
    needle = str(workflow_id or "").strip()
    if not needle:
        return None
    for item in items:
        if str(item.get("id") or "") == needle or str(item.get("name") or "") == needle:
            return dict(item)
    return None


def delete_catalog_workflow(workspace_root: str | Path, workflow_id: str, *, org: str = "") -> dict[str, Any]:
    catalog = load_catalog(workspace_root, org=org)
    items = [item for item in catalog.get("workflows", []) if isinstance(item, dict)]
    withdrawn = _withdrawn_workflow_names(catalog)
    needle = str(workflow_id or "").strip()
    if not needle:
        return {"status": "blocked", "reason": "workflow_id_required"}
    remaining: list[dict[str, Any]] = []
    removed: dict[str, Any] | None = None
    for item in items:
        if removed is None and (str(item.get("id") or "") == needle or str(item.get("name") or "") == needle):
            removed = dict(item)
            continue
        remaining.append(dict(item))
    if removed is None:
        return {"status": "blocked", "reason": "workflow_not_found"}
    catalog["workflows"] = remaining
    catalog["next_id"] = max(int(catalog.get("next_id") or 1), len(remaining) + 1)
    if removed.get("name"):
        withdrawn.add(str(removed.get("name")))
    if removed.get("id"):
        withdrawn.add(str(removed.get("id")))
    catalog["withdrawn_workflows"] = sorted(withdrawn)
    save_catalog(workspace_root, catalog, org=org)
    return {"status": "deleted", "workflow": removed, "workflow_id": str(removed.get("id") or needle)}


def publish_catalog_workflow(
    workspace_root: str | Path,
    payload: dict[str, Any],
    *,
    org: str = "",
    user_id: str = "",
    min_quality_score: float = 0.6,
    catalog_url_base: str = DEFAULT_CATALOG_URL_BASE,
) -> dict[str, Any]:
    workspace = open_workspace(workspace_root)
    workflow_yaml = str(payload.get("workflow_yaml") or "")
    workflow = None
    if workflow_yaml.strip():
        workflow = _workflow_from_yaml_text(workflow_yaml)
    else:
        workflow_name = str(payload.get("name") or payload.get("workflow_name") or "")
        workflow_ref = None
        if workflow_name:
            from visual_agent.workspace import find_workflow

            workflow_ref = find_workflow(workspace, workflow_name)
        if workflow_ref is None:
            return {"status": "blocked", "reason": "workflow_not_found"}
        workflow = parse_workflow_file(workflow_ref.path)
        workflow_yaml = workflow_ref.path.read_text(encoding="utf-8")

    validation = validate_workflow(workflow, strict=True)
    if not validation.valid:
        return {
            "status": "blocked",
            "reason": "workflow_validation_failed",
            "issues": [
                {"level": issue.level, "step_id": issue.step_id, "message": issue.message}
                for issue in validation.issues
            ],
        }
    visibility = str(payload.get("visibility") or workflow.visibility or "public").strip().lower()
    if visibility not in {"public", "private"}:
        return {"status": "blocked", "reason": "invalid_visibility"}
    if not workflow.license:
        return {"status": "blocked", "reason": "missing_license"}
    if contains_secret_text(workflow_yaml):
        return {"status": "blocked", "reason": "secret_text_detected"}
    quality = score_workflow_quality(workflow_yaml)
    if quality.total_score < float(min_quality_score):
        return {
            "status": "blocked",
            "reason": "quality_below_threshold",
            "quality_score": round(quality.total_score, 3),
            "min_quality_score": float(min_quality_score),
        }

    catalog = load_catalog(workspace.root, org=org)
    workflows = _workflow_map(catalog)
    withdrawn = _withdrawn_workflow_names(catalog)
    withdrawn.discard(workflow.name)
    withdrawn.discard(str(workflow.name))
    existing = workflows.get(workflow.name)
    catalog_id = str(existing.get("id") or workflow.name) if isinstance(existing, dict) else _next_catalog_id(catalog)
    created_at = float(existing.get("created_at") or time()) if isinstance(existing, dict) else time()
    downloads = int(existing.get("downloads") or 0) if isinstance(existing, dict) else 0
    existing_owner_user_id = str(existing.get("owner_user_id") or "") if isinstance(existing, dict) else ""
    published_url = f"{catalog_url_base.rstrip('/')}/{catalog_id}"
    record = CatalogWorkflow(
        id=catalog_id,
        name=workflow.name,
        description=workflow.description,
        tags=tuple(str(item) for item in workflow.tags),
        visibility=visibility,
        author=workflow.author,
        license=workflow.license,
        version=workflow.version,
        quality_score=int(round(quality.total_score * 100)),
        downloads=downloads,
        created_at=created_at,
        updated_at=time(),
        workflow_yaml=workflow_yaml,
        workflow_path=str(existing.get("workflow_path") or "") if isinstance(existing, dict) else "",
        published_url=published_url,
        source="publish",
        org=str(org or ""),
        owner_user_id=str(user_id or payload.get("user_id") or payload.get("user") or existing_owner_user_id),
    ).to_dict()
    workflows[workflow.name] = record
    catalog["workflows"] = sorted(workflows.values(), key=lambda item: str(item.get("name") or ""))
    catalog["next_id"] = max(int(catalog.get("next_id") or 1), _catalog_id_number(catalog_id) + 1)
    catalog["withdrawn_workflows"] = sorted(withdrawn)
    save_catalog(workspace.root, catalog, org=org)
    return {
        "status": "published",
        "id": catalog_id,
        "name": workflow.name,
        "version": workflow.version,
        "quality_score": record["quality_score"],
        "url": published_url,
        "catalog_path": str(catalog_path(workspace.root, org=org)),
        "workflow": record,
    }


def _workflow_from_yaml_text(text: str):
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to publish workflow YAML.") from exc
    payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise ValueError("Workflow YAML root must be an object.")
    return workflow_from_dict(payload)


def _workflow_map(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items = catalog.get("workflows") if isinstance(catalog.get("workflows"), list) else []
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, dict) and item.get("name"):
            result[str(item["name"])] = dict(item)
    return result


def _withdrawn_workflow_names(catalog: dict[str, Any]) -> set[str]:
    items = catalog.get("withdrawn_workflows") if isinstance(catalog.get("withdrawn_workflows"), list) else []
    return {str(item) for item in items if str(item)}


def _catalog_public_projection(item: dict[str, Any]) -> dict[str, Any]:
    public_item = dict(item)
    public_item.pop("workflow_yaml", None)
    return public_item


def _next_catalog_id(catalog: dict[str, Any]) -> str:
    next_id = int(catalog.get("next_id") or 1)
    return f"wf_{next_id:06d}"


def _catalog_id_number(catalog_id: str) -> int:
    text = str(catalog_id or "")
    if text.startswith("wf_") and text[3:].isdigit():
        return int(text[3:])
    return 0


def _catalog_scope_name(org: str) -> str:
    text = str(org or "").strip()
    if not text:
        return "default"
    cleaned = []
    for char in text.lower():
        if char.isalnum() or char in {"-", "_"}:
            cleaned.append(char)
        elif cleaned and cleaned[-1] != "_":
            cleaned.append("_")
    scope = "".join(cleaned).strip("_")
    return scope or "default"
