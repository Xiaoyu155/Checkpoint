"""Local web dashboard for DevPacer / Checkpoint.

A zero-dependency, local-only web view so a user can *see* the product: current
verification status, which coding agents are installed, and the mission /
plan lifecycle with stop reasons and evidence. Served with the stdlib HTTP
server; the page is a single self-contained HTML shell that polls a JSON API.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .agent_capabilities import agents_doctor
from .chief_plans_store import list_plans, load_plan, load_worker_records
from .chief_queue import list_mission_queue_items
from .missions import list_missions, load_mission
from .programs import list_programs
from .workbench_board import attach_board_fields, merge_mission_now, mission_review_payload


# In-flight workbench launches (missions started from the web form). The mission
# itself is the source of truth on disk; this registry only lets the page show a
# "starting/running" row before the mission file is written, plus launch errors.
_LAUNCHES: dict[str, dict[str, Any]] = {}
_LAUNCH_LOCK = threading.Lock()

# Queue worker process (started/stopped from the dashboard)
_WORKER_PROC: subprocess.Popen[bytes] | None = None
_WORKER_LOCK = threading.Lock()


def _record_launch(launch_id: str, **fields: Any) -> None:
    with _LAUNCH_LOCK:
        entry = _LAUNCHES.setdefault(launch_id, {"launch_id": launch_id})
        entry.update(fields)


def _launch_snapshot() -> list[dict[str, Any]]:
    with _LAUNCH_LOCK:
        return [dict(item) for item in _LAUNCHES.values()]


# ---------------------------------------------------------------------------
# Worker process management
# ---------------------------------------------------------------------------

def worker_status() -> dict[str, Any]:
    global _WORKER_PROC
    with _WORKER_LOCK:
        if _WORKER_PROC is None:
            return {"running": False, "pid": None}
        if _WORKER_PROC.poll() is not None:
            _WORKER_PROC = None
            return {"running": False, "pid": None}
        return {"running": True, "pid": _WORKER_PROC.pid}


def start_worker(workspace_root: Path) -> dict[str, Any]:
    global _WORKER_PROC
    with _WORKER_LOCK:
        if _WORKER_PROC is not None and _WORKER_PROC.poll() is None:
            return {"ok": False, "error": "Worker 已在运行中", "pid": _WORKER_PROC.pid}
        cmd = [sys.executable, "-m", "visual_agent", "mission", "worker",
               "--watch", "--workspace-root", str(workspace_root)]
        try:
            _WORKER_PROC = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "pid": _WORKER_PROC.pid}


def stop_worker() -> dict[str, Any]:
    global _WORKER_PROC
    with _WORKER_LOCK:
        if _WORKER_PROC is None or _WORKER_PROC.poll() is not None:
            _WORKER_PROC = None
            return {"ok": True, "was_running": False}
        _WORKER_PROC.terminate()
        try:
            _WORKER_PROC.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _WORKER_PROC.kill()
        _WORKER_PROC = None
        return {"ok": True, "was_running": True}


# ---------------------------------------------------------------------------
# Mission retry
# ---------------------------------------------------------------------------

def retry_mission(workspace_root: Path, mission_id: str) -> dict[str, Any]:
    """Create a new mission with the same goal/agent/test-command as a stopped one."""
    mission = load_mission(workspace_root, mission_id) or {}
    if not mission:
        return {"ok": False, "error": "找不到该 mission"}
    status = str(mission.get("status") or "")
    if status == "verified":
        return {"ok": False, "error": "任务已验收，无需重试"}
    goal = str(mission.get("objective") or "").strip()
    if not goal:
        return {"ok": False, "error": "原任务没有记录目标文本"}
    agent = str(mission.get("agent") or "")
    test_command = str(mission.get("test_command") or "")
    return start_workbench_mission(
        workspace_root=workspace_root,
        repo_root=str(workspace_root.parent),
        goal=goal,
        test_command=test_command,
        agent=agent,
        execute=True,
    )


# ---------------------------------------------------------------------------
# Dashboard data
# ---------------------------------------------------------------------------

def start_workbench_mission(
    *,
    workspace_root: str | Path,
    repo_root: str,
    goal: str,
    test_command: str,
    agent: str,
    execute: bool,
    merge_policy: str = "manual",
) -> dict[str, Any]:
    """Launch a mission from the workbench form in a background thread."""
    goal = str(goal or "").strip()
    if not goal:
        return {"ok": False, "error": "请填写目标（goal）。"}
    from .verification_profiles import resolve_test_command

    resolved_test_command, verification_profile = resolve_test_command(test_command or "auto", repo_root=repo_root or ".")
    launch_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    _record_launch(
        launch_id,
        goal=goal,
        execute=bool(execute),
        test_command=resolved_test_command or "",
        verification_profile=verification_profile or {},
        state="starting",
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    def _run() -> None:
        from .chief_run import run_chief_mission
        try:
            result = run_chief_mission(
                goal=goal,
                workspace_root=workspace_root,
                repo_root=repo_root or ".",
                test_command=(resolved_test_command or None),
                agents=((agent,) if agent else ()),
                execute=bool(execute),
                dry_run=not bool(execute),
                merge=bool(execute) and str(merge_policy or "manual").lower() == "auto",
            )
            mission = result.get("mission") if isinstance(result, dict) else {}
            _record_launch(
                launch_id,
                state="done",
                status=str((result or {}).get("status") or ""),
                stop_reason=str((result or {}).get("stop_reason") or ""),
                mission_id=str((mission or {}).get("mission_id") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            _record_launch(launch_id, state="error", error=str(exc)[:400])

    threading.Thread(target=_run, daemon=True).start()
    return {
        "ok": True,
        "launch_id": launch_id,
        "execute": bool(execute),
        "merge_policy": str(merge_policy or "manual"),
        "test_command": resolved_test_command or "",
        "verification_profile": verification_profile or {},
    }


_TIER_PRICE_PER_MTOK = {"cheap": 1.0, "standard": 4.0, "strong": 30.0}


def _build_value(root: Path, missions: list[dict[str, Any]], plans: list[dict[str, Any]]) -> dict[str, Any]:
    verified = sum(1 for m in missions if str(m.get("status") or "") == "verified")
    tier_counts = {"cheap": 0, "standard": 0, "strong": 0}
    spent_usd = 0.0
    saved_usd = 0.0
    input_tokens = 0
    output_tokens = 0
    real_runs = 0
    for summary in plans:
        plan_id = summary.get("plan_id")
        if not plan_id:
            continue
        payload = load_plan(root, plan_id) or {}
        for track in payload.get("worker_tracks") or []:
            tier = str((track or {}).get("tier") or "")
            if tier in tier_counts:
                tier_counts[tier] += 1
        for record in load_worker_records(root, plan_id) or []:
            usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
            cost = float(usage.get("cost_usd") or 0.0)
            if usage.get("cost_usd") is not None:
                real_runs += 1
                if usage.get("cost_is_savings"):
                    saved_usd += cost
                else:
                    spent_usd += cost
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)
    routed_tasks = tier_counts["cheap"] + tier_counts["standard"]
    return {
        "missions_total": len(missions),
        "verified": verified,
        "tier_counts": tier_counts,
        "downgraded_tasks": routed_tasks,
        "spent_usd": round(spent_usd, 4),
        "saved_usd": round(saved_usd, 4),
        "real_worker_runs": real_runs,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _read_status(workspace_root: Path) -> dict[str, Any]:
    candidates = [
        workspace_root.parent / ".visual-agent-status.md",
        workspace_root / ".visual-agent-status.md",
    ]
    for path in candidates:
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="ignore")
            state = "unknown"
            for line in text.splitlines():
                if line.lower().startswith("## status:"):
                    state = line.split(":", 1)[1].strip()
                    break
            return {"state": state, "path": str(path), "raw": text[:2000]}
    return {"state": "none", "path": "", "raw": ""}


def build_dashboard_data(workspace_root: str | Path) -> dict[str, Any]:
    root = Path(workspace_root).expanduser().resolve()
    missions = attach_board_fields(root, list_missions(root))
    plans = list_plans(root)
    try:
        raw_queue = list_mission_queue_items(root).get("entries", [])
    except Exception:
        raw_queue = []
    # Enrich queue entries with mission objective so the UI can show goal text
    mission_map = {str(m.get("mission_id") or ""): m for m in missions if m.get("mission_id")}
    queue: list[dict[str, Any]] = []
    for item in raw_queue:
        mid = str(item.get("mission_id") or "")
        enriched = dict(item)
        m = mission_map.get(mid) or {}
        enriched["objective"] = str(m.get("objective") or m.get("goal") or "")
        queue.append(enriched)
    try:
        programs = list_programs(root)
    except Exception:
        programs = []
    try:
        agents = agents_doctor()
    except Exception:
        agents = []
    counts: dict[str, int] = {}
    for mission in missions:
        counts[mission.get("status") or "unknown"] = counts.get(mission.get("status") or "unknown", 0) + 1
    installed_agents = [str(a.get("agent")) for a in agents if isinstance(a, dict) and a.get("installed")]
    return {
        "product": "DevPacer",
        "engine": "Checkpoint",
        "workspace_root": str(root),
        "repo_root": str(root.parent),
        "status": _read_status(root),
        "value": _build_value(root, missions, plans),
        "agents": agents,
        "installed_agents": installed_agents,
        "missions": missions,
        "mission_counts": counts,
        "plans": plans,
        "queue": queue,
        "programs": programs,
        "launches": _launch_snapshot(),
        "worker": worker_status(),
    }


def build_mission_detail(workspace_root: str | Path, mission_id: str) -> dict[str, Any]:
    root = Path(workspace_root).expanduser().resolve()
    mission = load_mission(root, mission_id) or {}
    rounds_path = root / "missions" / mission_id / "rounds.jsonl"
    rounds: list[dict[str, Any]] = []
    if rounds_path.exists():
        for line in rounds_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rounds.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    report_path = root / "missions" / mission_id / "final_report.md"
    final_report = report_path.read_text(encoding="utf-8", errors="ignore") if report_path.exists() else ""
    review = mission_review_payload(root, mission) if mission else {}
    decorated = attach_board_fields(root, [mission])[0] if mission else {}
    return {
        "mission": mission,
        "rounds": rounds,
        "final_report": final_report,
        "review": review,
        "can_merge": bool(decorated.get("can_merge")),
        "merge_state": str(decorated.get("merge_state") or ""),
    }


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>DevPacer 工作台</title>
<style>
:root {
  --bg:#0f1115; --card:#1a1d24; --card2:#1e2128; --line:#252a35;
  --fg:#e6e8ec; --mut:#8a90a0;
  --ok:#3fb950; --fail:#f85149; --warn:#d29922; --acc:#58a6ff;
}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;}

/* ---------- header ---------- */
header{
  padding:10px 24px;border-bottom:1px solid var(--line);
  display:flex;align-items:center;gap:12px;flex-wrap:wrap;
  position:sticky;top:0;background:var(--bg);z-index:20;
}
header h1{font-size:15px;font-weight:700;letter-spacing:.3px;white-space:nowrap;}
.hdr-sep{color:var(--line);}
#projPath{color:var(--mut);font-size:12px;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.spacer{flex:1;}
#agentsRow{display:flex;gap:6px;flex-wrap:wrap;}
.worker-ctl{display:flex;align-items:center;gap:8px;white-space:nowrap;}
.wdot{width:8px;height:8px;border-radius:50%;background:var(--mut);flex-shrink:0;}
.wdot.on{background:var(--ok);box-shadow:0 0 6px var(--ok);}
#wLabel{font-size:12px;color:var(--mut);}

/* ---------- pills ---------- */
.pill{padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap;display:inline-block;}
.pill.ok{background:rgba(63,185,80,.15);color:var(--ok);}
.pill.fail{background:rgba(248,81,73,.15);color:var(--fail);}
.pill.warn{background:rgba(210,153,34,.15);color:var(--warn);}
.pill.acc{background:rgba(88,166,255,.15);color:var(--acc);}
.pill.mut{background:rgba(138,144,160,.1);color:var(--mut);}
.pill.sm{font-size:11px;padding:1px 7px;}

/* ---------- buttons ---------- */
button{border-radius:8px;padding:7px 14px;cursor:pointer;font:inherit;font-size:13px;
  font-weight:600;border:1px solid var(--line);background:var(--card);color:var(--fg);}
button:hover:not(:disabled){border-color:var(--acc);color:var(--acc);}
button.primary{background:var(--acc);color:#04101f;border:none;}
button.primary:hover:not(:disabled){background:#79b8ff;}
button.danger{background:rgba(248,81,73,.12);color:var(--fail);border-color:rgba(248,81,73,.3);}
button.danger:hover:not(:disabled){background:rgba(248,81,73,.25);}
button.ghost{background:rgba(63,185,80,.1);color:var(--ok);border-color:rgba(63,185,80,.3);}
button.ghost:hover:not(:disabled){background:rgba(63,185,80,.2);}
button.act-merge{background:rgba(63,185,80,.12);color:var(--ok);
  border:1px solid rgba(63,185,80,.35);padding:4px 10px;font-size:12px;border-radius:6px;}
button.act-retry{background:rgba(88,166,255,.1);color:var(--acc);
  border:1px solid rgba(88,166,255,.3);padding:4px 10px;font-size:12px;border-radius:6px;}
button:disabled{opacity:.4;cursor:default;}

/* ---------- form inputs ---------- */
input,textarea,select{
  background:var(--card2);color:var(--fg);
  border:1px solid var(--line);border-radius:8px;
  padding:8px 10px;font:inherit;font-size:13px;width:100%;
  transition:border .15s;
}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--acc);}
select{width:auto;padding-right:24px;}
label{font-size:12px;color:var(--mut);display:block;margin-bottom:4px;}

/* ---------- layout ---------- */
.wrap{padding:18px 24px;max-width:1440px;margin:0 auto;}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
@media(max-width:760px){.g2{grid-template-columns:1fr;}}
.g2b{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:20px;}
@media(max-width:900px){.g2b{grid-template-columns:1fr;}}

/* ---------- composer ---------- */
.composer{margin-bottom:16px;}
.composer-hdr{font-size:13px;font-weight:700;margin-bottom:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.5px;}
.field{margin-bottom:10px;}
.field label{margin-bottom:5px;}
.rowline{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:10px;}
.rowline label{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--fg);margin:0;}
.rowline input[type=checkbox]{width:auto;}
.composer-hint{margin-top:10px;font-size:12px;color:var(--mut);
  background:rgba(88,166,255,.05);border:1px solid rgba(88,166,255,.12);
  border-radius:8px;padding:8px 10px;line-height:1.5;}
#launchMsg{font-size:12px;color:var(--acc);}

/* ---------- stats strip ---------- */
.stats{display:flex;gap:0;border:1px solid var(--line);border-radius:12px;
  overflow:hidden;margin-bottom:16px;}
.stat{flex:1;padding:12px 16px;border-right:1px solid var(--line);text-align:center;}
.stat:last-child{border-right:none;}
.stat-n{font-size:22px;font-weight:700;line-height:1.2;}
.stat-n.ok{color:var(--ok);} .stat-n.acc{color:var(--acc);}
.stat-l{font-size:11px;color:var(--mut);margin-top:2px;}

/* ---------- kanban ---------- */
.board{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:0;}
@media(max-width:1100px){.board{grid-template-columns:repeat(2,1fr);}}
@media(max-width:640px){.board{grid-template-columns:1fr;}}
.col{background:rgba(255,255,255,.02);border:1px solid var(--line);border-radius:12px;padding:10px;min-height:140px;}
.col-hdr{display:flex;justify-content:space-between;align-items:center;
  font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;
  color:var(--mut);margin-bottom:10px;padding:0 2px;}
.col-hdr .cnt{background:var(--line);border-radius:999px;padding:1px 8px;font-size:11px;color:var(--fg);}
.col-hdr.accent{color:var(--acc);} .col-hdr.warn{color:var(--warn);} .col-hdr.ok2{color:var(--ok);}
.empty{color:var(--mut);font-size:12px;padding:8px 4px;line-height:1.5;}
.tcard{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:10px 12px;margin-bottom:8px;cursor:pointer;transition:border .15s;}
.tcard:hover{border-color:var(--acc);}
.tcard-goal{font-size:13px;line-height:1.4;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
  margin-bottom:7px;}
.tcard-foot{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:4px;}
.tcard-actions{display:flex;gap:5px;margin-left:auto;}
.stop-tag{font-size:11px;color:var(--warn);background:rgba(210,153,34,.1);
  border-radius:4px;padding:1px 6px;}
.tcard-meta{font-size:11px;color:var(--mut);}

/* ---------- queue / programs ---------- */
.section-hdr{display:flex;align-items:baseline;gap:8px;margin-bottom:8px;}
.section-hdr h2{font-size:13px;font-weight:700;}
.section-hdr .sub{font-size:12px;color:var(--mut);}
.qrow{display:flex;justify-content:space-between;align-items:center;gap:10px;
  padding:8px 0;border-top:1px solid var(--line);}
.qrow:first-child{border-top:none;}
.qgoal{font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;}
.qmeta{display:flex;align-items:center;gap:6px;flex-shrink:0;}
.muts{font-size:11px;color:var(--mut);}
code{color:var(--acc);font-size:12px;}

/* ---------- drawer ---------- */
#drawer{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;z-index:50;}
.panel{position:absolute;top:0;right:0;bottom:0;width:min(700px,94vw);
  background:var(--card);border-left:1px solid var(--line);overflow-y:auto;padding:20px 24px;}
.panel-top{display:flex;justify-content:flex-end;margin-bottom:12px;}
.close-btn{background:none;border:none;color:var(--mut);cursor:pointer;font-size:13px;padding:4px 8px;}
.close-btn:hover{color:var(--fg);}
#drawerBody h2{font-size:16px;font-weight:700;margin-bottom:6px;line-height:1.4;}
#drawerBody h3{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;
  color:var(--mut);margin:18px 0 8px;}
#drawerBody p{font-size:13px;color:var(--mut);margin-bottom:6px;}
#drawerBody pre{white-space:pre-wrap;background:var(--bg);padding:12px;
  border-radius:8px;border:1px solid var(--line);font-size:12px;line-height:1.5;}
.stop-hint{background:rgba(210,153,34,.08);border:1px solid rgba(210,153,34,.2);
  border-radius:8px;padding:10px 12px;margin:8px 0;font-size:13px;line-height:1.6;}
.stop-hint .fix{color:var(--acc);margin-top:4px;}
.action-row{display:flex;gap:8px;margin:12px 0;}
.filerow{display:flex;justify-content:space-between;gap:12px;
  padding:5px 0;border-top:1px solid var(--line);font-size:12px;}
.filerow:first-child{border-top:none;}
.add{color:var(--ok);} .del{color:var(--fail);}
.round-row{display:flex;justify-content:space-between;align-items:center;gap:8px;
  padding:6px 0;border-top:1px solid var(--line);font-size:13px;}
.round-row:first-child{border-top:none;}
</style>
</head>
<body>

<header>
  <h1>DevPacer</h1>
  <span class="hdr-sep">|</span>
  <span id="projPath" title=""></span>
  <span class="spacer"></span>
  <div id="agentsRow"></div>
  <div class="worker-ctl">
    <div class="wdot" id="wDot"></div>
    <span id="wLabel">queue worker</span>
    <button id="btnWorker" onclick="toggleWorker()">启动 Worker</button>
  </div>
</header>

<div class="wrap">

  <!-- 任务表单 -->
  <div class="card composer">
    <div class="composer-hdr">新建任务</div>
    <div class="field">
      <label>目标 <span style="color:var(--mut)">— 用一句话说清楚要做什么</span></label>
      <textarea id="fGoal" rows="2" placeholder="例：修复 src/calc.py 的 add 函数让 pytest 通过 / 给登录页加「记住我」复选框"></textarea>
    </div>
    <div class="g2">
      <div class="field">
        <label>项目根目录 <span style="color:var(--mut)">（留空 = 工作区上级）</span></label>
        <input id="fRepo" placeholder="留空自动填入"/>
      </div>
      <div class="field">
        <label>验收命令 <span style="color:var(--mut)">（留空 = 自动探测）</span></label>
        <input id="fTest" placeholder="pytest -q / npm test / cargo test / npm run build"/>
      </div>
    </div>
    <div class="rowline">
      <label>编码 AI <select id="fAgent" style="margin-left:6px;"></select></label>
      <label><input type="checkbox" id="fAutoMerge"/> 验收通过后自动合并</label>
      <span class="spacer"></span>
      <button id="btnPreview" onclick="startMission(false)">预览（看计划·不花钱）</button>
      <button class="primary" id="btnRun" onclick="startMission(true)">▶ 开始执行</button>
      <span id="launchMsg"></span>
    </div>
    <div class="composer-hint">
      执行时在隔离 git 分支里改代码、跑验收命令；验收通过的任务出现在「待验收」列，点「合并」才进主分支，主分支全程不动。
      预览只花极少的钱看它打算怎么干，不改任何代码。
    </div>
  </div>

  <!-- 统计条 -->
  <div class="stats">
    <div class="stat"><div class="stat-n ok" id="vVerified">0</div><div class="stat-l">已验收交付</div></div>
    <div class="stat"><div class="stat-n acc" id="vDowngraded">0</div><div class="stat-l" id="vTiers">省钱路由</div></div>
    <div class="stat"><div class="stat-n" id="vSpent">$0.00</div><div class="stat-l">真实花费</div></div>
    <div class="stat"><div class="stat-n ok" id="vSaved">$0.00</div><div class="stat-l">省下（便宜后端）</div></div>
    <div class="stat"><div class="stat-n" id="vTokens">0</div><div class="stat-l">总 Tokens</div></div>
  </div>

  <!-- 看板 -->
  <div class="board">
    <div class="col">
      <div class="col-hdr">待办 <span class="cnt" id="cTodo">0</span></div>
      <div id="colTodo"><div class="empty">暂无待办。在上方填目标即可派发。</div></div>
    </div>
    <div class="col">
      <div class="col-hdr accent">执行中 <span class="cnt" id="cRun">0</span></div>
      <div id="colRun"><div class="empty">暂无执行中的任务。</div></div>
    </div>
    <div class="col">
      <div class="col-hdr warn">待验收 <span class="cnt" id="cReview">0</span></div>
      <div id="colReview"><div class="empty">没有等待合并的任务。</div></div>
    </div>
    <div class="col">
      <div class="col-hdr ok2">已完成 <span class="cnt" id="cDone">0</span></div>
      <div id="colDone"><div class="empty">还没有完成的任务。</div></div>
    </div>
  </div>

  <!-- 队列 + 项目托管 -->
  <div class="g2b">
    <div>
      <div class="section-hdr"><h2>托管队列</h2><span class="sub">批量排队的任务</span></div>
      <div class="card" id="queue"><div class="empty">加载中…</div></div>
    </div>
    <div>
      <div class="section-hdr"><h2>项目托管</h2><span class="sub">按 5 小时额度窗口规划的大任务</span></div>
      <div class="card" id="programs"><div class="empty">加载中…</div></div>
    </div>
  </div>

</div>

<!-- 详情侧抽屉 -->
<div id="drawer" onclick="if(event.target.id==='drawer')closeDrawer()">
  <div class="panel">
    <div class="panel-top">
      <button class="close-btn" onclick="closeDrawer()">✕ 关闭</button>
    </div>
    <div id="drawerBody"></div>
  </div>
</div>

<script>
// ---------- 停止原因中文翻译 + 修复建议 ----------
const STOP_REASON = {
  coverage_gap:        { zh:'未找到测试命令', fix:'在「验收命令」里填上测试命令，如 pytest -q / npm test' },
  permission_required: { zh:'仓库有未提交的改动', fix:'先在项目目录 git commit 或 git stash，再重试' },
  same_failure_repeated:{ zh:'同一错误修复两次仍然失败', fix:'打开最终报告看具体报错，把目标改得更精确再重试' },
  budget_exhausted:    { zh:'超出轮次预算', fix:'目标拆得更小，或命令行加 --max-rounds 参数再续跑' },
  worker_error:        { zh:'AI Worker 进程异常退出', fix:'运行 checkpoint agents doctor 确认 Agent 可用，再恢复任务' },
  quota_exhausted:     { zh:'AI 额度已用完', fix:'等额度恢复（Codex 每周 / Claude 每 5 小时），或切换编码 AI' },
  needs_clarification: { zh:'目标不够具体无法执行', fix:'把目标改写为：做什么 + 改哪里 + 什么叫完成' },
  verification_failed: { zh:'验收命令仍然失败', fix:'打开最终报告看具体报错，重新创建更精确的任务' },
  command_timeout:     { zh:'验收命令超时', fix:'加快测试速度，或先跑部分测试验证思路' },
  command_launch_error:{ zh:'验收命令无法启动', fix:'先手动运行一次验收命令确认它能跑' },
  test_command_invalid:{ zh:'验收命令有误', fix:'检查测试命令是否正确，先手动运行确认' },
  inspection_only:     { zh:'仅检查验收（无交互）', fix:'加 --run-profile supervised 做真实交互验收' },
};
function srInfo(code){ return STOP_REASON[code] || { zh: code||'', fix:'' }; }

const STATUS_ZH = {
  created:'待执行', running:'执行中', preview_running:'预览中', preview:'预览完成',
  verified:'验收通过', merged:'已合并', stopped:'已停止', failed:'失败',
};
const zh = s => STATUS_ZH[s] || s;

const esc = s => (s||'').replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function pc(s){
  const t=(s||'').toLowerCase();
  if(t.includes('pass')||t.includes('verified')||t.includes('merged')) return 'ok';
  if(t.includes('fail')||t.includes('error')||t.includes('conflict')) return 'fail';
  if(t.includes('clarif')||t.includes('coverage')||t.includes('budget')||
     t.includes('stopped')||t.includes('exhausted')||t.includes('repeated')) return 'warn';
  if(t.includes('running')||t.includes('preview')) return 'acc';
  return 'mut';
}

// ---------- Worker 控制 ----------
let _workerRunning = false;
async function toggleWorker(){
  const btn = document.getElementById('btnWorker');
  btn.disabled = true;
  try{
    const ep = _workerRunning ? '/api/worker/stop' : '/api/worker/start';
    const r = await (await fetch(ep,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
    if(!r.ok && r.error) alert('操作失败：'+r.error);
  }catch(e){ alert('请求失败：'+e); }
  btn.disabled = false;
  await load();
}
function updateWorker(w){
  _workerRunning = !!(w&&w.running);
  const dot = document.getElementById('wDot');
  const lbl = document.getElementById('wLabel');
  const btn = document.getElementById('btnWorker');
  dot.className = 'wdot' + (_workerRunning?' on':'');
  lbl.textContent = _workerRunning ? 'Queue Worker 运行中' : 'Queue Worker 已停止';
  btn.textContent = _workerRunning ? '停止 Worker' : '启动 Worker';
  btn.className = _workerRunning ? 'danger' : 'ghost';
}

// ---------- 新建任务 ----------
let _agentsFilled = false;
function fillAgents(d){
  if(_agentsFilled) return;
  const sel = document.getElementById('fAgent');
  const inst = d.installed_agents||[];
  const order = ['codex','claude-code'];
  const opts = inst.length ? order.filter(a=>inst.includes(a)).concat(inst.filter(a=>!order.includes(a))) : order;
  sel.innerHTML = opts.map(a=>`<option value="${esc(a)}">${esc(a)}</option>`).join('');
  if(d.repo_root) document.getElementById('fRepo').placeholder = d.repo_root;
  _agentsFilled = true;
}

async function startMission(execute){
  const goal = document.getElementById('fGoal').value.trim();
  const msg = document.getElementById('launchMsg');
  if(!goal){ msg.textContent='请先填写目标。'; return; }
  const body = {
    goal,
    repo_root: document.getElementById('fRepo').value.trim() || '',
    test_command: document.getElementById('fTest').value.trim(),
    agent: document.getElementById('fAgent').value,
    merge_policy: document.getElementById('fAutoMerge').checked ? 'auto' : 'manual',
    execute: !!execute
  };
  document.getElementById('btnPreview').disabled = true;
  document.getElementById('btnRun').disabled = true;
  msg.textContent = execute ? '派发中…' : '生成预览中…';
  try{
    const r = await (await fetch('/api/mission/start',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
    if(r.ok){
      msg.textContent = execute ? '✓ 已派发，在「执行中」列查看。' : '✓ 预览生成中，在「待验收」列查看。';
      document.getElementById('fGoal').value = '';
    } else {
      msg.textContent = '错误：'+esc(r.error||'未知');
    }
  }catch(e){ msg.textContent='请求失败：'+esc(String(e)); }
  finally{
    document.getElementById('btnPreview').disabled = false;
    document.getElementById('btnRun').disabled = false;
    load();
  }
}

// ---------- 合并 / 重试 ----------
async function mergeMission(ev, id){
  ev.stopPropagation();
  const btn = ev.target; btn.disabled=true; btn.textContent='合并中…';
  try{
    const r = await (await fetch('/api/mission/merge',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({mission_id:id})})).json();
    if(!r.ok) alert('合并失败：'+(r.error||(r.merge&&r.merge.reason)||'未知'));
  }catch(e){ alert('合并失败：'+e); }
  load();
}

async function retryMission(ev, id){
  ev.stopPropagation();
  const btn = ev.target; btn.disabled=true; btn.textContent='重试中…';
  try{
    const r = await (await fetch('/api/mission/retry',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({mission_id:id})})).json();
    if(r.ok){ closeDrawer(); }
    else{ alert('重试失败：'+(r.error||'未知')); btn.disabled=false; btn.textContent='↺ 重试'; }
  }catch(e){ alert('重试失败：'+e); btn.disabled=false; btn.textContent='↺ 重试'; }
  load();
}

// ---------- 看板 ----------
function missionCard(m){
  const sr = srInfo(m.stop_reason);
  const isStopped = m.status==='stopped'||m.status==='failed';
  const hasStop = m.stop_reason && m.stop_reason!=='verified';
  const mergeBtn = m.can_merge
    ? `<button class="act-merge" onclick="mergeMission(event,'${esc(m.mission_id)}')">✓ 合并</button>` : '';
  const retryBtn = isStopped && hasStop
    ? `<button class="act-retry" onclick="retryMission(event,'${esc(m.mission_id)}')">↺ 重试</button>` : '';
  const stopTag = hasStop && !m.can_merge
    ? `<span class="stop-tag">${esc(sr.zh)}</span>` : '';
  return `<div class="tcard" onclick="detail('${esc(m.mission_id)}')">
    <div class="tcard-goal">${esc(m.objective||'（无目标）')}</div>
    <div class="tcard-foot">
      <span class="pill ${pc(m.status)} sm">${esc(zh(m.status))}</span>
      ${stopTag}
      <span class="tcard-actions">${mergeBtn}${retryBtn}</span>
    </div>
    <div class="tcard-meta">${esc((m.mission_id||'').slice(0,17))}</div>
  </div>`;
}
function launchCard(l){
  const p = l.state==='error'?'fail':'acc';
  const txt = l.state==='error'?'启动失败':(l.execute?'派发中…':'预览中…');
  const extra = l.state==='error'?`<div class="tcard-meta" style="color:var(--fail)">${esc(l.error||'')}</div>`:'';
  return `<div class="tcard"><div class="tcard-goal">${esc(l.goal)}</div>
    <div class="tcard-foot"><span class="pill ${p} sm">${txt}</span></div>${extra}</div>`;
}
function renderBoard(d){
  const cols={todo:[],in_progress:[],in_review:[],done:[]};
  const runObjs = new Set((d.missions||[]).filter(m=>m.board_column==='in_progress').map(m=>m.objective));
  (d.launches||[]).forEach(l=>{
    if(l.state==='starting'&&!runObjs.has(l.goal)) cols.in_progress.push(launchCard(l));
    else if(l.state==='error') cols.in_review.push(launchCard(l));
  });
  (d.missions||[]).forEach(m=>{ (cols[m.board_column]||cols.in_review).push(missionCard(m)); });
  const emp=t=>`<div class="empty">${t}</div>`;
  document.getElementById('colTodo').innerHTML=cols.todo.join('')||emp('暂无待办。在上方填目标即可派发。');
  document.getElementById('colRun').innerHTML=cols.in_progress.join('')||emp('暂无执行中的任务。');
  document.getElementById('colReview').innerHTML=cols.in_review.join('')||emp('没有等待合并的任务。');
  document.getElementById('colDone').innerHTML=cols.done.join('')||emp('还没有完成的任务。');
  document.getElementById('cTodo').textContent=cols.todo.length;
  document.getElementById('cRun').textContent=cols.in_progress.length;
  document.getElementById('cReview').textContent=cols.in_review.length;
  document.getElementById('cDone').textContent=cols.done.length;
}

// ---------- 主加载 ----------
async function load(){
  try{
    const d = await (await fetch('/api/data')).json();
    fillAgents(d);

    const proj = document.getElementById('projPath');
    const rr = (d.repo_root||'').replace(/\\\\/g,'/');
    proj.textContent = rr.split('/').slice(-2).join('/')||d.workspace_root;
    proj.title = d.workspace_root;

    document.getElementById('agentsRow').innerHTML = (d.agents||[])
      .map(a=>`<span class="pill ${a.installed?'ok':'mut'} sm">${esc(a.display_name||a.agent)}</span>`).join(' ');

    updateWorker(d.worker||{});

    const v=d.value||{};
    document.getElementById('vVerified').textContent=v.verified||0;
    document.getElementById('vDowngraded').textContent=v.downgraded_tasks||0;
    const tc=v.tier_counts||{};
    document.getElementById('vTiers').textContent=
      `省钱路由（便宜${tc.cheap||0} 中${tc.standard||0} 强${tc.strong||0}）`;
    document.getElementById('vSpent').textContent='$'+(v.spent_usd||0).toFixed(2);
    document.getElementById('vSaved').textContent='$'+(v.saved_usd||0).toFixed(2);
    document.getElementById('vTokens').textContent=
      ((v.input_tokens||0)+(v.output_tokens||0)).toLocaleString();

    renderBoard(d);

    // 队列（enriched with objective from Python side）
    const qEl=document.getElementById('queue');
    if((d.queue||[]).length){
      qEl.innerHTML=(d.queue||[]).map(q=>{
        const sr=srInfo(q.stop_reason);
        const stopTag=q.stop_reason?`<span class="pill ${pc(q.status)} sm">${esc(sr.zh||q.stop_reason)}</span>`:'';
        return `<div class="qrow">
          <span class="qgoal">${esc(q.objective||q.mission_id)}</span>
          <span class="qmeta">
            <span class="pill ${pc(q.status)} sm">${esc(q.status)}</span>
            ${stopTag}
            <span class="muts">${esc(q.agent||'codex')}</span>
          </span>
        </div>`;
      }).join('');
    } else {
      qEl.innerHTML=`<div class="empty">暂无队列任务。<br>用 <code>checkpoint mission import --create --queue</code> 批量排队，<br>或点右上角「启动 Worker」让它自动处理。</div>`;
    }

    // 项目托管
    const gEl=document.getElementById('programs');
    if((d.programs||[]).length){
      gEl.innerHTML=(d.programs||[]).map(g=>`<div class="qrow">
        <span class="qgoal">${esc(g.objective||g.program_id)}</span>
        <span class="qmeta">
          <span class="pill ${pc(g.status)} sm">${esc(g.status)}</span>
          <span class="muts">${esc(String(g.task_count||0))} 个任务</span>
        </span>
      </div>`).join('');
    } else {
      gEl.innerHTML=`<div class="empty">暂无项目托管。<br>用 <code>checkpoint autopilot --file 开发计划.md --hours 5</code> 创建。</div>`;
    }
  }catch(e){ console.error('load',e); }
}

// ---------- 详情抽屉 ----------
function diffSection(rv){
  const ds=(rv||{}).diff_summary||{};
  if(!ds.file_count) return '';
  const files=(ds.changed_files||[]).slice(0,40).map(f=>`
    <div class="filerow"><span>${esc(f.path)}</span>
    <span><span class="add">+${f.lines_added||0}</span> <span class="del">-${f.lines_removed||0}</span></span></div>`).join('');
  const fns=(ds.functions_touched||[]).slice(0,20).map(x=>`<code>${esc(x)}</code>`).join('、');
  const checks=(ds.user_checklist||[]).map(c=>`<li>${esc(c)}</li>`).join('');
  const warn=(rv.warnings||[]).map(w=>`<div class="pill warn" style="display:block;margin:4px 0;">${esc(w)}</div>`).join('');
  return `<h3>改动摘要（${ds.file_count} 个文件，<span class="add">+${ds.lines_added}</span>/<span class="del">-${ds.lines_removed}</span>）</h3>
    ${warn}${files}
    ${fns?`<div class="muts" style="margin-top:6px;">涉及函数：${fns}</div>`:''}
    ${checks?`<h3>建议人工检查</h3><ul style="padding-left:18px;">${checks}</ul>`:''}`;
}

function closeDrawer(){ document.getElementById('drawer').style.display='none'; }

async function detail(id){
  const d = await (await fetch('/api/mission?id='+encodeURIComponent(id))).json();
  const m=d.mission||{};
  const rv=d.review||{};
  const sr=srInfo(m.stop_reason);
  const isStopped=m.status==='stopped'||m.status==='failed';
  const hasStop=m.stop_reason&&m.stop_reason!=='verified';
  const canRetry=isStopped&&hasStop;

  const mergeBtn=d.can_merge?`<button class="act-merge" style="padding:7px 16px;font-size:13px;" onclick="mergeMission(event,'${esc(m.mission_id)}')">✓ 合并到主分支</button>`:'';
  const retryBtn=canRetry?`<button class="act-retry" style="padding:7px 16px;font-size:13px;" onclick="retryMission(event,'${esc(m.mission_id)}')">↺ 用相同目标重试</button>`:'';
  const stopHint=hasStop?`<div class="stop-hint">
    <div>停止原因：<b>${esc(sr.zh)}</b></div>
    ${sr.fix?`<div class="fix">建议操作：${esc(sr.fix)}</div>`:''}
  </div>`:'';

  const rounds=(d.rounds||[]).map(r=>`<div class="round-row">
    <span>Round ${r.round}：<b>${esc(r.type)}</b></span>
    <span><span class="pill ${pc(r.status)} sm">${esc(r.status)}</span>
    ${r.stop_reason||r.reason?`<span class="muts">${esc(r.stop_reason||r.reason)}</span>`:''}</span>
  </div>`).join('');

  document.getElementById('drawerBody').innerHTML=
    `<h2>${esc(m.objective||id)}</h2>
     <p>状态：<span class="pill ${pc(m.status)}">${esc(zh(m.status))}</span>
     ${d.merge_state?' · 合并：'+esc(d.merge_state):''}</p>
     ${stopHint}
     <div class="action-row">${mergeBtn}${retryBtn}</div>
     ${rv.verdict?`<p>验收命令：<code>${esc(rv.command||'工作流验收')}</code> · 结论：<span class="pill ${pc(rv.verdict)} sm">${esc(rv.verdict)}</span></p>`:''}
     ${diffSection(rv)}
     <h3>执行轮次</h3>${rounds||'<div class="empty">暂无轮次记录</div>'}
     <h3>最终报告</h3><pre>${esc(d.final_report)||'（暂无报告）'}</pre>`;
  document.getElementById('drawer').style.display='block';
}

load();
setInterval(load, 4000);
</script>
</body>
</html>
"""


class _DashboardHandler(BaseHTTPRequestHandler):
    server: "_DashboardServer"

    def log_message(self, *args: Any) -> None:
        return

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(body, "application/json; charset=utf-8")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/data":
            self._send_json(build_dashboard_data(self.server.workspace_root))
            return
        if path == "/api/mission":
            from urllib.parse import parse_qs, urlparse
            mission_id = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            self._send_json(build_mission_detail(self.server.workspace_root, mission_id))
            return
        self.send_response(404)
        self.end_headers()

    def _read_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            parsed = json.loads(raw.decode("utf-8") or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, OSError):
            return {}

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        payload = self._read_body()

        if path == "/api/mission/start":
            result = start_workbench_mission(
                workspace_root=self.server.workspace_root,
                repo_root=str(payload.get("repo_root") or str(self.server.workspace_root.parent)),
                goal=str(payload.get("goal") or ""),
                test_command=str(payload.get("test_command") or ""),
                agent=str(payload.get("agent") or ""),
                execute=bool(payload.get("execute")),
                merge_policy=str(payload.get("merge_policy") or "manual"),
            )
            self._send_json(result)
        elif path == "/api/mission/merge":
            result = merge_mission_now(self.server.workspace_root, str(payload.get("mission_id") or ""))
            self._send_json(result)
        elif path == "/api/mission/retry":
            result = retry_mission(self.server.workspace_root, str(payload.get("mission_id") or ""))
            self._send_json(result)
        elif path == "/api/worker/start":
            self._send_json(start_worker(self.server.workspace_root))
        elif path == "/api/worker/stop":
            self._send_json(stop_worker())
        else:
            self.send_response(404)
            self.end_headers()


class _DashboardServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], workspace_root: Path):
        super().__init__(address, _DashboardHandler)
        self.workspace_root = workspace_root


def _bind_dashboard_server(host: str, port: int, root: Path) -> "_DashboardServer":
    candidates = [port, 8787, 8080, 8899, 9797, 0]
    seen: set[int] = set()
    last_error: Exception | None = None
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            return _DashboardServer((host, candidate), root)
        except (PermissionError, OSError) as exc:
            last_error = exc
            continue
    raise RuntimeError(f"Could not bind the dashboard to any port on {host}: {last_error}")


def serve_dashboard(*, workspace_root: str | Path, host: str = "127.0.0.1", port: int = 8787, open_browser: bool = True) -> None:
    root = Path(workspace_root).expanduser().resolve()
    server = _bind_dashboard_server(host, port, root)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/"
    if actual_port != port:
        print(f"Port {port} was unavailable; using {actual_port} instead.")
    print(f"DevPacer dashboard: {url}")
    print(f"Workspace: {root}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
