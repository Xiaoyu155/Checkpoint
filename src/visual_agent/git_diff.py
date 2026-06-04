from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Iterable


def changed_files(*, base: str = "HEAD", cwd: Path | None = None) -> list[str]:
    root = cwd or Path.cwd()
    files: set[str] = set()
    for command in (
        ["git", "diff", "--name-only", base],
        ["git", "diff", "--name-only", "--cached"],
    ):
        try:
            result = subprocess.run(
                command,
                cwd=str(root),
                text=True,
                capture_output=True,
                check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            return []
        files.update(normalize_changed_file(line) for line in result.stdout.splitlines() if line.strip())
    return sorted(item for item in files if item)


def affected_workflows(workflows: Iterable[Any], *, changed: list[str]) -> list[Any]:
    workflow_list = list(workflows)
    normalized_changed = [normalize_changed_file(item) for item in changed if str(item).strip()]
    if not normalized_changed:
        return workflow_list

    affected = []
    for workflow in workflow_list:
        affects = tuple(str(item).strip() for item in getattr(workflow, "affects", ()) or () if str(item).strip())
        if not affects:
            affected.append(workflow)
            continue
        if any(workflow_affects_changed_path(pattern, normalized_changed) for pattern in affects):
            affected.append(workflow)
    return affected


def workflow_affects_changed_path(pattern: str, changed: list[str]) -> bool:
    normalized = normalize_affects_pattern(pattern)
    if not normalized:
        return False
    if has_glob_syntax(normalized):
        from fnmatch import fnmatch

        return any(fnmatch(path, normalized) for path in changed)
    if normalized.endswith("/"):
        return any(path.startswith(normalized) for path in changed)
    return any(path == normalized or path.startswith(normalized + "/") for path in changed)


def normalize_changed_file(value: str) -> str:
    return str(value).replace("\\", "/").strip().lstrip("./")


def normalize_affects_pattern(value: str) -> str:
    text = normalize_changed_file(value)
    return text


def has_glob_syntax(value: str) -> bool:
    return any(char in value for char in "*?[")
