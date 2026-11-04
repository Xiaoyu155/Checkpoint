"""Bounded GPU-assisted search for parameterized, deterministic strategies.

Candidate scores rank work. A strategy is eligible only if it beats the flat
baseline, passes the hard risk gate, and passes an independent holdout check.
"""

from __future__ import annotations

from array import array
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import random
import struct
import sys
import time
from typing import Any, Sequence

try:
    import torch
except ImportError:  # PyTorch stays an optional runtime dependency.
    torch = None


MAX_CANDIDATES = 256
MAX_SCENARIOS = 512
MAX_STEPS = 8192
_TARGET_TENSOR_BYTES = 96 * 1024 * 1024
_BYTES_PER_EVALUATION_ELEMENT = 64
_MAX_CANDIDATES_PER_CHUNK = 8


@dataclass(frozen=True)
class StrategyCandidate:
    candidate_id: str
    lookback: int
    entry_threshold: float

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str):
            raise ValueError("candidate_id must be a string")
        candidate_id = self.candidate_id.strip()
        if not candidate_id or len(candidate_id) > 64:
            raise ValueError("candidate_id must contain 1 to 64 characters")
        object.__setattr__(self, "candidate_id", candidate_id)
        if isinstance(self.lookback, bool) or not isinstance(self.lookback, int):
            raise ValueError("lookback must be an integer")
        if not 2 <= self.lookback <= MAX_STEPS:
            raise ValueError(f"lookback must be between 2 and {MAX_STEPS}")
        if isinstance(self.entry_threshold, bool) or not isinstance(self.entry_threshold, (int, float)):
            raise ValueError("entry_threshold must be numeric")
        threshold = float(self.entry_threshold)
        if not math.isfinite(threshold) or not 0.0 <= threshold <= 0.5:
            raise ValueError("entry_threshold must be finite and between 0 and 0.5")
        object.__setattr__(self, "entry_threshold", threshold)


@dataclass(frozen=True)
class SearchPolicy:
    fee_bps: float = 8.0
    max_drawdown: float = 0.35
    risk_weight: float = 1.0
    volatility_weight: float = 0.25
    max_candidates: int = 128
    top_k: int = 3
    replay_tolerance: float = 2e-5

    def __post_init__(self) -> None:
        if isinstance(self.max_candidates, bool) or not isinstance(self.max_candidates, int):
            raise ValueError("max_candidates must be an integer")
        if isinstance(self.top_k, bool) or not isinstance(self.top_k, int):
            raise ValueError("top_k must be an integer")
        for name in ("fee_bps", "max_drawdown", "risk_weight", "volatility_weight", "replay_tolerance"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
        if not 0 < self.max_drawdown <= 1:
            raise ValueError("max_drawdown must be in (0, 1]")
        if not 1 <= int(self.max_candidates) <= MAX_CANDIDATES:
            raise ValueError(f"max_candidates must be between 1 and {MAX_CANDIDATES}")
        if not 1 <= int(self.top_k) <= min(8, int(self.max_candidates)):
            raise ValueError("top_k must be between 1 and min(8, max_candidates)")


@dataclass(frozen=True)
class CandidateScore:
    candidate: StrategyCandidate
    score: float
    mean_return: float
    return_std: float
    worst_max_drawdown: float
    eligible: bool
    rank: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": asdict(self.candidate),
            "score": self.score,
            "mean_return": self.mean_return,
            "return_std": self.return_std,
            "worst_max_drawdown": self.worst_max_drawdown,
            "eligible": self.eligible,
            "rank": self.rank,
        }


@dataclass(frozen=True)
class SearchResult:
    run_id: str
    backend: str
    elapsed_seconds: float
    scoring_seconds: float
    cpu_replay_seconds: float
    candidate_count: int
    scenario_count: int
    steps_per_scenario: int
    evaluated_decisions_per_second: float
    ranked: tuple[CandidateScore, ...]
    selected_candidate_id: str | None
    cpu_replay_count: int
    replay_passed: bool
    replay_status: str
    validation_score: float | None
    validation_max_drawdown: float | None
    validation_scenario_count: int
    input_sha256: str
    device_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "backend": self.backend,
            "elapsed_seconds": self.elapsed_seconds,
            "scoring_seconds": self.scoring_seconds,
            "cpu_replay_seconds": self.cpu_replay_seconds,
            "candidate_count": self.candidate_count,
            "scenario_count": self.scenario_count,
            "steps_per_scenario": self.steps_per_scenario,
            "evaluated_decisions_per_second": self.evaluated_decisions_per_second,
            "ranked": [item.to_dict() for item in self.ranked],
            "selected_candidate_id": self.selected_candidate_id,
            "cpu_replay_count": self.cpu_replay_count,
            "replay_passed": self.replay_passed,
            "replay_status": self.replay_status,
            "validation_score": self.validation_score,
            "validation_max_drawdown": self.validation_max_drawdown,
            "validation_scenario_count": self.validation_scenario_count,
            "input_sha256": self.input_sha256,
            "device_name": self.device_name,
        }


def parse_candidates(payload: Any) -> list[StrategyCandidate]:
    """Parse a strict model output shape; scoring rules cannot be model-edited."""
    rows = payload.get("candidates") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise ValueError("candidate payload must be a non-empty list or {candidates: [...]} object")
    candidates: list[StrategyCandidate] = []
    allowed = {"candidate_id", "lookback", "entry_threshold"}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != allowed:
            raise ValueError(f"candidate {index} must contain exactly {sorted(allowed)}")
        if not isinstance(row["candidate_id"], str):
            raise ValueError(f"candidate {index} candidate_id must be a string")
        if isinstance(row["lookback"], bool) or not isinstance(row["lookback"], int):
            raise ValueError(f"candidate {index} lookback must be an integer")
        if isinstance(row["entry_threshold"], bool) or not isinstance(row["entry_threshold"], (int, float)):
            raise ValueError(f"candidate {index} entry_threshold must be numeric")
        candidates.append(
            StrategyCandidate(
                candidate_id=str(row["candidate_id"]),
                lookback=row["lookback"],
                entry_threshold=float(row["entry_threshold"]),
            )
        )
    if len({item.candidate_id for item in candidates}) != len(candidates):
        raise ValueError("candidate_id values must be unique")
    return candidates


def expand_candidate_neighborhood(
    seeds: Sequence[StrategyCandidate],
    *,
    max_candidates: int = 128,
    max_lookback: int = MAX_STEPS,
    lookback_offsets: Sequence[int] = (-8, 0, 8),
    threshold_offsets: Sequence[float] = (-0.001, 0.0, 0.001),
) -> list[StrategyCandidate]:
    """Deterministically expand sparse proposals without giving them policy control."""
    if not seeds:
        raise ValueError("at least one seed candidate is required")
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int):
        raise ValueError("max_candidates must be an integer")
    if not 1 <= max_candidates <= MAX_CANDIDATES:
        raise ValueError(f"max_candidates must be between 1 and {MAX_CANDIDATES}")
    if isinstance(max_lookback, bool) or not isinstance(max_lookback, int) or not 2 <= max_lookback <= MAX_STEPS:
        raise ValueError(f"max_lookback must be between 2 and {MAX_STEPS}")
    if not lookback_offsets or not threshold_offsets:
        raise ValueError("neighborhood offsets must be non-empty")

    output: list[StrategyCandidate] = []
    seen: set[tuple[int, float]] = set()
    for seed in seeds:
        if not isinstance(seed, StrategyCandidate):
            raise TypeError("all seeds must be StrategyCandidate instances")
        for lookback_offset in lookback_offsets:
            lookback = max(2, min(max_lookback, seed.lookback + int(lookback_offset)))
            for threshold_offset in threshold_offsets:
                threshold = max(0.0, min(0.5, seed.entry_threshold + float(threshold_offset)))
                key = (lookback, round(threshold, 8))
                if key in seen:
                    continue
                seen.add(key)
                if lookback_offset == 0 and float(threshold_offset) == 0.0:
                    candidate_id = seed.candidate_id
                else:
                    digest = hashlib.sha256(
                        f"{seed.candidate_id}|{lookback}|{key[1]:.8f}".encode("utf-8")
                    ).hexdigest()[:12]
                    candidate_id = f"{seed.candidate_id[:48]}-{digest}"
                output.append(StrategyCandidate(candidate_id, lookback, key[1]))
                if len(output) >= max_candidates:
                    return output
    return output


def request_gemini_seed_candidates(
    goal: str,
    *,
    model: str = "gemini-3.8-flash-low",
    timeout_seconds: int = 90,
    max_seeds: int = 8,
    max_lookback: int = MAX_STEPS,
) -> tuple[list[StrategyCandidate], dict[str, Any]]:
    """Ask Gemini for a small typed proposal set; local policy remains authoritative."""
    clean_goal = str(goal or "").strip()
    if not clean_goal or len(clean_goal) > 2000:
        raise ValueError("goal must contain 1 to 2000 characters")
    if not 1 <= int(max_seeds) <= 8:
        raise ValueError("max_seeds must be between 1 and 8")
    if isinstance(max_lookback, bool) or not isinstance(max_lookback, int) or not 2 <= max_lookback <= MAX_STEPS:
        raise ValueError(f"max_lookback must be between 2 and {MAX_STEPS}")
    if not str(model).lower().startswith(("gemini-3.8", "gemini-3-8")):
        raise ValueError("model must explicitly select Gemini 3.8")
    from ..gemini_bridge import invoke_gemini

    prompt = (
        "Return JSON only with schema {\"candidates\":[{\"candidate_id\":string,"
        f"\"lookback\":integer 2..{max_lookback},\"entry_threshold\":number 0..0.5}}]}}. "
        f"Return at most {int(max_seeds)} candidates. Do not include fees, risk limits, test rules, code, "
        "or prose. The local evaluator fixes those policies and independently verifies scores.\n"
        "Task: " + clean_goal
    )
    response = invoke_gemini(
        prompt,
        model=model,
        expect_json=True,
        timeout_seconds=max(1, int(timeout_seconds)),
        system_prompt="Propose a few diverse parameter seeds. Do not claim they are profitable or validated.",
    )
    if not response.success:
        raise RuntimeError("Gemini candidate proposal failed: " + str(response.error or "unknown error")[:300])
    seeds = parse_candidates(response.parsed_json)
    if len(seeds) > int(max_seeds):
        raise ValueError(f"Gemini returned {len(seeds)} seeds; maximum is {int(max_seeds)}")
    if any(seed.lookback > max_lookback for seed in seeds):
        raise ValueError(f"Gemini returned a lookback greater than the requested maximum {max_lookback}")
    return seeds, {
        "requested_model": model,
        "elapsed_seconds": float(response.elapsed_seconds),
        "seed_count": len(seeds),
        "max_lookback": max_lookback,
    }


def make_synthetic_returns(
    *, scenario_count: int = 64, steps: int = 2048, seed: int = 11
) -> list[list[float]]:
    """Create repeatable toy returns for throughput checks, not investment claims."""
    if not 1 <= int(scenario_count) <= MAX_SCENARIOS:
        raise ValueError(f"scenario_count must be between 1 and {MAX_SCENARIOS}")
    if 2 <= int(steps) <= MAX_STEPS:
        pass
    else:
        raise ValueError(f"steps must be between 2 and {MAX_STEPS}")
    rng = random.Random(int(seed))
    scenarios: list[list[float]] = []
    for scenario_index in range(int(scenario_count)):
        drift = 0.00012 if scenario_index % 2 == 0 else -0.00012
        row = []
        for step in range(int(steps)):
            regime = 1.0 if (step // 256 + scenario_index) % 2 == 0 else -1.0
            row.append(rng.gauss(drift * regime, 0.01))
        scenarios.append(row)
    return scenarios


def search_strategies(
    candidates: Sequence[StrategyCandidate],
    scenario_returns: Sequence[Sequence[float]],
    *,
    validation_scenario_returns: Sequence[Sequence[float]] | None = None,
    policy: SearchPolicy | None = None,
    device: str = "auto",
) -> SearchResult:
    """Rank on selection scenarios, then gate the leader on untouched holdout data."""
    started = time.perf_counter()
    active_policy = policy or SearchPolicy()
    normalized = _validate_inputs(candidates, scenario_returns, active_policy)
    validation = (
        _validate_inputs(candidates, validation_scenario_returns, active_policy)[1]
        if validation_scenario_returns is not None
        else None
    )
    steps = len(normalized[1][0])
    if any(candidate.lookback >= steps for candidate in normalized[0]):
        raise ValueError("candidate lookback must be shorter than the scenario")
    if validation is not None and validation == normalized[1]:
        raise ValueError("validation scenarios must differ from selection scenarios")
    if validation is not None and len(validation[0]) != steps:
        raise ValueError("validation scenarios must have the same horizon as selection scenarios")
    selected_device = _resolve_device(device)
    scenario_hash = _input_hash(normalized[0], normalized[1], validation, active_policy)
    scoring_started = time.perf_counter()
    try:
        ranked = _evaluate(normalized[0], normalized[1], active_policy, selected_device)
    except Exception as exc:
        if selected_device != "cuda" or not _is_cuda_oom(exc):
            raise
        torch.cuda.empty_cache()
        selected_device = "cpu"
        ranked = _evaluate(normalized[0], normalized[1], active_policy, selected_device)
    scoring_seconds = time.perf_counter() - scoring_started
    eligible = [item for item in ranked if item.eligible][: active_policy.top_k]
    replayed: list[CandidateScore] = []
    replay_status = "not_run"
    selected_id: str | None = None
    validation_score: float | None = None
    validation_drawdown: float | None = None
    replay_started = time.perf_counter()
    if eligible and torch is not None:
        replayed = _evaluate_python(
            [item.candidate for item in eligible], normalized[1], active_policy
        )
        replay_status = "passed" if all(
            _scores_match(actual, expected, active_policy.replay_tolerance)
            for actual, expected in zip(eligible, replayed)
        ) else "failed"
        if replay_status == "passed" and validation is not None:
            leading = eligible[0]
            holdout = _evaluate_python([leading.candidate], validation, active_policy)[0]
            validation_score = holdout.score
            validation_drawdown = holdout.worst_max_drawdown
            if holdout.eligible:
                selected_id = leading.candidate.candidate_id
    replay_seconds = time.perf_counter() - replay_started

    elapsed = time.perf_counter() - started
    decisions = len(normalized[0]) * len(normalized[1]) * len(normalized[1][0])
    return SearchResult(
        run_id=scenario_hash[:20],
        backend=selected_device,
        elapsed_seconds=elapsed,
        scoring_seconds=scoring_seconds,
        cpu_replay_seconds=replay_seconds,
        candidate_count=len(normalized[0]),
        scenario_count=len(normalized[1]),
        steps_per_scenario=len(normalized[1][0]),
        evaluated_decisions_per_second=decisions / max(scoring_seconds, 1e-12),
        ranked=tuple(ranked),
        selected_candidate_id=selected_id,
        cpu_replay_count=len(replayed),
        replay_passed=replay_status == "passed",
        replay_status=replay_status,
        validation_score=validation_score,
        validation_max_drawdown=validation_drawdown,
        validation_scenario_count=len(validation) if validation is not None else 0,
        input_sha256=scenario_hash,
        device_name=_device_name(selected_device),
    )


def _validate_inputs(
    candidates: Sequence[StrategyCandidate],
    scenario_returns: Sequence[Sequence[float]],
    policy: SearchPolicy,
) -> tuple[list[StrategyCandidate], list[list[float]]]:
    items = list(candidates)
    if not items:
        raise ValueError("at least one candidate is required")
    if len(items) > policy.max_candidates:
        raise ValueError(f"candidate count exceeds policy maximum {policy.max_candidates}")
    if any(not isinstance(item, StrategyCandidate) for item in items):
        raise TypeError("all candidates must be StrategyCandidate instances")
    if len({item.candidate_id for item in items}) != len(items):
        raise ValueError("candidate_id values must be unique")

    rows: list[list[float]] = []
    for row_index, row in enumerate(scenario_returns):
        values = list(row)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise ValueError(f"scenario {row_index} returns must be numeric values")
        rows.append([float(value) for value in values])
    if not 1 <= len(rows) <= MAX_SCENARIOS:
        raise ValueError(f"scenario count must be between 1 and {MAX_SCENARIOS}")
    steps = len(rows[0]) if rows else 0
    if not 2 <= steps <= MAX_STEPS or any(len(row) != steps for row in rows):
        raise ValueError(f"each scenario must contain the same 2 to {MAX_STEPS} returns")
    for row in rows:
        if any(not math.isfinite(value) or abs(value) > 1.0 for value in row):
            raise ValueError("returns must be finite values in [-1, 1]")
    return items, rows


def _resolve_device(requested: str) -> str:
    name = str(requested or "auto").strip().lower()
    if name not in {"auto", "cpu", "cuda"}:
        raise ValueError("device must be one of: auto, cpu, cuda")
    if name == "cuda":
        if torch is None or not torch.cuda.is_available():
            raise RuntimeError("CUDA was requested but PyTorch CUDA is unavailable")
        return "cuda"
    if name == "auto" and torch is not None and torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _evaluate(
    candidates: Sequence[StrategyCandidate],
    scenarios: Sequence[Sequence[float]],
    policy: SearchPolicy,
    device: str,
) -> list[CandidateScore]:
    if not candidates:
        return []
    if torch is None:
        if device == "cuda":
            raise RuntimeError("CUDA evaluation requires PyTorch")
        return _evaluate_python(candidates, scenarios, policy)
    return _evaluate_torch(candidates, scenarios, policy, device)


def _evaluate_python(
    candidates: Sequence[StrategyCandidate],
    scenarios: Sequence[Sequence[float]],
    policy: SearchPolicy,
) -> list[CandidateScore]:
    scores: list[CandidateScore] = []
    fee = policy.fee_bps / 10000.0
    for candidate in candidates:
        totals: list[float] = []
        drawdowns: list[float] = []
        for row in scenarios:
            prefix = [0.0]
            for value in row:
                prefix.append(prefix[-1] + value)
            position = 0.0
            equity = 0.0
            peak = 0.0
            max_drawdown = 0.0
            for step, value in enumerate(row):
                start = max(0, step - candidate.lookback)
                active = step >= candidate.lookback and (
                    prefix[step] - prefix[start] > candidate.entry_threshold
                )
                next_position = 1.0 if active else 0.0
                turnover = abs(next_position - position)
                equity += next_position * value - turnover * fee
                peak = max(peak, equity)
                max_drawdown = max(max_drawdown, peak - equity)
                position = next_position
            totals.append(equity)
            drawdowns.append(max_drawdown)
        scores.append(_summarize(candidate, totals, drawdowns, policy))
    return _sort_scores(scores)


def _evaluate_torch(
    candidates: Sequence[StrategyCandidate],
    scenarios: Sequence[Sequence[float]],
    policy: SearchPolicy,
    device: str,
) -> list[CandidateScore]:
    assert torch is not None
    target = torch.device(device)
    returns = torch.tensor(scenarios, dtype=torch.float64, device=target)
    scenario_count, steps = returns.shape
    prefix = torch.cat(
        (torch.zeros((scenario_count, 1), dtype=returns.dtype, device=target), returns.cumsum(dim=1)),
        dim=1,
    )
    timestamps = torch.arange(steps, dtype=torch.int64, device=target)
    memory_budget = _TARGET_TENSOR_BYTES
    if target.type == "cuda":
        free_bytes, _total_bytes = torch.cuda.mem_get_info(target)
        persistent_bytes = scenario_count * (steps + 1) * 8
        memory_budget = min(_TARGET_TENSOR_BYTES, max(0, int(free_bytes * 0.60) - persistent_bytes))
    chunk_size = _candidate_chunk_size(scenario_count, steps, memory_budget)
    output: list[CandidateScore] = []
    fee = policy.fee_bps / 10000.0
    offset = 0
    with torch.no_grad():
        while offset < len(candidates):
            chunk = candidates[offset: offset + chunk_size]
            try:
                output.extend(
                    _evaluate_torch_chunk(
                        chunk,
                        returns,
                        prefix,
                        timestamps,
                        policy,
                        target,
                        fee,
                    )
                )
            except Exception as exc:
                if target.type != "cuda" or not _is_cuda_oom(exc) or chunk_size <= 1:
                    raise
                chunk_size = max(1, chunk_size // 2)
                torch.cuda.empty_cache()
                continue
            offset += len(chunk)
    if target.type == "cuda":
        torch.cuda.synchronize(target)
    return _sort_scores(output)


def _evaluate_torch_chunk(
    candidates: Sequence[StrategyCandidate],
    returns: Any,
    prefix: Any,
    timestamps: Any,
    policy: SearchPolicy,
    target: Any,
    fee: float,
) -> list[CandidateScore]:
    assert torch is not None
    scenario_count, steps = returns.shape
    lookbacks = torch.tensor([c.lookback for c in candidates], dtype=torch.int64, device=target)
    thresholds = torch.tensor([c.entry_threshold for c in candidates], dtype=returns.dtype, device=target)
    starts = (timestamps.unsqueeze(0) - lookbacks.unsqueeze(1)).clamp_min(0)
    start_values = prefix.unsqueeze(1).expand(scenario_count, len(candidates), steps + 1).gather(
        2, starts.unsqueeze(0).expand(scenario_count, len(candidates), steps)
    )
    momentum = prefix[:, :-1].unsqueeze(1) - start_values
    active = timestamps.unsqueeze(0) >= lookbacks.unsqueeze(1)
    positions = ((momentum > thresholds.view(1, -1, 1)) & active.view(1, -1, steps)).to(returns.dtype)
    prior = torch.cat((torch.zeros_like(positions[:, :, :1]), positions[:, :, :-1]), dim=2)
    pnl = positions * returns.unsqueeze(1) - (positions - prior).abs() * fee
    curves = pnl.cumsum(dim=2)
    peaks = torch.cummax(curves, dim=2).values
    max_drawdowns = (peaks - curves).amax(dim=2)
    totals = curves[:, :, -1]
    means = totals.mean(dim=0)
    deviations = totals.std(dim=0, unbiased=False)
    worst_drawdowns = max_drawdowns.amax(dim=0)
    output = []
    values = zip(means.cpu().tolist(), deviations.cpu().tolist(), worst_drawdowns.cpu().tolist())
    for candidate, (mean, deviation, worst_drawdown) in zip(candidates, values):
        score = mean - policy.risk_weight * worst_drawdown - policy.volatility_weight * deviation
        output.append(CandidateScore(
            candidate=candidate,
            score=float(score),
            mean_return=float(mean),
            return_std=float(deviation),
            worst_max_drawdown=float(worst_drawdown),
            eligible=float(worst_drawdown) <= policy.max_drawdown and score > 0.0,
        ))
    return output


def _candidate_chunk_size(scenario_count: int, steps: int, memory_budget_bytes: int) -> int:
    bytes_per_candidate = max(1, int(scenario_count) * int(steps) * _BYTES_PER_EVALUATION_ELEMENT)
    return max(1, min(_MAX_CANDIDATES_PER_CHUNK, int(memory_budget_bytes) // bytes_per_candidate))


def _is_cuda_oom(error: Exception) -> bool:
    if torch is None:
        return False
    oom_type = getattr(torch.cuda, "OutOfMemoryError", None)
    return oom_type is not None and isinstance(error, oom_type)


def _summarize(
    candidate: StrategyCandidate,
    totals: Sequence[float],
    drawdowns: Sequence[float],
    policy: SearchPolicy,
) -> CandidateScore:
    mean = sum(totals) / len(totals)
    variance = sum((value - mean) ** 2 for value in totals) / len(totals)
    deviation = math.sqrt(variance)
    worst_drawdown = max(drawdowns)
    score = mean - policy.risk_weight * worst_drawdown - policy.volatility_weight * deviation
    return CandidateScore(
        candidate=candidate,
        score=score,
        mean_return=mean,
        return_std=deviation,
        worst_max_drawdown=worst_drawdown,
        eligible=worst_drawdown <= policy.max_drawdown and score > 0.0,
    )


def _sort_scores(scores: Sequence[CandidateScore]) -> list[CandidateScore]:
    ordered = sorted(
        scores,
        key=lambda item: (
            not item.eligible,
            -item.score,
            item.candidate.lookback,
            item.candidate.candidate_id,
        ),
    )
    return [
        CandidateScore(
            candidate=item.candidate,
            score=item.score,
            mean_return=item.mean_return,
            return_std=item.return_std,
            worst_max_drawdown=item.worst_max_drawdown,
            eligible=item.eligible,
            rank=index + 1,
        )
        for index, item in enumerate(ordered)
    ]


def _scores_match(left: CandidateScore, right: CandidateScore, tolerance: float) -> bool:
    return all(
        abs(getattr(left, field) - getattr(right, field)) <= tolerance
        for field in ("score", "mean_return", "return_std", "worst_max_drawdown")
    ) and left.eligible == right.eligible


def _input_hash(
    candidates: Sequence[StrategyCandidate],
    scenarios: Sequence[Sequence[float]],
    validation_scenarios: Sequence[Sequence[float]] | None,
    policy: SearchPolicy,
) -> str:
    digest = hashlib.sha256()
    digest.update(b"pacer-vnext-strategy-search-v2\0")
    digest.update(json.dumps(asdict(policy), sort_keys=True, separators=(",", ":")).encode("utf-8"))
    for candidate in candidates:
        digest.update(json.dumps(asdict(candidate), sort_keys=True, separators=(",", ":")).encode("utf-8"))
    for group in (scenarios, validation_scenarios):
        digest.update(struct.pack("<QQ", len(group or ()), len(group[0]) if group else 0))
        for row in group or ():
            digest.update(struct.pack("<Q", len(row)))
            values = array("d", row)
            if values.itemsize != 8:
                digest.update(struct.pack("<" + "d" * len(row), *row))
            else:
                if sys.byteorder != "little":
                    values.byteswap()
                digest.update(values.tobytes())
    return digest.hexdigest()


def _device_name(device: str) -> str:
    if device == "cuda" and torch is not None:
        return str(torch.cuda.get_device_name(0))
    return "cpu"
