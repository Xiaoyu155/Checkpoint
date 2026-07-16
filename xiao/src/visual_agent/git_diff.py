from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Iterable

from .change_set import collect_repository_change_set
from .context_ingestion import CodeChange


def changed_files(*, base: str = "HEAD", cwd: Path | None = None, include_untracked: bool = True) -> list[str]:
    root = cwd or Path.cwd()
    change_set = collect_repository_change_set(repo_root=root, base_ref=base)
    if not change_set.complete:
        return []
    paths = [item.path for item in change_set.changes]
    if include_untracked:
        return sorted(dict.fromkeys(paths))
    untracked = set(git_untracked_files(cwd=root))
    return sorted(dict.fromkeys(path for path in paths if path not in untracked))


def collect_code_changes(
    *,
    base: str = "HEAD",
    cwd: Path | None = None,
    include_untracked: bool = True,
    max_file_bytes: int = 200_000,
) -> tuple[CodeChange, ...]:
    root = cwd or Path.cwd()
    change_set = collect_repository_change_set(repo_root=root, base_ref=base)
    if not change_set.complete:
        return ()
    untracked = set(git_untracked_files(cwd=root)) if not include_untracked else set()
    changes: list[CodeChange] = []
    for fact in change_set.changes:
        relative_path = fact.path
        if relative_path in untracked:
            continue
        path = root / relative_path
        exists = path.exists()
        if exists and path.is_dir():
            continue
        before = git_show_file(base, relative_path, cwd=root)
        if exists:
            try:
                if path.stat().st_size > max_file_bytes:
                    continue
                after = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
        else:
            after = ""
        if fact.state == "deleted" or (before is not None and not exists):
            change_type = "deleted"
        elif fact.state == "created" or before is None:
            change_type = "added"
        else:
            change_type = "modified"
        changes.append(
            CodeChange(
                file_path=relative_path,
                before=before,
                after=after,
                change_type=change_type,  # type: ignore[arg-type]
            )
        )
    return tuple(changes)


def git_diff_name_status(*, base: str = "HEAD", cwd: Path | None = None) -> dict[str, str]:
    root = cwd or Path.cwd()
    change_set = collect_repository_change_set(repo_root=root, base_ref=base)
    if not change_set.complete:
        return {}
    status_codes = {"created": "A", "modified": "M", "deleted": "D"}
    return {
        item.path: status_codes[item.state]
        for item in change_set.changes
        if item.state in status_codes
    }


def git_untracked_files(*, cwd: Path | None = None) -> list[str]:
    root = cwd or Path.cwd()
    try:
        result = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard"],
            cwd=str(root),
            text=True,
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []
    return sorted(normalize_changed_file(line) for line in result.stdout.splitlines() if line.strip())


def git_show_file(base: str, relative_path: str, *, cwd: Path | None = None) -> str | None:
    root = cwd or Path.cwd()
    try:
        result = subprocess.run(
            ["git", "show", f"{base}:{relative_path}"],
            cwd=str(root),
            text=True,
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, UnicodeDecodeError):
        return None
    return result.stdout


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
    text = str(value).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def normalize_affects_pattern(value: str) -> str:
    text = normalize_changed_file(value)
    return text


def has_glob_syntax(value: str) -> bool:
    return any(char in value for char in "*?[")
