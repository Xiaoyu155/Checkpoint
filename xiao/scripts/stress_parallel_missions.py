#!/usr/bin/env python3
"""Launch multiple concurrent Pacer missions (true multi-process stress)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--wait-register-seconds", type=float, default=45.0)
    args = parser.parse_args()

    py = str(Path(args.python).resolve())
    proj = Path(args.repo_root).resolve()
    ws = Path(args.workspace_root).resolve()
    log_dir = ws / "stress-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    test_cmd = f"{py} -m pytest -q"

    tasks = [
        {
            "id": "T1",
            "agent": "codex",
            "allow_test": False,
            "goal": (
                "In app/services/backtest.py add a concise Chinese module-level docstring "
                "(3-5 lines) describing backtest purpose, main inputs and outputs. "
                "Do not change any logic, signatures, or test files."
            ),
        },
        {
            "id": "T2",
            "agent": "claude-code",
            "allow_test": True,
            "goal": (
                "In tests/test_risk.py add one focused unit test that RiskEngine.evaluate_order "
                "rejects non-finite price (nan or inf). Keep all existing tests green. "
                "You may edit test files only as needed for this test; do not change product "
                "risk logic unless required for correctness."
            ),
        },
        {
            "id": "T3",
            "agent": "codex",
            "allow_test": False,
            "goal": (
                "In app/services/virtual_account.py add a short Chinese comment or docstring on "
                "the main public class explaining it is a paper/virtual account for simulation. "
                "Do not change logic or tests."
            ),
        },
        {
            "id": "T4",
            "agent": "claude-code",
            "allow_test": False,
            "goal": (
                "In app/services/sample_data.py add a Chinese docstring to the primary sample-data "
                "loading function describing when sample data is used. No logic or test changes."
            ),
        },
        {
            "id": "T5",
            "agent": "codex",
            "allow_test": False,
            "goal": (
                "In app/models/types.py add brief Chinese comments for Side and Signal only. "
                "Do not change values, logic, or tests."
            ),
        },
        {
            "id": "T6",
            "agent": "claude-code",
            "allow_test": False,
            "goal": (
                "In app/cli.py add one Chinese sentence to the module docstring about risk checks "
                "taking priority over strategy. No behavior or test changes."
            ),
        },
    ]

    manifest = log_dir / f"manifest-py-{stamp}.jsonl"
    procs: list[tuple[str, subprocess.Popen[str], object, object]] = []
    for t in tasks:
        out = log_dir / f"{t['id']}-{stamp}.out.log"
        err = log_dir / f"{t['id']}-{stamp}.err.log"
        argv = [
            py,
            "-m",
            "visual_agent.cli",
            "mission",
            "start",
            "--goal",
            t["goal"],
            "--test-command",
            test_cmd,
            "--agent",
            t["agent"],
            "--execute",
            "--allow-dirty",
            "--background",
            "--max-rounds",
            "3",
            "--max-repair-rounds",
            "1",
            "--max-wall-minutes",
            "45",
            "--max-worker-minutes",
            "30",
            "--repo-root",
            str(proj),
            "--workspace-root",
            str(ws),
            "--format",
            "markdown",
        ]
        if t["allow_test"]:
            argv.append("--allow-test-edits")
        fo = out.open("w", encoding="utf-8", errors="replace")
        fe = err.open("w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(
            argv,
            cwd=str(proj),
            stdout=fo,
            stderr=fe,
            text=True,
        )
        rec = {
            "id": t["id"],
            "agent": t["agent"],
            "pid": proc.pid,
            "out": str(out),
            "err": str(err),
            "started": datetime.now(timezone.utc).isoformat(),
        }
        with manifest.open("a", encoding="utf-8") as mh:
            mh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"STARTED {t['id']} pid={proc.pid} agent={t['agent']}", flush=True)
        procs.append((t["id"], proc, fo, fe))
        time.sleep(1.2)

    print(f"MANIFEST {manifest}", flush=True)
    print(f"Waiting {args.wait_register_seconds:.0f}s for registration...", flush=True)
    time.sleep(max(5.0, float(args.wait_register_seconds)))

    listed = subprocess.run(
        [
            py,
            "-m",
            "visual_agent.cli",
            "mission",
            "list",
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
        check=False,
    )
    print((listed.stdout or listed.stderr or "")[:4000], flush=True)
    print(
        "launcher_poll",
        [(i, p.poll()) for i, p, _fo, _fe in procs],
        flush=True,
    )
    # leave children running; close only our log handles if process exited
    for _i, p, fo, fe in procs:
        if p.poll() is not None:
            fo.close()
            fe.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
