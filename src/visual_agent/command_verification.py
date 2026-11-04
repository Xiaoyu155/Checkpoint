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
import os
import hashlib
import subprocess
from pathlib import Path
from time import monotonic
from typing import Any

from .process_guard import guarded_run
from .security import redact_secret_text
from .subprocess_window import hidden_subprocess_kwargs
from .verification_profiles import (conditional_test_command_short_circuit, validate_safe_verification_argv,
                                    validate_safe_verification_command, parse_verification_command_argv)


def verification_command_argv(command: str, repo_root: str | Path) -> list[str]:
    safe, reason = validate_safe_verification_command(command, repo_root=repo_root)
    if not safe:
        raise ValueError(reason)
    return parse_verification_command_argv(command)

# The command gate is only trustworthy while the tests themselves are what the
# user wrote. A worker that edits tests can turn any failure green, so test
# changes are detected mechanically — the prompt-level "do not weaken tests"
# instruction is a request, this is the enforcement.
_TEST_DIR_NAMES = {"tests", "test", "__tests__", "spec", "specs", "testing", "eval", "evaluation", "evaluations", "acceptance", "regression_tests"}
_TEST_BASENAME = re.compile(r"(^test_.+|^(?:test|spec)\.[^.]+$|.+_test\.[^.]+$|.+\.test\.[^.]+$|.+\.spec\.[^.]+$|^conftest\.py$)", re.IGNORECASE)
NON_REPAIRABLE_COMMAND_FAILURE_KINDS = frozenset(
    {
        "test_command_invalid",
        "command_launch_error",
        "command_timeout",
        "verification_environment_missing",
        "pytest_not_importable",
        "conditional_test_command_short_circuit",
    }
)

_ENVIRONMENT_MISSING_MARKERS = (
    "qwen_api_key missing",
    "ai_acceptance_use_qwen is not 1",
    "external ai review skipped while ai required",
    "external ai judge missing",
    "ai知识审查: skipped",
    "ai judge returned no json object",
)

_DEPENDENCY_MISSING_MARKERS = (
    "err_module_not_found",
    "cannot find package",
    "cannot find module",
    "modulenotfounderror: no module named",
    "importerror: no module named",
)


def verification_env_from_required_names(names: list[str] | tuple[str, ...] | None) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in names or ():
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        entries.append({"kind": "env_var", "name": name})
    return entries


def normalize_verification_env(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        if kind == "env_var":
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            key = (kind, name)
            if key in seen:
                continue
            seen.add(key)
            normalized.append({"kind": kind, "name": name})
        elif kind == "marker":
            pattern = str(item.get("pattern") or "").strip()
            if not pattern:
                continue
            key = (kind, pattern)
            if key in seen:
                continue
            seen.add(key)
            normalized.append({"kind": kind, "pattern": pattern})
    return normalized


def is_test_path(path: str) -> bool:
    normalized = str(path).replace("\\", "/").strip()
    if not normalized:
        return False
    parts = normalized.split("/")
    if any(part.lower() in {"__pycache__", ".git", ".pacer", ".pytest_cache", ".pytest_cache_local", ".ruff_cache"} for part in parts):
        return False
    if Path(parts[-1]).suffix.lower() in {".pyc", ".pyo", ".class", ".o", ".obj"}:
        return False
    if any(part.lower() in _TEST_DIR_NAMES for part in parts[:-1]):
        return True
    return bool(_TEST_BASENAME.match(parts[-1]))


def snapshot_verification_files(*, repo_root: str | Path, command: str = "", include_tests: bool = True) -> dict[str, str | None]:
    """Capture this task's starting bytes, including user-owned uncommitted tests."""
    root = Path(repo_root).resolve()
    paths: set[str] = set(acceptance_chain_files(root, command)) if command else set()
    if include_tests:
        try:
            result = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                                    capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Cannot establish the task's test baseline") from exc
        if result.returncode == 0:
            paths.update(p for p in result.stdout.decode("utf-8").split("\0") if p and is_test_path(p))
        else:
            excluded = {".git", ".pacer", ".agent-workspace", ".checkpoint-worktrees", ".venv", "venv", "node_modules", "__pycache__"}
            for folder, dirs, files in os.walk(root):
                dirs[:] = [name for name in dirs if name not in excluded]
                for name in files:
                    rel = (Path(folder) / name).relative_to(root).as_posix()
                    if is_test_path(rel):
                        paths.add(rel)
    snapshot: dict[str, str | None] = {}
    for rel in sorted(paths):
        path = root / rel
        if path.resolve().is_relative_to(root):
            snapshot[rel] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return snapshot


def changed_snapshot_files(baseline: dict[str, str | None], current: dict[str, str | None]) -> list[str]:
    return sorted(path for path in baseline.keys() | current.keys() if baseline.get(path) != current.get(path))


def changed_test_files(*, repo_root: str | Path, base_ref: str | None = None) -> list[str]:
    """List test files the worker touched (tracked diff against base + untracked).

    Degrades open on git errors: a broken git setup should surface as the test
    command failing, not as a phantom tampering verdict.
    """
    root = Path(repo_root).expanduser().resolve()
    touched: set[str] = set()
    diff_args = ["git", "-C", str(root), "-c", "core.quotePath=false", "diff", "--name-only"]
    if str(base_ref or "").strip():
        diff_args.append(str(base_ref).strip())
    for args in (diff_args, ["git", "-C", str(root), "-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard"]):
        try:
            completed = subprocess.run(args, capture_output=True, text=True, timeout=30.0, encoding="utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired):
            return []
        if completed.returncode != 0:
            return []
        touched.update(line.strip() for line in completed.stdout.splitlines() if line.strip())
    return sorted(path for path in touched if is_test_path(path))


def acceptance_chain_files(repo_root: str | Path, command: str) -> list[str]:
    root = Path(repo_root).expanduser().resolve()
    text = str(command or "").lower()
    files: list[str] = []
    if any(marker in text for marker in ("npm ", "pnpm ", "yarn ")):
        files.append("package.json")
    if "pytest" in text or "python -m pytest" in text:
        for name in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "conftest.py"):
            if (root / name).exists():
                files.append(name)
    if "make " in text:
        files.append("Makefile")
    if "cargo " in text:
        files.append("Cargo.toml")
    deduped: list[str] = []
    for item in files:
        normalized = item.replace("\\", "/")
        if normalized not in deduped:
            deduped.append(normalized)
    return deduped


def changed_acceptance_chain_files(
    *,
    repo_root: str | Path,
    command: str,
    base_ref: str | None = None,
) -> list[str]:
    chain = set(acceptance_chain_files(repo_root, command))
    if not chain:
        return []
    root = Path(repo_root).expanduser().resolve()
    touched: set[str] = set()
    diff_args = ["git", "-C", str(root), "-c", "core.quotePath=false", "diff", "--name-only"]
    if str(base_ref or "").strip():
        diff_args.append(str(base_ref).strip())
    for args in (diff_args, ["git", "-C", str(root), "-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard"]):
        try:
            completed = subprocess.run(args, capture_output=True, text=True, timeout=30.0, encoding="utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired):
            return []
        if completed.returncode != 0:
            return []
        touched.update(line.replace("\\", "/").strip() for line in completed.stdout.splitlines() if line.strip())
    return sorted(path for path in touched if path in chain)


def acceptance_chain_repair_brief(tampered: list[str], *, base_ref: str | None = None) -> dict[str, Any]:
    base = str(base_ref or "").strip() or "the branch base"
    files = "\n".join(f"- {item}" for item in tampered)
    return {
        "status": "found",
        "source": "acceptance_chain_tampering",
        "tampered_acceptance_chain_files": tampered,
        "message": f"Worker modified {len(tampered)} acceptance-chain file(s); the command gate refuses to run.",
        "repair_prompt": (
            "You modified files that define the acceptance command, so the verification gate cannot trust the result.\n"
            f"Revert these files to {base} (e.g. `git checkout {base} -- <path>`):\n{files}\n"
            "Then fix the production code without changing the test command, package script, or test runner config."
        ),
    }


def tamper_repair_brief(tampered: list[str], *, base_ref: str | None = None) -> dict[str, Any]:
    files = "\n".join(f"- {item}" for item in tampered)
    return {
        "status": "found",
        "source": "test_tampering",
        "tampered_test_files": tampered,
        "message": f"Worker changed {len(tampered)} protected acceptance input(s); the command gate refuses to run.",
        "repair_prompt": (
            "The verification gate detected a change to protected acceptance inputs or a newly added test escape call. "
            "Do not edit, delete, or run repository rollback commands on tests or acceptance configuration. "
            "TamperGuard restores protected bytes from its captured snapshot when available. "
            "Fix product code only, then rerun the unchanged acceptance suite.\n"
            f"Detected inputs:\n{files}"
        ),
    }


def run_command_verification(
    *,
    command: str | list[str] | tuple[str, ...],
    repo_root: str | Path,
    timeout_seconds: float = 900.0,
    env: dict[str, str] | None = None,
    verification_env: list[dict[str, Any]] | None = None,
    timeout_reason: str | None = None,
    base_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    root = Path(repo_root).expanduser().resolve()
    if not isinstance(command, (list, tuple)) or any(not isinstance(item, str) for item in command):
        return {"verdict": "fail", "command": "", "exit_code": None, "reason": "verification requires an argv list of strings", "failure_kind": "test_command_invalid"}
    argv_command = True
    argv = list(command)
    cmd = subprocess.list2cmdline(argv)
    prepared_command = argv
    if not cmd:
        return {"verdict": "skipped", "command": "", "exit_code": None, "reason": "no test command provided"}
    if argv_command:
        is_safe, safety_reason = validate_safe_verification_argv(argv, repo_root=root)
        if not is_safe:
            return {
                "verdict": "fail",
                "command": cmd,
                "exit_code": None,
                "output_tail": "",
                "reason": f"verification argv rejected by safety policy: {safety_reason}",
                "failure_kind": "test_command_invalid",
                "classification_confidence": "definitive",
            }
    short_circuit = conditional_test_command_short_circuit(root, cmd)
    if short_circuit:
        return {
            "verdict": "fail",
            "command": cmd,
            "exit_code": None,
            "output_tail": "",
            "reason": str(short_circuit.get("message") or "conditional test command would skip acceptance"),
            "failure_kind": str(short_circuit.get("reason") or "conditional_test_command_short_circuit"),
            "classification_confidence": "definitive",
            "short_circuit_marker": str(short_circuit.get("marker") or ""),
        }
    verification_env = normalize_verification_env(verification_env)
    missing_env = _missing_declared_env_vars(verification_env)
    if missing_env:
        return {
            "verdict": "fail",
            "command": cmd,
            "exit_code": None,
            "output_tail": "",
            "reason": "missing declared verification environment variable(s): " + ", ".join(missing_env),
            "failure_kind": "verification_environment_missing",
            "classification_confidence": "definitive",
            "missing_env_vars": missing_env,
        }
    child_env = {**os.environ, **(env or {})}
    child_env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    baseline = snapshot_verification_files(repo_root=root, command=cmd)
    started = monotonic()
    try:
        completed = guarded_run(
            prepared_command,
            cwd=str(root),
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=child_env,
            **hidden_subprocess_kwargs(),
        )
        exit_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        elapsed_seconds = max(0.0, monotonic() - started)
        effective_timeout = float(timeout_seconds)
        reason = str(timeout_reason or "").strip()
        timeout_output = redact_secret_text(_decode(exc.stdout) + "\n" + _decode(exc.stderr))
        return {
            "verdict": "fail",
            "command": cmd,
            "exit_code": 124,
            "output_tail": timeout_output[-4000:],
            "raw_output_tail": timeout_output[-32768:],
            "reason": f"test command timed out after {effective_timeout:.0f}s",
            "failure_kind": "command_timeout",
            "classification_confidence": "definitive",
            "timeout_seconds": effective_timeout,
            "base_timeout_seconds": float(base_timeout_seconds) if base_timeout_seconds is not None else effective_timeout,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "timeout_reason": reason,
            "dependency_bootstrap": reason in {"missing_node_modules", "missing_python_venv"},
            "suggested_timeout_seconds": _suggested_timeout_seconds(effective_timeout, elapsed_seconds),
        }
    except OSError as exc:
        launch_output = redact_secret_text(str(exc))
        return {
            "verdict": "fail",
            "command": cmd,
            "exit_code": -1,
            "output_tail": launch_output[:2000],
            "raw_output_tail": launch_output[-32768:],
            "reason": "could not launch test command",
            "failure_kind": "command_launch_error",
            "classification_confidence": "definitive",
        }

    changed = changed_snapshot_files(baseline, snapshot_verification_files(repo_root=root, command=cmd))
    if changed:
        return {"verdict": "fail", "command": cmd, "exit_code": exit_code,
                "failure_kind": "test_command_invalid", "reason": "Acceptance files changed during verification: " + ", ".join(changed), "output_tail": ""}
    combined = (stdout + ("\n" if stdout and stderr else "") + stderr).strip()
    if exit_code != 0:
        combined = _append_referenced_log_tails(combined, root)
    combined = redact_secret_text(combined)
    classification = (
        {"failure_kind": "", "classification_confidence": ""}
        if exit_code == 0
        else classify_command_failure_detail(
            exit_code=exit_code,
            output=combined,
            verification_env=verification_env,
        )
    )
    result = {
        "verdict": "pass" if exit_code == 0 else "fail",
        "command": cmd,
        "exit_code": exit_code,
        "output_tail": combined[-4000:],
        "raw_output_tail": combined[-32768:],
        "failure_kind": classification.get("failure_kind") or "",
    }
    if classification.get("classification_confidence"):
        result["classification_confidence"] = classification["classification_confidence"]
    if classification.get("matched_marker"):
        result["matched_marker"] = classification["matched_marker"]
    # Name the shell that ran the gate: "it passed" means little without it, and
    # a wrong-shell failure is unreadable without saying so out loud.
    from .shell_dialect import prepare_shell_invocation, shell_mismatch_hint

    if argv_command:
        shell_used = "argv"
        result["shell_used"] = shell_used
        result["shell_dialect"] = "argv"
    else:
        invocation = prepare_shell_invocation(cmd)
        shell_used = invocation["shell_used"]
        result["shell_used"] = shell_used
        result["shell_dialect"] = invocation["dialect"]
        if invocation["warnings"]:
            result["shell_warnings"] = invocation["warnings"]
    if exit_code != 0 and not argv_command:
        hint = shell_mismatch_hint(command=cmd, shell_used=shell_used, output=combined)
        if hint:
            result["shell_hint"] = hint
            result["failure_kind"] = "test_command_wrong_shell"
            result["classification_confidence"] = "definitive"
    return result


def _strip_balanced_quotes(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1].strip()
    return text


def _is_windows() -> bool:
    return os.name == "nt"


def command_repair_brief(result: dict[str, Any]) -> dict[str, Any]:
    """Turn a failed command result into a repair brief for the worker."""
    output = str(result.get("output_tail") or "")
    failure_kind = str(result.get("failure_kind") or "command_failed")
    classification_confidence = str(result.get("classification_confidence") or "")
    confidence_note = (
        " This was a heuristic classification; ask the operator to confirm the environment before taking action."
        if classification_confidence == "heuristic"
        else ""
    )
    if failure_kind == "verification_environment_missing":
        repair_prompt = (
            f"The verification command `{result.get('command')}` failed because its required "
            f"verification environment is missing ({failure_kind}, exit {result.get('exit_code')}). "
            "Do not change product code, tests, eval scripts, or acceptance thresholds to work around this. "
            "Report the missing external service/key setup and ask the operator to configure the verification "
            f"environment before retrying.{confidence_note} Command output (tail):\n\n{output[-2500:]}"
        )
    elif failure_kind == "command_timeout":
        timeout_text = _format_seconds(result.get("timeout_seconds"))
        elapsed_text = _format_seconds(result.get("elapsed_seconds"))
        suggested = _format_seconds(result.get("suggested_timeout_seconds"))
        dependency_text = "yes" if bool(result.get("dependency_bootstrap")) else "no"
        reason = str(result.get("timeout_reason") or "base_timeout")
        repair_prompt = (
            f"The verification command `{result.get('command')}` timed out "
            f"({failure_kind}, exit {result.get('exit_code')}). Actual elapsed time: {elapsed_text}; "
            f"effective timeout: {timeout_text}; first dependency install likely: {dependency_text} "
            f"({reason}). Do not guess at code changes from a timeout alone. Report the timeout and "
            f"suggest retrying with `--timeout-seconds {suggested}` after confirming dependencies are installed. "
            f"Command output (tail):\n\n{output[-2500:]}"
        )
    elif failure_kind in NON_REPAIRABLE_COMMAND_FAILURE_KINDS:
        repair_prompt = (
            f"The verification command `{result.get('command')}` could not be trusted "
            f"({failure_kind}, exit {result.get('exit_code')}). Do not guess at code changes. "
            "Report the command/environment issue and suggest the exact corrected test command or setup step. "
            f"Command output (tail):\n\n{output[-2500:]}"
        )
    else:
        repair_prompt = (
            f"The verification command `{result.get('command')}` failed with exit code "
            f"{result.get('exit_code')}. Fix the code so it passes. Do not change the test "
            f"command or weaken the tests. Command output (tail):\n\n{output[-2500:]}"
        )
    brief = {
        "status": "found",
        "source": "test_command",
        "command": result.get("command"),
        "exit_code": result.get("exit_code"),
        "failure_kind": failure_kind,
        "message": f"Test command failed (exit {result.get('exit_code')}): {result.get('command')}",
        "repair_prompt": repair_prompt,
    }
    if classification_confidence:
        brief["classification_confidence"] = classification_confidence
    for key in ("timeout_seconds", "elapsed_seconds", "dependency_bootstrap", "suggested_timeout_seconds", "timeout_reason"):
        if key in result:
            brief[key] = result[key]
    return brief


def _suggested_timeout_seconds(timeout_seconds: float, elapsed_seconds: float) -> float:
    target = max(float(timeout_seconds), float(elapsed_seconds)) + 600.0
    return float(int((target + 59.0) // 60.0) * 60)


def _format_seconds(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "unknown"
    return f"{number:.0f}s"


def classify_command_failure(*, exit_code: int, output: str) -> str:
    return classify_command_failure_detail(exit_code=exit_code, output=output).get("failure_kind", "command_failed")


def classify_command_failure_detail(
    *,
    exit_code: int,
    output: str,
    verification_env: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    text = str(output or "").lower()
    if int(exit_code) in {127, 9009}:
        return {"failure_kind": "test_command_invalid", "classification_confidence": "definitive"}
    shell_error_markers = (
        "is not recognized as an internal or external command",
        "not recognized as the name of a cmdlet",
        "missing script:",
    )
    head = "\n".join(text.splitlines()[:10])
    if any(marker in head for marker in shell_error_markers):
        return {"failure_kind": "test_command_invalid", "classification_confidence": "definitive"}
    declared_marker = _declared_marker_match(str(output or ""), normalize_verification_env(verification_env))
    if declared_marker:
        return {
            "failure_kind": "verification_environment_missing",
            "classification_confidence": "definitive",
            "matched_marker": declared_marker,
        }
    if "no module named pytest" in text or "modulenotfounderror: no module named 'pytest'" in text:
        return {
            "failure_kind": "pytest_not_importable",
            "classification_confidence": "definitive",
            "matched_marker": "no module named pytest",
        }
    for marker in _ENVIRONMENT_MISSING_MARKERS:
        if marker in text:
            return {
                "failure_kind": "verification_environment_missing",
                "classification_confidence": "heuristic",
                "matched_marker": marker,
            }
    # Missing dependencies are an environment fact, not a coding failure. Pacer
    # verifies inside an isolation worktree, which by design holds only tracked
    # files — node_modules and .venv are gitignored and do not follow it there.
    for marker in _DEPENDENCY_MISSING_MARKERS:
        if marker in text:
            return {
                "failure_kind": "dependencies_missing",
                "classification_confidence": "heuristic",
                "matched_marker": marker,
            }
    return {"failure_kind": "command_failed", "classification_confidence": "definitive"}


def _missing_declared_env_vars(verification_env: list[dict[str, str]]) -> list[str]:
    missing: list[str] = []
    environ = os.environ
    for item in verification_env:
        if item.get("kind") != "env_var":
            continue
        name = str(item.get("name") or "").strip()
        if name and not str(environ.get(name) or "").strip():
            missing.append(name)
    return missing


def _declared_marker_match(output: str, verification_env: list[dict[str, str]]) -> str:
    text = str(output or "")
    lowered = text.lower()
    for item in verification_env:
        if item.get("kind") != "marker":
            continue
        pattern = str(item.get("pattern") or "").strip()
        if not pattern:
            continue
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return pattern
        except re.error:
            if pattern.lower() in lowered:
                return pattern
    return ""


def _os_environ() -> dict[str, str]:
    import os

    return dict(os.environ)


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _append_referenced_log_tails(output: str, repo_root: Path) -> str:
    text = str(output or "")
    if not text:
        return text
    chunks = [text]
    for rel in _referenced_log_paths(text)[:5]:
        tail = _safe_repo_log_tail(repo_root, rel)
        if tail:
            chunks.append(f"\n--- referenced log: {rel} ---\n{tail}")
    return "\n".join(chunks)


def _referenced_log_paths(text: str) -> list[str]:
    seen: set[str] = set()
    paths: list[str] = []
    for match in re.finditer(r"(?P<path>[^\s()]+\.log)", str(text or "")):
        raw = match.group("path").strip().strip("`'\"")
        normalized = raw.replace("\\", "/").lstrip("/")
        if not normalized or normalized in seen or "://" in normalized:
            continue
        seen.add(normalized)
        paths.append(normalized)
    return paths


def _safe_repo_log_tail(repo_root: Path, relative_path: str, *, max_chars: int = 2500) -> str:
    normalized = str(relative_path or "").replace("\\", "/").strip().lstrip("/")
    if not normalized or ".." in normalized.split("/"):
        return ""
    path = (repo_root / normalized).resolve()
    try:
        path.relative_to(repo_root.resolve())
    except ValueError:
        return ""
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-max_chars:]
    except OSError:
        return ""
