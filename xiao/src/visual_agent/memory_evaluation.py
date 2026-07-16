"""Optional standards-based evaluation for project-memory retrieval."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


DEFAULT_MEMORY_EVAL_CUTOFF = 3


def evaluate_memory_ranking(
    *,
    qrels: Any,
    run: Any,
    cutoff: int = DEFAULT_MEMORY_EVAL_CUTOFF,
) -> dict[str, Any]:
    """Evaluate a TREC-style qrels/run pair through ``ir_measures``.

    Pacer deliberately delegates metric definitions to ``ir_measures``. The
    optional dependency is not required by the runtime memory path.
    """
    ranking_cutoff = int(cutoff)
    if ranking_cutoff <= 0:
        raise ValueError("cutoff must be greater than zero")
    try:
        ir_measures = _load_ir_measures()
    except ImportError as exc:
        return {
            "schema_version": 1,
            "status": "unavailable",
            "provider": "ir_measures",
            "reason_code": "dependency_missing",
            "message": str(exc) or "The optional ir-measures dependency is not installed.",
            "install_hint": 'pip install "visual-agent[eval]"',
            "cutoff": ranking_cutoff,
            "measures": {},
        }

    requested = [
        ir_measures.R @ ranking_cutoff,
        ir_measures.RR @ ranking_cutoff,
        ir_measures.nDCG @ ranking_cutoff,
        ir_measures.Success @ ranking_cutoff,
    ]
    calculated = ir_measures.calc_aggregate(requested, qrels, run)
    measures = {str(measure): float(calculated[measure]) for measure in requested}
    return {
        "schema_version": 1,
        "status": "evaluated",
        "provider": "ir_measures",
        "provider_version": str(getattr(ir_measures, "__version__", "unknown")),
        "cutoff": ranking_cutoff,
        "query_count": len(qrels) if isinstance(qrels, Mapping) else None,
        "measures": measures,
        "semantics": {
            f"RR@{ranking_cutoff}": "Aggregate RR is mean reciprocal rank across queries.",
            f"Success@{ranking_cutoff}": "At least one relevant memory appears in the top results.",
        },
    }


def _load_ir_measures() -> Any:
    import ir_measures

    return ir_measures
