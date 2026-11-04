from visual_agent import cli
from visual_agent.vnext import strategy_search as search


def test_windows_executable_name_on_any_host(monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", [r"C:\Python\Scripts\pacer.exe"])
    assert cli.current_cli_name() == "pacer"


def test_search_without_torch_replays_cpu(monkeypatch):
    monkeypatch.setattr(search, "torch", None)
    candidate = search.StrategyCandidate("a", 2, 0.0)
    result = search.search_strategies(
        [candidate], [[0.01] * 8], validation_scenario_returns=[[0.02] * 8],
        policy=search.SearchPolicy(max_drawdown=1.0, max_candidates=1, top_k=1),
    )
    assert result.backend == "cpu"
    assert result.replay_passed
    assert result.selected_candidate_id == "a"
