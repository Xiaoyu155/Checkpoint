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
) -> dict[str, Any]:
    """Launch a mission from the workbench form in a background thread.

    Runs ``run_chief_mission`` in-process (it honors ``test_command``), so the
    dashboard server thread returns immediately and the polling page shows the
    mission as it progresses. Preview (execute=False) is fast; execute runs a
    real worker and merges verified changes back."""
    goal = str(goal or "").strip()
    if not goal:
        return {"ok": False, "error": "请填写目标（goal）。"}
    launch_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    _record_launch(
        launch_id,
        goal=goal,
        execute=bool(execute),
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
                test_command=(test_command or None),
                agents=((agent,) if agent else ()),
                execute=bool(execute),
                dry_run=not bool(execute),
                merge=bool(execute),
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
    return {"ok": True, "launch_id": launch_id, "execute": bool(execute)}


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
    missions = list_missions(root)
    plans = list_plans(root)
    try:
        queue = list_mission_queue_items(root).get("items", [])
    except Exception:
        queue = []
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
    return {"mission": mission, "rounds": rounds, "final_report": final_report}


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>DevPacer</title>
<style>
  :root { --bg:#0f1115; --card:#1a1d24; --line:#2a2f3a; --fg:#e6e8ec; --mut:#8a90a0;
          --ok:#3fb950; --fail:#f85149; --warn:#d29922; --acc:#58a6ff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; }
  header { padding:18px 24px; border-bottom:1px solid var(--line); display:flex; align-items:center; gap:16px; }
  header h1 { font-size:18px; margin:0; letter-spacing:.5px; }
  header .sub { color:var(--mut); font-size:12px; }
  .pill { padding:3px 10px; border-radius:999px; font-size:12px; font-weight:600; }
  .pill.ok{background:rgba(63,185,80,.15);color:var(--ok)} .pill.fail{background:rgba(248,81,73,.15);color:var(--fail)}
  .pill.warn{background:rgba(210,153,34,.15);color:var(--warn)} .pill.mut{background:rgba(138,144,160,.15);color:var(--mut)}
  .wrap { padding:24px; max-width:1200px; margin:0 auto; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:16px; }
  .hero { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:16px; margin-bottom:16px; }
  .card.big { background:linear-gradient(160deg,#1c2230,#161a21); }
  .card.big .stat { font-size:42px; }
  .stat.ok{color:var(--ok)} .stat.acc{color:var(--acc)}
  .bar { display:flex; height:8px; border-radius:6px; overflow:hidden; margin-top:8px; background:var(--line); }
  .bar i { display:block; height:100%; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; }
  .card h2 { font-size:13px; text-transform:uppercase; letter-spacing:.5px; color:var(--mut); margin:0 0 12px; }
  .stat { font-size:28px; font-weight:700; }
  .row { display:flex; justify-content:space-between; gap:8px; padding:8px 0; border-top:1px solid var(--line); cursor:default; }
  .row:first-of-type{border-top:none}
  .row.click{cursor:pointer} .row.click:hover{background:rgba(88,166,255,.06)}
  .obj { color:var(--fg); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .meta { color:var(--mut); font-size:12px; }
  .agents li { list-style:none; padding:6px 0; border-top:1px solid var(--line); }
  .agents li:first-child{border-top:none}
  section { margin-top:28px; }
  section > h2 { font-size:14px; color:var(--fg); margin:0 0 12px; }
  #detail { position:fixed; inset:0; background:rgba(0,0,0,.6); display:none; align-items:center; justify-content:center; padding:24px; }
  #detail .panel { background:var(--card); border:1px solid var(--line); border-radius:12px; max-width:820px; width:100%; max-height:86vh; overflow:auto; padding:20px; }
  #detail pre { white-space:pre-wrap; background:var(--bg); padding:12px; border-radius:8px; border:1px solid var(--line); }
  .close { float:right; color:var(--mut); cursor:pointer; }
  .empty { color:var(--mut); padding:12px 0; }
  code { color:var(--acc); }
</style>
</head>
<body>
<header>
  <h1>DevPacer</h1>
  <span class="sub">verified delivery engine: <b>Checkpoint</b></span>
  <span id="statusPill" class="pill mut">…</span>
  <span class="sub" id="ws"></span>
</header>
<div class="wrap">
  <section class="card" style="margin-bottom:20px;">
    <h2>新建任务（工作台）</h2>
    <div style="display:grid;gap:10px;">
      <textarea id="fGoal" rows="2" placeholder="用一句话说要做什么，例如：修复 src/calc.py 的 add，让 pytest 通过" style="width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:10px;font:inherit;"></textarea>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
        <input id="fRepo" placeholder="项目根目录（默认：工作区上级）" style="background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px;font:inherit;"/>
        <input id="fTest" placeholder="验收命令，如 pytest -q / npm test / cargo test" style="background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px;font:inherit;"/>
      </div>
      <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;">
        <label class="meta">编码 agent
          <select id="fAgent" style="background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:6px;font:inherit;"></select>
        </label>
        <button id="btnPreview" onclick="startMission(false)" style="background:#243; color:var(--ok); border:1px solid var(--line); border-radius:8px; padding:9px 16px; cursor:pointer; font:inherit; font-weight:600;">预览（不花钱）</button>
        <button id="btnRun" onclick="startMission(true)" style="background:var(--acc); color:#04101f; border:none; border-radius:8px; padding:9px 18px; cursor:pointer; font:inherit; font-weight:700;">开始执行</button>
        <span id="launchMsg" class="meta"></span>
      </div>
      <div class="meta">预览只看它准备怎么干、派给谁、怎么验收；执行会在隔离分支里真改代码、跑验收命令，过了才合并回主分支。含糊的目标会被拦下来让你先说清楚。</div>
    </div>
  </section>
  <section class="hero">
    <div class="card big"><h2>已验收交付</h2><div id="vVerified" class="stat ok">0</div><div class="meta">通过 Checkpoint 产品验收的任务（不是"AI 说完成了"）</div></div>
    <div class="card big"><h2>省钱大脑路由</h2><div id="vDowngraded" class="stat acc">0</div><div class="meta" id="vTiers">把机械/普通任务派给便宜模型，而不是无脑用最贵的</div></div>
    <div class="card big"><h2>真实花费</h2><div id="vSpent" class="stat">$0</div><div class="meta" id="vSpentMeta">每次跑完记真实成本；估算省下 <b id="vSaved">$0</b></div></div>
  </section>
  <div class="grid">
    <div class="card"><h2>验收状态</h2><div id="status" class="stat">—</div><div class="meta" id="statusPath"></div></div>
    <div class="card"><h2>任务（missions）</h2><div id="missionTotal" class="stat">0</div><div class="meta" id="missionBreak"></div></div>
    <div class="card"><h2>已安装的编码 agent</h2><ul class="agents" id="agents"></ul></div>
  </div>

  <section>
    <h2>任务列表 <span class="meta">点击查看轮次与最终报告</span></h2>
    <div class="card" id="missions"><div class="empty">加载中…</div></div>
  </section>

  <section>
    <h2>计划（plans）</h2>
    <div class="card" id="plans"><div class="empty">加载中…</div></div>
  </section>
</div>

<div id="detail" onclick="if(event.target.id==='detail')this.style.display='none'">
  <div class="panel"><span class="close" onclick="document.getElementById('detail').style.display='none'">✕ 关闭</span><div id="detailBody"></div></div>
</div>

<script>
const pill = s => { const t=(s||'').toLowerCase();
  if(t.includes('pass')||t.includes('verified')) return 'ok';
  if(t.includes('fail')) return 'fail';
  if(t.includes('clarif')||t.includes('coverage')||t.includes('budget')||t.includes('stopped')) return 'warn';
  return 'mut'; };
const esc = s => (s||'').replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

let agentsFilled = false;
function fillAgents(d){
  if(agentsFilled) return;
  const sel = document.getElementById('fAgent');
  const installed = (d.installed_agents||[]);
  const opts = installed.length ? installed : ['claude-code','codex'];
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
    execute: !!execute
  };
  document.getElementById('btnPreview').disabled = true;
  document.getElementById('btnRun').disabled = true;
  msg.textContent = execute ? '已开始执行，下面会实时更新…' : '正在生成预览…';
  try{
    const r = await (await fetch('/api/mission/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})).json();
    msg.textContent = r.ok ? (execute?'任务已派发，隔离分支里干活中。':'预览已生成，见下方任务列表。') : ('无法开始：'+esc(r.error||'未知错误'));
  }catch(e){ msg.textContent = '请求失败：'+esc(String(e)); }
  finally{
    document.getElementById('btnPreview').disabled = false;
    document.getElementById('btnRun').disabled = false;
    load();
  }
}

async function load(){
  const d = await (await fetch('/api/data')).json();
  fillAgents(d);
  document.getElementById('ws').textContent = d.workspace_root;
  const v = d.value || {};
  document.getElementById('vVerified').textContent = v.verified || 0;
  document.getElementById('vDowngraded').textContent = v.downgraded_tasks || 0;
  const tc = v.tier_counts || {};
  document.getElementById('vTiers').innerHTML =
    `便宜 <b>${tc.cheap||0}</b> · 中档 <b>${tc.standard||0}</b> · 强 <b>${tc.strong||0}</b>`;
  document.getElementById('vSpent').textContent = '$' + (v.spent_usd||0).toFixed(2);
  // Saved = cost that ran on a cheap external backend (billed to cheap credits,
  // not the Claude subscription). Real, measured per run.
  document.getElementById('vSaved').textContent = '$'+(v.saved_usd||0).toFixed(2);
  document.getElementById('vSpentMeta').innerHTML = v.real_worker_runs
    ? ('订阅真实花费；走便宜后端省下 <b id="vSaved">$'+(v.saved_usd||0).toFixed(2)+'</b>')
    : '真跑一个任务后这里显示真实成本与省下的钱';
  const st = d.status.state || 'none';
  const sp = document.getElementById('statusPill'); sp.textContent = st; sp.className = 'pill '+pill(st);
  document.getElementById('status').textContent = st;
  document.getElementById('statusPath').textContent = d.status.path||'';
  document.getElementById('missionTotal').textContent = d.missions.length;
  document.getElementById('missionBreak').innerHTML = Object.entries(d.mission_counts||{})
     .map(([k,v])=>`<span class="pill ${pill(k)}">${esc(k)} ${v}</span>`).join(' ') || '—';

  document.getElementById('agents').innerHTML = (d.agents||[]).map(a=>{
    const ok = a.installed; const v = a.version?(' '+esc(a.version)):'';
    return `<li><span class="pill ${ok?'ok':'mut'}">${ok?'已安装':'未找到'}</span> <b>${esc(a.display_name||a.agent)}</b><span class="meta">${v}</span></li>`;
  }).join('') || '<li class="empty">无</li>';

  const active = (d.launches||[]).filter(l=>l.state==='starting'||l.state==='error');
  const lv = active.map(l=>{
    const p = l.state==='error' ? 'fail' : 'warn';
    const txt = l.state==='error' ? ('启动失败：'+esc(l.error||'')) : (l.execute?'派发中…':'生成预览中…');
    return `<div class="row"><span class="obj">${esc(l.goal)}</span><span><span class="pill ${p}">${txt}</span></span></div>`;
  }).join('');

  const mv = d.missions.length ? d.missions.map(m=>`
     <div class="row click" onclick="detail('${esc(m.mission_id)}')">
       <span class="obj">${esc(m.objective)||'(无目标)'}</span>
       <span><span class="pill ${pill(m.status)}">${esc(m.status)}</span> <span class="meta">${esc(m.stop_reason)}</span></span>
     </div>`).join('') : '<div class="empty">还没有任务。在上面的工作台填目标点“开始执行”，或运行 <code>checkpoint mission start --goal "…"</code></div>';
  document.getElementById('missions').innerHTML = lv + mv;

  const pv = d.plans.length ? d.plans.map(p=>`
     <div class="row"><span class="obj">${esc(p.objective)}</span>
     <span><span class="pill ${pill(p.status)}">${esc(p.status)}</span> <span class="meta">${esc(p.plan_id)}</span></span></div>`).join('')
     : '<div class="empty">还没有计划。</div>';
  document.getElementById('plans').innerHTML = pv;
}

async function detail(id){
  const d = await (await fetch('/api/mission?id='+encodeURIComponent(id))).json();
  const rounds = (d.rounds||[]).map(r=>`<div class="row"><span>Round ${esc(String(r.round))}: <b>${esc(r.type)}</b></span>
      <span><span class="pill ${pill(r.status)}">${esc(r.status)}</span> <span class="meta">${esc(r.stop_reason||'')}</span></span></div>`).join('');
  document.getElementById('detailBody').innerHTML =
     `<h2>${esc((d.mission||{}).objective||id)}</h2>
      <p class="meta">status: <span class="pill ${pill((d.mission||{}).status)}">${esc((d.mission||{}).status)}</span> · stop: ${esc((d.mission||{}).stop_reason||'')}</p>
      <h3>轮次</h3>${rounds||'<div class="empty">无</div>'}
      <h3>最终报告</h3><pre>${esc(d.final_report)||'(无)'}</pre>`;
  document.getElementById('detail').style.display='flex';
}
load(); setInterval(load, 5000);
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
        if path != "/api/mission/start":
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
        result = start_workbench_mission(
            workspace_root=self.server.workspace_root,
            repo_root=str(payload.get("repo_root") or str(self.server.workspace_root.parent)),
            goal=str(payload.get("goal") or ""),
            test_command=str(payload.get("test_command") or ""),
            agent=str(payload.get("agent") or ""),
            execute=bool(payload.get("execute")),
        )
        self._send(json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")


class _DashboardServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], workspace_root: Path):
        super().__init__(address, _DashboardHandler)
        self.workspace_root = workspace_root


def serve_dashboard(*, workspace_root: str | Path, host: str = "127.0.0.1", port: int = 8787, open_browser: bool = True) -> None:
    root = Path(workspace_root).expanduser().resolve()
    server = _DashboardServer((host, port), root)
    url = f"http://{host}:{port}/"
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
