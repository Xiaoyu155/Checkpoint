"""Layer 3 verification: milestone checkpoint for human review.

A milestone is a batch of tasks that together deliver a user-visible outcome
(e.g. "login flow", "voice session history").  When those tasks finish,
the tool should NOT ask the user to review every task's diff — instead it
generates one compressed review sheet that lists what happened, 2-3 things
the user should manually verify, and how to report problems.

The user only needs to make a strategic go/no-go decision, not a code review.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .diff_summary import build_diff_summary, format_diff_summary


def generate_milestone_checkpoint(
    *,
    milestone_label: str,
    completed_tasks: list[dict[str, Any]],
    workspace_root: str | Path,
    repo_root: str | Path | None = None,
    base_ref: str | None = None,
) -> dict[str, Any]:
    """Aggregate completed tasks into a single human review sheet.

    Args:
        milestone_label: Human-readable name, e.g. "登录流程" or "Phase-1"
        completed_tasks: List of task dicts from program / queue.
                         Each should have at minimum: objective, status,
                         and optionally diff_summary (pre-computed) or
                         worktree path for live diff computation.
        workspace_root: DevPacer workspace root for saving the checkpoint.
        repo_root: If given, compute a live diff_summary from the repo now.
        base_ref: Git ref to diff against (defaults to HEAD).

    Returns dict with:
        milestone_label, generated_at, task_count, verified_count,
        what_was_done, what_to_check, how_to_report,
        estimated_check_minutes, diff_summary (aggregated or live),
        markdown (full formatted text).
    """
    now = datetime.now(timezone.utc).isoformat()

    verified = [t for t in completed_tasks if str(t.get("status") or "") == "verified"]
    not_verified = [t for t in completed_tasks if str(t.get("status") or "") != "verified"]

    what_was_done = _summarise_tasks(completed_tasks)

    # Aggregate diff summaries from individual tasks, or compute a fresh one.
    agg_diff: dict[str, Any] = _aggregate_diff_summaries(completed_tasks)
    if repo_root and not agg_diff.get("file_count"):
        try:
            agg_diff = build_diff_summary(repo_root=repo_root, base_ref=base_ref)
        except Exception:  # noqa: BLE001
            pass

    what_to_check = _build_review_checklist(completed_tasks, agg_diff)
    how_to_report = _build_report_guide(completed_tasks)
    minutes = _estimate_check_minutes(agg_diff, len(what_to_check))

    checkpoint: dict[str, Any] = {
        "milestone_label": milestone_label,
        "generated_at": now,
        "task_count": len(completed_tasks),
        "verified_count": len(verified),
        "unverified_count": len(not_verified),
        "what_was_done": what_was_done,
        "what_to_check": what_to_check,
        "how_to_report": how_to_report,
        "estimated_check_minutes": minutes,
        "diff_summary": agg_diff,
    }
    checkpoint["markdown"] = format_milestone_checkpoint(checkpoint)

    # Persist to workspace.
    saved_path = _save_checkpoint(checkpoint, workspace_root=workspace_root, label=milestone_label)
    checkpoint["saved_path"] = saved_path

    return checkpoint


def format_milestone_checkpoint(checkpoint: dict[str, Any]) -> str:
    """Render the checkpoint as Markdown for dashboard or workbench display."""
    label = checkpoint.get("milestone_label") or "里程碑"
    tc = checkpoint.get("task_count", 0)
    vc = checkpoint.get("verified_count", 0)
    uc = checkpoint.get("unverified_count", 0)
    minutes = checkpoint.get("estimated_check_minutes", 3)

    lines: list[str] = [
        f"## 里程碑核查单：{label}",
        "",
        f"**任务数**: {tc}　**已验收**: {vc}　**未验收**: {uc}　"
        f"**预计核查时间**: {minutes} 分钟",
        "",
    ]

    # What was done
    done_items: list[str] = checkpoint.get("what_was_done") or []
    if done_items:
        lines += ["### 这一批做了什么", ""]
        for item in done_items:
            lines.append(f"- {item}")
        lines.append("")

    # What to check
    check_items: list[str] = checkpoint.get("what_to_check") or []
    if check_items:
        lines += ["### 你需要做的（最少核查）", ""]
        for i, item in enumerate(check_items, 1):
            lines.append(f"{i}. {item}")
        lines.append("")

    # How to report
    report_items: list[str] = checkpoint.get("how_to_report") or []
    if report_items:
        lines += ["### 如果有问题，怎么告诉工具", ""]
        for item in report_items:
            lines.append(f"- {item}")
        lines.append("")

    # Diff summary
    diff = checkpoint.get("diff_summary")
    if diff and diff.get("file_count"):
        lines.append(format_diff_summary(diff))

    return "\n".join(lines).rstrip()


# ── internals ─────────────────────────────────────────────────────────────────


def _summarise_tasks(tasks: list[dict[str, Any]]) -> list[str]:
    items: list[str] = []
    for task in tasks:
        obj = str(task.get("objective") or task.get("goal") or "").strip()
        status = str(task.get("status") or "").strip()
        tag = "✅" if status == "verified" else ("❌" if status in {"verification_failed", "worker_failed"} else "⏳")
        if obj:
            items.append(f"{tag} {obj}")
    return items or ["（任务目标未记录）"]


def _aggregate_diff_summaries(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge per-task diff_summary dicts into one aggregated view."""
    file_map: dict[str, dict[str, Any]] = {}
    total_added = 0
    total_removed = 0
    all_symbols: list[dict[str, str]] = []
    all_checklist: list[str] = []
    checklist_seen: set[str] = set()

    for task in tasks:
        ds = task.get("diff_summary") if isinstance(task.get("diff_summary"), dict) else None
        if not ds:
            continue
        total_added += int(ds.get("lines_added") or 0)
        total_removed += int(ds.get("lines_removed") or 0)
        for f in ds.get("changed_files") or []:
            path = str(f.get("path") or "")
            if path and path not in file_map:
                file_map[path] = f
        for sym in ds.get("functions_touched") or []:
            name = str(sym.get("name") or "")
            if name and not any(s["name"] == name for s in all_symbols):
                all_symbols.append(sym)
        for hint in ds.get("user_checklist") or []:
            if hint not in checklist_seen:
                checklist_seen.add(hint)
                all_checklist.append(hint)

    file_count = len(file_map)
    if not file_count:
        return {}

    large_diff = file_count > 40 or (total_added + total_removed) > 2000
    return {
        "file_count": file_count,
        "lines_added": total_added,
        "lines_removed": total_removed,
        "large_diff": large_diff,
        "changed_files": sorted(file_map.values(), key=lambda f: f["path"]),
        "functions_touched": all_symbols[:20],
        "user_checklist": all_checklist,
        "summary_text": (
            f"里程碑共涉及 {file_count} 个文件，"
            f"新增 {total_added} 行，删除 {total_removed} 行。"
            + ("改动体量较大，请认真审查。" if large_diff else "体量正常。")
        ),
    }


def _build_review_checklist(
    tasks: list[dict[str, Any]],
    agg_diff: dict[str, Any],
) -> list[str]:
    """Build 1-3 concrete human actions for the milestone."""
    checklist: list[str] = []

    # Pull from aggregated diff checklist
    for hint in (agg_diff.get("user_checklist") or [])[:3]:
        checklist.append(hint)

    # If some tasks were not verified, add a specific prompt
    unverified = [t for t in tasks if str(t.get("status") or "") not in {"verified"}]
    if unverified:
        objs = "、".join(
            str(t.get("objective") or t.get("goal") or "未知任务") for t in unverified[:2]
        )
        suffix = f"等 {len(unverified)} 个任务" if len(unverified) > 2 else ""
        checklist.append(f"以下任务未通过自动验收，需重点关注：{objs}{suffix}")

    return checklist or ["运行应用，操作本里程碑涉及的功能，确认行为符合预期"]


def _build_report_guide(tasks: list[dict[str, Any]]) -> list[str]:
    return [
        "如果运行时崩溃或出现错误弹窗，告诉我错误信息或截图",
        "如果功能缺失或行为异常，描述「你做了什么」和「实际看到了什么」",
        "如果看起来正常，只需告诉我「通过」即可继续下一批任务",
    ]


def _estimate_check_minutes(agg_diff: dict[str, Any], checklist_len: int) -> int:
    file_count = int(agg_diff.get("file_count") or 0)
    base = 2
    if file_count > 20:
        base = 5
    elif file_count > 5:
        base = 3
    return base + max(0, checklist_len - 1)


def _save_checkpoint(
    checkpoint: dict[str, Any],
    *,
    workspace_root: str | Path,
    label: str,
) -> str:
    try:
        ws = Path(workspace_root).expanduser().resolve()
        milestones_dir = ws / "milestones"
        milestones_dir.mkdir(parents=True, exist_ok=True)
        safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        md_path = milestones_dir / f"{ts}_{safe_label}.md"
        json_path = milestones_dir / f"{ts}_{safe_label}.json"
        md_path.write_text(str(checkpoint.get("markdown") or ""), encoding="utf-8")
        exportable = {k: v for k, v in checkpoint.items() if k != "markdown"}
        json_path.write_text(
            json.dumps(exportable, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return str(md_path)
    except OSError:
        return ""
