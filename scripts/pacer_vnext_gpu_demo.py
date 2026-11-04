from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import asdict

from visual_agent.vnext.strategy_search import (
    SearchPolicy,
    StrategyCandidate,
    expand_candidate_neighborhood,
    make_synthetic_returns,
    request_gemini_seed_candidates,
    search_strategies,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded PyTorch CUDA strategy search smoke benchmark")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--scenarios", type=int, default=64)
    parser.add_argument("--steps", type=int, default=2048)
    parser.add_argument("--candidates", type=int, default=64)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--gemini-seeds", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.candidates <= 128 or args.repeats < 1 or args.steps < 3:
        parser.error("candidate count must be 1..128, repeats >= 1, and steps >= 3")

    returns = make_synthetic_returns(scenario_count=args.scenarios, steps=args.steps, seed=11)
    validation_returns = make_synthetic_returns(
        scenario_count=args.scenarios, steps=args.steps, seed=29
    )
    proposal_metadata = {"type": "local_grid"}
    seed_candidates = []
    max_lookback = min(512, args.steps - 1)
    if args.gemini_seeds:
        seeds, proposal_metadata = request_gemini_seed_candidates(
            "Provide diverse momentum lookback and entry-threshold seeds for a synthetic long/flat strategy benchmark.",
            max_lookback=max_lookback,
        )
        seed_candidates = seeds
        candidates = expand_candidate_neighborhood(
            seeds,
            max_candidates=args.candidates,
            max_lookback=max_lookback,
        )
    else:
        lookbacks = [
            lookback
            for lookback in (8, 16, 32, 64, 128, 192, 256, 384)
            if lookback < args.steps
        ]
        if not lookbacks:
            lookbacks = [args.steps - 1]
        candidates = [
            StrategyCandidate(
                candidate_id=f"lb{lookback}-th{threshold_index}",
                lookback=lookback,
                entry_threshold=threshold_index * 0.001,
            )
            for threshold_index in range(16)
            for lookback in lookbacks
        ][: args.candidates]
    if not candidates:
        raise RuntimeError("candidate expansion returned no candidates")
    policy = SearchPolicy(
        fee_bps=8.0,
        max_drawdown=0.6,
        risk_weight=1.0,
        volatility_weight=0.25,
        max_candidates=len(candidates),
        top_k=3,
    )

    measurements: dict[str, list[dict]] = {"cpu": [], "cuda": []}
    for device in ("cpu", "cuda"):
        if device == "cuda":
            import torch

            if not torch.cuda.is_available():
                continue
            torch.cuda.synchronize()
            torch.cuda.reset_peak_memory_stats()
        search_strategies(
            candidates[:2],
            returns[:2],
            validation_scenario_returns=validation_returns[:2],
            policy=policy,
            device=device,
        )
        for _ in range(args.repeats):
            result = search_strategies(
                candidates,
                returns,
                validation_scenario_returns=validation_returns,
                policy=policy,
                device=device,
            )
            measurements[device].append(result.to_dict())

    summary = {}
    for device, rows in measurements.items():
        if not rows:
            continue
        summary[device] = {
            "backend": rows[-1]["backend"],
            "device_name": rows[-1]["device_name"],
            "median_scoring_seconds": statistics.median(item["scoring_seconds"] for item in rows),
            "median_total_seconds": statistics.median(item["elapsed_seconds"] for item in rows),
            "median_decisions_per_second": statistics.median(
                item["evaluated_decisions_per_second"] for item in rows
            ),
            "selected_candidate_id": rows[-1]["selected_candidate_id"],
            "top_candidates": rows[-1]["ranked"][:5],
            "cpu_replay_count": rows[-1]["cpu_replay_count"],
            "replay_passed": rows[-1]["replay_passed"],
            "replay_status": rows[-1]["replay_status"],
            "validation_score": rows[-1]["validation_score"],
            "validation_max_drawdown": rows[-1]["validation_max_drawdown"],
            "validation_scenario_count": rows[-1]["validation_scenario_count"],
            "run_id": rows[-1]["run_id"],
        }
        if device == "cuda":
            import torch

            summary[device]["peak_allocated_mib"] = round(
                torch.cuda.max_memory_allocated() / (1024 * 1024), 2
            )
    gpu = summary.get("cuda")
    cpu = summary.get("cpu")
    if gpu and cpu:
        summary["cuda_scoring_speedup_vs_cpu"] = (
            cpu["median_scoring_seconds"] / max(gpu["median_scoring_seconds"], 1e-12)
        )
        summary["top_rank_candidate_agrees"] = (
            gpu["top_candidates"][0]["candidate"]["candidate_id"]
            == cpu["top_candidates"][0]["candidate"]["candidate_id"]
        )
        summary["selected_candidate_agrees"] = (
            gpu["selected_candidate_id"] == cpu["selected_candidate_id"]
            if gpu["selected_candidate_id"] is not None or cpu["selected_candidate_id"] is not None
            else None
        )
    print(json.dumps({
        "note": "Synthetic returns are a throughput fixture, not market evidence or an investment claim.",
        "dimensions": {"candidates": len(candidates), "scenarios": len(returns), "steps": len(returns[0])},
        "proposal": proposal_metadata,
        "seed_candidates": [asdict(candidate) for candidate in seed_candidates],
        "runs_per_device": args.repeats,
        "results": summary,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
