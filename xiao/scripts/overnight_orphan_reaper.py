#!/usr/bin/env python3
"""Reap orphaned Pacer background missions during long-host runs.

If background.json claims a running worker but the PID is dead (or belongs to
another mission), mark background stale and resume once with --background.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


def utc_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(path: Path, msg: str) -> None:
    line = f"[{utc_now()}] {msg}"
    print(line, flush=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    except Exception:
        return False


def cmdline_for_pid(pid: int) -> str:
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        return (completed.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def mark_stale(bg_path: Path, reason: str) -> None:
    data = load_json(bg_path) or {}
    data["status"] = "stale_dead_worker"
    data["stale_reason"] = reason
    data["stale_at"] = datetime.now().isoformat()
    data["pid"] = 0
    data["worker_pid"] = 0
    bg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resume_mission(
    *,
    py: str,
    proj: Path,
    ws: Path,
    mission_id: str,
    test_cmd: str,
    agent: str = "codex",
) -> tuple[int, str]:
    env = os.environ.copy()
    src = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    try:
        completed = subprocess.run(
            [
                py,
                "-m",
                "visual_agent.cli",
                "mission",
                "resume",
                "--mission",
                mission_id,
                "--execute",
                "--allow-dirty",
                "--background",
                "--agent",
                agent or "codex",
                "--test-command",
                test_cmd,
                "--repo-root",
                str(proj),
                "--workspace-root",
                str(ws),
                "--format",
                "markdown",
            ],
            cwd=str(proj),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    text = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
    return int(completed.returncode), text[-800:]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--hours", type=float, default=3.5)
    parser.add_argument("--interval-seconds", type=float, default=300.0)
    parser.add_argument("--max-resumes-per-tick", type=int, default=3)
    args = parser.parse_args()

    py = str(Path(args.python).resolve())
    proj = Path(args.repo_root).resolve()
    ws = Path(args.workspace_root).resolve()
    log_path = ws / "overnight-stress" / "orphan-reaper.log"
    test_cmd = f"{py} -m pytest -q"
    deadline = time.time() + max(0.5, float(args.hours)) * 3600
    resumed_ids: set[str] = set()

    log(log_path, f"START orphan reaper hours={args.hours} pid={os.getpid()}")

    while time.time() < deadline:
        resumes = 0
        missions = ws / "missions"
        if missions.is_dir():
            for mdir in sorted(missions.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                if resumes >= int(args.max_resumes_per_tick):
                    break
                if not mdir.is_dir():
                    continue
                mid = mdir.name
                mf = mdir / "mission.json"
                bg = mdir / "background.json"
                pf = mdir / "progress.json"
                mission = load_json(mf) if mf.exists() else None
                if not mission:
                    continue
                status = str(mission.get("status") or "")
                if status in {"verified", "stopped", "preview", "merged"}:
                    continue
                stop = str(mission.get("stop_reason") or "")
                if stop in {"verified", "preview_only"}:
                    continue

                stage = ""
                if pf.exists():
                    progress = load_json(pf) or {}
                    stage = str(progress.get("stage") or "")

                needs = False
                reason = ""
                if bg.exists():
                    bdata = load_json(bg) or {}
                    bstatus = str(bdata.get("status") or "")
                    bpid = int(bdata.get("pid") or bdata.get("worker_pid") or 0)
                    if bstatus in {"running", "started"} and bpid > 0:
                        if not pid_alive(bpid):
                            needs = True
                            reason = f"dead_pid={bpid}"
                        else:
                            cmd = cmdline_for_pid(bpid)
                            if cmd and mid not in cmd and "chief-background-worker" in cmd:
                                needs = True
                                reason = f"pid_reused_other_mission pid={bpid}"
                    elif status in {"running", "created", "background_running"} and stage in {
                        "planning",
                        "created",
                        "dispatch_ready",
                    }:
                        # no live bg after soft age
                        age = time.time() - mdir.stat().st_mtime
                        if age > 600:
                            needs = True
                            reason = f"stale_stage={stage} age={int(age)}s"
                elif status in {"running", "created", "background_running"}:
                    age = time.time() - mdir.stat().st_mtime
                    if age > 600 and stage in {"planning", "created", ""}:
                        needs = True
                        reason = f"no_background age={int(age)}s"

                if not needs:
                    continue
                if mid in resumed_ids:
                    # allow one more resume after 20 min
                    continue

                log(log_path, f"ORPHAN {mid} reason={reason} status={status} stage={stage}")
                # Prefer in-product reconcile (PID ownership + one-shot auto-resume).
                try:
                    from visual_agent.chief_background import reconcile_workspace_backgrounds

                    results = reconcile_workspace_backgrounds(
                        ws, update=True, limit=20, auto_resume=True, max_auto_resumes=2
                    )
                    for item in results:
                        if item.get("mission_id") == mid:
                            ar = item.get("auto_resume") if isinstance(item.get("auto_resume"), dict) else {}
                            log(
                                log_path,
                                f"RECONCILE {mid} bg={((item.get('background') or {}).get('status'))} "
                                f"auto={ar.get('status')} stop={ar.get('stop_reason')}",
                            )
                            if ar.get("status") == "background_started":
                                resumed_ids.add(mid)
                                resumes += 1
                            break
                    else:
                        # Fall back to CLI resume if reconcile did not handle this id.
                        if bg.exists():
                            try:
                                mark_stale(bg, reason)
                            except OSError as exc:
                                log(log_path, f"STALE_FAIL {mid}: {exc}")
                                continue
                        code, tail = resume_mission(
                            py=py, proj=proj, ws=ws, mission_id=mid, test_cmd=test_cmd
                        )
                        log(log_path, f"RESUME {mid} code={code} tail={tail[:300]!r}")
                        resumed_ids.add(mid)
                        resumes += 1
                except Exception as exc:  # noqa: BLE001
                    log(log_path, f"RECONCILE_FAIL {mid}: {exc}")
                    if bg.exists():
                        try:
                            mark_stale(bg, reason)
                        except OSError:
                            pass
                    code, tail = resume_mission(
                        py=py, proj=proj, ws=ws, mission_id=mid, test_cmd=test_cmd
                    )
                    log(log_path, f"RESUME {mid} code={code} tail={tail[:300]!r}")
                    resumed_ids.add(mid)
                    resumes += 1
                time.sleep(4.0)

        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(float(args.interval_seconds), max(60.0, remaining)))

    log(log_path, f"DONE resumed_unique={len(resumed_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
