"""Persistent Cross-Session & Cross-Window Execution Trace Ledger for Pacer.

Records structured execution logs (traces) across all terminal sessions,
CMD windows, and Claude Code conversations into append-only JSONL ledgers:
1. Workspace-local ledger: `<workspace_root>/.pacer/traces.jsonl`
2. Global persistent ledger: `~/.pacer/traces.jsonl`
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any
import uuid


@dataclass
class PacerTraceEntry:
    trace_id: str
    timestamp: str
    pid: int
    session_source: str  # cli | claude_code | mcp | api
    workspace_root: str
    goal: str
    primary_backend: str
    effective_backend: str
    failover_path: list[str] = field(default_factory=list)
    status: str = "success"  # success | failed | partial
    elapsed_seconds: float = 0.0
    patched_files: list[str] = field(default_factory=list)
    diff_stats: dict[str, int] = field(default_factory=dict)
    verification_command: str = ""
    verification_verdict: str = ""  # pass | fail | skipped | error
    verification_output: str = ""
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # --- Real usage accounting (replaces the old fixed-constant estimate) ---
    # prompt_tokens: estimated size of the prompts actually sent to models,
    # measured per task via canvas_manager.estimate_tokens().
    prompt_tokens: int = 0
    # model_calls: number of real backend invocations (planning, coder,
    # retries, fallbacks, review). Counted, never assumed.
    model_calls: int = 0
    # usage_measurement: "measured" when the values above come from the run,
    # "unknown" when the producer could not supply them. Never "estimated"
    # against an invented baseline.
    usage_measurement: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def get_global_pacer_dir() -> Path:
    p = Path(os.path.expanduser("~/.pacer")).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_workspace_pacer_dir(workspace_root: str | Path | None = None) -> Path:
    ws = Path(workspace_root or Path.cwd()).expanduser().resolve()
    p = ws / ".pacer"
    p.mkdir(parents=True, exist_ok=True)
    return p


def record_pacer_trace(
    *,
    goal: str,
    workspace_root: str | Path,
    primary_backend: str,
    effective_backend: str,
    status: str,
    elapsed_seconds: float,
    failover_path: list[str] | None = None,
    patched_files: list[str] | None = None,
    diff_stats: dict[str, int] | None = None,
    verification_command: str = "",
    verification_verdict: str = "",
    verification_output: str = "",
    error: str | None = None,
    session_source: str = "cli",
    metadata: dict[str, Any] | None = None,
    prompt_tokens: int = 0,
    model_calls: int = 0,
    usage_measurement: str = "unknown",
) -> PacerTraceEntry:
    """Durably record a trace entry to both workspace-local and user-global JSONL ledgers.

    `prompt_tokens` and `model_calls` are the real quantities observed during
    the run. When the producer cannot supply them they stay 0 and
    `usage_measurement` stays "unknown" — no value is ever back-filled from
    an invented baseline.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    trace_id = f"trace-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ws_str = str(Path(workspace_root).expanduser().resolve().as_posix())

    measured_prompt_tokens = max(0, int(prompt_tokens or 0))
    measured_model_calls = max(0, int(model_calls or 0))
    if usage_measurement not in ("measured", "unknown"):
        usage_measurement = "unknown"
    if usage_measurement == "measured" and (measured_prompt_tokens == 0 and measured_model_calls == 0):
        # Nothing was actually observed — do not claim measurement.
        usage_measurement = "unknown"

    entry = PacerTraceEntry(
        trace_id=trace_id,
        timestamp=now_iso,
        pid=os.getpid(),
        session_source=session_source,
        workspace_root=ws_str,
        goal=goal,
        primary_backend=primary_backend,
        effective_backend=effective_backend,
        failover_path=failover_path or [],
        status=status,
        elapsed_seconds=round(elapsed_seconds, 2),
        patched_files=patched_files or [],
        diff_stats=diff_stats or {},
        verification_command=verification_command,
        verification_verdict=verification_verdict,
        verification_output=(verification_output or "")[:500],
        error=error,
        metadata=metadata or {},
        prompt_tokens=measured_prompt_tokens,
        model_calls=measured_model_calls,
        usage_measurement=usage_measurement,
    )

    line = json.dumps(entry.to_dict(), ensure_ascii=False) + "\n"

    # 1. Write to workspace local ledger
    try:
        ws_pacer = get_workspace_pacer_dir(workspace_root)
        local_log = ws_pacer / "traces.jsonl"
        with local_log.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass

    # 2. Write to global persistent ledger
    try:
        global_pacer = get_global_pacer_dir()
        global_log = global_pacer / "traces.jsonl"
        with global_log.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass

    return entry


def read_pacer_traces(
    *,
    workspace_root: str | Path | None = None,
    global_only: bool = False,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """Read recent traces, merging workspace-local and global ledgers."""
    seen_ids: set[str] = set()
    traces: list[dict[str, Any]] = []

    files_to_read: list[Path] = []
    if not global_only:
        local_pacer = get_workspace_pacer_dir(workspace_root)
        files_to_read.append(local_pacer / "traces.jsonl")

    files_to_read.append(get_global_pacer_dir() / "traces.jsonl")

    for fpath in files_to_read:
        if not fpath.is_file():
            continue
        try:
            with fpath.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        tid = str(data.get("trace_id") or "")
                        if tid and tid not in seen_ids:
                            seen_ids.add(tid)
                            traces.append(data)
                    except Exception:
                        continue
        except Exception:
            continue

    # Sort descending by timestamp
    traces.sort(key=lambda x: str(x.get("timestamp") or ""), reverse=True)
    return traces[:limit]


def compute_aggregate_savings(traces: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Aggregate REAL usage across recorded traces.

    The previous implementation compared every task against two invented
    constants (8,500 "traditional" tokens vs 1,200 "Pacer" tokens) and
    reported a fixed 94.5% savings even with zero traces. That produced
    numbers that could not be substantiated and made failed runs look like
    savings.

    This implementation reports only what was actually observed:

    - `prompt_tokens`    : sum of the estimated prompt sizes that were
                           really sent to models during the recorded runs.
    - `model_calls`      : number of real backend invocations.
    - `savings_pct`      : `None` unless a measured traditional baseline was
                           recorded alongside the Pacer run. No invented
                           baseline means no savings claim.
    - `estimated_usd_saved`: `None` for the same reason.

    Time figures are measured elapsed time versus a configurable baseline;
    when no baseline is configured the time saving is also reported as
    unknown rather than invented.
    """
    if traces is None:
        traces = read_pacer_traces(limit=1000)

    total_tasks = len(traces)
    successful_tasks = sum(1 for t in traces if t.get("status") == "success")

    # --- Real usage, aggregated from what the runs actually recorded ---
    measured_traces = [t for t in traces if t.get("usage_measurement") == "measured"]

    def _as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    total_prompt_tokens = sum(_as_int(t.get("prompt_tokens")) for t in measured_traces)
    total_model_calls = sum(_as_int(t.get("model_calls")) for t in measured_traces)

    # Savings require a *measured* traditional baseline recorded by the
    # caller. Absent that, there is nothing to subtract from and nothing to
    # claim.
    traditional_baseline_tokens = 0
    baseline_traces = 0
    for t in measured_traces:
        baseline = t.get("traditional_baseline_prompt_tokens")
        if baseline is not None:
            traditional_baseline_tokens += _as_int(baseline)
            baseline_traces += 1

    total_tokens_saved: int | None = None
    savings_pct: float | None = None
    estimated_usd_saved: float | None = None
    if baseline_traces > 0 and traditional_baseline_tokens > 0:
        total_tokens_saved = max(0, traditional_baseline_tokens - total_prompt_tokens)
        savings_pct = round(
            (total_tokens_saved / traditional_baseline_tokens) * 100, 1
        )
        estimated_usd_saved = round((total_tokens_saved / 1_000_000) * 3.0, 2)

    # --- Time: measured elapsed time against an explicit baseline ---
    total_time_spent_sec = sum(
        float(t.get("elapsed_seconds") or 0.0) for t in traces if t.get("elapsed_seconds")
    )
    time_baseline_sec = _as_int_or_none(
        (traces[0].get("traditional_baseline_seconds") if traces else None)
    )
    if time_baseline_sec and total_tasks:
        total_time_saved_sec: float | None = max(
            0.0, (total_tasks * time_baseline_sec) - total_time_spent_sec
        )
    else:
        total_time_saved_sec = None

    # Core pillars activity breakdown
    pillars_breakdown = {
        "pillar_1_ast_index": {
            "name": "Local AST Symbol Indexer",
            "name_zh": "本地 AST 毫秒级符号提取",
            "count": total_tasks,
            "avg_latency_ms": None,
            "desc": "毫秒级提取 Class/Def 签名与 Traceback，消除大模型遍历耗时",
        },
        "pillar_2_stateless_canvas": {
            "name": "Stateless Canvas & Prompt Compression",
            "name_zh": "无状态状态画布 (<=1500 Tokens)",
            "count": total_tasks,
            "tokens_saved": total_tokens_saved,
            "measured_prompt_tokens": total_prompt_tokens,
            "desc": "单回合严格控制上下文；token 数为实测预估提示词规模",
        },
        "pillar_3_multi_model_orchestration": {
            "name": "Multi-Model Zero-Waiting Router",
            "name_zh": "多模型无缝阶梯容错路由",
            "primary_tier": "Grok 4.7-latest (Sub2API)",
            "escalation_tier": "Codex Plus (OAuth)",
            "inspector_tier": "Gemini 3.8 Flash",
            "model_calls": total_model_calls,
            "desc": "3秒极速流式出码，故障无缝阶梯降级",
        },
        "pillar_4_diff_patcher": {
            "name": "Aider-Format Atomic Diff Patcher",
            "name_zh": "SEARCH/REPLACE 原子补丁注入",
            "patches_applied": sum(len(t.get("patched_files") or []) for t in traces),
            "desc": "精准差异替换，带事务校验与语法自愈",
        },
        "pillar_5_tamper_guard": {
            "name": "Verification Tamper Guard & Tracing",
            "name_zh": "自动化验收门禁与跨会话留痕",
            "verifications_run": sum(1 for t in traces if t.get("verification_command")),
            "desc": "严格禁止篡改测试用例，全生命周期持久审计",
        },
    }

    return {
        "total_tasks": total_tasks,
        "successful_tasks": successful_tasks,
        "success_rate_pct": round((successful_tasks / total_tasks) * 100, 1) if total_tasks > 0 else None,
        "total_tokens_saved": total_tokens_saved,
        "total_tokens_consumed": total_prompt_tokens,
        "measured_prompt_tokens": total_prompt_tokens,
        "model_calls": total_model_calls,
        "measured_traces": len(measured_traces),
        "usage_measurement": "measured" if measured_traces else "unknown",
        "savings_pct": savings_pct,
        "estimated_usd_saved": estimated_usd_saved,
        "total_time_spent_sec": round(total_time_spent_sec, 1),
        "total_time_saved_sec": None if total_time_saved_sec is None else round(total_time_saved_sec, 1),
        "total_time_saved_minutes": (
            None if total_time_saved_sec is None else round(total_time_saved_sec / 60.0, 1)
        ),
        "pillars": pillars_breakdown,
    }


def _as_int_or_none(value: Any) -> float | None:
    """Return a positive float, or None when the value is missing/zero."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None

