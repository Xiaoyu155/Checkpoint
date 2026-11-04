from __future__ import annotations

import pytest

from visual_agent.vnext import strategy_search as search


def test_candidate_schema_does_not_let_model_override_cost_or_acceptance() -> None:
    parsed = search.parse_candidates(
        {"candidates": [{"candidate_id": "a", "lookback": 4, "entry_threshold": 0.01}]}
    )
    assert parsed == [search.StrategyCandidate("a", 4, 0.01)]

    with pytest.raises(ValueError, match="exactly"):
        search.parse_candidates(
            {"candidates": [{"candidate_id": "a", "lookback": 4, "entry_threshold": 0.01, "fee_bps": 0}]}
        )


def test_neighborhood_expansion_is_deterministic_and_capped() -> None:
    seeds = [search.StrategyCandidate("seed-a", 16, 0.01), search.StrategyCandidate("seed-b", 32, 0.02)]
    first = search.expand_candidate_neighborhood(seeds, max_candidates=12)
    second = search.expand_candidate_neighborhood(seeds, max_candidates=12)
    assert first == second
    assert len(first) == 12
    assert len({item.candidate_id for item in first}) == 12
    assert len({(item.lookback, item.entry_threshold) for item in first}) == 12

    bounded = search.expand_candidate_neighborhood(
        [search.StrategyCandidate("near-limit", 30, 0.01)],
        max_candidates=16,
        max_lookback=32,
    )
    assert all(item.lookback <= 32 for item in bounded)


def test_search_is_bounded_and_fails_closed_on_bad_scenarios() -> None:
    policy = search.SearchPolicy(max_candidates=1, top_k=1)
    candidates = [search.StrategyCandidate("a", 2, 0.01), search.StrategyCandidate("b", 3, 0.01)]
    with pytest.raises(ValueError, match="exceeds policy"):
        search.search_strategies(candidates, [[0.01] * 8], policy=policy, device="cpu")

    with pytest.raises(ValueError, match="finite"):
        search.search_strategies(
            [candidates[0]], [[0.0, float("nan"), 0.0]], device="cpu"
        )


def test_cpu_tensor_scores_match_independent_python_reference() -> None:
    candidates = [search.StrategyCandidate("fast", 2, 0.002), search.StrategyCandidate("slow", 4, 0.006)]
    scenarios = [
        [0.01, 0.01, -0.02, 0.03, -0.01, 0.01, 0.02, -0.005],
        [-0.01, -0.01, 0.02, -0.03, 0.01, -0.01, -0.02, 0.005],
    ]
    policy = search.SearchPolicy(max_drawdown=1.0)
    tensor_scores = search._evaluate_torch(candidates, scenarios, policy, "cpu")
    reference_scores = search._evaluate_python(candidates, scenarios, policy)
    assert [item.candidate.candidate_id for item in tensor_scores] == [
        item.candidate.candidate_id for item in reference_scores
    ]
    for actual, expected in zip(tensor_scores, reference_scores):
        assert actual.score == pytest.approx(expected.score, abs=2e-6)
        assert actual.mean_return == pytest.approx(expected.mean_return, abs=2e-6)
        assert actual.return_std == pytest.approx(expected.return_std, abs=2e-6)
        assert actual.worst_max_drawdown == pytest.approx(expected.worst_max_drawdown, abs=2e-6)


def test_signal_uses_only_returns_before_the_evaluated_step() -> None:
    candidate = search.StrategyCandidate("no-lookahead", 2, 0.3)
    scored = search._evaluate_torch(
        [candidate],
        [[0.2, 0.2, -0.5, 0.0]],
        search.SearchPolicy(max_drawdown=1.0),
        "cpu",
    )[0]
    assert scored.mean_return == pytest.approx(-0.5016, abs=2e-6)


def test_search_does_not_select_a_candidate_that_loses_to_flat() -> None:
    candidate = search.StrategyCandidate("losing", 2, 0.005)
    selection = [[0.01, 0.01, -0.02, -0.02, -0.02, -0.02]]
    validation = [[0.015, 0.015, -0.03, -0.03, -0.03, -0.03]]
    result = search.search_strategies(
        [candidate],
        selection,
        validation_scenario_returns=validation,
        policy=search.SearchPolicy(max_drawdown=1.0, max_candidates=1, top_k=1),
        device="cpu",
    )
    assert result.ranked[0].score < 0.0
    assert result.ranked[0].eligible is False
    assert result.selected_candidate_id is None
    assert result.replay_status == "not_run"


def test_search_requires_distinct_holdout_to_select_and_checks_it() -> None:
    candidates = [
        search.StrategyCandidate("fast", 2, 0.005),
        search.StrategyCandidate("slow", 3, 0.005),
    ]
    selection = [
        [0.01, 0.011, 0.012, 0.013, 0.014, 0.015, 0.016, 0.017],
        [0.008, 0.009, 0.01, 0.011, 0.012, 0.013, 0.014, 0.015],
    ]
    validation = [
        [0.006, 0.007, 0.008, 0.009, 0.01, 0.011, 0.012, 0.013],
        [0.007, 0.008, 0.009, 0.01, 0.011, 0.012, 0.013, 0.014],
    ]
    policy = search.SearchPolicy(max_drawdown=1.0, max_candidates=2, top_k=2)
    result = search.search_strategies(
        candidates,
        selection,
        validation_scenario_returns=validation,
        policy=policy,
        device="cpu",
    )
    assert result.selected_candidate_id == result.ranked[0].candidate.candidate_id
    assert result.replay_status == "passed"
    assert result.cpu_replay_count == 2
    assert result.validation_scenario_count == 2
    assert result.validation_score is not None and result.validation_score > 0.0

    without_holdout = search.search_strategies(candidates, selection, policy=policy, device="cpu")
    assert without_holdout.selected_candidate_id is None
    assert without_holdout.validation_scenario_count == 0

    losing_holdout = [
        [0.01, 0.01, -0.02, -0.02, -0.02, -0.02, -0.02, -0.02],
        [0.012, 0.012, -0.025, -0.025, -0.025, -0.025, -0.025, -0.025],
    ]
    rejected = search.search_strategies(
        candidates,
        selection,
        validation_scenario_returns=losing_holdout,
        policy=policy,
        device="cpu",
    )
    assert rejected.validation_score is not None and rejected.validation_score <= 0.0
    assert rejected.selected_candidate_id is None

    with pytest.raises(ValueError, match="must differ"):
        search.search_strategies(
            candidates,
            selection,
            validation_scenario_returns=selection,
            policy=policy,
            device="cpu",
        )
    with pytest.raises(ValueError, match="same horizon"):
        search.search_strategies(
            candidates,
            selection,
            validation_scenario_returns=[[0.01] * 7, [0.02] * 7],
            policy=policy,
            device="cpu",
        )


def test_input_hash_includes_shape_and_full_precision_values() -> None:
    candidate = [search.StrategyCandidate("a", 2, 0.01)]
    policy = search.SearchPolicy(max_candidates=1, top_k=1)
    flat = search._input_hash(candidate, [[0.1, 0.2, 0.3, 0.4]], None, policy)
    reshaped = search._input_hash(candidate, [[0.1, 0.2], [0.3, 0.4]], None, policy)
    precise = search._input_hash(candidate, [[0.1, 0.2, 0.3, 0.40000001]], None, policy)
    assert flat != reshaped
    assert flat != precise
    assert search.StrategyCandidate(" a ", 2, 0) == search.StrategyCandidate("a", 2, 0.0)


def test_cuda_chunk_budget_uses_intermediate_tensor_bytes() -> None:
    assert search._candidate_chunk_size(10, 100, 10 * 100 * 64 * 5) == 5
    assert search._candidate_chunk_size(10, 100, 10 * 100 * 64 * 20) == search._MAX_CANDIDATES_PER_CHUNK


def test_cuda_out_of_memory_falls_back_to_cpu(monkeypatch) -> None:
    candidates = [search.StrategyCandidate("a", 2, 0.002)]
    scenarios = [[0.01, 0.011, 0.012, 0.013], [0.008, 0.009, 0.01, 0.011]]
    calls = []

    monkeypatch.setattr(search.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(search.torch.cuda, "empty_cache", lambda: None)

    def evaluate(items, rows, policy, device):
        calls.append(device)
        if device == "cuda":
            raise search.torch.cuda.OutOfMemoryError("synthetic OOM")
        return search._evaluate_python(items, rows, policy)

    monkeypatch.setattr(search, "_evaluate", evaluate)
    result = search.search_strategies(
        candidates,
        scenarios,
        policy=search.SearchPolicy(max_candidates=1, top_k=1),
        device="auto",
    )

    assert calls == ["cuda", "cpu"]
    assert result.backend == "cpu"
    assert result.device_name == "cpu"


@pytest.mark.skipif(
    search.torch is None or not search.torch.cuda.is_available(),
    reason="CUDA smoke requires a working PyTorch CUDA runtime",
)
def test_cuda_oom_retries_with_a_halved_chunk(monkeypatch) -> None:
    candidates = [search.StrategyCandidate(f"c{index}", 2, index * 0.001) for index in range(8)]
    scenarios = [[0.01, 0.011, 0.012, 0.013], [0.008, 0.009, 0.01, 0.011]]
    calls = []
    evaluate_chunk = search._evaluate_torch_chunk

    def fail_first_large_chunk(items, *args):
        calls.append(len(items))
        if len(items) > 1 and len(calls) == 1:
            raise search.torch.cuda.OutOfMemoryError("synthetic OOM")
        return evaluate_chunk(items, *args)

    monkeypatch.setattr(search, "_evaluate_torch_chunk", fail_first_large_chunk)
    search._evaluate_torch(candidates, scenarios, search.SearchPolicy(max_candidates=8, top_k=1), "cuda")

    assert calls[0] == 8
    assert calls[1:] == [4, 4]


def test_input_scenarios_reject_strings_booleans_and_unusable_lookbacks() -> None:
    candidate = search.StrategyCandidate("a", 2, 0.01)
    with pytest.raises(ValueError, match="numeric"):
        search.search_strategies([candidate], [[0.1, "0.2", 0.3]], device="cpu")
    with pytest.raises(ValueError, match="numeric"):
        search.search_strategies([candidate], [[0.1, True, 0.3]], device="cpu")
    with pytest.raises(ValueError, match="shorter than"):
        search.search_strategies(
            [search.StrategyCandidate("too-long", 4, 0.01)],
            [[0.1, 0.1, 0.1, 0.1]],
            device="cpu",
        )


@pytest.mark.skipif(
    search.torch is None or not search.torch.cuda.is_available(),
    reason="CUDA smoke requires a working PyTorch CUDA runtime",
)
def test_cuda_rank_is_cpu_replayed_before_selection() -> None:
    candidates = [search.StrategyCandidate("a", 2, 0.002), search.StrategyCandidate("b", 3, 0.004)]
    scenarios = [
        [0.01, 0.011, 0.012, 0.013, 0.014, 0.015, 0.016, 0.017],
        [0.008, 0.009, 0.01, 0.011, 0.012, 0.013, 0.014, 0.015],
    ]
    validation = [
        [0.006, 0.007, 0.008, 0.009, 0.01, 0.011, 0.012, 0.013],
        [0.007, 0.008, 0.009, 0.01, 0.011, 0.012, 0.013, 0.014],
    ]
    result = search.search_strategies(
        candidates,
        scenarios,
        validation_scenario_returns=validation,
        policy=search.SearchPolicy(max_drawdown=1.0, top_k=2),
        device="cuda",
    )
    assert result.backend == "cuda"
    assert result.device_name == search.torch.cuda.get_device_name(0)
    assert result.cpu_replay_count == 2
    assert result.replay_passed is True
    assert result.replay_status == "passed"
    assert result.selected_candidate_id is not None
    assert result.input_sha256
