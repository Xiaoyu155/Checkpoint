from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Iterable

from .context_ingestion import CodeChange


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


def collect_code_changes(
    *,
    base: str = "HEAD",
    cwd: Path | None = None,
    include_untracked: bool = True,
    max_file_bytes: int = 200_000,
) -> tuple[CodeChange, ...]:
    root = cwd or Path.cwd()
    status_by_path = git_diff_name_status(base=base, cwd=root)
    if include_untracked:
        for path in git_untracked_files(cwd=root):
            status_by_path.setdefault(path, "A")
    changes: list[CodeChange] = []
    for relative_path in sorted(status_by_path):
        path = root / relative_path
        status = status_by_path[relative_path]
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
        if status.startswith("D") or (before is not None and not exists):
            change_type = "deleted"
        elif status.startswith("A") or before is None:
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
    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", base],
            cwd=str(root),
            text=True,
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {}
    status_by_path: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0].strip()
        # Rename/copy entries include old and new path; use the new path as the current file.
        path = normalize_changed_file(parts[-1])
        if path:
            status_by_path[path] = status
    return status_by_path


def git_untracked_files(*, cwd: Path | None = None) -> list[str]:
    root = cwd or Path.cwd()
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
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
