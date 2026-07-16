"""Multi-project xiao dashboard.

This is the portfolio view for users who want one workbench to supervise
multiple repositories at once. Each project still keeps its own durable
workspace under ``<project>/.agent-workspace``; this server only aggregates
status, missions, value metrics, and live logs.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .chief_plans_store import load_verification, load_worker_records, plan_dir
from .dashboard import build_dashboard_data, build_mission_detail
from .missions import load_rounds
from .workspace import init_workspace


def normalize_project_root(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def portfolio_workspace_for_project(project_root: str | Path) -> Path:
    project = normalize_project_root(project_root)
    return init_workspace(project / ".agent-workspace", with_demo=False).root


def build_portfolio_data(project_roots: list[str | Path]) -> dict[str, Any]:
    projects: list[dict[str, Any]] = []
    totals = {
        "projects": 0,
        "missions": 0,
        "running": 0,
        "verified": 0,
        "saved_usd": 0.0,
        "saved_quota_percent": 0.0,
        "saved_minutes": 0.0,
        "mimo_runs": 0,
    }
    for raw in project_roots:
        root = normalize_project_root(raw)
        entry: dict[str, Any] = {
            "name": root.name,
            "project_root": str(root),
            "workspace_root": str(root / ".agent-workspace"),
            "ok": root.exists() and root.is_dir(),
            "error": "",
        }
        if not entry["ok"]:
            entry["error"] = "项目路径不存在"
            projects.append(entry)
            continue
        try:
            workspace = portfolio_workspace_for_project(root)
            data = build_dashboard_data(workspace)
            missions = data.get("missions") if isinstance(data.get("missions"), list) else []
            running = [
                m for m in missions
                if str(m.get("status") or "") in {"running", "created", "preview_running", "background_running"}
                or str(m.get("board_column") or "") == "in_progress"
            ]
            latest_mission = missions[0] if missions else {}
            live_logs = {}
            progress = {}
            if latest_mission.get("mission_id"):
                mission_detail = build_mission_detail(workspace, str(latest_mission["mission_id"]))
                progress = build_project_progress(workspace, latest_mission, mission_detail)
                live_logs = build_project_live_logs(progress, mission_detail)
            value = data.get("value") if isinstance(data.get("value"), dict) else {}
            mimo = value.get("mimo_efficiency") if isinstance(value.get("mimo_efficiency"), dict) else {}
            quota_percent = _saved_quota_percent(mimo)
            entry.update(
                {
                    "workspace_root": str(workspace),
                    "repo_root": data.get("repo_root"),
                    "status": data.get("status"),
                    "worker": data.get("worker"),
                    "value": value,
                    "missions": enrich_mission_evidence(workspace, missions[:12]),
                    "queue": data.get("queue") or [],
                    "launches": data.get("launches") or [],
                    "latest_live_logs": live_logs,
                    "progress": progress,
                    "saved_quota_percent": quota_percent,
                }
            )
            totals["projects"] += 1
            totals["missions"] += len(missions)
            totals["running"] += len(running)
            totals["verified"] += int(value.get("verified") or 0)
            totals["saved_usd"] += float(mimo.get("saved_usd") or 0.0)
            totals["saved_quota_percent"] += quota_percent
            totals["saved_minutes"] += float(mimo.get("saved_minutes") or 0.0)
            totals["mimo_runs"] += int(mimo.get("mimo_runs") or 0)
        except Exception as exc:  # noqa: BLE001 - one broken project must not hide the other two
            entry["ok"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"
        projects.append(entry)
    totals["saved_usd"] = round(float(totals["saved_usd"]), 4)
    totals["saved_minutes"] = round(float(totals["saved_minutes"]), 2)
    totals["saved_quota_percent"] = round(float(totals["saved_quota_percent"]) / max(int(totals["projects"] or 0), 1), 1)
    return {
        "schema_version": 1,
        "product": "xiao",
        "view": "portfolio",
        "totals": totals,
        "projects": projects,
    }


def build_project_progress(
    workspace_root: str | Path,
    mission: dict[str, Any],
    mission_detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(workspace_root).expanduser().resolve()
    mission_id = str(mission.get("mission_id") or "").strip()
    detail = mission_detail or (build_mission_detail(root, mission_id) if mission_id else {})
    rounds = detail.get("rounds") if isinstance(detail.get("rounds"), list) else []
    if not rounds and mission_id:
        rounds = load_rounds(root, mission_id)
    latest_round = rounds[-1] if rounds else {}
    background = _inspect_background(root, mission_id) if mission_id else {}
    agent = _extract_agent_from_rounds(rounds)
    model = _effective_model(agent, _extract_model_from_rounds(rounds))
    live_logs = detail.get("live_logs") if isinstance(detail.get("live_logs"), dict) else {}
    log_note = ""
    if live_logs.get("count") and not str(live_logs.get("latest_tail") or "").strip():
        log_note = "日志文件已创建，等待 worker 输出。"
    phase = _progress_phase(mission, latest_round, background)
    return {
        "mission_id": mission_id,
        "phase": phase,
        "status": str(mission.get("status") or ""),
        "stop_reason": str(mission.get("stop_reason") or ""),
        "latest_round_type": str(latest_round.get("type") or ""),
        "latest_round_status": str(latest_round.get("status") or ""),
        "background_status": str(background.get("status") or ""),
        "background_process_state": str(background.get("process_state") or ""),
        "background_alive": bool(background.get("alive")),
        "pid": background.get("pid"),
        "worker_pid": background.get("worker_pid"),
        "model": model,
        "agent": agent,
        "stdout_log": str(background.get("stdout_log") or ""),
        "stderr_log": str(background.get("stderr_log") or ""),
        "log_note": log_note,
    }


def enrich_mission_evidence(workspace_root: str | Path, missions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach compact Pacer execution evidence to mission summaries.

    The portfolio view is used as an audit surface. A plain mission status is not
    enough; users need to see worker, backend, verification, merge, and report
    traces without opening files manually.
    """
    root = Path(workspace_root).expanduser().resolve()
    enriched: list[dict[str, Any]] = []
    for mission in missions:
        item = dict(mission)
        mission_id = str(item.get("mission_id") or "").strip()
        plan_id = str(item.get("plan_id") or mission_id).strip()
        workers = load_worker_records(root, plan_id) if plan_id else []
        latest_worker = workers[-1] if workers else {}
        verification = load_verification(root, plan_id) if plan_id else None
        verification = verification if isinstance(verification, dict) else {}
        rounds = load_rounds(root, mission_id) if mission_id else []
        merge_rounds = [r for r in rounds if isinstance(r, dict) and str(r.get("type") or "") == "merge"]
        latest_merge = merge_rounds[-1] if merge_rounds else {}
        backend = latest_worker.get("backend") if isinstance(latest_worker.get("backend"), dict) else {}
        log_path = str(latest_worker.get("log_path") or "")
        report_path = root / "missions" / mission_id / "final_report.md" if mission_id else Path()
        command = ""
        if isinstance(verification.get("command_verification"), dict):
            command = str(verification["command_verification"].get("command") or "")
        if not command:
            command = str(verification.get("command") or "")
        item["pacer_evidence"] = {
            "worker_status": str(latest_worker.get("status") or ""),
            "worker_command": str(latest_worker.get("command") or ""),
            "worker_exit_code": latest_worker.get("exit_code"),
            "worker_seconds": latest_worker.get("elapsed_seconds"),
            "agent": str(latest_worker.get("agent") or item.get("agent") or ""),
            "backend": str(backend.get("name") or ""),
            "model": str(backend.get("model") or ""),
            "worktree": str(latest_worker.get("cwd") or ""),
            "log_path": log_path,
            "log_tail": _read_log_tail(Path(log_path), max_chars=1600) if log_path else "",
            "verification_verdict": str(verification.get("verdict") or ""),
            "verification_command": command,
            "verification_path": str(plan_dir(root, plan_id) / "verification.json") if plan_id else "",
            "merge_status": str(latest_merge.get("status") or item.get("merge_state") or ""),
            "merge_reason": str(latest_merge.get("stop_reason") or ""),
            "final_report": str(report_path) if report_path else "",
            "worker_records": len(workers),
            "rounds": len(rounds),
        }
        enriched.append(item)
    return enriched


def build_project_live_logs(progress: dict[str, Any], mission_detail: dict[str, Any]) -> dict[str, Any]:
    """Expose the actual background stdout/stderr tail for the portfolio view.

    The normal mission detail may show derived logs. The portfolio should map to
    the real worker files so users can see the same output the process writes.
    """
    sources: list[dict[str, Any]] = []
    for label, key in (("stdout", "stdout_log"), ("stderr", "stderr_log")):
        path_text = str(progress.get(key) or "").strip()
        if not path_text:
            continue
        path = Path(path_text)
        tail = _read_log_tail(path, max_chars=7000)
        sources.append(
            {
                "label": label,
                "path": str(path),
                "exists": path.exists(),
                "modified": path.stat().st_mtime if path.exists() else 0,
                "tail": tail,
            }
        )
    if not sources:
        existing = mission_detail.get("live_logs") if isinstance(mission_detail.get("live_logs"), dict) else {}
        return existing
    latest = max(sources, key=lambda item: float(item.get("modified") or 0))
    latest_tail = str(latest.get("tail") or "")
    if not latest_tail:
        fallback = mission_detail.get("live_logs") if isinstance(mission_detail.get("live_logs"), dict) else {}
        latest_tail = str(fallback.get("latest_tail") or "")
    return {
        "count": len(sources),
        "latest_tail": latest_tail,
        "latest_path": str(latest.get("path") or ""),
        "latest_label": str(latest.get("label") or ""),
        "sources": sources,
    }


def _read_log_tail(path: Path, *, max_chars: int = 7000) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - max_chars * 3))
            data = handle.read()
        return data.decode("utf-8", errors="replace")[-max_chars:]
    except OSError:
        return ""


def _saved_quota_percent(mimo: dict[str, Any]) -> float:
    saved = float(mimo.get("saved_usd") or 0.0)
    spent = float(mimo.get("spent_usd") or 0.0)
    if saved <= 0 and spent <= 0:
        return 0.0
    return round(saved / max(saved + spent, 0.01) * 100.0, 1)


def _inspect_background(workspace_root: Path, mission_id: str) -> dict[str, Any]:
    try:
        from .chief_background import inspect_background_state

        payload = inspect_background_state(workspace_root=workspace_root, mission_id=mission_id, update=True)
    except Exception as exc:  # noqa: BLE001 - progress telemetry must not break the dashboard
        return {"status": "unknown", "error": f"{type(exc).__name__}: {exc}"}
    return payload if isinstance(payload, dict) else {}


def _extract_model_from_rounds(rounds: list[dict[str, Any]]) -> str:
    for item in reversed(rounds):
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        worker = payload.get("worker") if isinstance(payload.get("worker"), dict) else {}
        argv = worker.get("argv") if isinstance(worker.get("argv"), list) else []
        for index, arg in enumerate(argv):
            if str(arg) == "--model" and index + 1 < len(argv):
                return str(argv[index + 1])
        command = str(worker.get("command") or "")
        marker = "--model "
        if marker in command:
            return command.split(marker, 1)[1].split(maxsplit=1)[0].strip('"')
    return ""


def _extract_agent_from_rounds(rounds: list[dict[str, Any]]) -> str:
    for item in reversed(rounds):
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        worker = payload.get("worker") if isinstance(payload.get("worker"), dict) else {}
        agent = str(worker.get("agent") or "").strip()
        if agent:
            return agent
    return ""


def _effective_model(agent: str, planned_model: str) -> str:
    name = str(agent or "").strip()
    if name:
        try:
            from .agent_backends import resolve_backend_by_name

            backend = resolve_backend_by_name(name)
        except Exception:  # noqa: BLE001 - model telemetry must degrade to the planned command
            backend = None
        if backend and str(backend.get("model") or "").strip():
            return str(backend["model"])
    return str(planned_model or "")


def _progress_phase(mission: dict[str, Any], latest_round: dict[str, Any], background: dict[str, Any]) -> str:
    bg_status = str(background.get("status") or "")
    bg_state = str(background.get("process_state") or "")
    if bg_status == "running" or bg_state == "running" or background.get("alive"):
        return "后台执行中"
    if bg_status == "completed":
        result_status = str(background.get("result_status") or mission.get("status") or "")
        result_reason = str(background.get("result_stop_reason") or mission.get("stop_reason") or "")
        if result_status == "verified":
            return "已验收通过"
        if result_status == "stopped":
            return f"已停止：{result_reason or '任务未通过'}"
        return "后台执行结束"
    if bg_status in {"failed", "timeout", "orphaned"}:
        return "后台异常停止"
    round_type = str(latest_round.get("type") or "")
    round_status = str(latest_round.get("status") or "")
    if round_type == "dispatch_preview":
        return "已生成执行计划"
    if round_type == "background":
        return "后台已启动"
    if round_type:
        return f"{round_type}: {round_status or '进行中'}"
    status = str(mission.get("status") or "")
    return status or "等待任务落盘"


PORTFOLIO_HTML = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>xiao 多项目观察台</title>
<style>
:root{--bg:#0b1117;--panel:#111720;--card:#161d27;--card2:#1b2430;--line:#283341;--fg:#e6edf3;--mut:#90a0b3;--ok:#4ade80;--warn:#f2c94c;--fail:#fb7185;--acc:#60a5fa}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{height:58px;display:flex;align-items:center;gap:14px;padding:0 18px;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:10}
.brand{font-size:16px;font-weight:760}.sub{color:var(--mut);font-size:12px}.spacer{flex:1}.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:650;background:rgba(144,160,179,.12);color:var(--mut);white-space:nowrap}.pill.ok{background:rgba(74,222,128,.12);color:var(--ok)}.pill.warn{background:rgba(242,201,76,.12);color:var(--warn)}.pill.fail{background:rgba(251,113,133,.12);color:var(--fail)}.pill.acc{background:rgba(96,165,250,.12);color:var(--acc)}
main{padding:18px;display:flex;flex-direction:column;gap:14px}.stats{display:grid;grid-template-columns:repeat(7,minmax(112px,1fr));gap:10px}.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}.label{color:var(--mut);font-size:11px;font-weight:700}.value{font-size:22px;font-weight:760;margin-top:4px}
.tabs{display:flex;gap:8px;flex-wrap:wrap}.tab{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:8px 11px;min-width:120px;text-align:left}.tab.active{border-color:var(--acc);background:rgba(96,165,250,.12)}.tab-name{font-weight:760}.tab-sub{display:block;color:var(--mut);font-size:11px;margin-top:2px}.project{background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}.ph{padding:13px 14px;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:flex-start}.pt{font-size:15px;font-weight:750}.path{color:var(--mut);font-size:11px;word-break:break-all}.body{padding:12px 14px;display:grid;gap:12px}.mini{display:grid;grid-template-columns:repeat(6,1fr);gap:8px}.mini div{background:var(--card2);border:1px solid rgba(255,255,255,.04);border-radius:7px;padding:8px}.mini b{display:block;font-size:16px}.section-title{font-size:11px;color:var(--mut);font-weight:800;text-transform:uppercase;letter-spacing:.6px;margin-bottom:5px}.progress{background:var(--card2);border:1px solid rgba(255,255,255,.04);border-radius:7px;padding:9px}.progress-line{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}.mission{padding:9px;border:1px solid rgba(255,255,255,.04);border-radius:7px;background:var(--card2);margin-bottom:8px}.mission-goal{font-size:12px;max-height:38px;overflow:hidden}.mission-meta{margin-top:6px;display:flex;gap:5px;align-items:center;flex-wrap:wrap}.evidence{margin-top:8px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}.ev{min-width:0;background:#0d131b;border:1px solid rgba(255,255,255,.04);border-radius:6px;padding:6px}.ev span{display:block;color:var(--mut);font-size:10px;font-weight:750}.ev code{display:block;color:#d7e3f1;font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.ev.wide{grid-column:1/-1}.ev-tail{white-space:pre-wrap;max-height:92px;overflow:auto}.logbox{white-space:pre-wrap;word-break:break-word;background:#090d13;border:1px solid var(--line);border-radius:7px;color:#d7e3f1;padding:9px;height:360px;overflow:auto;margin:0}.empty{color:var(--mut);font-size:12px;padding:8px;background:var(--card2);border-radius:7px}button{border:1px solid var(--line);background:var(--card2);color:var(--fg);border-radius:7px;padding:7px 11px;font:inherit;font-weight:650;cursor:pointer}button:hover{border-color:var(--acc);color:var(--acc)}
@media(max-width:900px){.stats{grid-template-columns:repeat(2,1fr)}.mini{grid-template-columns:repeat(2,1fr)}header{height:auto;min-height:58px;flex-wrap:wrap;padding:10px 14px}.tab{flex:1;min-width:150px}}
</style>
</head>
<body>
<header><div><div class="brand">xiao 多项目观察台</div><div class="sub">同时监督多个项目的开发、验收、实时日志和额度指标</div></div><div class="spacer"></div><button onclick="load()">刷新</button><span id="stamp" class="pill acc">加载中</span></header>
<main>
<section class="stats">
  <div class="stat"><div class="label">项目</div><div id="sProjects" class="value">0</div></div>
  <div class="stat"><div class="label">任务</div><div id="sMissions" class="value">0</div></div>
  <div class="stat"><div class="label">执行中</div><div id="sRunning" class="value">0</div></div>
  <div class="stat"><div class="label">已验收</div><div id="sVerified" class="value">0</div></div>
  <div class="stat"><div class="label">MiMo 节省</div><div id="sSaved" class="value">$0.00</div></div>
  <div class="stat"><div class="label">额度节省</div><div id="sQuota" class="value">0%</div></div>
  <div class="stat"><div class="label">MiMo 运行</div><div id="sMimo" class="value">0</div></div>
</section>
<section id="tabs" class="tabs"></section>
<section id="projects"></section>
</main>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let activeProject=localStorage.getItem('xiao.activeProject')||'';
function cls(s){s=String(s||'').toLowerCase();if(s.includes('verified')||s.includes('pass'))return'ok';if(s.includes('running')||s.includes('preview'))return'acc';if(s.includes('fail')||s.includes('error'))return'fail';if(s.includes('stop')||s.includes('clarif')||s.includes('budget'))return'warn';return''}
function evidence(m){
  const e=m.pacer_evidence||{};
  if(!e.worker_status&&!e.verification_verdict&&!e.log_path) return '';
  return `<div class="evidence">
    <div class="ev"><span>Worker</span><code>${esc(e.worker_status||'-')} ${esc(e.worker_exit_code!==undefined?'exit '+e.worker_exit_code:'')}</code></div>
    <div class="ev"><span>后端 / 模型</span><code>${esc(e.backend||e.agent||'-')} ${esc(e.model||'')}</code></div>
    <div class="ev"><span>验收</span><code>${esc(e.verification_verdict||'-')}</code></div>
    <div class="ev"><span>Merge</span><code>${esc(e.merge_status||'-')} ${esc(e.merge_reason||'')}</code></div>
    <div class="ev wide"><span>验收命令</span><code title="${esc(e.verification_command||'')}">${esc(e.verification_command||'-')}</code></div>
    <div class="ev wide"><span>Worktree</span><code title="${esc(e.worktree||'')}">${esc(e.worktree||'-')}</code></div>
    <div class="ev wide"><span>日志 / 报告</span><code title="${esc((e.log_path||'')+' | '+(e.final_report||''))}">${esc(e.log_path||'-')}</code></div>
    ${e.log_tail?`<div class="ev wide"><span>Worker 日志尾部</span><code class="ev-tail">${esc(e.log_tail)}</code></div>`:''}
  </div>`;
}
function mission(m){return `<div class="mission"><div class="mission-goal">${esc(m.objective||m.goal||'（无目标）')}</div><div class="mission-meta"><span class="pill ${cls(m.status)}">${esc(m.status||'unknown')}</span>${m.stop_reason?`<span class="pill warn">${esc(m.stop_reason)}</span>`:''}<span class="pill">${esc((m.mission_id||'').slice(0,17))}</span></div>${evidence(m)}</div>`}
function tab(p,i){
  const pg=p.progress||{}, run=pg.background_alive||String(pg.phase||'').includes('执行中');
  const active=(p.project_root===activeProject)||(!activeProject&&i===0);
  return `<button class="tab ${active?'active':''}" onclick="activeProject='${esc(p.project_root)}';localStorage.setItem('xiao.activeProject',activeProject);render(window.lastPortfolio)"><span class="tab-name">${esc(p.name)}</span><span class="tab-sub">${run?'执行中':esc(pg.phase||p.status?.state||'等待')}</span></button>`;
}
function project(p){
  const v=p.value||{}, me=v.mimo_efficiency||{}, logs=p.latest_live_logs||{}, pg=p.progress||{};
  const missions=(p.missions||[]).slice(0,5).map(mission).join('')||'<div class="empty">暂无 mission</div>';
  const tail=logs.latest_tail?`<pre id="logbox" class="logbox">${esc(logs.latest_tail)}</pre>`:`<div class="empty">${esc(pg.log_note||'暂无实时日志')}</div>`;
  const progress=`<div class="progress"><div class="section-title">当前进展</div><div>${esc(pg.phase||'等待任务')}</div><div class="progress-line">${pg.model?`<span class="pill acc">${esc(pg.model)}</span>`:''}${pg.agent?`<span class="pill">${esc(pg.agent)}</span>`:''}${pg.pid?`<span class="pill">PID ${esc(pg.pid)}</span>`:''}${pg.worker_pid?`<span class="pill">Worker ${esc(pg.worker_pid)}</span>`:''}${pg.latest_round_type?`<span class="pill">${esc(pg.latest_round_type)} ${esc(pg.latest_round_status)}</span>`:''}</div></div>`;
  const status=p.ok?'<span class="pill ok">可观察</span>':`<span class="pill fail">${esc(p.error||'不可用')}</span>`;
  return `<article class="project"><div class="ph"><div style="flex:1"><div class="pt">${esc(p.name)}</div><div class="path">${esc(p.project_root)}</div></div>${status}</div><div class="body">
    <div class="mini"><div><span class="label">任务</span><b>${(p.missions||[]).length}</b></div><div><span class="label">队列</span><b>${(p.queue||[]).length}</b></div><div><span class="label">已验收</span><b>${v.verified||0}</b></div><div><span class="label">MiMo</span><b>${me.mimo_runs||0}</b></div><div><span class="label">额度节省</span><b>${Number(p.saved_quota_percent||0).toFixed(1)}%</b></div><div><span class="label">节省时间</span><b>${Number(me.saved_minutes||0).toFixed(1)}分</b></div></div>
    ${progress}
    <div><div class="section-title">Pacer 测试证据</div>${missions}</div>
    <div><div class="section-title">实时日志 ${logs.latest_path?`<span class="pill">${esc(logs.latest_label||'log')}</span>`:''}</div>${tail}</div>
  </div></article>`
}
function render(d){
  window.lastPortfolio=d||{projects:[]};
  const ps=d.projects||[];
  if(!activeProject&&ps[0]) activeProject=ps[0].project_root;
  tabs.innerHTML=ps.map(tab).join('');
  const selected=ps.find(p=>p.project_root===activeProject)||ps[0];
  projects.innerHTML=selected?project(selected):'<div class="empty">暂无项目</div>';
  requestAnimationFrame(()=>{const lb=document.getElementById('logbox'); if(lb) lb.scrollTop=lb.scrollHeight;});
}
async function load(){
  const d=await (await fetch('/api/portfolio')).json();
  const t=d.totals||{};
  sProjects.textContent=t.projects||0;sMissions.textContent=t.missions||0;sRunning.textContent=t.running||0;sVerified.textContent=t.verified||0;sSaved.textContent='$'+Number(t.saved_usd||0).toFixed(2);sQuota.textContent=Number(t.saved_quota_percent||0).toFixed(1)+'%';sMimo.textContent=t.mimo_runs||0;
  render(d);
  stamp.textContent='已刷新 '+new Date().toLocaleTimeString();
}
load();setInterval(load,3000);
</script>
</body>
</html>
"""


class _PortfolioHandler(BaseHTTPRequestHandler):
    server: "_PortfolioServer"

    def log_message(self, *_args: Any) -> None:
        return

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in {"/", "/index.html"}:
            self._send(PORTFOLIO_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/portfolio":
            payload = build_portfolio_data(self.server.project_roots)
            self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        self.send_response(404)
        self.end_headers()


class _PortfolioServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], project_roots: list[Path]):
        super().__init__(address, _PortfolioHandler)
        self.project_roots = project_roots


def _bind_portfolio_server(host: str, port: int, roots: list[Path]) -> "_PortfolioServer":
    candidates = [port, 8797, 8798, 8898, 0]
    seen: set[int] = set()
    last_error: Exception | None = None
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            return _PortfolioServer((host, candidate), roots)
        except (PermissionError, OSError) as exc:
            last_error = exc
    raise RuntimeError(f"Could not bind xiao portfolio dashboard on {host}: {last_error}")


def serve_portfolio_dashboard(
    *,
    project_roots: list[str | Path],
    host: str = "127.0.0.1",
    port: int = 8797,
    open_browser: bool = True,
) -> None:
    roots = [normalize_project_root(path) for path in project_roots]
    server = _bind_portfolio_server(host, port, roots)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/"
    if actual_port != port:
        print(f"Port {port} was unavailable; using {actual_port} instead.")
    print(f"xiao portfolio dashboard: {url}")
    print("Projects:")
    for root in roots:
        print(f"- {root}")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
