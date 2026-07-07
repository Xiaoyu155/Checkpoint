from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import to_jsonable
from .preflight import detect_project_type
from .programs import program_dir


def build_reference_pack(
    *,
    objective: str,
    repo_root: str | Path,
    task_id: str = "",
) -> dict[str, Any]:
    project_type = detect_project_type(repo_root) or "unknown"
    keywords = reference_keywords(objective=objective, project_type=project_type)
    return {
        "schema_version": 1,
        "kind": "reference_pack",
        "task_id": task_id,
        "objective": str(objective),
        "project_type": project_type,
        "search_keywords": keywords,
        "source_policy": [
            "Prefer official documentation and mature maintained repositories.",
            "Use GitHub examples as design references, not as code to copy wholesale.",
            "Avoid adding new dependencies unless the project already uses them or the task clearly requires one.",
            "Summarize external code patterns; do not paste large third-party code blocks into worker prompts.",
        ],
        "worker_constraints": [
            "Follow established framework patterns for this project type.",
            "Keep changes scoped to the task.",
            "If references are insufficient, implement the smallest standard approach and record the uncertainty.",
        ],
    }


def save_reference_pack(
    *,
    workspace_root: str | Path,
    program_id: str,
    pack: dict[str, Any],
) -> dict[str, Any]:
    directory = program_dir(workspace_root, program_id) / "references"
    directory.mkdir(parents=True, exist_ok=True)
    task_id = str(pack.get("task_id") or "program")
    path = directory / f"{_safe_token(task_id)}.md"
    path.write_text(reference_pack_to_markdown(pack) + "\n", encoding="utf-8")
    json_path = directory / f"{_safe_token(task_id)}.json"
    json_path.write_text(json.dumps(to_jsonable(pack), ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(path), "json_path": str(json_path)}


def reference_pack_to_markdown(pack: dict[str, Any]) -> str:
    lines = ["## Reference Pack", ""]
    lines.append(f"Task: `{pack.get('task_id') or ''}`")
    lines.append(f"Objective: {pack.get('objective') or ''}")
    lines.append(f"Project type: `{pack.get('project_type') or 'unknown'}`")
    keywords = pack.get("search_keywords") if isinstance(pack.get("search_keywords"), list) else []
    if keywords:
        lines.extend(["", "### Search Keywords", ""])
        lines.extend(f"- {item}" for item in keywords)
    for title, key in (("Source Policy", "source_policy"), ("Worker Constraints", "worker_constraints")):
        items = pack.get(key) if isinstance(pack.get(key), list) else []
        if items:
            lines.extend(["", f"### {title}", ""])
            lines.extend(f"- {item}" for item in items)
    return "\n".join(lines).rstrip()


def reference_keywords(*, objective: str, project_type: str) -> list[str]:
    base = _tokens(objective)
    keywords = []
    if project_type and project_type != "unknown":
        keywords.append(f"{project_type} {' '.join(base[:5])} implementation example")
        keywords.append(f"{project_type} {' '.join(base[:5])} official docs")
    keywords.append(" ".join(base[:6]) + " github example")
    if "flutter" in project_type or any(term in objective.lower() for term in ("flutter", "语音", "路由", "悬浮")):
        keywords.extend(
            [
                "Flutter go_router StatefulShellRoute indexedStack preserve state",
                "Flutter voice session floating overlay example",
            ]
        )
    if any(term in objective.lower() for term in ("登录", "auth", "认证")):
        keywords.append(f"{project_type} authentication flow official docs")
    if any(term in objective.lower() for term in ("支付", "payment", "alipay", "支付宝")):
        keywords.append(f"{project_type} payment integration official docs")
    return _dedupe([item.strip() for item in keywords if item.strip()])


def payload_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2)


def _tokens(value: str) -> list[str]:
    raw = re.split(r"[\s,.;:!?，。；：！？、/\\()\[\]{}_-]+", str(value).lower())
    tokens = [item for item in raw if len(item) >= 2]
    for term in ("flutter", "voice", "router", "语音", "悬浮", "登录", "支付", "路由", "历史记录"):
        if term in str(value).lower() and term not in tokens:
            tokens.append(term)
    return tokens or ["implementation"]


def _dedupe(items: list[str]) -> list[str]:
    out = []
    seen = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out[:8]


def _safe_token(value: str) -> str:
    token = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(value))
    while "--" in token:
        token = token.replace("--", "-")
    return token.strip("-") or "reference"
