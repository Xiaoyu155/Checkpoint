"""Make the isolation worktree runnable before verification touches it.

A ``git worktree`` carries only tracked files, so gitignored dependency trees
(``node_modules``) never follow it. Verification then fails with
``Cannot find package`` for reasons that have nothing to do with the worker.

Pacer installs them in the worktree rather than linking the source repo's copy:
sharing a mutable ``node_modules`` between the user's project and an autonomous
worker would give up the isolation the product exists to provide. The cost is
small in practice because package managers keep their own global cache — a
262-package ``npm ci`` measured at ~7s on a warm cache.

Only lockfile-driven, deterministic installs are used. Ecosystems that fetch on
demand during their own test command (cargo, go) need nothing here.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from time import monotonic
from typing import Any


# Deterministic install per manager; no lockfile means no bootstrap, because
# a resolving install could silently pick different versions than the user has.
_BOOTSTRAP_COMMANDS: dict[str, tuple[str, ...]] = {
    "npm": ("npm", "ci"),
    "pnpm": ("pnpm", "install", "--frozen-lockfile"),
    "yarn": ("yarn", "install", "--immutable"),
}

_LOCKFILE_MANAGERS: tuple[tuple[str, str], ...] = (
    ("pnpm-lock.yaml", "pnpm"),
    ("package-lock.json", "npm"),
    ("yarn.lock", "yarn"),
)


def detect_bootstrap(worktree: str | Path) -> dict[str, Any]:
    """Decide whether this worktree needs a dependency install, and which one."""

    root = Path(worktree).expanduser().resolve()
    if not (root / "package.json").is_file():
        return {"needed": False, "reason": "not_a_node_project", "manager": "", "lockfile": ""}
    manager = ""
    lockfile = ""
    for name, candidate in _LOCKFILE_MANAGERS:
        if (root / name).is_file():
            manager, lockfile = candidate, name
            break
    if not manager:
        return {"needed": False, "reason": "no_lockfile", "manager": "", "lockfile": ""}
    if (root / "node_modules" / ".package-lock.json").is_file():
        return {"needed": False, "reason": "already_installed", "manager": manager, "lockfile": lockfile}
    return {"needed": True, "reason": "dependencies_missing", "manager": manager, "lockfile": lockfile}


def bootstrap_worktree_dependencies(
    worktree: str | Path,
    *,
    enabled: bool = True,
    timeout_seconds: float = 900.0,
) -> dict[str, Any]:
    """Install dependencies inside ``worktree``.

    Never raises and never blocks the mission: a failed bootstrap leaves the
    existing ``dependencies_missing`` classification to explain the outcome.
    """

    root = Path(worktree).expanduser().resolve()
    detection = detect_bootstrap(root)
    if not enabled:
        return {**detection, "status": "disabled"}
    if not detection["needed"]:
        return {**detection, "status": "skipped"}

    argv = _BOOTSTRAP_COMMANDS.get(str(detection["manager"]))
    if not argv:
        return {**detection, "status": "skipped", "reason": "unsupported_manager"}
    executable = shutil.which(argv[0])
    if not executable:
        return {**detection, "status": "unavailable", "reason": f"{argv[0]}_not_on_path"}

    started = monotonic()
    try:
        completed = subprocess.run(
            [executable, *argv[1:]],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {**detection, "status": "timeout", "command": " ".join(argv), "elapsed_seconds": round(monotonic() - started, 2)}
    except OSError as exc:
        return {**detection, "status": "failed", "command": " ".join(argv), "error": str(exc)[:300]}

    elapsed = round(monotonic() - started, 2)
    if completed.returncode != 0:
        tail = ((completed.stderr or "") + "\n" + (completed.stdout or "")).strip()[-1200:]
        return {
            **detection,
            "status": "failed",
            "command": " ".join(argv),
            "exit_code": completed.returncode,
            "elapsed_seconds": elapsed,
            "output_tail": tail,
        }
    return {
        **detection,
        "status": "installed",
        "command": " ".join(argv),
        "exit_code": 0,
        "elapsed_seconds": elapsed,
    }
