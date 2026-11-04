"""Delivery check — does the change survive leaving this machine?

Acceptance answers "did the command pass in the worker's directory". That is not
the same question as "can the user get this". On 2026-08-04 a dogfood mission was
verified, its acceptance suite passed 22/22, and the feature still could not run
from a clean checkout: the code imported a symbol that only existed in the
developer's uncommitted edits. Pacer said done; nothing had been delivered.

So this module asks the next question, and it asks it the only way that works:

    check out the committed branch into an empty directory, and run the real
    acceptance command there.

Static analysis cannot replace this. The first version of this check verified
that every imported file was tracked by git — it passed, and the delivery still
failed, because the file was tracked and the *symbol inside it* was not.

Three verdicts, and the third one matters as much as the others:

* ``delivered`` — clean checkout, real command, passed.
* ``not_delivered`` — clean checkout, real command, failed. The work is real but
  the user cannot have it yet; something it depends on is uncommitted.
* ``unchecked`` — the check could not be performed honestly (not a git repo, no
  acceptance command, dependencies could not be provided). Never guess in this
  case: an unrunnable check is not a pass, and it is not a failure either.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from .subprocess_window import hidden_subprocess_kwargs

#: Directories that are legitimately absent from a clean checkout because they
#: are environment, not source. Delivery asks whether the *committed source* is
#: complete, so these are provided to the checkout rather than held against it.
DEPENDENCY_DIRS: tuple[str, ...] = ("node_modules", ".venv", "venv", "vendor")

OUTPUT_TAIL_LIMIT = 8000


def run_delivery_check(
    *,
    repo_root: Path,
    branch: str,
    command: str,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 1800.0,
    dependency_dirs: tuple[str, ...] = DEPENDENCY_DIRS,
    runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Check out ``branch`` clean and run ``command`` there."""
    started = monotonic()
    command = str(command or "").strip()
    if not command:
        return _unchecked("no acceptance command, so delivery cannot be demonstrated")
    if not _is_git_repo(repo_root):
        return _unchecked(f"not a git repository: {repo_root}")
    if not _branch_exists(repo_root, branch):
        return _unchecked(f"branch not found: {branch}")

    holder = Path(tempfile.mkdtemp(prefix="pacer-delivery-"))
    checkout = holder / "checkout"
    try:
        added = _add_worktree(repo_root, checkout, branch)
        if added is not None:
            return _unchecked(f"could not create a clean checkout: {added}")
        linked, missing = _provide_dependencies(repo_root, checkout, dependency_dirs)
        if missing:
            # A command that fails only because its dependencies are absent says
            # nothing about the committed source, so reporting a failure here
            # would be exactly the false red this project keeps removing.
            return _unchecked(
                "could not provide dependencies to the clean checkout: " + ", ".join(missing)
            )
        result = _run(command, cwd=checkout, env=env, timeout_seconds=timeout_seconds, runner=runner)
        exit_code = result.get("exit_code")
        payload = {
            "schema_version": 1,
            "status": "delivered" if exit_code == 0 else "not_delivered",
            "command": command,
            "branch": branch,
            "commit": _rev_parse(repo_root, branch),
            "exit_code": exit_code,
            "output_tail": _tail(str(result.get("output") or "")),
            "dependencies_linked": linked,
            "elapsed_seconds": round(monotonic() - started, 3),
        }
        if payload["status"] == "not_delivered":
            payload["reason"] = (
                "The committed branch does not pass acceptance on its own. The work exists, "
                "but something it needs is still uncommitted, so the user cannot have it yet."
            )
        return payload
    finally:
        _remove_worktree(repo_root, checkout)
        shutil.rmtree(holder, ignore_errors=True)


def _unchecked(reason: str) -> dict[str, Any]:
    return {"schema_version": 1, "status": "unchecked", "reason": reason}


def _is_git_repo(repo_root: Path) -> bool:
    return _git(repo_root, "rev-parse", "--is-inside-work-tree").returncode == 0


def _branch_exists(repo_root: Path, branch: str) -> bool:
    return _git(repo_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0


def _rev_parse(repo_root: Path, ref: str) -> str:
    completed = _git(repo_root, "rev-parse", ref)
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _add_worktree(repo_root: Path, checkout: Path, branch: str) -> str | None:
    """Materialize the committed branch, detached so the branch stays usable."""
    completed = _git(repo_root, "worktree", "add", "--detach", str(checkout), branch)
    if completed.returncode != 0:
        return (completed.stderr or completed.stdout).strip()[:300]
    return None


def _remove_worktree(repo_root: Path, checkout: Path) -> None:
    _git(repo_root, "worktree", "remove", "--force", str(checkout))
    _git(repo_root, "worktree", "prune")


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=120.0,
            check=False,
            # Every git call here is a background probe; hide its console window.
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(args=list(args), returncode=1, stdout="", stderr=str(error))


def _provide_dependencies(
    repo_root: Path,
    checkout: Path,
    dependency_dirs: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    linked: list[str] = []
    missing: list[str] = []
    for name in dependency_dirs:
        source = repo_root / name
        if not source.is_dir():
            continue
        target = checkout / name
        if target.exists():
            continue
        if _link_dir(source, target):
            linked.append(name)
        else:
            missing.append(name)
    return linked, missing


def _link_dir(source: Path, target: Path) -> bool:
    """Link rather than copy — node_modules is routinely hundreds of megabytes."""
    try:
        os.symlink(source, target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if sys.platform == "win32":
        # Junctions need no elevation, unlike symlinks on default Windows.
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target), str(source)],
            capture_output=True,
            text=True,
            check=False,
            # `cmd /c` would otherwise flash a console for a directory junction.
            **hidden_subprocess_kwargs(),
        )
        if completed.returncode == 0:
            return True
    try:
        shutil.copytree(source, target)
        return True
    except (OSError, shutil.Error):
        return False


def _run(
    command: str,
    *,
    cwd: Path,
    env: dict[str, str] | None,
    timeout_seconds: float,
    runner: Callable[..., dict[str, Any]] | None,
) -> dict[str, Any]:
    if runner is not None:
        return runner(command=command, cwd=cwd, env=env, timeout_seconds=timeout_seconds)
    run_env = {**os.environ, **(env or {})}
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            env=run_env,
            check=False,
            # shell=True on Windows is the worst offender for window flashes.
            **hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return {"exit_code": 124, "output": f"delivery check timed out after {timeout_seconds:g}s"}
    except OSError as error:
        return {"exit_code": 125, "output": str(error)}
    return {"exit_code": completed.returncode, "output": (completed.stdout or "") + (completed.stderr or "")}


def _tail(text: str) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= OUTPUT_TAIL_LIMIT:
        return text
    return encoded[-OUTPUT_TAIL_LIMIT:].decode("utf-8", errors="ignore")


def delivery_markdown_lines(check: dict[str, Any] | None) -> list[str]:
    """User-facing section. States plainly what was and was not demonstrated."""
    if not isinstance(check, dict):
        return []
    status = str(check.get("status") or "")
    if status == "delivered":
        return [
            "",
            "### 交付验证",
            "",
            "我把这次改动单独拿出来、在一个干净的目录里重新跑了一遍你的验收命令，通过了。",
            "意思是：这份成果不依赖我这台机器上的任何临时状态，你拿走就能用。",
        ]
    if status == "not_delivered":
        return [
            "",
            "### 交付验证：没通过",
            "",
            "改动本身是真的（验收在工作目录里过了），但把它单独拿到一个干净目录里就跑不起来——",
            "说明它还依赖着某些没有被一起交出去的东西。",
            "",
            "**现在还不能说这件事做完了。** 失败详情：",
            "",
            "```",
            str(check.get("output_tail") or "").strip()[-1500:],
            "```",
        ]
    if status == "unchecked":
        return [
            "",
            "### 交付验证：没能做",
            "",
            f"这次没法验证「拿走就能用」（{check.get('reason') or '原因未知'}）。",
            "所以下面的结论只覆盖「在我这里跑得通」，不覆盖「你拿走也跑得通」。",
        ]
    return []
