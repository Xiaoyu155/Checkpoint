from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .pacer_verification import PACER_COMMAND_BATCH_KIND
from .security import redact_secret_text, scrub_secrets
from .subprocess_window import isolated_process_group_kwargs, prepare_subprocess_command, terminate_process_tree


DEFAULT_TAIL_CHARS = 2000
MAX_TAIL_CHARS = 8000


def run_compact_command_batch(
    *,
    workspace_root: str | Path,
    repo_root: str | Path,
    steps: list[dict[str, Any]],
    stop_on_failure: bool = False,
    tail_chars: int = DEFAULT_TAIL_CHARS,
    launch_id: str = "",
    batch_kind: str = PACER_COMMAND_BATCH_KIND,
    source_tool: str = "",
    policy_version: int | None = None,
    step_classes: list[str] | None = None,
) -> dict[str, Any]:
    workspace = Path(workspace_root).expanduser().resolve()
    repo = Path(repo_root).expanduser().resolve()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    run_dir = workspace / "pacer_native" / "commands" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    bounded_tail = max(200, min(MAX_TAIL_CHARS, int(tail_chars)))
    records: list[dict[str, Any]] = []
    selected_steps = steps[:20]
    started = time.monotonic()

    for index, raw_step in enumerate(selected_steps, start=1):
        record = _run_compact_step(
            raw_step,
            index=index,
            repo_root=repo,
            run_dir=run_dir,
            tail_chars=bounded_tail,
        )
        records.append(record)
        if stop_on_failure and record["status"] not in {"passed", "not_applicable"}:
            break

    passed = sum(1 for item in records if item["status"] == "passed")
    failed = sum(1 for item in records if item["status"] == "failed")
    timed_out = sum(1 for item in records if item["status"] == "timeout")
    not_applicable = sum(1 for item in records if item["status"] == "not_applicable")
    status = "passed" if records and passed + not_applicable == len(records) and passed > 0 else "failed"
    skipped_steps = [
        str(item.get("name") or f"step-{index}").strip()[:80]
        for index, item in enumerate(selected_steps[len(records) :], start=len(records) + 1)
    ]
    payload = {
        "schema_version": 1,
        "kind": str(batch_kind or PACER_COMMAND_BATCH_KIND),
        "run_id": run_id,
        "launch_id": str(launch_id or ""),
        "status": status,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "requested_steps": len(selected_steps),
        "executed_steps": len(records),
        "skipped_steps": skipped_steps,
        "passed": passed,
        "failed": failed,
        "timed_out": timed_out,
        "not_applicable": not_applicable,
        "records": records,
        "run_dir": str(run_dir),
        "context_policy": {
            "full_output_local": all(item.get("logs_redacted", True) for item in records),
            "returned_tail_chars_per_stream": bounded_tail,
            "old_observations_elided": True,
            "redaction_fail_closed": True,
        },
    }
    if source_tool:
        payload["source_tool"] = str(source_tool)
    if policy_version is not None:
        payload["policy_version"] = int(policy_version)
    if step_classes is not None:
        payload["step_classes"] = [str(value) for value in step_classes[: len(selected_steps)]]
    (run_dir / "summary.json").write_text(
        json.dumps(scrub_secrets(payload), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def _run_compact_step(
    raw_step: dict[str, Any],
    *,
    index: int,
    repo_root: Path,
    run_dir: Path,
    tail_chars: int,
) -> dict[str, Any]:
    name = str(raw_step.get("name") or f"step-{index}").strip()[:80]
    argv = [str(item) for item in raw_step.get("argv") or []]
    if not argv:
        return {"name": name, "status": "failed", "exit_code": None, "reason": "argv_required"}
    cwd = _resolve_step_cwd(repo_root, str(raw_step.get("cwd") or "."))
    inapplicable = _inapplicable_reason(argv, cwd)
    if inapplicable:
        return {
            "name": name,
            "status": "not_applicable",
            "exit_code": None,
            "reason": inapplicable,
            "command": argv,
            "cwd": str(cwd),
            "elapsed_seconds": 0.0,
            "stdout_lines": 0,
            "stderr_lines": 0,
            "stdout_tail": "",
            "stderr_tail": "",
        }
    timeout_seconds = max(1.0, min(3600.0, float(raw_step.get("timeout_seconds") or 600.0)))
    env = os.environ.copy()
    for key, value in (raw_step.get("env") or {}).items():
        if str(key).strip():
            env[str(key)] = str(value)
    stdout_path = run_dir / f"{index:02d}-{_safe_name(name)}.stdout.log"
    stderr_path = run_dir / f"{index:02d}-{_safe_name(name)}.stderr.log"
    command = prepare_subprocess_command(argv)
    started = time.monotonic()
    process: subprocess.Popen[Any] | None = None
    status = "failed"
    exit_code: int | None = None
    reason = ""
    try:
        with stdout_path.open("w", encoding="utf-8", errors="replace") as stdout_handle, stderr_path.open(
            "w", encoding="utf-8", errors="replace"
        ) as stderr_handle:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                **isolated_process_group_kwargs(),
            )
            try:
                exit_code = process.wait(timeout=timeout_seconds)
                status = "passed" if exit_code == 0 else "failed"
            except subprocess.TimeoutExpired:
                terminate_process_tree(process)
                exit_code = process.poll()
                status = "timeout"
                reason = f"timed_out_after_{timeout_seconds:g}s"
    except OSError as exc:
        reason = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        pending_exception = sys.exc_info()[1]
        cleanup_error: BaseException | None = None
        logs_redacted = True
        if process is not None:
            try:
                process_running = process.poll() is None
            except BaseException as exc:  # cleanup must continue during interruption
                cleanup_error = exc
                process_running = True
            if process_running:
                try:
                    terminate_process_tree(process)
                except BaseException as exc:  # cleanup must continue to log redaction
                    cleanup_error = cleanup_error or exc
        for path in (stdout_path, stderr_path):
            try:
                logs_redacted = _redact_log_file(path) and logs_redacted
            except BaseException as exc:
                logs_redacted = False
                cleanup_error = cleanup_error or exc
        if pending_exception is None and cleanup_error is not None:
            raise cleanup_error

    if logs_redacted:
        stdout_tail, stdout_lines = _log_tail(stdout_path, tail_chars)
        stderr_tail, stderr_lines = _log_tail(stderr_path, tail_chars)
        returned_stdout_log = str(stdout_path)
        returned_stderr_log = str(stderr_path)
    else:
        status = "failed"
        reason = "log_redaction_failed"
        stdout_tail, stdout_lines = "", 0
        stderr_tail, stderr_lines = "", 0
        returned_stdout_log = ""
        returned_stderr_log = ""
    return scrub_secrets(
        {
            "name": name,
            "status": status,
            "exit_code": exit_code,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "reason": reason,
            "command": argv,
            "cwd": str(cwd),
            "stdout_lines": stdout_lines,
            "stderr_lines": stderr_lines,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "stdout_log": returned_stdout_log,
            "stderr_log": returned_stderr_log,
            "logs_redacted": logs_redacted,
        }
    )


def _resolve_step_cwd(repo_root: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    resolved = path.resolve() if path.is_absolute() else (repo_root / path).resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError("step cwd must stay inside repo_root") from exc
    if not resolved.is_dir():
        raise ValueError(f"step cwd does not exist: {resolved}")
    return resolved


def _inapplicable_reason(argv: list[str], cwd: Path) -> str:
    normalized = [item.lower() for item in argv]
    if normalized[:3] == ["git", "diff", "--check"] and not _inside_git_worktree(cwd):
        return "not_a_git_worktree"
    return ""


def _inside_git_worktree(cwd: Path) -> bool:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            **isolated_process_group_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and completed.stdout.strip().lower() == "true"


def _log_tail(path: Path, max_chars: int) -> tuple[str, int]:
    try:
        line_count = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                line_count += chunk.count(b"\n")
            size = handle.tell()
            handle.seek(max(0, size - max(max_chars * 4, 4096)))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return "", 0
    if text and not text.endswith("\n"):
        line_count += 1
    return text[-max_chars:], line_count


def _redact_log_file(path: Path) -> bool:
    if not path.exists():
        return True
    temporary = path.with_suffix(path.suffix + ".redacting")
    try:
        with path.open("r", encoding="utf-8", errors="replace") as source, temporary.open(
            "w", encoding="utf-8", errors="replace"
        ) as target:
            for line in source:
                target.write(redact_secret_text(line))
        temporary.replace(path)
        return True
    except OSError:
        # The raw subprocess output must not survive as a readable model artifact.
        try:
            path.write_text("[LOG REDACTION FAILED; OUTPUT REMOVED]\n", encoding="utf-8")
        except OSError:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _safe_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "_.-" else "-" for char in value).strip("-.")
    return cleaned[:60] or "step"
