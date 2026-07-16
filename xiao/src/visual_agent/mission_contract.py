"""Requirement contract helpers for Pacer missions.

The intake UI can ask several short questions before a mission is dispatched.
This module turns that conversation into a compact, durable contract that the
planner, worker prompt, reports, and dashboards can all read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Sequence


def normalize_requirement_contract(
    payload: dict[str, Any] | None = None,
    *,
    goal: str = "",
    answers: Sequence[str] | None = None,
    repo_root: str | Path | None = None,
    test_command: str | None = None,
    agent: str | None = None,
) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    has_contract_source = bool(source)
    incoming_answers = _strings(source.get("answers"))
    extra_answers = _strings(answers or [])
    merged_answers = _dedupe([*incoming_answers, *extra_answers])

    if not has_contract_source and not merged_answers:
        return {}

    input_goal = _first_text(source.get("input_goal"), source.get("goal"), goal)
    suggested_goal = _first_text(source.get("suggested_goal"), source.get("final_goal"), goal, input_goal)
    final_goal = _first_text(source.get("final_goal"), suggested_goal, goal, input_goal)
    acceptance_hint = _first_text(source.get("acceptance_hint"))
    questions = _dedupe(_strings(source.get("clarifying_questions")))

    if not any([input_goal, suggested_goal, final_goal, merged_answers, acceptance_hint, questions]):
        return {}

    contract: dict[str, Any] = {
        "schema_version": 1,
        "kind": "requirement_contract",
        "source": _first_text(source.get("source"), "manual"),
        "input_goal": input_goal,
        "suggested_goal": suggested_goal,
        "final_goal": final_goal,
        "answers": merged_answers,
        "acceptance_hint": acceptance_hint,
        "clarifying_questions": questions,
    }
    if "already_clear" in source:
        contract["already_clear"] = bool(source.get("already_clear"))
    if source.get("model_id"):
        contract["model_id"] = str(source.get("model_id"))
    if source.get("intake_policy"):
        contract["intake_policy"] = str(source.get("intake_policy"))
    if source.get("model_unavailable") is not None:
        contract["model_unavailable"] = bool(source.get("model_unavailable"))
    if source.get("model_error"):
        contract["model_error"] = str(source.get("model_error"))
    if repo_root is not None and str(repo_root).strip():
        contract["repo_root"] = str(repo_root)
    if test_command is not None and str(test_command).strip():
        contract["test_command"] = str(test_command).strip()
    if agent is not None and str(agent).strip():
        contract["agent"] = str(agent).strip()
    return contract


def requirement_contract_planning_answers(contract: dict[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(contract, dict) or not contract:
        return ()
    answers: list[str] = []
    input_goal = str(contract.get("input_goal") or "").strip()
    final_goal = str(contract.get("final_goal") or "").strip()
    suggested_goal = str(contract.get("suggested_goal") or "").strip()
    if input_goal and final_goal and input_goal != final_goal:
        answers.append(f"原始目标：{input_goal}")
    if suggested_goal and suggested_goal not in {input_goal, final_goal}:
        answers.append(f"模型建议目标：{suggested_goal}")
    for item in _strings(contract.get("answers")):
        answers.append(f"用户补充：{item}")
    hint = str(contract.get("acceptance_hint") or "").strip()
    if hint:
        answers.append(f"验收提示：{hint}")
    test_command = str(contract.get("test_command") or "").strip()
    if test_command:
        answers.append(f"验收命令：{test_command}")
    return tuple(_dedupe(answers))


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Iterable):
        result: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                result.append(text)
        return result
    text = str(value).strip()
    return [text] if text else []


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
