from __future__ import annotations

import json
from pathlib import Path

from visual_agent.cli import main
from visual_agent.pacer_trace import (
    PacerTraceEntry,
    get_global_pacer_dir,
    get_workspace_pacer_dir,
    read_pacer_traces,
    record_pacer_trace,
)


def test_record_and_read_pacer_trace(tmp_path: Path, monkeypatch) -> None:
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    global_dir = tmp_path / "global_pacer"
    global_dir.mkdir()

    monkeypatch.setattr("visual_agent.pacer_trace.get_global_pacer_dir", lambda: global_dir)

    entry = record_pacer_trace(
        goal="实现战术撤离游戏",
        workspace_root=ws_dir,
        primary_backend="grok",
        effective_backend="gemini",
        failover_path=["grok -> gemini (HTTP 429)"],
        status="success",
        elapsed_seconds=4.82,
        patched_files=["examples/delta_action/core.py"],
        diff_stats={"insertions": 45, "deletions": 2},
        verification_command="pytest examples/delta_action/test_delta.py -q",
        verification_verdict="pass",
        session_source="claude_code",
    )

    assert entry.trace_id.startswith("trace-")
    assert entry.goal == "实现战术撤离游戏"
    assert entry.failover_path == ["grok -> gemini (HTTP 429)"]

    # Verify workspace local ledger file was written
    ws_ledger = ws_dir / ".pacer" / "traces.jsonl"
    assert ws_ledger.is_file()
    ws_lines = ws_ledger.read_text(encoding="utf-8").strip().splitlines()
    assert len(ws_lines) == 1
    data = json.loads(ws_lines[0])
    assert data["trace_id"] == entry.trace_id
    assert data["effective_backend"] == "gemini"

    # Verify global ledger file was written
    global_ledger = global_dir / "traces.jsonl"
    assert global_ledger.is_file()
    global_lines = global_ledger.read_text(encoding="utf-8").strip().splitlines()
    assert len(global_lines) == 1

    # Read back traces
    traces = read_pacer_traces(workspace_root=ws_dir)
    assert len(traces) == 1
    assert traces[0]["trace_id"] == entry.trace_id
    assert traces[0]["verification_verdict"] == "pass"


def test_pacer_trace_cli(tmp_path: Path, monkeypatch, capsys) -> None:
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    global_dir = tmp_path / "global_pacer"
    global_dir.mkdir()

    monkeypatch.setattr("visual_agent.pacer_trace.get_global_pacer_dir", lambda: global_dir)
    monkeypatch.chdir(ws_dir)

    record_pacer_trace(
        goal="修复状态机漏洞",
        workspace_root=ws_dir,
        primary_backend="grok",
        effective_backend="grok",
        status="success",
        elapsed_seconds=3.2,
        patched_files=["core.py"],
        verification_command="pytest",
        verification_verdict="pass",
        session_source="cli",
    )

    # Test human readable format
    code = main(["trace"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Pacer 执行审计留痕" in out
    assert "修复状态机漏洞" in out
    assert "grok" in out

    # Test JSON output
    code_json = main(["trace", "--json"])
    assert code_json == 0
    out_json = capsys.readouterr().out
    data = json.loads(out_json)
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["goal"] == "修复状态机漏洞"
