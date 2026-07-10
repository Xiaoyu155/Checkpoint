from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import to_jsonable
from .resources import bundled_path
from .workspace import Workspace


TEMPLATE_ROOT = bundled_path("templates")


@dataclass(frozen=True)
class TemplateRef:
    id: str
    name: str
    description: str
    path: Path
    workflow: str
    inputs: str | None = None
    fixtures: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


def list_templates(template_root: str | Path = TEMPLATE_ROOT) -> tuple[TemplateRef, ...]:
    root = Path(template_root)
    if not root.exists():
        return ()
    templates: list[TemplateRef] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        manifest = path / "template.json"
        if not manifest.exists():
            continue
        templates.append(template_from_manifest(manifest))
    return tuple(templates)


def get_template(template_id: str, template_root: str | Path = TEMPLATE_ROOT) -> TemplateRef:
    for template in list_templates(template_root):
        if template.id == template_id:
            return template
    raise FileNotFoundError(f"Template not found: {template_id}")


def template_from_manifest(path: str | Path) -> TemplateRef:
    manifest_path = Path(path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    return TemplateRef(
        id=str(payload["id"]),
        name=str(payload["name"]),
        description=str(payload.get("description", "")),
        path=manifest_path.parent,
        workflow=str(payload["workflow"]),
        inputs=payload.get("inputs"),
        fixtures=tuple(payload.get("fixtures") or ()),
        tags=tuple(payload.get("tags") or ()),
    )


def install_template(workspace: Workspace, template_id: str, *, overwrite: bool = False) -> dict[str, Any]:
    template = get_template(template_id)
    copied: list[str] = []

    copied.append(copy_template_file(template.path / template.workflow, workspace.workflows_dir, overwrite=overwrite))
    if template.inputs:
        copied.append(copy_template_file(template.path / template.inputs, workspace.inputs_dir, overwrite=overwrite))
    for fixture in template.fixtures:
        copied.append(copy_template_file(template.path / fixture, workspace.fixtures_dir, overwrite=overwrite))

    return {
        "template": to_jsonable(template),
        "workspace": str(workspace.root),
        "copied": copied,
    }


def copy_template_file(source: Path, target_dir: Path, *, overwrite: bool) -> str:
    target = target_dir / source.name
    if target.exists() and not overwrite:
        return str(target)
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return str(target)
