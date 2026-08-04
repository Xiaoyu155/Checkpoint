"""Cross-workspace usage timeline.

Pacer writes one ``journey.json`` per mission, but until now the only way to see
what Pacer actually did over the past days was to open ``.agent-workspace`` by
hand, one workspace at a time. This module walks every workspace it can find and
folds the mission journeys into a single time-ordered record.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .mission_journey import build_mission_journey, mission_journey_path
from .missions import missions_dir


# Sandboxes, virtualenvs and dependency trees never hold a real workspace, and
# walking them on Windows costs seconds per level.
_SKIP_DIRECTORY_NAMES = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "build",
}

_PILLAR_TITLES = {
    "routing": "路由",
    "memory": "记忆",
    "managed": "托管",
    "acceptance": "验收",
    "delivery": "交付",
}

_STATUS_MARKS = {
    "passed": "✓",
    "incomplete": "!",
    "ready": "·",
    "not_applicable": "-",
    "pending": "·",
    "blocked": "✗",
    "failed": "✗",
}


def discover_workspace_roots(base: str | Path, *, max_depth: int = 3) -> list[Path]:
    """Find every ``.agent-workspace`` directory under ``base``.

    ``max_depth`` counts directory levels below ``base``; the default reaches
    sibling project sandboxes (``base/pacer-demo/.agent-workspace``) without
    descending into their isolation worktrees.
    """

    root = Path(base).expanduser().resolve()
    if not root.is_dir():
        return []
    found: list[Path] = []
    direct = root / ".agent-workspace"
    if direct.is_dir():
        found.append(direct)

    def walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir() or entry.is_symlink():
                continue
            name = entry.name
            if name in _SKIP_DIRECTORY_NAMES:
                continue
            if name == ".agent-workspace":
                if entry not in found:
                    found.append(entry)
                continue
            if name.startswith(".") and name != ".agent-workspace":
                continue
            walk(entry, depth + 1)

    walk(root, 1)
    return found


def collect_usage_timeline(
    roots: list[str | Path],
    *,
    days: int = 14,
    limit: int = 100,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fold mission journeys from every root into one time-ordered payload."""

    reference = now or datetime.now(timezone.utc)
    cutoff = reference - timedelta(days=max(int(days), 0)) if days else None
    entries: list[dict[str, Any]] = []
    scanned: list[str] = []
    for root in roots:
        workspace_root = Path(root).expanduser().resolve()
        scanned.append(str(workspace_root))
        entries.extend(_collect_workspace_entries(workspace_root, cutoff))
    entries.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    truncated = len(entries) > limit > 0
    if truncated:
        entries = entries[:limit]
    return {
        "schema_version": 1,
        "product": "Pacer",
        "kind": "usage_timeline",
        "generated_at": reference.isoformat(),
        "days": int(days),
        "workspaces_scanned": scanned,
        "entry_count": len(entries),
        "truncated": truncated,
        "totals": _totals(entries),
        "entries": entries,
    }


def _collect_workspace_entries(workspace_root: Path, cutoff: datetime | None) -> list[dict[str, Any]]:
    root = missions_dir(workspace_root)
    if not root.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for mission_path in root.glob("*/mission.json"):
        mission = _read_json(mission_path)
        if not isinstance(mission, dict) or bool(mission.get("hidden")):
            continue
        updated_at = str(mission.get("updated_at") or mission.get("created_at") or "")
        if cutoff is not None and not _within(updated_at, cutoff):
            continue
        mission_id = str(mission.get("mission_id") or mission_path.parent.name)
        journey = _load_or_build_journey(workspace_root, mission_id, mission)
        entries.append(_entry(workspace_root, mission, journey, updated_at))
    return entries


def _load_or_build_journey(
    workspace_root: Path,
    mission_id: str,
    mission: dict[str, Any],
) -> dict[str, Any]:
    stored = _read_json(mission_journey_path(workspace_root, mission_id))
    if isinstance(stored, dict) and stored.get("phases"):
        return stored
    # Older missions predate journey.json; rebuilding keeps history readable
    # instead of showing a hole in the timeline.
    try:
        return build_mission_journey(
            workspace_root=workspace_root,
            mission_id=mission_id,
            mission=mission,
        )
    except Exception:  # noqa: BLE001 - a broken record must not kill the timeline
        return {}


def _entry(
    workspace_root: Path,
    mission: dict[str, Any],
    journey: dict[str, Any],
    updated_at: str,
) -> dict[str, Any]:
    phases = journey.get("phases") if isinstance(journey.get("phases"), list) else []
    pillars: dict[str, str] = {}
    routing_summary = ""
    memory_entries = 0
    acceptance_tier = ""
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        phase_id = str(phase.get("id") or "")
        pillars[phase_id] = str(phase.get("status") or "")
        details = phase.get("details") if isinstance(phase.get("details"), dict) else {}
        if phase_id == "routing":
            provider = str(details.get("provider") or "")
            model = str(details.get("model") or "")
            agent = str(details.get("actual_agent") or details.get("expected_agent") or "")
            routing_summary = " / ".join(part for part in (agent, provider, model) if part)
        elif phase_id == "memory":
            memory_entries = int(details.get("selected_entries") or 0)
        elif phase_id == "acceptance":
            acceptance_tier = str(details.get("acceptance_tier") or "")
    return {
        "workspace_root": str(workspace_root),
        "project": workspace_root.parent.name,
        "mission_id": str(mission.get("mission_id") or ""),
        "objective": str(mission.get("objective") or ""),
        "status": str(mission.get("status") or ""),
        "stop_reason": str(mission.get("stop_reason") or ""),
        "created_at": str(mission.get("created_at") or ""),
        "updated_at": updated_at,
        "journey_status": str(journey.get("status") or ""),
        "can_claim_verified": bool(journey.get("can_claim_verified")),
        "can_claim_delivered": bool(journey.get("can_claim_delivered")),
        "next_action": str(journey.get("next_action") or ""),
        "reason_codes": [str(code) for code in (journey.get("reason_codes") or [])],
        "routing": routing_summary,
        "memory_entries": memory_entries,
        "acceptance_tier": acceptance_tier,
        "pillars": pillars,
    }


def _totals(entries: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        "missions": len(entries),
        "verified": 0,
        "delivered": 0,
        "blocked": 0,
        "memory_entries_injected": 0,
        "projects": 0,
    }
    projects: set[str] = set()
    for entry in entries:
        projects.add(str(entry.get("project") or ""))
        if entry.get("can_claim_verified"):
            totals["verified"] += 1
        if entry.get("can_claim_delivered"):
            totals["delivered"] += 1
        pillars = entry.get("pillars") if isinstance(entry.get("pillars"), dict) else {}
        if any(str(status) in {"blocked", "failed"} for status in pillars.values()):
            totals["blocked"] += 1
        totals["memory_entries_injected"] += int(entry.get("memory_entries") or 0)
    totals["projects"] = len(projects)
    return totals


def usage_timeline_to_markdown(payload: dict[str, Any]) -> str:
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    lines = [
        f"# Pacer 使用时间线（最近 {payload.get('days')} 天）",
        "",
        f"- 任务：`{totals.get('missions', 0)}` 个，跨 `{totals.get('projects', 0)}` 个项目",
        f"- 通过验收：`{totals.get('verified', 0)}` · 已交付：`{totals.get('delivered', 0)}` · 有阻塞环节：`{totals.get('blocked', 0)}`",
        f"- 注入记忆条目：`{totals.get('memory_entries_injected', 0)}`",
        "",
    ]
    if not entries:
        lines.append("这段时间没有任务记录。把 `--days` 调大，或用 `--workspace-root` 指定工作区。")
        return "\n".join(lines)
    for entry in entries:
        pillars = entry.get("pillars") if isinstance(entry.get("pillars"), dict) else {}
        chain = " ".join(
            f"{_STATUS_MARKS.get(str(pillars.get(pillar) or ''), '·')}{_PILLAR_TITLES[pillar]}"
            for pillar in ("routing", "memory", "managed", "acceptance", "delivery")
            if pillar in pillars
        )
        objective = str(entry.get("objective") or "")
        if len(objective) > 90:
            objective = objective[:89] + "…"
        lines.append(f"## {_short_time(str(entry.get('updated_at') or ''))} · {entry.get('project')} · {entry.get('journey_status') or entry.get('status')}")
        lines.append(f"- 目标：{objective}")
        lines.append(f"- 闭环：{chain or '无证据'}")
        if entry.get("routing"):
            lines.append(f"- 路由：{entry.get('routing')}")
        if entry.get("memory_entries"):
            lines.append(f"- 记忆：注入 {entry.get('memory_entries')} 条")
        if entry.get("acceptance_tier") == "regression_clear":
            lines.append("- 验收：只证明没弄坏，没证明目标达成")
        if entry.get("next_action"):
            lines.append(f"- 下一步：{entry.get('next_action')}")
        if entry.get("reason_codes"):
            lines.append(f"- 原因码：{', '.join(entry['reason_codes'])}")
        lines.append(f"- 记录：`{entry.get('workspace_root')}` / `{entry.get('mission_id')}`")
        lines.append("")
    if payload.get("truncated"):
        lines.append("（结果已按 --limit 截断。）")
    return "\n".join(lines)


def _short_time(value: str) -> str:
    parsed = _parse(value)
    if parsed is None:
        return value or "?"
    return parsed.astimezone().strftime("%m-%d %H:%M")


def _within(value: str, cutoff: datetime) -> bool:
    parsed = _parse(value)
    if parsed is None:
        return True
    return parsed >= cutoff


def _parse(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
