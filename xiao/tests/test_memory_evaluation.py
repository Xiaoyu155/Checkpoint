from __future__ import annotations

import pytest

from visual_agent import memory_evaluation


class FakeMeasure:
    def __init__(self, name: str) -> None:
        self.name = name

    def __matmul__(self, cutoff: int) -> "FakeMeasure":
        return FakeMeasure(f"{self.name}@{cutoff}")

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FakeMeasure) and self.name == other.name

    def __str__(self) -> str:
        return self.name


class FakeIrMeasures:
    __version__ = "test"
    R = FakeMeasure("R")
    RR = FakeMeasure("RR")
    nDCG = FakeMeasure("nDCG")
    Success = FakeMeasure("Success")

    def __init__(self) -> None:
        self.calls = []

    def calc_aggregate(self, measures, qrels, run):
        self.calls.append({"measures": measures, "qrels": qrels, "run": run})
        scores = {
            "R@3": 0.75,
            "RR@3": 0.5,
            "nDCG@3": 0.625,
            "Success@3": 1.0,
        }
        return {measure: scores[str(measure)] for measure in measures}


def test_memory_evaluation_delegates_standard_metrics_to_ir_measures(monkeypatch) -> None:
    provider = FakeIrMeasures()
    monkeypatch.setattr(memory_evaluation, "_load_ir_measures", lambda: provider)
    qrels = {"task-1": {"mission:one": 2, "mission:two": 0}}
    run = {"task-1": {"mission:one": 75.0, "mission:two": 5.0}}

    payload = memory_evaluation.evaluate_memory_ranking(qrels=qrels, run=run)

    assert payload["status"] == "evaluated"
    assert payload["provider"] == "ir_measures"
    assert payload["provider_version"] == "test"
    assert payload["query_count"] == 1
    assert payload["measures"] == {
        "R@3": 0.75,
        "RR@3": 0.5,
        "nDCG@3": 0.625,
        "Success@3": 1.0,
    }
    assert len(provider.calls) == 1
    assert [str(measure) for measure in provider.calls[0]["measures"]] == [
        "R@3",
        "RR@3",
        "nDCG@3",
        "Success@3",
    ]
    assert provider.calls[0]["qrels"] == qrels
    assert provider.calls[0]["run"] == run


def test_memory_evaluation_reports_missing_optional_dependency(monkeypatch) -> None:
    def dependency_missing():
        raise ModuleNotFoundError("No module named 'ir_measures'")

    monkeypatch.setattr(memory_evaluation, "_load_ir_measures", dependency_missing)

    payload = memory_evaluation.evaluate_memory_ranking(qrels={}, run={})

    assert payload["status"] == "unavailable"
    assert payload["reason_code"] == "dependency_missing"
    assert payload["provider"] == "ir_measures"
    assert payload["measures"] == {}
    assert "visual-agent[eval]" in payload["install_hint"]


def test_memory_evaluation_rejects_non_positive_cutoff() -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        memory_evaluation.evaluate_memory_ranking(qrels={}, run={}, cutoff=0)
