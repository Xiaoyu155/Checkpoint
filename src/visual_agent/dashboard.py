"""Local web dashboard for DevPacer / Checkpoint.

A zero-dependency, local-only web view so a user can *see* the product: current
verification status, which coding agents are installed, and the mission /
plan lifecycle with stop reasons and evidence. Served with the stdlib HTTP
server; the page is a single self-contained HTML shell that polls a JSON API.
"""

from __future__ import annotations

import json
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


def _record_launch(launch_id: str, **fields: Any) -> None:
    with _LAUNCH_LOCK:
        entry = _LAUNCHES.setdefault(launch_id, {"launch_id": launch_id})
        entry.update(fields)


def _launch_snapshot() -> list[dict[str, Any]]:
    with _LAUNCH_LOCK:
        return [dict(item) for item in _LAUNCHES.values()]


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
    """Launch a mission from the workbench form in a background thread.

    Runs ``run_chief_mission`` in-process (it honors ``test_command``), so the
    dashboard server thread returns immediately and the polling page shows the
    mission as it progresses. Preview (execute=False) is fast; execute runs a
    real worker; verified changes wait for review unless merge_policy is auto."""
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
        except Exception as exc:  # noqa: BLE001 - surface launch failures to the page
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


# Rough public per-Mtoken USD prices, only for an *estimated* savings figure so
# the user can see the benefit. Not billing; real spend comes from worker records.
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
                # A cheap external backend (e.g. MiMo) reports a Claude-priced cost
                # but bills its own cheap credits, so it counts as saved, not spent.
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
    # Status file lives at repo root next to the workspace, or inside it.
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
        queue = list_mission_queue_items(root).get("entries", [])
    except Exception:
        queue = []
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


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>DevPacer 工作台</title>
<style>
  :root { --bg:#0f1115; --card:#1a1d24; --line:#2a2f3a; --fg:#e6e8ec; --mut:#8a90a0;
          --ok:#3fb950; --fail:#f85149; --warn:#d29922; --acc:#58a6ff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; }
  header { padding:14px 24px; border-bottom:1px solid var(--line); display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
  header h1 { font-size:18px; margin:0; letter-spacing:.5px; }
  header .sub { color:var(--mut); font-size:12px; }
  header .spacer { flex:1; }
  .pill { padding:3px 10px; border-radius:999px; font-size:12px; font-weight:600; white-space:nowrap; }
  .pill.ok{background:rgba(63,185,80,.15);color:var(--ok)} .pill.fail{background:rgba(248,81,73,.15);color:var(--fail)}
  .pill.warn{background:rgba(210,153,34,.15);color:var(--warn)} .pill.mut{background:rgba(138,144,160,.15);color:var(--mut)}
  .pill.acc{background:rgba(88,166,255,.15);color:var(--acc)}
  .wrap { padding:20px 24px; max-width:1400px; margin:0 auto; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; }
  .meta { color:var(--mut); font-size:12px; }
  .empty { color:var(--mut); padding:10px 4px; font-size:13px; }
  code { color:var(--acc); }
  input, textarea, select { background:var(--bg); color:var(--fg); border:1px solid var(--line); border-radius:8px; padding:8px; font:inherit; }
  button { border-radius:8px; padding:8px 14px; cursor:pointer; font:inherit; font-weight:600; border:1px solid var(--line); background:var(--card); color:var(--fg); }
  button.primary { background:var(--acc); color:#04101f; border:none; font-weight:700; }
  button.ghost { background:#243; color:var(--ok); }
  button.merge { background:rgba(63,185,80,.15); color:var(--ok); border:1px solid rgba(63,185,80,.4); padding:4px 12px; font-size:12px; }
  button:disabled { opacity:.5; cursor:default; }

  /* composer */
  .composer { display:grid; gap:10px; margin-bottom:18px; }
  .composer .rowline { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  .composer textarea { width:100%; resize:vertical; }
  .composer .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
  @media (max-width:800px){ .composer .grid2{grid-template-columns:1fr;} }

  /* value strip */
  .strip { display:flex; gap:20px; flex-wrap:wrap; margin-bottom:18px; padding:10px 14px; }
  .strip .item { display:flex; align-items:baseline; gap:8px; }
  .strip .num { font-size:22px; font-weight:700; }
  .num.ok{color:var(--ok)} .num.acc{color:var(--acc)}

  /* board */
  .board { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; align-items:start; }
  @media (max-width:1100px){ .board{grid-template-columns:repeat(2,1fr);} }
  @media (max-width:640px){ .board{grid-template-columns:1fr;} }
  .col { background:rgba(255,255,255,.02); border:1px solid var(--line); border-radius:12px; padding:10px; min-height:120px; }
  .col h3 { margin:2px 4px 10px; font-size:13px; color:var(--mut); text-transform:uppercase; letter-spacing:.6px; display:flex; justify-content:space-between; }
  .col h3 .cnt { color:var(--fg); background:var(--line); border-radius:999px; padding:0 8px; font-size:12px; }
  .tcard { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 12px; margin-bottom:8px; cursor:pointer; }
  .tcard:hover { border-color:var(--acc); }
  .tcard .obj { font-size:13px; line-height:1.4; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; margin-bottom:8px; }
  .tcard .foot { display:flex; justify-content:space-between; align-items:center; gap:6px; flex-wrap:wrap; }
  .tcard .foot .meta { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:100%; }

  section { margin-top:26px; }
  section > h2 { font-size:14px; margin:0 0 10px; }
  .row { display:flex; justify-content:space-between; gap:8px; padding:8px 0; border-top:1px solid var(--line); }
  .row:first-of-type{border-top:none}
  .obj { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .grid2col { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
  @media (max-width:900px){ .grid2col{grid-template-columns:1fr;} }

  /* drawer */
  #drawer { position:fixed; inset:0; background:rgba(0,0,0,.55); display:none; z-index:50; }
  #drawer .panel { position:absolute; top:0; right:0; bottom:0; width:min(680px,92vw); background:var(--card); border-left:1px solid var(--line); overflow:auto; padding:20px 22px; }
  #drawer h2 { margin:0 44px 8px 0; font-size:16px; }
  #drawer h3 { font-size:13px; color:var(--mut); text-transform:uppercase; letter-spacing:.5px; margin:20px 0 8px; }
  #drawer pre { white-space:pre-wrap; background:var(--bg); padding:12px; border-radius:8px; border:1px solid var(--line); font-size:12px; }
  #drawer .close { position:absolute; top:14px; right:18px; color:var(--mut); cursor:pointer; font-size:15px; }
  .filerow { display:flex; justify-content:space-between; gap:10px; padding:5px 0; border-top:1px solid var(--line); font-size:12px; }
  .filerow:first-of-type{border-top:none}
  .add{color:var(--ok)} .del{color:var(--fail)}
  ul.checklist { margin:6px 0; padding-left:20px; }
  ul.checklist li { margin:4px 0; }
</style>
</head>
<body>
<header>
  <h1>DevPacer 工作台</h1>
  <span class="sub">验收引擎 <b>Checkpoint</b></span>
  <span id="statusPill" class="pill mut">…</span>
  <span class="spacer"></span>
  <span id="agentsInline" class="sub"></span>
  <span class="sub" id="ws"></span>
</header>
<div class="wrap">

  <div class="card composer">
    <textarea id="fGoal" rows="2" placeholder="用一句话说要做什么，例如：修复 src/calc.py 的 add，让 pytest 通过"></textarea>
    <div class="grid2">
      <input id="fRepo" placeholder="项目根目录（默认：工作区上级）"/>
      <input id="fTest" placeholder="验收命令，如 pytest -q / npm test（留空 = 自动探测）"/>
    </div>
    <div class="rowline">
      <label class="meta">编码 agent <select id="fAgent"></select></label>
      <label class="meta"><input id="fAutoMerge" type="checkbox"/> 验收通过后自动合并</label>
      <button class="ghost" id="btnPreview" onclick="startMission(false)">预览（不花钱）</button>
      <button class="primary" id="btnRun" onclick="startMission(true)">开始执行</button>
      <span id="launchMsg" class="meta"></span>
    </div>
    <div class="meta">执行时在隔离分支里改代码、跑验收命令；验收通过的任务出现在「待验收」列，点「合并」才进主分支。预览只花很少的钱看它准备怎么干。</div>
  </div>

  <div class="card strip">
    <span class="item"><span class="num ok" id="vVerified">0</span><span class="meta">已验收交付</span></span>
    <span class="item"><span class="num acc" id="vDowngraded">0</span><span class="meta" id="vTiers">省钱路由</span></span>
    <span class="item"><span class="num" id="vSpent">$0</span><span class="meta">真实花费</span></span>
    <span class="item"><span class="num ok" id="vSaved">$0</span><span class="meta">省下（便宜后端）</span></span>
    <span class="item"><span class="num" id="vTokens">0</span><span class="meta">tokens</span></span>
  </div>

  <div class="board">
    <div class="col"><h3>待办 <span class="cnt" id="cTodo">0</span></h3><div id="colTodo"></div></div>
    <div class="col"><h3>进行中 <span class="cnt" id="cRun">0</span></h3><div id="colRun"></div></div>
    <div class="col"><h3>待验收 <span class="cnt" id="cReview">0</span></h3><div id="colReview"></div></div>
    <div class="col"><h3>已完成 <span class="cnt" id="cDone">0</span></h3><div id="colDone"></div></div>
  </div>

  <section>
    <div class="grid2col">
      <div>
        <h2>托管队列 <span class="meta">批量排队的任务</span></h2>
        <div class="card" id="queue"><div class="empty">加载中…</div></div>
      </div>
      <div>
        <h2>项目托管 <span class="meta">按 5 小时额度窗口规划的大任务</span></h2>
        <div class="card" id="programs"><div class="empty">加载中…</div></div>
      </div>
    </div>
  </section>
</div>

<div id="drawer" onclick="if(event.target.id==='drawer')this.style.display='none'">
  <div class="panel">
    <span class="close" onclick="document.getElementById('drawer').style.display='none'">✕ 关闭</span>
    <div id="drawerBody"></div>
  </div>
</div>

<script>
const pill = s => { const t=(s||'').toLowerCase();
  if(t.includes('pass')||t.includes('verified')||t.includes('merged')) return 'ok';
  if(t.includes('fail')||t.includes('error')||t.includes('conflict')) return 'fail';
  if(t.includes('clarif')||t.includes('coverage')||t.includes('budget')||t.includes('stopped')) return 'warn';
  if(t.includes('running')||t.includes('preview')) return 'acc';
  return 'mut'; };
const esc = s => (s||'').replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const zh = s => ({created:'待执行', running:'执行中', preview_running:'预览中', preview:'预览完成',
  verified:'验收通过', merged:'已合并', stopped:'已停止', failed:'失败'})[s] || s;

let agentsFilled = false;
function fillAgents(d){
  if(agentsFilled) return;
  const sel = document.getElementById('fAgent');
  const installed = (d.installed_agents||[]);
  const order = ['codex','claude-code'];
  const opts = installed.length ? order.filter(a => installed.includes(a)).concat(installed.filter(a => !order.includes(a))) : order;
  sel.innerHTML = opts.map(a=>`<option value="${esc(a)}">${esc(a)}</option>`).join('');
  if(d.repo_root && !document.getElementById('fRepo').value) document.getElementById('fRepo').placeholder = d.repo_root;
  agentsFilled = true;
}

async function startMission(execute){
  const goal = document.getElementById('fGoal').value.trim();
  const msg = document.getElementById('launchMsg');
  if(!goal){ msg.textContent = '请先填写目标。'; return; }
  const body = {
    goal,
    repo_root: document.getElementById('fRepo').value.trim() || document.getElementById('fRepo').placeholder || '.',
    test_command: document.getElementById('fTest').value.trim(),
    agent: document.getElementById('fAgent').value,
    merge_policy: document.getElementById('fAutoMerge').checked ? 'auto' : 'manual',
    execute: !!execute
  };
  document.getElementById('btnPreview').disabled = true;
  document.getElementById('btnRun').disabled = true;
  msg.textContent = execute ? '已开始执行，看板会实时更新…' : '正在生成预览…';
  try{
    const r = await (await fetch('/api/mission/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})).json();
    msg.textContent = r.ok ? (execute?'任务已派发，进行中列可见。':'预览生成中，稍后见待验收列。') : ('无法开始：'+esc(r.error||'未知错误'));
    if(r.ok) document.getElementById('fGoal').value = '';
  }catch(e){ msg.textContent = '请求失败：'+esc(String(e)); }
  finally{
    document.getElementById('btnPreview').disabled = false;
    document.getElementById('btnRun').disabled = false;
    load();
  }
}

async function mergeMission(ev, id){
  ev.stopPropagation();
  const btn = ev.target; btn.disabled = true; btn.textContent = '合并中…';
  try{
    const r = await (await fetch('/api/mission/merge', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({mission_id:id})})).json();
    if(!r.ok){ alert('合并失败：' + (r.error || (r.merge&&r.merge.reason) || (r.merge&&r.merge.status) || '未知原因')); }
  }catch(e){ alert('合并请求失败：'+e); }
  load();
}

function missionCard(m){
  const mergeBtn = m.can_merge ? `<button class="merge" onclick="mergeMission(event,'${esc(m.mission_id)}')">合并</button>` : '';
  const stop = m.stop_reason && m.stop_reason!=='verified' ? ` · ${esc(m.stop_reason)}` : '';
  return `<div class="tcard" onclick="detail('${esc(m.mission_id)}')">
    <div class="obj">${esc(m.objective)||'(无目标)'}</div>
    <div class="foot">
      <span><span class="pill ${pill(m.status)}">${esc(zh(m.status))}</span>${mergeBtn?' '+mergeBtn:''}</span>
      <span class="meta">${esc(m.mission_id).slice(0,17)}${stop}</span>
    </div></div>`;
}

function launchCard(l){
  const p = l.state==='error' ? 'fail' : 'acc';
  const txt = l.state==='error' ? '启动失败' : (l.execute?'派发中…':'预览中…');
  const extra = l.state==='error' ? `<div class="meta">${esc(l.error||'')}</div>` : '';
  return `<div class="tcard"><div class="obj">${esc(l.goal)}</div>
    <div class="foot"><span class="pill ${p}">${txt}</span></div>${extra}</div>`;
}

function renderBoard(d){
  const cols = {todo:[], in_progress:[], in_review:[], done:[]};
  const runningObjectives = new Set((d.missions||[]).filter(m=>m.board_column==='in_progress').map(m=>m.objective));
  (d.launches||[]).forEach(l=>{
    if(l.state==='starting' && !runningObjectives.has(l.goal)) cols.in_progress.push(launchCard(l));
    else if(l.state==='error') cols.in_review.push(launchCard(l));
  });
  (d.missions||[]).forEach(m=>{ (cols[m.board_column]||cols.in_review).push(missionCard(m)); });
  const empty = t => `<div class="empty">${t}</div>`;
  document.getElementById('colTodo').innerHTML = cols.todo.join('') || empty('暂无待办。上面填目标即可开工。');
  document.getElementById('colRun').innerHTML = cols.in_progress.join('') || empty('没有正在执行的任务。');
  document.getElementById('colReview').innerHTML = cols.in_review.join('') || empty('没有等你验收的任务。');
  document.getElementById('colDone').innerHTML = cols.done.join('') || empty('还没有完成的任务。');
  document.getElementById('cTodo').textContent = cols.todo.length;
  document.getElementById('cRun').textContent = cols.in_progress.length;
  document.getElementById('cReview').textContent = cols.in_review.length;
  document.getElementById('cDone').textContent = cols.done.length;
}

async function load(){
  const d = await (await fetch('/api/data')).json();
  fillAgents(d);
  document.getElementById('ws').textContent = d.workspace_root;
  const v = d.value || {};
  document.getElementById('vVerified').textContent = v.verified || 0;
  document.getElementById('vDowngraded').textContent = v.downgraded_tasks || 0;
  const tc = v.tier_counts || {};
  document.getElementById('vTiers').textContent = `省钱路由（便宜${tc.cheap||0}·中${tc.standard||0}·强${tc.strong||0}）`;
  document.getElementById('vSpent').textContent = '$' + (v.spent_usd||0).toFixed(2);
  document.getElementById('vSaved').textContent = '$' + (v.saved_usd||0).toFixed(2);
  document.getElementById('vTokens').textContent = ((v.input_tokens||0)+(v.output_tokens||0)).toLocaleString();
  const st = d.status.state || 'none';
  const sp = document.getElementById('statusPill'); sp.textContent = st; sp.className = 'pill '+pill(st);
  document.getElementById('agentsInline').innerHTML = (d.agents||[])
    .map(a=>`<span class="pill ${a.installed?'ok':'mut'}">${esc(a.display_name||a.agent)}</span>`).join(' ');

  renderBoard(d);

  const qv = (d.queue||[]).length ? (d.queue||[]).map(q=>`
     <div class="row">
       <span class="obj">${esc(q.mission_id||'')}</span>
       <span>
         <span class="pill ${pill(q.status)}">${esc(q.status)}</span>
         <span class="meta">agent=${esc(q.agent||'默认')} · merge=${esc(q.merge_policy||'manual')} · 尝试=${esc(String(q.attempts||0))}</span>
       </span>
     </div>`).join('') : '<div class="empty">暂无托管队列。用 <code>checkpoint mission import --create --queue</code> 批量排队。</div>';
  document.getElementById('queue').innerHTML = qv;

  const gv = (d.programs||[]).length ? (d.programs||[]).map(g=>`
     <div class="row">
       <span class="obj">${esc(g.objective||'')}</span>
       <span><span class="pill ${pill(g.status)}">${esc(g.status)}</span> <span class="meta">${esc(g.program_id)} · 任务=${esc(String(g.task_count||0))}</span></span>
     </div>`).join('') : '<div class="empty">暂无项目托管。用 <code>checkpoint autopilot --file 开发计划.md --hours 5</code> 创建。</div>';
  document.getElementById('programs').innerHTML = gv;
}

function diffSection(rv){
  const ds = (rv||{}).diff_summary || {};
  if(!ds.file_count) return '';
  const files = (ds.changed_files||[]).slice(0,40).map(f=>`
    <div class="filerow"><span class="obj">${esc(f.path)}</span>
    <span><span class="add">+${f.lines_added||0}</span> <span class="del">-${f.lines_removed||0}</span></span></div>`).join('');
  const fns = (ds.functions_touched||[]).slice(0,20).map(x=>`<code>${esc(x)}</code>`).join('、');
  const checks = (ds.user_checklist||[]).map(c=>`<li>${esc(c)}</li>`).join('');
  const warn = (rv.warnings||[]).map(w=>`<div class="pill warn" style="display:block;margin:6px 0;">${esc(w)}</div>`).join('');
  return `<h3>改动摘要（${ds.file_count} 个文件，<span class="add">+${ds.lines_added}</span>/<span class="del">-${ds.lines_removed}</span>）</h3>
    ${warn}${files}
    ${fns?`<div class="meta" style="margin-top:8px;">涉及函数：${fns}</div>`:''}
    ${checks?`<h3>建议人工检查</h3><ul class="checklist">${checks}</ul>`:''}`;
}

async function detail(id){
  const d = await (await fetch('/api/mission?id='+encodeURIComponent(id))).json();
  const m = d.mission || {};
  const rv = d.review || {};
  const rounds = (d.rounds||[]).map(r=>`<div class="row"><span>Round ${esc(String(r.round))}: <b>${esc(r.type)}</b></span>
      <span><span class="pill ${pill(r.status)}">${esc(r.status)}</span> <span class="meta">${esc(r.stop_reason||r.reason||'')}</span></span></div>`).join('');
  const mergeBtn = d.can_merge ? `<button class="merge" style="font-size:14px;padding:8px 20px;" onclick="mergeMission(event,'${esc(m.mission_id)}');document.getElementById('drawer').style.display='none';">✓ 合并到主分支</button>` : '';
  const verdict = rv.verdict ? `<p class="meta">验收命令：<code>${esc(rv.command||'(工作流验收)')}</code> · 结论：<span class="pill ${pill(rv.verdict)}">${esc(rv.verdict)}</span></p>` : '';
  document.getElementById('drawerBody').innerHTML =
     `<h2>${esc(m.objective||id)}</h2>
      <p class="meta">状态：<span class="pill ${pill(m.status)}">${esc(zh(m.status))}</span> · ${esc(m.stop_reason||'')} ${d.merge_state?('· 合并：'+esc(d.merge_state)):''}</p>
      ${mergeBtn}
      ${verdict}
      ${diffSection(rv)}
      <h3>轮次</h3>${rounds||'<div class="empty">无</div>'}
      <h3>最终报告</h3><pre>${esc(d.final_report)||'(无)'}</pre>`;
  document.getElementById('drawer').style.display='block';
}
load(); setInterval(load, 4000);
</script>
</body>
</html>
"""


class _DashboardHandler(BaseHTTPRequestHandler):
    server: "_DashboardServer"

    def log_message(self, *args: Any) -> None:  # silence default logging
        return

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/data":
            data = build_dashboard_data(self.server.workspace_root)
            self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        if path == "/api/mission":
            from urllib.parse import parse_qs, urlparse

            mission_id = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            data = build_mission_detail(self.server.workspace_root, mission_id)
            self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path not in ("/api/mission/start", "/api/mission/merge"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, OSError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if path == "/api/mission/merge":
            result = merge_mission_now(self.server.workspace_root, str(payload.get("mission_id") or ""))
        else:
            result = start_workbench_mission(
                workspace_root=self.server.workspace_root,
                repo_root=str(payload.get("repo_root") or str(self.server.workspace_root.parent)),
                goal=str(payload.get("goal") or ""),
                test_command=str(payload.get("test_command") or ""),
                agent=str(payload.get("agent") or ""),
                execute=bool(payload.get("execute")),
                merge_policy=str(payload.get("merge_policy") or "manual"),
            )
        self._send(json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")


class _DashboardServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], workspace_root: Path):
        super().__init__(address, _DashboardHandler)
        self.workspace_root = workspace_root


def _bind_dashboard_server(host: str, port: int, root: Path) -> "_DashboardServer":
    """Bind the server, tolerating Windows port reservations / in-use ports.

    Windows raises PermissionError (WinError 10013) when a port falls in a
    reserved Hyper-V/WSL range, and OSError when a port is already in use. Rather
    than crash (the user just sees the dashboard "not open"), try the requested
    port, then a few common fallbacks, then an OS-assigned free port."""
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
