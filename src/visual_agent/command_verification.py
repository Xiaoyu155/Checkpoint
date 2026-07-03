"""Command (test/build) verification gate.

Checkpoint's workflow verification proves a *product UI* works, but a brand-new
project has no authored workflows. To make DevPacer usable on any project, this
gate simply runs the project's own test/build command (``pytest``, ``npm test``,
``cargo test`` ...): exit 0 is a pass, anything else is a fail with the command
output kept as failure evidence.

This doubles as the deterministic hook: a worker cannot claim success without the
command passing, so "I think it's fixed" never gets trusted and the repair loop
gets real evidence instead of the model's own words.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

# The command gate is only trustworthy while the tests themselves are what the
# user wrote. A worker that edits tests can turn any failure green, so test
# changes are detected mechanically — the prompt-level "do not weaken tests"
# instruction is a request, this is the enforcement.
_TEST_DIR_NAMES = {"tests", "test", "__tests__", "spec", "specs", "testing"}
_TEST_BASENAME = re.compile(r"(^test_.+|.+_test\.[^.]+$|.+\.test\.[^.]+$|.+\.spec\.[^.]+$|^conftest\.py$)", re.IGNORECASE)


def is_test_path(path: str) -> bool:
    normalized = str(path).replace("\\", "/").strip()
    if not normalized:
        return False
    parts = normalized.split("/")
    if any(part.lower() in _TEST_DIR_NAMES for part in parts[:-1]):
        return True
    return bool(_TEST_BASENAME.match(parts[-1]))


def changed_test_files(*, repo_root: str | Path, base_ref: str | None = None) -> list[str]:
    """List test files the worker touched (tracked diff against base + untracked).

    Degrades open on git errors: a broken git setup should surface as the test
    command failing, not as a phantom tampering verdict.
    """
    root = Path(repo_root).expanduser().resolve()
    touched: set[str] = set()
    diff_args = ["git", "-C", str(root), "diff", "--name-only"]
    if str(base_ref or "").strip():
        diff_args.append(str(base_ref).strip())
    for args in (diff_args, ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"]):
        try:
            completed = subprocess.run(args, capture_output=True, text=True, timeout=30.0, encoding="utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired):
            return []
        if completed.returncode != 0:
            return []
        touched.update(line.strip() for line in completed.stdout.splitlines() if line.strip())
    return sorted(path for path in touched if is_test_path(path))


def tamper_repair_brief(tampered: list[str], *, base_ref: str | None = None) -> dict[str, Any]:
    base = str(base_ref or "").strip() or "the branch base"
    files = "\n".join(f"- {item}" for item in tampered)
    return {
        "status": "found",
        "source": "test_tampering",
        "tampered_test_files": tampered,
        "message": f"Worker modified {len(tampered)} test file(s); the command gate refuses to run against edited tests.",
        "repair_prompt": (
            "You modified test files, which the verification gate forbids: the tests are the"
            " acceptance contract and must stay exactly as the user wrote them.\n"
            f"Revert these files to {base} (e.g. `git checkout {base} -- <path>`):\n{files}\n"
            "Then fix the production code so the original tests pass."
        ),
    }


def run_command_verification(
    *,
    command: str,
    repo_root: str | Path,
    timeout_seconds: float = 900.0,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    root = Path(repo_root).expanduser().resolve()
    cmd = str(command or "").strip()
    if not cmd:
        return {"verdict": "skipped", "command": "", "reason": "no test command provided"}
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(root),
            shell=True,  # let users pass a natural command line (pytest -q, npm test)
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=({**_os_environ(), **env} if env else None),
        )
        exit_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        return {
            "verdict": "fail",
            "command": cmd,
            "exit_code": 124,
            "output_tail": (_decode(exc.stdout) + "\n" + _decode(exc.stderr))[-4000:],
            "reason": f"test command timed out after {timeout_seconds:.0f}s",
        }
    except OSError as exc:
        return {"verdict": "fail", "command": cmd, "exit_code": -1, "output_tail": str(exc)[:2000], "reason": "could not launch test command"}

    combined = (stdout + ("\n" if stdout and stderr else "") + stderr).strip()
    return {
        "verdict": "pass" if exit_code == 0 else "fail",
        "command": cmd,
        "exit_code": exit_code,
        "output_tail": combined[-4000:],
    }


def command_repair_brief(result: dict[str, Any]) -> dict[str, Any]:
    """Turn a failed command result into a repair brief for the worker."""
    output = str(result.get("output_tail") or "")
    return {
        "status": "found",
        "source": "test_command",
        "command": result.get("command"),
        "exit_code": result.get("exit_code"),
        "message": f"Test command failed (exit {result.get('exit_code')}): {result.get('command')}",
        "repair_prompt": (
            f"The verification command `{result.get('command')}` failed with exit code "
            f"{result.get('exit_code')}. Fix the code so it passes. Do not change the test "
            f"command or weaken the tests. Command output (tail):\n\n{output[-2500:]}"
        ),
    }


def _os_environ() -> dict[str, str]:
    import os

    return dict(os.environ)


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
