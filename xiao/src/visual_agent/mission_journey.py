from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .chief_plans_store import (
    load_dispatch_records,
    load_plan,
    load_verification,
    load_worker_records,
    plan_dir,
)
from .mission_progress import load_mission_progress
from .missions import load_mission, load_rounds, mission_dir, missions_dir
from .models import to_jsonable


PHASE_ORDER = ("routing", "memory", "managed", "acceptance", "delivery")


def mission_journey_path(workspace_root: str | Path, mission_id: str) -> Path:
    return mission_dir(workspace_root, mission_id) / "journey.json"


def build_mission_journey(
    *,
    workspace_root: str | Path,
    mission_id: str,
    mission: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    dispatch: dict[str, Any] | None = None,
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(workspace_root).expanduser().resolve()
    mission_payload = mission if isinstance(mission, dict) else load_mission(root, mission_id)
    if not mission_payload:
        return {
            "schema_version": 1,
            "mission_id": str(mission_id),
            "status": "blocked",
            "continuity_status": "broken",
            "can_claim_verified": False,
            "can_claim_delivered": False,
            "summary": "任务记录不存在，无法重建闭环。",
            "phases": [],
            "links": [],
            "reason_codes": ["mission_missing"],
        }

    mid = str(mission_payload.get("mission_id") or mission_id)
    plan_id = str(mission_payload.get("plan_id") or "")
    plan_payload = plan if isinstance(plan, dict) and plan else (load_plan(root, plan_id) or {})
    workers = load_worker_records(root, plan_id) if plan_id else []
    verification = load_verification(root, plan_id) if plan_id else None
    verification = verification if isinstance(verification, dict) else {}
    dispatch_records = load_dispatch_records(root, plan_id) if plan_id else []
    dispatch_payload = dispatch if isinstance(dispatch, dict) else {}
    dispatch_record = _current_dispatch_record(dispatch_payload, dispatch_records)
    progress_payload = progress if isinstance(progress, dict) else load_mission_progress(root, mid)
    rounds = load_rounds(root, mid)

    routing = _routing_phase(plan_payload, workers, dispatch_payload, dispatch_record)
    memory = _memory_phase(
        plan_payload,
        dispatch_payload,
        dispatch_record,
        executed=bool(workers or dispatch_record),
    )
    memory_ids = (memory.get("details") or {}).get("memory_ids") or []
    if memory_ids:
        labels = _resolve_memory_entry_labels(root, memory_ids)
        if labels:
            memory = {**memory, "details": {**memory["details"], "memory_entry_labels": labels}}
    managed = _managed_phase(mission_payload, workers, dispatch_payload, dispatch_record, progress_payload)
    acceptance = _acceptance_phase(mission_payload, verification, dispatch_payload)
    delivery = _delivery_phase(
        root=root,
        mission=mission_payload,
        dispatch=dispatch_payload,
        dispatch_record=dispatch_record,
        rounds=rounds,
    )
    phases = [routing, memory, managed, acceptance, delivery]
    links = _continuity_links(
        mission=mission_payload,
        plan_id=plan_id,
        workers=workers,
        verification=verification,
        dispatch_record=dispatch_record,
        phases={item["id"]: item for item in phases},
    )
    continuity_status = _continuity_status(links)
    phase_map = {item["id"]: item for item in phases}
    can_claim_verified = (
        phase_map["routing"]["status"] in {"passed", "incomplete"}
        and phase_map["managed"]["status"] == "passed"
        and phase_map["acceptance"]["status"] == "passed"
        and phase_map["memory"]["status"] not in {"blocked", "failed"}
        and not any(item["status"] == "broken" for item in links[:3])
    )
    can_claim_delivered = (
        can_claim_verified
        and phase_map["routing"]["status"] == "passed"
        and phase_map["delivery"]["status"] == "passed"
    )
    status = _journey_status(
        mission=mission_payload,
        phases=phase_map,
        can_claim_verified=can_claim_verified,
        can_claim_delivered=can_claim_delivered,
    )
    current_phase = next(
        (item["id"] for item in phases if item["status"] not in {"passed", "not_applicable", "incomplete"}),
        "delivery",
    )
    reason_codes = list(
        dict.fromkeys(
            code
            for item in [*phases, *links]
            for code in item.get("reason_codes") or []
            if str(code)
        )
    )
    result = {
        "schema_version": 1,
        "product": "Pacer",
        "kind": "mission_journey",
        "generated_at": _utc_now(),
        "workspace_root": str(root),
        "mission_id": mid,
        "plan_id": plan_id,
        "objective": str(mission_payload.get("objective") or ""),
        "mission_status": str(mission_payload.get("status") or ""),
        "stop_reason": str(mission_payload.get("stop_reason") or ""),
        "status": status,
        "current_phase": current_phase,
        "continuity_status": continuity_status,
        "can_claim_verified": can_claim_verified,
        "can_claim_delivered": can_claim_delivered,
        "summary": _journey_summary(phases),
        "next_action": _next_action(status, delivery),
        "reason_codes": reason_codes,
        "phases": phases,
        "links": links,
        "evidence": {
            "mission": str(mission_dir(root, mid) / "mission.json"),
            "progress": str(mission_dir(root, mid) / "progress.json"),
            "plan": str(plan_dir(root, plan_id) / "plan.json") if plan_id else "",
            "dispatches": str(plan_dir(root, plan_id) / "dispatches.jsonl") if plan_id else "",
            "verification": str(plan_dir(root, plan_id) / "verification.json") if plan_id else "",
            "journey": str(mission_journey_path(root, mid)),
        },
    }
    if progress_payload:
        result["last_activity_at"] = str(progress_payload.get("last_activity_at") or "")
    return result


def save_mission_journey(
    workspace_root: str | Path,
    mission_id: str,
    journey: dict[str, Any],
) -> dict[str, Any]:
    path = mission_journey_path(workspace_root, mission_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(to_jsonable(journey), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        last_error: OSError | None = None
        for attempt in range(12):
            try:
                os.replace(temporary, path)
                last_error = None
                break
            except OSError as exc:
                last_error = exc
                winerr = getattr(exc, "winerror", None)
                if winerr not in {5, 32} and not isinstance(exc, PermissionError):
                    raise
                time.sleep(0.05 * (attempt + 1))
        if last_error is not None:
            raise last_error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return {"path": str(path), "journey": journey}


def build_latest_mission_journey(workspace_root: str | Path) -> dict[str, Any]:
    root = missions_dir(workspace_root)
    if not root.is_dir():
        return {}
    candidates = sorted(
        root.glob("*/mission.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            mission = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(mission, dict) or bool(mission.get("hidden")):
            continue
        mission_id = str(mission.get("mission_id") or path.parent.name)
        return build_mission_journey(
            workspace_root=Path(workspace_root),
            mission_id=mission_id,
            mission=mission,
        )
    return {}


def mission_journey_to_markdown(journey: dict[str, Any]) -> str:
    if not journey or not journey.get("phases"):
        return "闭环证据暂不可用。"
    lines = [
        f"闭环：**{journey.get('status') or 'unknown'}**",
        f"- {journey.get('summary') or ''}",
    ]
    if journey.get("next_action"):
        lines.append(f"- 下一步：{journey.get('next_action')}")
    if journey.get("continuity_status") == "broken":
        lines.append("- 证据链断开，Pacer 不会把这次任务当作完整交付。")
    phases = journey.get("phases") if isinstance(journey.get("phases"), list) else []
    for phase in phases:
        if not isinstance(phase, dict) or phase.get("id") != "memory":
            continue
        labels = (phase.get("details") or {}).get("memory_entry_labels") or []
        if labels:
            lines.append("- 注入的记忆条目：")
            for entry in labels[:6]:
                obj = str(entry.get("objective") or "")[:80]
                lines.append(f"  - `{entry.get('id')}` {obj}")
        break
    return "\n".join(lines)


_PHASE_MARKS = {
    "passed": "✓",
    "incomplete": "!",
    "ready": "·",
    "not_applicable": "-",
    "pending": "·",
    "blocked": "✗",
    "failed": "✗",
}


def mission_journey_report(journey: dict[str, Any]) -> str:
    """Full human-readable evidence chain for one mission.

    ``mission_journey_to_markdown`` is the one-glance form used inside chat
    replies; this is the version behind ``pacer journey``, so it spells out every
    phase and where the evidence lives.
    """

    if not journey or not journey.get("phases"):
        return "闭环证据暂不可用：这个任务还没有 journey 记录。"
    lines = [
        f"# 任务闭环 · {journey.get('mission_id') or ''}",
        "",
        f"- 目标：{journey.get('objective') or ''}",
        f"- 状态：**{journey.get('status') or 'unknown'}**（{journey.get('summary') or ''}）",
        f"- 可以声称通过验收：`{bool(journey.get('can_claim_verified'))}` · 可以声称已交付：`{bool(journey.get('can_claim_delivered'))}`",
    ]
    if journey.get("next_action"):
        lines.append(f"- 下一步：{journey.get('next_action')}")
    if journey.get("continuity_status") == "broken":
        lines.append("- ⚠ 证据链断开，Pacer 不会把这次任务当作完整交付。")
    lines.append("")
    phases = journey.get("phases") if isinstance(journey.get("phases"), list) else []
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        status = str(phase.get("status") or "")
        mark = _PHASE_MARKS.get(status, "·")
        lines.append(f"## {mark} {phase.get('title') or phase.get('id')} — {status}")
        if phase.get("summary"):
            lines.append(f"- {phase.get('summary')}")
        details = phase.get("details") if isinstance(phase.get("details"), dict) else {}
        for key, value in details.items():
            if value in ("", None, [], {}, False, 0):
                continue
            if key == "memory_entry_labels" and isinstance(value, list):
                lines.append("- 注入的记忆条目：")
                for entry in value[:8]:
                    if isinstance(entry, dict):
                        lines.append(f"  - `{entry.get('id')}` {str(entry.get('objective') or '')[:80]}")
                continue
            lines.append(f"- {key}: `{value}`")
        if phase.get("reason_codes"):
            lines.append(f"- 原因码：{', '.join(str(code) for code in phase['reason_codes'])}")
        lines.append("")
    if journey.get("workspace_root"):
        lines.append(f"证据目录：`{journey.get('workspace_root')}`")
    return "\n".join(lines)


def _routing_phase(
    plan: dict[str, Any],
    workers: list[dict[str, Any]],
    dispatch: dict[str, Any],
    record: dict[str, Any],
) -> dict[str, Any]:
    worker = _latest_worker(workers, dispatch)
    tracks = plan.get("worker_tracks") if isinstance(plan.get("worker_tracks"), list) else []
    selected = next((item for item in tracks if isinstance(item, dict) and item.get("track_kind") != "inspection"), {})
    expected_agent = str(selected.get("agent") or "")
    actual_agent = str(worker.get("agent") or "")
    provider = str(worker.get("resolved_provider") or record.get("resolved_provider") or "")
    model = str(worker.get("resolved_model") or record.get("resolved_model") or "")
    routing_evidence = (
        (dispatch.get("managed_runtime") or {}).get("routing_evidence")
        if isinstance(dispatch.get("managed_runtime"), dict)
        else {}
    )
    if not isinstance(routing_evidence, dict):
        routing_evidence = {}
    if not routing_evidence and isinstance(record.get("managed_runtime"), dict):
        persisted_routing = record["managed_runtime"].get("routing_evidence")
        routing_evidence = persisted_routing if isinstance(persisted_routing, dict) else {}
    if not routing_evidence and isinstance(worker.get("routing_evidence"), dict):
        routing_evidence = worker["routing_evidence"]
    request = routing_evidence.get("request") if isinstance(routing_evidence.get("request"), dict) else {}
    requested_provider = str(request.get("provider") or "")
    requested_model = str(request.get("model") or "")
    request_matches = (not requested_provider or requested_provider == provider) and (
        not requested_model or requested_model == model
    )
    if worker and (not expected_agent or expected_agent == actual_agent) and provider and model and request_matches:
        status = "passed"
        reasons: list[str] = []
    elif worker:
        if expected_agent and expected_agent != actual_agent:
            status = "blocked"
            reasons = ["routing_worker_mismatch"]
        elif not request_matches:
            status = "blocked"
            reasons = ["routing_request_mismatch"]
        else:
            # Worker ran, but the record does not say which provider/model served
            # it. Acceptance is independent of this, so the mission can still be
            # called verified — but the routing pillar must not read as passed,
            # and Pacer will not claim delivery on an unproven routing chain.
            status = "incomplete"
            reasons = ["routing_identity_missing"]
    elif plan:
        status = "ready"
        reasons = ["routing_not_executed"]
    else:
        status = "pending"
        reasons = ["routing_plan_missing"]
    return {
        "id": "routing",
        "pillar": "routing",
        "title": "路由选择",
        "status": status,
        "summary": f"{actual_agent or expected_agent or 'worker'} · {provider or '待定'} / {model or '待定'}",
        "reason_codes": reasons,
        "details": {
            "expected_agent": expected_agent,
            "actual_agent": actual_agent,
            "provider": provider,
            "model": model,
            "decision_id": str(routing_evidence.get("decision_id") or ""),
            "requested_provider": requested_provider,
            "requested_model": requested_model,
        },
    }


def _resolve_memory_entry_labels(root: Path, memory_ids: list[str]) -> list[dict[str, str]]:
    labels: list[dict[str, str]] = []
    for mid in memory_ids[:10]:
        mission_id = mid[len("mission:"):] if mid.startswith("mission:") else mid
        try:
            m = load_mission(root, mission_id)
            if isinstance(m, dict):
                labels.append({
                    "id": mid,
                    "objective": str(m.get("objective") or ""),
                    "status": str(m.get("status") or ""),
                })
        except Exception:
            labels.append({"id": mid, "objective": "", "status": ""})
    return labels


def _memory_phase(
    plan: dict[str, Any],
    dispatch: dict[str, Any],
    record: dict[str, Any],
    *,
    executed: bool,
) -> dict[str, Any]:
    usage = dispatch.get("project_memory_usage") if isinstance(dispatch.get("project_memory_usage"), dict) else {}
    if not usage and isinstance(record.get("project_memory_usage"), dict):
        usage = record["project_memory_usage"]
    if not usage:
        memory = plan.get("project_memory") if isinstance(plan.get("project_memory"), dict) else {}
        usage = memory.get("usage") if isinstance(memory.get("usage"), dict) else {}
    mode = str(usage.get("memory_mode") or "enabled")
    selected = int(usage.get("selected_entries") or 0)
    injected = bool(usage.get("dispatch_injected"))
    selected_ids = [str(item) for item in usage.get("injected_memory_ids") or [] if str(item)]
    dispatch_ids = [str(item) for item in usage.get("dispatch_memory_ids") or [] if str(item)]
    ids_match = bool(selected_ids) and set(selected_ids).issubset(set(dispatch_ids))
    if mode == "disabled":
        status = "not_applicable"
        summary = "本次策略关闭了本地记忆。"
        reasons = ["memory_disabled"]
    elif selected == 0:
        status = "not_applicable"
        summary = "没有相关历史，按新任务启动。"
        reasons = ["memory_lookup_empty"]
    elif injected and ids_match:
        status = "passed"
        summary = f"已把 {len(dispatch_ids)} 条相关本地记忆交给 worker。"
        reasons = []
    elif not executed:
        status = "ready"
        summary = f"已找到 {selected} 条相关记忆，等待随 worker 一起交接。"
        reasons = ["memory_waiting_for_dispatch"]
    else:
        status = "blocked"
        summary = "找到了相关记忆，但没有完整进入实际 worker。"
        reasons = ["memory_dispatch_chain_broken"]
    return {
        "id": "memory",
        "pillar": "memory",
        "title": "本地记忆",
        "status": status,
        "summary": summary,
        "reason_codes": reasons,
        "details": {
            "memory_mode": mode,
            "selected_entries": selected,
            "dispatch_injected": injected,
            "memory_ids": dispatch_ids,
        },
    }


def _managed_phase(
    mission: dict[str, Any],
    workers: list[dict[str, Any]],
    dispatch: dict[str, Any],
    record: dict[str, Any],
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    worker = _latest_worker(workers, dispatch)
    progress_payload = progress if isinstance(progress, dict) else {}
    mission_status = str(mission.get("status") or "")
    dispatch_status = str(dispatch.get("status") or record.get("status") or "")
    worker_status = str(worker.get("status") or "")
    progress_worker_status = str(progress_payload.get("worker_status") or "")
    progress_worker_active = (
        bool(progress_payload.get("background_alive"))
        or str(progress_payload.get("stage") or "") in {"worker_starting", "worker_running"}
        or str(progress_payload.get("activity") or "") in {"worker_executing", "waiting_for_output"}
    )
    managed_runtime = dispatch.get("managed_runtime") if isinstance(dispatch.get("managed_runtime"), dict) else {}
    if not managed_runtime and isinstance(record.get("managed_runtime"), dict):
        managed_runtime = record["managed_runtime"]
    if mission_status in {"verified", "merged"} and dispatch_status in {"verified", "merged"}:
        status = "passed"
        reasons: list[str] = (
            [] if worker_status == "completed" else ["managed_worker_unclean_but_accepted"]
        )
        summary = (
            f"worker={worker_status or '未报告完成'} · 强验收已接管最终结论"
            if worker_status != "completed"
            else f"worker={worker_status} · mission={mission_status}"
        )
    elif mission_status in {"background_running", "running", "created", "preview"}:
        reported_worker_status = worker_status or progress_worker_status or ("running" if progress_worker_active else "")
        status = "active" if worker or progress_worker_active else "ready"
        reasons = ["managed_in_progress"]
        summary = f"worker={reported_worker_status or '未启动'} · mission={mission_status or '未知'}"
    elif worker_status in {"failed", "crashed", "blocked"} or mission_status in {"failed", "blocked", "stopped"}:
        status = "blocked"
        reasons = ["managed_worker_failed"]
        summary = f"worker={worker_status or '未启动'} · mission={mission_status or '未知'}"
    else:
        status = "pending"
        reasons = ["managed_not_started"]
        summary = f"worker={worker_status or '未启动'} · mission={mission_status or '未知'}"
    budget_status = str(managed_runtime.get("budget_status") or "")
    if budget_status == "exhausted":
        reasons.append(
            "managed_budget_exhausted_after_completion"
            if status == "passed"
            else "managed_budget_exhausted"
        )
        summary += " · budget=exhausted"
    elif budget_status == "usage_unknown":
        reasons.append("managed_usage_unknown")
        summary += " · budget=usage_unknown"
    return {
        "id": "managed",
        "pillar": "managed",
        "title": "托管执行",
        "status": status,
        "summary": summary,
        "reason_codes": reasons,
        "details": {
            "worker_status": worker_status,
            "progress_worker_status": progress_worker_status,
            "worker_attempts": int(record.get("worker_attempts") or len(workers)),
            "dispatch_status": dispatch_status,
            "idempotency_key": str(managed_runtime.get("idempotency_key") or ""),
            "budget_status": budget_status,
        },
    }


def _acceptance_phase(
    mission: dict[str, Any],
    verification: dict[str, Any],
    dispatch: dict[str, Any],
) -> dict[str, Any]:
    latest = dispatch.get("latest_verification") if isinstance(dispatch.get("latest_verification"), dict) else verification
    latest = latest if isinstance(latest, dict) else {}
    command = latest.get("command_verification") if isinstance(latest.get("command_verification"), dict) else {}
    verdict = str(latest.get("verdict") or command.get("verdict") or "")
    # Records written before acceptance grading existed carry no tier; they keep
    # their historical reading rather than being retroactively downgraded.
    grade = latest.get("acceptance") if isinstance(latest.get("acceptance"), dict) else {}
    tier = str(grade.get("tier") or "")
    if verdict == "pass" and tier and tier != "verified":
        # The gate passed but did not prove the objective. Saying "passed" here
        # is exactly the false green this grading exists to stop.
        status = "incomplete"
        reasons = [str(grade.get("reason_code") or "acceptance_not_discriminating")]
        summary = f"只证明没弄坏：{command.get('command') or 'Checkpoint workflow'}"
    elif verdict == "pass":
        status = "passed"
        reasons: list[str] = []
        summary = f"验收通过：{command.get('command') or 'Checkpoint workflow'}"
    elif verdict in {"fail", "inspection_only", "coverage_gap"}:
        status = "blocked"
        reasons = [f"acceptance_{verdict}"]
        summary = f"验收未通过：{verdict}"
    elif str(mission.get("status") or "") in {"running", "background_running"}:
        status = "active"
        reasons = ["acceptance_waiting"]
        summary = "等待托管执行完成后验收。"
    else:
        status = "pending"
        reasons = ["acceptance_missing"]
        summary = "还没有可信验收结果。"
    return {
        "id": "acceptance",
        "pillar": "acceptance",
        "title": "强验收",
        "status": status,
        "summary": summary,
        "reason_codes": reasons,
        "details": {
            "verdict": verdict,
            "command": str(command.get("command") or ""),
            "exit_code": command.get("exit_code"),
            # ``run_profile`` describes the Checkpoint workflow profile, not the
            # test command. Reporting it next to a command that really ran made
            # passing acceptance read as "dry-run"; state execution directly.
            "executed": bool(command.get("command")),
            "workflow_run_profile": str(latest.get("run_profile") or ""),
            "acceptance_tier": tier,
            "acceptance_message": str(grade.get("message") or ""),
            "gate_discriminating": grade.get("discriminating"),
        },
    }


def _delivery_phase(
    *,
    root: Path,
    mission: dict[str, Any],
    dispatch: dict[str, Any],
    dispatch_record: dict[str, Any],
    rounds: list[dict[str, Any]],
) -> dict[str, Any]:
    merge = dispatch.get("merge") if isinstance(dispatch.get("merge"), dict) else {}
    if not merge and isinstance(dispatch_record.get("merge"), dict):
        merge = dispatch_record["merge"]
    if not merge:
        merge = next(
            (
                {
                    "status": str(item.get("status") or ""),
                    "reason": str(item.get("reason") or ""),
                    "commit": str(item.get("commit") or ""),
                }
                for item in reversed(rounds)
                if str(item.get("type") or "") == "merge"
            ),
            {},
        )
    merge_status = str(merge.get("status") or "")
    verified = str(mission.get("status") or "") in {"verified", "merged"}
    pacer_repo = _is_pacer_repo(Path(str(mission.get("repo_root") or root)))
    dogfood = _dogfood_binding(root, str(mission.get("mission_id") or "")) if pacer_repo else {}
    if pacer_repo and dogfood.get("passed"):
        status = "passed"
        summary = "Pacer 自身改动已绑定严格 Dogfood 证据。"
        reasons: list[str] = []
    elif pacer_repo and verified:
        status = "partial"
        summary = "代码已验收，但本 mission 尚未绑定严格 Dogfood 发布证据。"
        reasons = ["dogfood_evidence_not_bound"]
    elif merge_status in {"merged", "nothing_to_merge"}:
        status = "passed"
        summary = "验收结果已交付到目标分支。"
        reasons = []
    elif verified and not bool(mission.get("merge")):
        status = "ready"
        summary = "验收已通过，等待用户确认合并。"
        reasons = ["delivery_waiting_for_merge"]
    elif verified:
        status = "blocked"
        summary = str(merge.get("reason") or "请求了合并，但交付未完成。")
        reasons = ["delivery_merge_failed"]
    else:
        status = "pending"
        summary = "验收通过后才会进入交付。"
        reasons = ["delivery_waiting_for_acceptance"]
    return {
        "id": "delivery",
        "pillar": "dogfood",
        "title": "交付 / Dogfood" if pacer_repo else "结果交付",
        "status": status,
        "summary": summary,
        "reason_codes": reasons,
        "details": {
            "pacer_repo": pacer_repo,
            "merge_requested": bool(mission.get("merge")),
            "merge_status": merge_status,
            "merge_commit": str(merge.get("commit") or ""),
            "dogfood_launch_id": str(dogfood.get("launch_id") or ""),
        },
    }


def _continuity_links(
    *,
    mission: dict[str, Any],
    plan_id: str,
    workers: list[dict[str, Any]],
    verification: dict[str, Any],
    dispatch_record: dict[str, Any],
    phases: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    mission_id = str(mission.get("mission_id") or "")
    latest_worker = workers[-1] if workers else {}
    dispatch_mission = str(dispatch_record.get("mission_id") or mission_id)
    routing_status = str(phases["routing"].get("status") or "")
    managed_status = str(phases["managed"].get("status") or "")
    acceptance_status = str(phases["acceptance"].get("status") or "")
    delivery_status = str(phases["delivery"].get("status") or "")
    routing_ok = routing_status in {"passed", "incomplete"} and bool(latest_worker)
    memory_selected = int((phases["memory"].get("details") or {}).get("selected_entries") or 0)
    memory_ok = phases["memory"]["status"] == "passed" if memory_selected else True
    verification_plan = str(verification.get("plan_id") or plan_id)
    managed_ok = phases["managed"]["status"] == "passed"
    acceptance_ok = phases["acceptance"]["status"] == "passed"
    return [
        _link(
            "routing_to_managed",
            routing_ok and dispatch_mission == mission_id,
            "路由决定已由同一 mission 的 worker 执行。",
            "routing_dispatch_mismatch",
            pending=routing_status in {"ready", "pending", "active"} and managed_status in {"ready", "pending", "active"},
        ),
        _link(
            "memory_to_managed",
            memory_ok,
            "相关记忆已进入同一次 worker handoff。" if memory_selected else "没有相关历史，本次无需记忆交接。",
            "memory_dispatch_chain_broken",
            not_applicable=memory_selected == 0,
            pending=phases["memory"]["status"] == "ready" and managed_status in {"ready", "pending", "active"},
        ),
        _link(
            "managed_to_acceptance",
            managed_ok and acceptance_ok and verification_plan == plan_id,
            (
                "验收门跑过了，但它证明不了这次目标达成。"
                if acceptance_status == "incomplete"
                else "worker 产物由同一 plan 的验收门检查。"
            ),
            "managed_acceptance_chain_broken",
            # An acceptance gate that ran but did not discriminate is weak
            # evidence, not a broken chain.
            partial=managed_ok and acceptance_status == "incomplete",
            pending=managed_status in {"ready", "pending", "active"} or acceptance_status in {"pending", "active"},
        ),
        _link(
            "acceptance_to_delivery",
            acceptance_ok and phases["delivery"]["status"] in {"passed", "ready"},
            phases["delivery"]["summary"],
            "acceptance_delivery_chain_pending",
            ready=delivery_status == "ready",
            pending=acceptance_status in {"pending", "active"},
            # Weak acceptance evidence blocks the delivery claim, but the chain
            # itself is not broken — the user just has to decide.
            partial=(acceptance_ok and delivery_status == "partial") or acceptance_status == "incomplete",
        ),
    ]


def _link(
    link_id: str,
    ok: bool,
    summary: str,
    failure_code: str,
    *,
    not_applicable: bool = False,
    ready: bool = False,
    pending: bool = False,
    partial: bool = False,
) -> dict[str, Any]:
    if not_applicable:
        status = "not_applicable"
        reasons: list[str] = []
    elif pending:
        status = "pending"
        reasons = []
    elif partial:
        status = "partial"
        reasons = []
    elif ok and ready:
        status = "ready"
        reasons = []
    elif ok:
        status = "passed"
        reasons = []
    else:
        status = "broken"
        reasons = [failure_code]
    return {"id": link_id, "status": status, "summary": summary, "reason_codes": reasons}


def _continuity_status(links: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("status") or "") for item in links}
    if "broken" in statuses:
        return "broken"
    if "pending" in statuses:
        return "in_progress"
    if statuses & {"ready", "partial"}:
        return "connected_pending_delivery"
    return "connected"


def _journey_status(
    *,
    mission: dict[str, Any],
    phases: dict[str, dict[str, Any]],
    can_claim_verified: bool,
    can_claim_delivered: bool,
) -> str:
    if can_claim_delivered:
        return "completed"
    if can_claim_verified:
        return "verified_pending_dogfood" if phases["delivery"]["status"] == "partial" else "verified_pending_delivery"
    if any(item["status"] in {"blocked", "failed"} for item in phases.values()):
        return "blocked"
    if phases["acceptance"]["status"] == "incomplete":
        # The work ran and broke nothing, but nothing proved the objective.
        return "regression_clear"
    if str(mission.get("status") or "") in {"running", "background_running", "created", "preview"}:
        return "in_progress"
    return "partial"


def _journey_summary(phases: list[dict[str, Any]]) -> str:
    labels = {
        "passed": "已完成",
        "ready": "待确认",
        "active": "进行中",
        "pending": "等待中",
        "blocked": "被阻塞",
        "failed": "失败",
        "partial": "证据待补",
        "incomplete": "证据不完整",
        "not_applicable": "本次无需",
    }
    return " → ".join(
        f"{item['title']} {labels.get(str(item.get('status') or ''), '未知')}"
        for item in phases
    )


def _next_action(status: str, delivery: dict[str, Any]) -> str:
    if status == "completed":
        return "闭环已经完成，可以查看最终报告和账本。"
    if status == "verified_pending_delivery":
        if delivery.get("status") == "blocked":
            return f"验收已经通过，但交付被阻塞：{delivery.get('summary') or '请检查隔离 worktree 后重试。'}"
        return "验收已经通过；确认后把隔离 worktree 合并进目标分支。"
    if status == "verified_pending_dogfood":
        return "用户任务已验收；发布 Pacer 前还需绑定严格 Dogfood 证据。"
    if status == "blocked":
        return "先处理第一个被阻塞阶段，再从原 mission 恢复。"
    if status == "regression_clear":
        return (
            "改动没弄坏现有测试，但验收命令证明不了这次目标已达成。"
            "给一条改动前会失败的验收命令再跑一次，或者自己看一眼 diff 再决定合并。"
        )
    if delivery.get("status") == "pending":
        return "继续托管，完成 worker 与验收后再交付。"
    return "查看阶段原因代码，修复断链后继续原 mission。"


def _current_dispatch_record(
    dispatch: dict[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    current = dispatch.get("dispatch_record") if isinstance(dispatch.get("dispatch_record"), dict) else {}
    return current or (records[-1] if records else {})


def _latest_worker(
    workers: list[dict[str, Any]], dispatch: dict[str, Any]
) -> dict[str, Any]:
    current = dispatch.get("worker_record") if isinstance(dispatch.get("worker_record"), dict) else {}
    return current or (workers[-1] if workers else {})


def _is_pacer_repo(repo_root: Path) -> bool:
    root = repo_root.expanduser().resolve()
    return (root / "src" / "visual_agent").is_dir() and (root / ".pacer" / "dogfood.json").is_file()


def _dogfood_binding(workspace_root: Path, mission_id: str) -> dict[str, Any]:
    try:
        from .pacer_launch_context import read_reconciled_active_launch
        from .pacer_pillars import assess_pillar

        launch = read_reconciled_active_launch(workspace_root)
        pillars = launch.get("pillars") if isinstance(launch.get("pillars"), dict) else {}
        dogfood = pillars.get("dogfood") if isinstance(pillars.get("dogfood"), dict) else {}
        assessment = assess_pillar("dogfood", dogfood)
        bound_mission = str(launch.get("mission_id") or launch.get("source_mission_id") or "")
        return {
            "passed": bool(assessment.get("passed")) and bound_mission == mission_id,
            "launch_id": str(launch.get("launch_id") or ""),
            "bound_mission_id": bound_mission,
        }
    except Exception:
        return {"passed": False, "launch_id": "", "bound_mission_id": ""}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
