#!/usr/bin/env python3
"""3-4h multi-mission Pacer host stress supervisor.

Launches waves of concurrent missions against a real repo, polls statuses,
records memory/model usage, and writes a final report. Safe defaults:
--allow-dirty, no auto-merge, isolated worktrees only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(path: Path, message: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, flush=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def load_missions(ws: Path, prefix: str | None = None) -> list[dict]:
    rows: list[dict] = []
    root = ws / "missions"
    if not root.is_dir():
        return rows
    for path in root.iterdir():
        if not path.is_dir():
            continue
        mf = path / "mission.json"
        if not mf.exists():
            continue
        try:
            data = json.loads(mf.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        created = str(data.get("created_at") or "")
        if prefix and not created.startswith(prefix):
            continue
        progress: dict = {}
        pf = path / "progress.json"
        if pf.exists():
            try:
                progress = json.loads(pf.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                progress = {}
        rows.append(
            {
                "id": str(data.get("mission_id") or path.name),
                "status": str(data.get("status") or ""),
                "stop": str(data.get("stop_reason") or ""),
                "objective": str(data.get("objective") or "")[:100],
                "created_at": created,
                "stage": str(progress.get("stage") or ""),
                "product_files": progress.get("changed_product_file_count"),
                "agent": str(progress.get("agent") or ""),
            }
        )
    return sorted(rows, key=lambda r: r["created_at"], reverse=True)


def is_active(row: dict) -> bool:
    """True only for genuinely in-flight missions (not terminal ghosts)."""
    status = str(row.get("status") or "")
    stage = str(row.get("stage") or "")
    stop = str(row.get("stop") or "")
    if status in {"verified", "stopped", "preview", "merged", "orphaned"}:
        return False
    if stop in {"verified", "preview_only", "worker_orphaned", "quota_exhausted"}:
        return False
    if stage in {"verified", "blocked", "worker_failed_tests_pass"}:
        return False
    if status in {"running", "created", "background_running"}:
        return True
    return stage in {
        "worker_running",
        "worker_starting",
        "verification_running",
        "background_started",
        "worker_started",
        "dispatch_ready",
        "planning",
        "resuming",
        "verifying",
        "repairing",
    }


def probe_agent_or_skip(agent: str) -> tuple[bool, str]:
    """Return (ok, reason). Soft-import Pacer liveness when available."""
    try:
        from visual_agent.provider_liveness import probe_worker_agent_liveness

        probe = probe_worker_agent_liveness(agent)
        if probe.get("ok"):
            return True, "ok"
        return False, f"{probe.get('stop_reason')}: {probe.get('message')}"
    except Exception as exc:  # noqa: BLE001
        return True, f"probe_skipped:{exc}"


def launch_mission(
    *,
    py: str,
    proj: Path,
    ws: Path,
    goal: str,
    agent: str,
    allow_test: bool,
    log_dir: Path,
    tag: str,
) -> dict:
    out = log_dir / f"{tag}.out.log"
    err = log_dir / f"{tag}.err.log"
    test_cmd = f"{py} -m pytest -q"
    argv = [
        py,
        "-m",
        "visual_agent.cli",
        "mission",
        "start",
        "--goal",
        goal,
        "--test-command",
        test_cmd,
        "--agent",
        agent or DEFAULT_AGENT,
        "--execute",
        "--allow-dirty",
        "--background",
        "--max-rounds",
        "3",
        "--max-repair-rounds",
        "1",
        "--max-wall-minutes",
        "50",
        "--max-worker-minutes",
        "35",
        "--repo-root",
        str(proj),
        "--workspace-root",
        str(ws),
        "--format",
        "markdown",
    ]
    if allow_test:
        argv.append("--allow-test-edits")
    fo = out.open("w", encoding="utf-8", errors="replace")
    fe = err.open("w", encoding="utf-8", errors="replace")
    env = os.environ.copy()
    # Ensure visual_agent is importable for mission start subprocesses.
    src_hint = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = src_hint + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        argv,
        cwd=str(proj),
        stdout=fo,
        stderr=fe,
        text=True,
        env=env,
    )
    return {
        "tag": tag,
        "pid": proc.pid,
        "agent": agent,
        "goal": goal[:120],
        "out": str(out),
        "err": str(err),
        "started": utc_now(),
        "proc": proc,
        "fo": fo,
        "fe": fe,
    }


def collect_model_usage(ws: Path, limit: int = 20) -> list[dict]:
    plans = ws / "chief_plans"
    if not plans.is_dir():
        return []
    rows: list[dict] = []
    for plan_dir in sorted(plans.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        workers = plan_dir / "workers.jsonl"
        if not workers.exists():
            continue
        try:
            rec = json.loads(workers.read_text(encoding="utf-8-sig").splitlines()[-1])
        except (OSError, json.JSONDecodeError, IndexError):
            continue
        usage = rec.get("usage") if isinstance(rec.get("usage"), dict) else {}
        rows.append(
            {
                "plan": plan_dir.name,
                "agent": rec.get("agent"),
                "model": rec.get("resolved_model"),
                "status": rec.get("status"),
                "exit_code": rec.get("exit_code"),
                "total_tokens": usage.get("total_tokens") or usage.get("input_tokens"),
                "cost_usd": usage.get("cost_usd"),
            }
        )
    return rows


def memory_snippet(ws: Path, py: str, proj: Path) -> str:
    try:
        completed = subprocess.run(
            [
                py,
                "-m",
                "visual_agent.cli",
                "mission",
                "memory",
                "--workspace-root",
                str(ws),
                "--limit",
                "6",
                "--format",
                "markdown",
            ],
            cwd=str(proj),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"memory error: {exc}"
    text = (completed.stdout or completed.stderr or "").strip()
    return text[:2500]


def run_pytest(py: str, proj: Path) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            [py, "-m", "pytest", "-q", "--tb=no"],
            cwd=str(proj),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    tail = ((completed.stdout or "") + (completed.stderr or ""))[-400:]
    return int(completed.returncode), tail


# Token source policy for long-host: Codex is primary (user quota path).
# Keep agent="codex" for essentially all waves; Claude is not used in overnight stress.
DEFAULT_AGENT = "codex"

WAVE_TASKS: list[list[dict]] = [
    # Wave A: docs/comments across modules
    [
        {
            "id": "A1",
            "agent": "codex",
            "allow_test": False,
            "goal": (
                "In app/services/market_data.py add a concise Chinese module docstring describing "
                "public market data fetch purpose. No logic or test changes."
            ),
        },
        {
            "id": "A2",
            "agent": "codex",
            "allow_test": False,
            "goal": (
                "In app/services/paper_trading.py add a Chinese class/function docstring for the main "
                "paper trading entry explaining simulation-only intent. No logic or test changes."
            ),
        },
        {
            "id": "A3",
            "agent": "codex",
            "allow_test": False,
            "goal": (
                "In app/strategies/base.py add a short Chinese comment on the strategy base class "
                "about risk checks preceding execution. No logic or test changes."
            ),
        },
        {
            "id": "A4",
            "agent": "codex",
            "allow_test": True,
            "goal": (
                "In tests/test_virtual_account.py add one small unit test for an obvious safe edge case "
                "if missing; keep all tests green. Prefer minimal test-only change."
            ),
        },
    ],
    # Wave B: strategy/docs
    [
        {
            "id": "B1",
            "agent": "codex",
            "allow_test": False,
            "goal": (
                "In app/strategies/rsi.py add Chinese docstring for the main RSI strategy class/function. "
                "No logic or test changes."
            ),
        },
        {
            "id": "B2",
            "agent": "codex",
            "allow_test": False,
            "goal": (
                "In app/strategies/dca.py add Chinese docstring for the main DCA strategy class/function. "
                "No logic or test changes."
            ),
        },
        {
            "id": "B3",
            "agent": "codex",
            "allow_test": False,
            "goal": (
                "In app/strategies/grid.py add Chinese docstring for the main grid strategy class/function. "
                "No logic or test changes."
            ),
        },
        {
            "id": "B4",
            "agent": "codex",
            "allow_test": False,
            "goal": (
                "In app/config.py add a short Chinese comment near RiskLimits explaining risk-first defaults. "
                "No behavior or test changes."
            ),
        },
    ],
    # Wave C: deeper but still safe
    [
        {
            "id": "C1",
            "agent": "codex",
            "allow_test": False,
            "goal": (
                "In app/services/plan_validator.py add Chinese docstrings for public validation helpers. "
                "Do not change validation logic or tests."
            ),
        },
        {
            "id": "C2",
            "agent": "codex",
            "allow_test": False,
            "goal": (
                "In app/services/auto_runner.py add a Chinese module docstring describing auto-run loop intent. "
                "No logic or test changes."
            ),
        },
        {
            "id": "C3",
            "agent": "codex",
            "allow_test": True,
            "goal": (
                "In tests/test_backtest.py add one lightweight assertion-only unit test for a clear pure helper "
                "or obvious edge if available; keep suite green. Minimal test-only edits."
            ),
        },
        {
            "id": "C3b",
            "agent": "codex",
            "allow_test": False,
            "goal": (
                "In README.md add a short Chinese subsection 'Pacer 托管备注' with 2-3 lines that tests must pass "
                "before merge. Do not change code."
            ),
        },
    ],
    # Wave D: continuity / memory-oriented
    [
        {
            "id": "D1",
            "agent": "codex",
            "allow_test": False,
            "goal": (
                "Using project memory of prior risk.py comment tasks, confirm evaluate_order Chinese comment exists "
                "in worktrees or source; if missing in app/services/risk.py on this branch/worktree context, add one "
                "line only. No logic/test changes."
            ),
        },
        {
            "id": "D2",
            "agent": "codex",
            "allow_test": False,
            "goal": (
                "In PRODUCT_SPEC.md append a short Chinese bullet list (3 items) under a new '工程约束' section if "
                "absent: risk first, paper before live, tests before merge. Minimal doc-only change."
            ),
        },
        {
            "id": "D3",
            "agent": "codex",
            "allow_test": False,
            "goal": (
                "In DEVELOPMENT_PLAN.md append one Chinese checkbox item about continuous pytest acceptance for "
                "hosted changes. Doc-only."
            ),
        },
    ],
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--hours", type=float, default=3.5)
    parser.add_argument("--wave-gap-minutes", type=float, default=35.0)
    parser.add_argument("--poll-seconds", type=float, default=90.0)
    parser.add_argument("--start-wave", type=int, default=0, help="Skip earlier waves (resume support)")
    parser.add_argument("--max-active", type=int, default=6, help="Do not launch new wave if active >= this")
    args = parser.parse_args()

    py = str(Path(args.python).resolve())
    proj = Path(args.repo_root).resolve()
    ws = Path(args.workspace_root).resolve()
    log_dir = ws / "overnight-stress"
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    session_log = log_dir / f"supervisor-{run_id}.log"
    report_path = log_dir / f"report-{run_id}.md"
    manifest = log_dir / f"launches-{run_id}.jsonl"
    session_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    # Persist pid for health checks
    pid_file = log_dir / "supervisor.pid"

    hours = max(1.0, float(args.hours))
    deadline = time.time() + hours * 3600
    log(session_log, f"START overnight supervisor hours={hours} repo={proj} pid={os.getpid()}")
    log(session_log, f"session_prefix_hint={session_prefix}")
    try:
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass

    launched: list[dict] = []
    wave_index = max(0, min(int(args.start_wave), len(WAVE_TASKS)))
    next_wave_at = time.time()  # launch first pending wave immediately
    max_active = max(1, int(args.max_active))
    state = {"wave_index": wave_index, "next_wave_at": next_wave_at}
    baseline_code, baseline_tail = run_pytest(py, proj)
    log(session_log, f"baseline pytest code={baseline_code} tail={baseline_tail!r}")
    log(session_log, f"start_wave={state['wave_index']} max_active={max_active}")

    while time.time() < deadline:
        try:
            _supervise_tick(
                py=py,
                proj=proj,
                ws=ws,
                log_dir=log_dir,
                session_log=session_log,
                manifest=manifest,
                launched=launched,
                state=state,
                max_active=max_active,
                wave_gap_minutes=float(args.wave_gap_minutes),
                poll_seconds=float(args.poll_seconds),
            )
        except Exception as exc:  # noqa: BLE001
            log(session_log, f"TICK_ERROR {type(exc).__name__}: {exc}")

        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(float(args.poll_seconds), max(30.0, remaining)))

    wave_index = int(state["wave_index"])

    # Final report (also reachable after loop)
    rows = [r for r in load_missions(ws) if r["created_at"] >= "2026-07-17T14:00:00"]
    summary = {
        "verified": sum(1 for r in rows if r["status"] == "verified" or r["stop"] == "verified"),
        "stopped": sum(1 for r in rows if r["status"] == "stopped"),
        "running": sum(1 for r in rows if is_active(r)),
        "preview": sum(1 for r in rows if r["status"] == "preview" or r["stop"] == "preview_only"),
        "total_considered": len(rows),
        "waves_launched": wave_index,
        "launches_attempted": len(launched),
    }
    usage = collect_model_usage(ws, limit=30)
    code, tail = run_pytest(py, proj)
    mem = memory_snippet(ws, py, proj)

    report = [
        f"# Overnight long-host stress report ({run_id})",
        "",
        f"- repo: `{proj}`",
        f"- hours requested: {hours}",
        f"- ended: {utc_now()}",
        f"- summary: `{json.dumps(summary, ensure_ascii=False)}`",
        f"- main pytest exit: `{code}`",
        f"- main pytest tail: `{tail.strip()}`",
        "",
        "## Missions (since 14:00 UTC-ish local run day)",
        "",
    ]
    for row in rows[:40]:
        report.append(
            f"- `{row['id']}` **{row['status']}/{row['stop'] or '-'}** "
            f"stage={row['stage'] or '-'} files={row['product_files']} — {row['objective']}"
        )
    report.extend(["", "## Model usage (recent workers)", ""])
    for item in usage[:20]:
        report.append(
            f"- `{item['plan'][:28]}` agent={item['agent']} model={item['model']} "
            f"status={item['status']} tokens={item['total_tokens']} cost={item['cost_usd']}"
        )
    report.extend(["", "## Memory snapshot", "", "```", mem[:2000], "```", ""])
    report.extend(
        [
            "## Conclusion hints",
            "",
            "- True long-host requires concurrent missions to reach terminal states without main-branch corruption.",
            "- Watch for: PermissionError on mission.json, wrong python inside worker, budget demotion, model hijack.",
            f"- Supervisor log: `{session_log}`",
            f"- Launch manifest: `{manifest}`",
            "",
        ]
    )
    report_path.write_text("\n".join(report), encoding="utf-8")
    log(session_log, f"FINAL summary={summary}")
    log(session_log, f"REPORT {report_path}")
    try:
        pid_file.unlink(missing_ok=True)
    except OSError:
        pass
    return 0 if summary["running"] == 0 and code == 0 else 1


def _supervise_tick(
    *,
    py: str,
    proj: Path,
    ws: Path,
    log_dir: Path,
    session_log: Path,
    manifest: Path,
    launched: list[dict],
    state: dict,
    max_active: int,
    wave_gap_minutes: float,
    poll_seconds: float,
) -> None:
    wave_index = int(state["wave_index"])
    next_wave_at = float(state["next_wave_at"])
    rows_all = load_missions(ws)
    active = [r for r in rows_all if is_active(r)]
    if time.time() >= next_wave_at and wave_index < len(WAVE_TASKS) and len(active) < max_active:
        wave = WAVE_TASKS[wave_index]
        log(session_log, f"LAUNCH wave {wave_index} tasks={len(wave)} active_now={len(active)}")
        # Pre-wave token/agent liveness — skip whole wave if primary agents are dead.
        agents_in_wave = sorted({str(t.get("agent") or "codex") for t in wave})
        wave_blocked = False
        for agent_name in agents_in_wave:
            ok, reason = probe_agent_or_skip(agent_name)
            log(session_log, f"LIVENESS agent={agent_name} ok={ok} reason={reason}")
            if not ok:
                wave_blocked = True
        if wave_blocked:
            log(session_log, f"SKIP wave {wave_index}: agent liveness failed (quota/login/unavailable)")
            wave_index += 1
            next_wave_at = time.time() + max(600.0, float(wave_gap_minutes) * 60.0)
        else:
            for task in wave:
                tag = f"W{wave_index}-{task['id']}-{datetime.now().strftime('%H%M%S')}"
                try:
                    info = launch_mission(
                        py=py,
                        proj=proj,
                        ws=ws,
                        goal=task["goal"],
                        agent=task["agent"],
                        allow_test=bool(task.get("allow_test")),
                        log_dir=log_dir,
                        tag=tag,
                    )
                except Exception as exc:  # noqa: BLE001
                    log(session_log, f"LAUNCH_FAIL {task['id']}: {exc}")
                    continue
                rec = {k: v for k, v in info.items() if k not in {"proc", "fo", "fe"}}
                with manifest.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
                launched.append(info)
                log(session_log, f"STARTED {tag} pid={info['pid']} agent={info['agent']}")
                time.sleep(2.0)
            wave_index += 1
            next_wave_at = time.time() + max(600.0, float(wave_gap_minutes) * 60.0)

    recent = [r for r in rows_all if r["created_at"] >= "2026-07-17T14:00:00"]
    verified = sum(1 for r in recent if r["status"] == "verified" or r["stop"] == "verified")
    stopped = sum(1 for r in recent if r["status"] == "stopped")
    running = sum(1 for r in recent if is_active(r))
    log(
        session_log,
        f"SNAPSHOT recent={len(recent)} active={running} verified={verified} stopped={stopped} "
        f"waves_launched={wave_index}/{len(WAVE_TASKS)}",
    )
    for row in recent[:12]:
        log(
            session_log,
            f"  {row['id'][:24]} {row['status']}/{row['stop'] or '-'} "
            f"stage={row['stage'] or '-'} files={row['product_files']} {row['objective'][:60]}",
        )

    for info in launched:
        proc = info.get("proc")
        if proc is not None and proc.poll() is not None:
            try:
                info["fo"].close()
                info["fe"].close()
            except Exception:
                pass
            info["proc"] = None

    # Occasional memory + pytest health (~every 10 minutes)
    if int(time.time()) % 600 < max(30.0, float(poll_seconds)):
        mem = memory_snippet(ws, py, proj)
        log(session_log, "MEMORY_HEAD\n" + mem[:1200])
        code, tail = run_pytest(py, proj)
        log(session_log, f"PYTEST_MAIN code={code} tail={tail!r}")
        # Built-in orphan reconcile + optional one-shot auto-resume
        try:
            from visual_agent.chief_background import reconcile_workspace_backgrounds

            reconciled = reconcile_workspace_backgrounds(ws, update=True, limit=40, auto_resume=True)
            if reconciled:
                log(session_log, f"RECONCILE count={len(reconciled)}")
                for item in reconciled[:8]:
                    ar = item.get("auto_resume") if isinstance(item.get("auto_resume"), dict) else None
                    if ar:
                        log(
                            session_log,
                            f"  AUTO_RESUME {item.get('mission_id')} "
                            f"status={ar.get('status')} stop={ar.get('stop_reason')}",
                        )
        except Exception as exc:  # noqa: BLE001
            log(session_log, f"RECONCILE_ERR {type(exc).__name__}: {exc}")

    state["wave_index"] = wave_index
    state["next_wave_at"] = next_wave_at


if __name__ == "__main__":
    raise SystemExit(main())
