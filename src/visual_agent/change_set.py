from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .subprocess_window import hidden_subprocess_kwargs


CHANGE_SET_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ChangeFact:
    path: str
    state: str
    artifact_role: str
    renamed_from: str = ""
    renamed_to: str = ""

    def to_dict(self) -> dict[str, str]:
        payload = {
            "path": self.path,
            "state": self.state,
            "artifact_role": self.artifact_role,
        }
        if self.renamed_from:
            payload["renamed_from"] = self.renamed_from
        if self.renamed_to:
            payload["renamed_to"] = self.renamed_to
        return payload


@dataclass(frozen=True)
class ChangeSet:
    repo_root: Path
    base_ref: str
    base_commit: str
    complete: bool
    changes: tuple[ChangeFact, ...]
    errors: tuple[str, ...]
    digest: str

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CHANGE_SET_SCHEMA_VERSION,
            "base_ref": self.base_ref,
            "base_commit": self.base_commit,
            "complete": self.complete,
            "changes": [item.to_dict() for item in self.changes],
            "errors": list(self.errors),
            "digest": self.digest,
        }


def collect_repository_change_set(
    *,
    repo_root: str | Path,
    base_ref: str = "HEAD",
) -> ChangeSet:
    """Collect one bounded file-fact projection from Git's stable machine protocol.

    Baseline-aware task attribution remains owned by ``task_review``. This
    adapter creates the same trusted baseline shape for a caller-selected Git
    ref so completion review, workflow selection, Chief scope checks, and UI
    projections consume identical path/state semantics.
    """
    from .task_review import SOURCE_BASELINE_SCHEMA_VERSION, derive_task_source_changes

    root = Path(repo_root).expanduser().resolve()
    clean_ref = str(base_ref or "HEAD").strip() or "HEAD"
    base_commit = _git_output(root, ["rev-parse", "--verify", f"{clean_ref}^{{commit}}"])
    prefix = _git_output(root, ["rev-parse", "--show-prefix"])
    if base_commit is None and clean_ref == "HEAD":
        inside = _git_output(root, ["rev-parse", "--is-inside-work-tree"])
        if inside is not None and inside.strip() == "true":
            base_commit = ""
    if base_commit is None or prefix is None:
        return _build_change_set(
            repo_root=root,
            base_ref=clean_ref,
            base_commit="",
            complete=False,
            changes=(),
            errors=("git_base_unavailable",),
        )
    baseline = {
        "schema_version": SOURCE_BASELINE_SCHEMA_VERSION,
        "kind": "git",
        "repo_root": str(root),
        "captured_at": "",
        "complete": True,
        "head": base_commit.strip(),
        "git_prefix": prefix.strip().replace("\\", "/"),
        "initial_changes": [],
        "entries": {},
        "file_count": 0,
    }
    payload = derive_task_source_changes(repo_root=root, source_baseline=baseline)
    raw_changes = payload.get("changes") if isinstance(payload.get("changes"), list) else []
    facts = tuple(
        ChangeFact(
            path=str(item.get("path") or ""),
            state=str(item.get("state") or ""),
            artifact_role=str(item.get("artifact_role") or ""),
            renamed_from=str(item.get("renamed_from") or ""),
            renamed_to=str(item.get("renamed_to") or ""),
        )
        for item in raw_changes
        if isinstance(item, dict) and str(item.get("path") or "")
    )
    errors = tuple(str(item) for item in payload.get("errors") or [] if str(item))
    return _build_change_set(
        repo_root=root,
        base_ref=clean_ref,
        base_commit=base_commit.strip(),
        complete=bool(payload.get("complete")) and not errors,
        changes=facts,
        errors=errors,
    )


def _build_change_set(
    *,
    repo_root: Path,
    base_ref: str,
    base_commit: str,
    complete: bool,
    changes: tuple[ChangeFact, ...],
    errors: tuple[str, ...],
) -> ChangeSet:
    canonical = json.dumps(
        {
            "schema_version": CHANGE_SET_SCHEMA_VERSION,
            "base_ref": base_ref,
            "base_commit": base_commit,
            "complete": bool(complete),
            "changes": [item.to_dict() for item in changes],
            "errors": list(errors),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ChangeSet(
        repo_root=repo_root,
        base_ref=base_ref,
        base_commit=base_commit,
        complete=bool(complete),
        changes=changes,
        errors=errors,
        digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _git_output(repo_root: Path, args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None
