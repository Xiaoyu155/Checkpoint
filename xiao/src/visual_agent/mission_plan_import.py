"""Import a development plan into DevPacer mission drafts.

Users often already have a markdown plan, issue checklist, or roadmap before
they open Codex. Importing that plan should not spend model tokens. This module
extracts concrete mission objectives, optionally creates preview missions, and
can queue those previews only when explicitly requested.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .chief_queue import mission_queue_item_to_dict, submit_mission_queue_item
from .chief_run import run_chief_mission
from .models import to_jsonable


MISSION_IMPORTS_DIRNAME = "mission_imports"


@dataclass(frozen=True)
class MissionDraft:
    index: int
    objective: str
    source_line: int
    source_type: str
    section: str = ""
    raw: str = ""


def mission_imports_dir(workspace_root: str | Path) -> Path:
    return Path(workspace_root).expanduser().resolve() / MISSION_IMPORTS_DIRNAME


def mission_import_dir(workspace_root: str | Path, import_id: str) -> Path:
    return mission_imports_dir(workspace_root) / str(import_id)


def make_mission_import_id(source_text: str, *, source_path: str = "", now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    stamp = moment.strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha1((source_path + "\n" + source_text).encode("utf-8")).hexdigest()[:6]
    return f"{stamp}-{digest}"


def parse_development_plan(text: str, *, source_name: str = "", limit: int | None = None) -> dict[str, Any]:
    """Extract mission drafts from markdown-ish development plans.

    Preference order is intentional: explicit unchecked task lines are more
    actionable than section headings. If a plan only has headings, headings are
    used as coarse mission drafts.
    """
    lines = text.splitlines()
    title = Path(source_name).name if source_name else "Development plan"
    section = ""
    in_fence = False
    task_candidates: list[dict[str, Any]] = []
    heading_candidates: list[dict[str, Any]] = []
    skipped_done = 0

    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.+?)\s*$", stripped)
        if heading_match:
            level = len(heading_match.group(1))
            heading = _clean_objective(heading_match.group(2))
            if level == 1 and heading:
                title = heading
            elif heading:
                section = heading
                if _is_actionable_heading(heading):
                    heading_candidates.append(
                        {
                            "objective": heading,
                            "source_line": line_no,
                            "source_type": "heading",
                            "section": "",
                            "raw": line.rstrip(),
                        }
                    )
            continue

        checkbox = re.match(r"^[-*+]\s+\[([ xX])\]\s+(.+?)\s*$", stripped)
        if checkbox:
            checked = checkbox.group(1).lower() == "x"
            objective = _clean_objective(checkbox.group(2))
            if checked:
                skipped_done += 1
                continue
            _append_task_candidate(
                task_candidates,
                objective=objective,
                section=section,
                source_line=line_no,
                source_type="task",
                raw=line.rstrip(),
            )
            continue

        numbered = re.match(r"^(?:\d+[\.)]|[A-Za-z][\.)])\s+(.+?)\s*$", stripped)
        bullet = re.match(r"^[-*+]\s+(.+?)\s*$", stripped)
        if numbered or bullet:
            body = (numbered or bullet).group(1)
            objective = _clean_objective(body)
            _append_task_candidate(
                task_candidates,
                objective=objective,
                section=section,
                source_line=line_no,
                source_type="numbered" if numbered else "bullet",
                raw=line.rstrip(),
            )

    selected = task_candidates or heading_candidates
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    max_items = max(0, int(limit)) if limit is not None else None
    for candidate in selected:
        objective = str(candidate.get("objective") or "").strip()
        if not _is_actionable_objective(objective):
            continue
        key = re.sub(r"\s+", " ", objective.lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append({**candidate, "index": len(deduped) + 1})
        if max_items is not None and len(deduped) >= max_items:
            break

    return {
        "schema_version": 1,
        "title": title,
        "source_name": source_name,
        "drafts": deduped,
        "total_drafts": len(deduped),
        "skipped_completed_tasks": skipped_done,
    }


def import_development_plan(
    *,
    source_file: str | Path,
    workspace_root: str | Path,
    repo_root: str | Path = ".",
    base: str = "HEAD",
    create: bool = False,
    queue: bool = False,
    limit: int = 8,
    agents: tuple[str, ...] = (),
    answers: tuple[str, ...] = (),
    interview: bool = False,
    max_rounds: int = 2,
    max_wall_minutes: int = 60,
    max_worker_minutes: int = 45,
    run_profile: str = "dry-run",
    include_slow: bool = False,
    max_workflows: int = 10,
    timeout_seconds: float = 1800.0,
    allow_dirty: bool = False,
    allow_coverage_gap: bool = False,
    test_command: str | None = None,
    allow_test_edits: bool = False,
    merge_policy: str = "manual",
    priority: int = 0,
    force: bool = False,
) -> dict[str, Any]:
    source_path = Path(source_file).expanduser().resolve()
    text = source_path.read_text(encoding="utf-8")
    parsed = parse_development_plan(text, source_name=str(source_path), limit=limit)
    import_id = make_mission_import_id(text, source_path=str(source_path))
    workspace_path = Path(workspace_root).expanduser().resolve()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "product": "DevPacer",
        "verification_engine": "Checkpoint",
        "kind": "mission_plan_import",
        "import_id": import_id,
        "source_path": str(source_path),
        "source_title": parsed["title"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "workspace_root": str(workspace_path),
        "repo_root": str(Path(repo_root).expanduser().resolve()),
        "status": "drafted",
        "drafts": parsed["drafts"],
        "total_drafts": parsed["total_drafts"],
        "skipped_completed_tasks": parsed["skipped_completed_tasks"],
        "created_missions": [],
        "queued_items": [],
        "warnings": [],
    }
    if not parsed["drafts"]:
        payload["status"] = "empty"
        payload["warnings"].append("No actionable mission drafts were found in the plan.")
        saved = save_mission_import(payload, workspace_root=workspace_path, import_id=import_id)
        payload["saved_path"] = saved["path"]
        return payload

    if create or queue:
        for draft in parsed["drafts"]:
            result = run_chief_mission(
                goal=str(draft["objective"]),
                workspace_root=workspace_path,
                repo_root=repo_root,
                base=base,
                agents=agents,
                answers=answers,
                interview=interview,
                max_rounds=max_rounds,
                max_wall_minutes=max_wall_minutes,
                max_worker_minutes=max_worker_minutes,
                execute=False,
                dry_run=True,
                run_profile=run_profile,
                include_slow=include_slow,
                max_workflows=max_workflows,
                timeout_seconds=timeout_seconds,
                allow_dirty=allow_dirty,
                allow_coverage_gap=allow_coverage_gap,
                test_command=test_command,
                allow_test_edits=allow_test_edits,
            )
            mission = result.get("mission") if isinstance(result.get("mission"), dict) else {}
            created = {
                "draft_index": draft["index"],
                "objective": draft["objective"],
                "status": result.get("status"),
                "stop_reason": result.get("stop_reason"),
                "mission_id": mission.get("mission_id"),
                "plan_id": mission.get("plan_id"),
                "final_report_path": result.get("final_report_path"),
            }
            payload["created_missions"].append(created)
            if queue:
                mission_id = str(mission.get("mission_id") or "")
                if str(result.get("status") or "") != "preview" or not mission_id:
                    payload["warnings"].append(
                        f"Draft {draft['index']} was not queued because mission creation ended with "
                        f"{result.get('status')}/{result.get('stop_reason')}."
                    )
                    continue
                try:
                    item = submit_mission_queue_item(
                        workspace_root=workspace_path,
                        mission_id=mission_id,
                        priority=priority,
                        run_profile=run_profile,
                        include_slow=include_slow,
                        max_workflows=max_workflows,
                        timeout_seconds=timeout_seconds,
                        allow_dirty=allow_dirty,
                        allow_coverage_gap=allow_coverage_gap,
                        agent=agents[0] if agents else None,
                        test_command=test_command,
                        allow_test_edits=allow_test_edits,
                        merge_policy=merge_policy,
                        force=force,
                    )
                except (FileNotFoundError, RuntimeError, ValueError) as exc:
                    payload["warnings"].append(f"Draft {draft['index']} queue submit failed: {exc}")
                    continue
                payload["queued_items"].append({"draft_index": draft["index"], **mission_queue_item_to_dict(item)})
        if queue:
            payload["status"] = "queued" if payload["queued_items"] else "created"
        else:
            payload["status"] = "created"

    saved = save_mission_import(payload, workspace_root=workspace_path, import_id=import_id)
    payload["saved_path"] = saved["path"]
    return payload


def save_mission_import(payload: dict[str, Any], *, workspace_root: str | Path, import_id: str | None = None) -> dict[str, Any]:
    iid = import_id or str(payload.get("import_id") or "")
    if not iid:
        iid = make_mission_import_id(json.dumps(payload, ensure_ascii=False))
    directory = mission_import_dir(workspace_root, iid)
    directory.mkdir(parents=True, exist_ok=True)
    record = dict(payload)
    record["import_id"] = iid
    path = directory / "import.json"
    path.write_text(json.dumps(to_jsonable(record), ensure_ascii=False, indent=2), encoding="utf-8")
    return {"import_id": iid, "path": str(path)}


def mission_plan_import_to_markdown(payload: dict[str, Any]) -> str:
    lines = ["## DevPacer Mission Import", ""]
    lines.append(f"Status: `{payload.get('status')}`")
    lines.append(f"Source: `{payload.get('source_path')}`")
    lines.append(f"Drafts: `{payload.get('total_drafts', 0)}`")
    if payload.get("saved_path"):
        lines.append(f"Saved import: `{payload.get('saved_path')}`")
    drafts = payload.get("drafts") if isinstance(payload.get("drafts"), list) else []
    if drafts:
        lines.extend(["", "### Draft Missions", ""])
        for draft in drafts:
            lines.append(f"- {draft.get('index')}. {draft.get('objective')}")
            if draft.get("source_line"):
                lines.append(f"  source: line {draft.get('source_line')} · {draft.get('source_type')}")
    created = payload.get("created_missions") if isinstance(payload.get("created_missions"), list) else []
    if created:
        lines.extend(["", "### Created Missions", ""])
        for item in created:
            lines.append(
                f"- draft {item.get('draft_index')}: `{item.get('mission_id')}` "
                f"[{item.get('status')}/{item.get('stop_reason')}] {item.get('objective')}"
            )
    queued = payload.get("queued_items") if isinstance(payload.get("queued_items"), list) else []
    if queued:
        lines.extend(["", "### Queued Items", ""])
        for item in queued:
            lines.append(f"- draft {item.get('draft_index')}: `{item.get('queue_id')}` -> `{item.get('status')}`")
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    if warnings:
        lines.extend(["", "### Warnings", ""])
        for warning in warnings:
            lines.append(f"- {warning}")
    return "\n".join(lines).rstrip()


def payload_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2)


def _append_task_candidate(
    candidates: list[dict[str, Any]],
    *,
    objective: str,
    section: str,
    source_line: int,
    source_type: str,
    raw: str,
) -> None:
    if not _is_actionable_objective(objective):
        return
    if section and section.lower() not in objective.lower() and len(objective) + len(section) < 220:
        objective = f"{section}: {objective}"
    candidates.append(
        {
            "objective": objective.strip(),
            "source_line": source_line,
            "source_type": source_type,
            "section": section,
            "raw": raw,
        }
    )


def _clean_objective(text: str) -> str:
    value = str(text).strip()
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"[*~]+", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" \t-:：。.;；")


def _is_actionable_heading(text: str) -> bool:
    lower = text.lower()
    if lower in {"overview", "background", "notes", "summary", "done", "completed", "已完成", "背景", "说明"}:
        return False
    return _is_actionable_objective(text)


def _is_actionable_objective(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 6:
        return False
    if _looks_like_question_prompt(stripped):
        return False
    lower = stripped.lower()
    if lower.startswith(("note:", "notes:", "risk:", "risks:", "warning:", "参考", "备注")):
        return False
    if re.fullmatch(r"[-=*_ ]+", stripped):
        return False
    return True


def _looks_like_question_prompt(text: str) -> bool:
    stripped = text.strip().strip('"“”「」『』`')
    if stripped.endswith(("?", "？")):
        return True
    return False
