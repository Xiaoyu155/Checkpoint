#!/usr/bin/env python3
"""Heartbeat loop for overnight Pacer host supervision.

Prints one structured line every interval so external monitors can watch.
Does not launch missions; only observes workspace + supervisor log.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path


ACTIVE_STAGES = {
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


def load_rows(ws: Path) -> list[dict]:
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
        stage = ""
        pf = path / "progress.json"
        if pf.exists():
            try:
                stage = str(json.loads(pf.read_text(encoding="utf-8-sig")).get("stage") or "")
            except (OSError, json.JSONDecodeError):
                stage = ""
        rows.append(
            {
                "id": str(data.get("mission_id") or path.name),
                "status": str(data.get("status") or ""),
                "stop": str(data.get("stop_reason") or ""),
                "created_at": str(data.get("created_at") or ""),
                "stage": stage,
                "objective": str(data.get("objective") or "")[:80],
            }
        )
    return rows


def is_active(row: dict) -> bool:
    if row["status"] in {"running", "created", "background_running"}:
        return True
    return row["stage"] in ACTIVE_STAGES


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--hours", type=float, default=3.6)
    parser.add_argument("--interval-seconds", type=float, default=180.0)
    parser.add_argument("--since", default="2026-07-17T14:00:00")
    parser.add_argument("--supervisor-pid", type=int, default=0)
    args = parser.parse_args()

    ws = Path(args.workspace_root).resolve()
    out_dir = ws / "overnight-stress"
    out_dir.mkdir(parents=True, exist_ok=True)
    hb = out_dir / "health-heartbeats.log"
    deadline = time.time() + max(0.5, float(args.hours)) * 3600
    n = 0

    while time.time() < deadline:
        n += 1
        rows = [r for r in load_rows(ws) if r["created_at"] >= args.since]
        active = [r for r in rows if is_active(r)]
        verified = [r for r in rows if r["status"] == "verified" or r["stop"] == "verified"]
        stopped = [r for r in rows if r["status"] == "stopped"]
        sup_alive = "unknown"
        if args.supervisor_pid:
            # Windows: check process existence via tasklist-like open
            try:
                import ctypes

                kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                SYNCHRONIZE = 0x00100000
                handle = kernel32.OpenProcess(SYNCHRONIZE, 0, int(args.supervisor_pid))
                if handle:
                    kernel32.CloseHandle(handle)
                    sup_alive = "yes"
                else:
                    sup_alive = "no"
            except Exception:
                sup_alive = "check_failed"

        # latest supervisor log line
        latest_sup = ""
        logs = sorted(out_dir.glob("supervisor-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if logs:
            try:
                lines = logs[0].read_text(encoding="utf-8", errors="replace").splitlines()
                latest_sup = lines[-1] if lines else ""
            except OSError:
                latest_sup = ""

        stuck = [
            r
            for r in active
            if r["stage"] in {"dispatch_ready", "created"} or r["status"] == "created"
        ]
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = (
            f"[{ts}] HB#{n} sup={sup_alive} recent={len(rows)} "
            f"active={len(active)} verified={len(verified)} stopped={len(stopped)} "
            f"stuckish={len(stuck)}"
        )
        print(line, flush=True)
        if latest_sup:
            print(f"  SUP {latest_sup}", flush=True)
        for r in active[:8]:
            print(
                f"  ACT {r['id'][:28]} {r['status']}/{r['stop'] or '-'} "
                f"stage={r['stage'] or '-'} {r['objective'][:50]}",
                flush=True,
            )
        with hb.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            for r in active[:8]:
                handle.write(
                    f"  ACT {r['id']} {r['status']}/{r['stop'] or '-'} stage={r['stage']}\n"
                )

        # alert lines for monitor consumers
        if sup_alive == "no":
            print("ALERT supervisor_dead", flush=True)
        if len(stuck) >= 3:
            print(f"ALERT many_stuck={len(stuck)}", flush=True)

        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(float(args.interval_seconds), max(30.0, remaining)))

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] health_loop_done n={n}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
