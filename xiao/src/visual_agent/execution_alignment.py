"""Regression guard for worker prompts that discourage codebase exploration."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Any, Sequence


FORBIDDEN_EXPLORATION_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "repository_scan_ban",
        "Worker prompts must not prohibit repository-wide discovery.",
        re.compile(
            r"\b(?:do\s+not|don't|never)\s+"
            r"(?:scan(?:ning)?|grep(?:ping)?)(?:\s+or\s+(?:scan(?:ning)?|grep(?:ping)?))?\b"
            r"(?:\s+(?:the\s+)?(?:(?:whole|entire|full)\s+)?(?:codebase|repo(?:sitory)?))?",
            re.IGNORECASE,
        ),
    ),
    (
        "model_budget_conservation",
        "Worker prompts must not trade codebase understanding for model/token budget conservation.",
        re.compile(
            r"\b(?:conserve|save|minimi[sz]e|reduce)\s+"
            r"(?:(?:the|your)\s+)?(?:(?:model|token|inference)\s+)?budget\b",
            re.IGNORECASE,
        ),
    ),
    (
        "files_read_restriction",
        "Worker prompts must not restrict reading to an assumed minimal file set.",
        re.compile(
            r"\bread\s+only\s+(?:the\s+)?files\s+(?:you|that\s+you)\s+need\b",
            re.IGNORECASE,
        ),
    ),
    (
        "broad_search_ban",
        "Worker prompts must not discourage broad repository search when it is needed.",
        re.compile(
            r"\bavoid\s+(?:a\s+)?(?:broad|repo(?:sitory)?-wide|codebase-wide)\s+"
            r"(?:search(?:es|ing)?|scan(?:s|ning)?|grep(?:ping)?)\b",
            re.IGNORECASE,
        ),
    ),
)


def audit_prompt_text(text: str) -> list[dict[str, Any]]:
    """Return structured findings for exploration-limiting prompt language."""
    value = str(text or "")
    issues: list[dict[str, Any]] = []
    for code, message, pattern in FORBIDDEN_EXPLORATION_PATTERNS:
        for match in pattern.finditer(value):
            issues.append(
                {
                    "code": code,
                    "message": message,
                    "matched_text": match.group(0),
                    "start": match.start(),
                    "end": match.end(),
                }
            )
    return issues


def lint_worker_prompt_source(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Statically audit prompt literals in ``chief_dispatch.py``.

    The source is parsed rather than imported. This keeps the guard independent
    from the dispatch runtime and avoids executing import-time side effects.
    """
    source_path = Path(path).expanduser().resolve() if path is not None else Path(__file__).with_name("chief_dispatch.py")
    try:
        source = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [_source_issue("source_read_error", source_path, f"Could not read worker prompt source: {exc}")]
    try:
        tree = ast.parse(source, filename=str(source_path))
    except SyntaxError as exc:
        issue = _source_issue("source_parse_error", source_path, f"Could not parse worker prompt source: {exc.msg}")
        issue["line"] = exc.lineno
        return [issue]

    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if not functions:
        return [
            _source_issue(
                "worker_prompt_source_missing",
                source_path,
                "No functions were found in the worker dispatch source.",
            )
        ]

    issues: list[dict[str, Any]] = []
    # Worker-visible text is assembled in dispatch, backend, and reporting
    # functions whose names do not consistently contain "prompt". Audit every
    # function literal so moving a template cannot silently bypass the guard.
    for function in functions:
        nested_functions = {
            node
            for node in ast.walk(function)
            if node is not function and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for node in ast.walk(function):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if any(node in set(ast.walk(nested)) for nested in nested_functions):
                continue
            for issue in audit_prompt_text(node.value):
                issues.append(
                    {
                        **issue,
                        "source_path": str(source_path),
                        "function": function.name,
                        "line": getattr(node, "lineno", None),
                    }
                )
    return issues


def build_worker_prompt_alignment_check(path: str | Path | None = None) -> dict[str, Any]:
    """Return the prompt lint in the same structured shape for every gate."""
    issues = lint_worker_prompt_source(path)
    return {
        "status": "blocked" if issues else "ok",
        "issue_count": len(issues),
        "issues": issues,
    }


def _source_issue(code: str, path: Path, message: str) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "matched_text": "",
        "source_path": str(path),
        "function": "",
        "line": None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) > 1:
        print("usage: python -m visual_agent.execution_alignment [chief_dispatch.py]", file=sys.stderr)
        return 2
    check = build_worker_prompt_alignment_check(args[0] if args else None)
    issues = check["issues"]
    if not issues:
        print("Worker prompt alignment: ok")
        return 0
    for issue in issues:
        location = str(issue.get("source_path") or "")
        if issue.get("line"):
            location += f":{issue['line']}"
        match = str(issue.get("matched_text") or "")
        detail = f" ({match!r})" if match else ""
        print(f"{location}: {issue.get('code')}: {issue.get('message')}{detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
