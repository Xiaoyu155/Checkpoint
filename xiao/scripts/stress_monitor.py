#!/usr/bin/env python3
"""Poll mission statuses for a Pacer stress wave until terminal or timeout."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path


def load_stress_missions(ws: Path, created_prefix: str) -> list[dict]:
    rows: list[dict] = []
    root = ws / "missions"
    if not root.is_dir():
        return rows
    for path in root.iterdir():
        if not path.is_dir():
            continue
        mission_file = path / "mission.json"
        if not mission_file.exists():
            continue
        try:
            data = json.loads(mission_file.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        created = str(data.get("created_at") or "")
        if not created.startswith(created_prefix):
            continue
        progress: dict = {}
        progress_file = path / "progress.json"
        if progress_file.exists():
            try:
                progress = json.loads(progress_file.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                progress = {}
        rows.append(
            {
                "id": str(data.get("mission_id") or path.name),
                "status": str(data.get("status") or ""),
                "stop": str(data.get("stop_reason") or ""),
                "objective": str(data.get("objective") or "")[:80],
                "stage": str(progress.get("stage") or ""),
                "agent": str(progress.get("agent") or ""),
                "product_files": progress.get("changed_product_file_count"),
            }
        )
    return sorted(rows, key=lambda item: item["id"])


def is_terminal(row: dict) -> bool:
    status = row["status"]
    stage = row["stage"]
    if status in {"running", "created"}:
        return False
    if stage in {
        "worker_running",
        "worker_starting",
        "verification_running",
        "background_started",
        "worker_started",
    }:
        return False
    return status in {"verified", "stopped", "preview", "verified_blocked"} or bool(row["stop"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--created-prefix", default="2026-07-17T15:0")
    parser.add_argument("--max-minutes", type=float, default=50.0)
    parser.add_argument("--python", default="python")
    args = parser.parse_args()

    ws = Path(args.workspace_root).resolve()
    repo = Path(args.repo_root).resolve()
    deadline = time.time() + max(60.0, float(args.max_minutes) * 60.0)
    last_print = 0.0

    while time.time() < deadline:
        rows = load_stress_missions(ws, args.created_prefix)
        running = sum(1 for row in rows if not is_terminal(row))
        now = time.time()
        if now - last_print >= 30.0 or running == 0:
            print(
                f"--- {datetime.now().isoformat(timespec='seconds')} "
                f"total={len(rows)} active={running}",
                flush=True,
            )
            for row in rows:
                print(
                    f"  {row['id'][:26]}  {row['status']}/{row['stop'] or '-'}  "
                    f"stage={row['stage'] or '-'}  files={row['product_files']}  {row['objective']}",
                    flush=True,
                )
            last_print = now
        if rows and running == 0:
            break
        time.sleep(20)

    rows = load_stress_missions(ws, args.created_prefix)
    summary = {"verified": 0, "stopped": 0, "preview": 0, "running": 0, "other": 0}
    print("\n==== FINAL ====", flush=True)
    for row in rows:
        status = row["status"]
        stop = row["stop"]
        if status == "verified" or stop == "verified":
            summary["verified"] += 1
        elif status == "preview" or stop == "preview_only":
            summary["preview"] += 1
        elif not is_terminal(row):
            summary["running"] += 1
        elif status == "stopped":
            summary["stopped"] += 1
        else:
            summary["other"] += 1
        print(json.dumps(row, ensure_ascii=False), flush=True)
    print("SUMMARY", json.dumps(summary), flush=True)

    pytest = subprocess.run(
        [args.python, "-m", "pytest", "-q", "--tb=no"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    tail = ((pytest.stdout or "") + (pytest.stderr or ""))[-300:]
    print("MAIN_PYTEST", pytest.returncode, tail, flush=True)

    # model usage for stress wave
    plans = ws / "chief_plans"
    if plans.is_dir():
        print("\n==== MODEL USAGE (recent plans) ====", flush=True)
        items = sorted(plans.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:12]
        for plan_dir in items:
            workers = plan_dir / "workers.jsonl"
            if not workers.exists():
                continue
            try:
                rec = json.loads(workers.read_text(encoding="utf-8-sig").splitlines()[-1])
            except (OSError, json.JSONDecodeError, IndexError):
                continue
            usage = rec.get("usage") if isinstance(rec.get("usage"), dict) else {}
            print(
                plan_dir.name[:28],
                "agent=",
                rec.get("agent"),
                "model=",
                rec.get("resolved_model"),
                "status=",
                rec.get("status"),
                "tokens=",
                usage.get("total_tokens") or usage.get("input_tokens"),
                flush=True,
            )
    return 0 if summary.get("running", 0) == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
