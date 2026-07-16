from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PACER_COMMAND_BATCH_KIND = "pacer_command_batch"
PACER_VERIFICATION_BATCH_KIND = "pacer_verification_batch"
PACER_VERIFICATION_SOURCE_TOOL = "run_pacer_verification"
PACER_VERIFICATION_POLICY_VERSION = 1
SUBSTANTIVE_STEP_CLASSES = frozenset({"test", "build", "analyze", "compile"})
ACCEPTANCE_STEP_CLASSES = frozenset({"test", "build", "analyze"})
_MAX_TRUSTED_RECEIPTS = 256
_TRUST_SECRET = secrets.token_bytes(32)
_TRUST_LOCK = threading.Lock()
_TRUSTED_RECEIPTS: OrderedDict[tuple[str, str, str], tuple[str, str]] = OrderedDict()


@dataclass(frozen=True, slots=True)
class VerificationBatchValidation:
    valid: bool
    errors: tuple[str, ...]
    step_classes: tuple[str, ...]
    substantive_step_classes: tuple[str, ...]


def classify_verification_step(argv: list[str]) -> str:
    if not argv:
        return "unknown"
    executable = Path(argv[0]).name.lower()
    args = [value.lower() for value in argv[1:]]
    if len(args) >= 2 and args[0] == "-m":
        if args[:3] == ["-m", "visual_agent.cli", "codex-check"]:
            return "analyze"
        return {
            "pytest": "test",
            "unittest": "test",
            "compileall": "compile",
            "ruff": "analyze",
            "mypy": "analyze",
        }.get(args[1], "unknown")
    if executable in {"pytest", "pytest.exe"}:
        return "test"
    if executable in {"ruff", "ruff.exe", "mypy", "mypy.exe"}:
        return "analyze"
    if executable in {"git", "git.exe"}:
        return "inspect"
    if executable in {"npm", "npm.cmd", "pnpm", "pnpm.cmd", "yarn", "yarn.cmd"}:
        if args[:1] == ["test"] or args[:2] == ["run", "test"]:
            return "test"
        if args[:2] == ["run", "build"]:
            return "build"
        if len(args) >= 2 and args[0] == "run" and args[1] in {"check", "lint"}:
            return "analyze"
        return "unknown"
    if executable in {"dart", "dart.exe", "flutter", "flutter.bat"}:
        if args[:1] == ["test"]:
            return "test"
        if args[:1] == ["analyze"]:
            return "analyze"
        return "unknown"
    if executable in {"go", "go.exe", "cargo", "cargo.exe"} and args[:1] == ["test"]:
        return "test"
    return "unknown"


def is_noop_verification_step(argv: list[str]) -> bool:
    if not argv:
        return True
    lowered = [value.lower() for value in argv[1:]]
    if any(value in {"--help", "-h", "--version"} for value in lowered):
        return True
    executable = Path(argv[0]).name.lower()
    is_pytest = executable in {"pytest", "pytest.exe"} or (len(lowered) >= 2 and lowered[:2] == ["-m", "pytest"])
    if not is_pytest:
        return False
    pytest_noops = {
        "--co",
        "--collect-only",
        "--cache-show",
        "--fixtures",
        "--fixtures-per-test",
        "--markers",
        "--setup-plan",
        "--trace-config",
    }
    return any(value in pytest_noops or value.startswith("--collect-only=") for value in lowered)


def validate_pacer_verification_batch(
    payload: dict[str, Any],
    *,
    workspace_root: str | Path | None = None,
    trusted_receipt: str = "",
    expected_launch_id: str | None = None,
    expected_run_id: str = "",
    allow_compile_only: bool = False,
) -> VerificationBatchValidation:
    validation = audit_pacer_verification_batch(
        payload,
        expected_launch_id=expected_launch_id,
        expected_run_id=expected_run_id,
        allow_compile_only=allow_compile_only,
    )
    if not isinstance(payload, dict):
        return validation
    errors = list(validation.errors)
    errors.extend(
        trusted_verification_receipt_errors(
            payload,
            workspace_root=workspace_root,
            trusted_receipt=trusted_receipt,
        )
    )
    unique_errors = tuple(dict.fromkeys(errors))
    return VerificationBatchValidation(
        valid=not unique_errors,
        errors=unique_errors,
        step_classes=validation.step_classes,
        substantive_step_classes=validation.substantive_step_classes,
    )


def audit_pacer_verification_batch(
    payload: dict[str, Any],
    *,
    expected_launch_id: str | None = None,
    expected_run_id: str = "",
    allow_compile_only: bool = False,
) -> VerificationBatchValidation:
    """Validate on-disk evidence structurally without granting process trust."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return VerificationBatchValidation(False, ("payload_not_object",), (), ())
    if str(payload.get("kind") or "") != PACER_VERIFICATION_BATCH_KIND:
        errors.append("kind_not_verification_batch")
    if str(payload.get("source_tool") or "") != PACER_VERIFICATION_SOURCE_TOOL:
        errors.append("source_tool_mismatch")
    if _integer(payload.get("policy_version")) != PACER_VERIFICATION_POLICY_VERSION:
        errors.append("policy_version_mismatch")
    if expected_run_id and str(payload.get("run_id") or "") != expected_run_id:
        errors.append("run_id_mismatch")
    if expected_launch_id is not None and str(payload.get("launch_id") or "") != expected_launch_id:
        errors.append("launch_id_mismatch")

    count_names = ("requested_steps", "executed_steps", "passed", "failed", "timed_out", "not_applicable")
    counts = {name: _integer(payload.get(name)) for name in count_names}
    if any(value is None or value < 0 for value in counts.values()):
        errors.append("invalid_batch_counts")
    else:
        requested = counts["requested_steps"] or 0
        executed = counts["executed_steps"] or 0
        passed = counts["passed"] or 0
        failed = counts["failed"] or 0
        timed_out = counts["timed_out"] or 0
        not_applicable = counts["not_applicable"] or 0
        if str(payload.get("status") or "") != "passed":
            errors.append("batch_status_not_passed")
        if executed <= 0 or requested != executed:
            errors.append("requested_executed_mismatch")
        if failed != 0 or timed_out != 0:
            errors.append("failed_or_timed_out_steps")
        if passed <= 0 or passed + not_applicable != executed:
            errors.append("incomplete_step_results")

    records = payload.get("records")
    expected_records = counts.get("executed_steps")
    if not isinstance(records, list) or expected_records is None or len(records) != expected_records:
        errors.append("records_count_mismatch")
        records = []

    derived_classes: list[str] = []
    passed_substantive: list[str] = []
    record_status_counts = {"passed": 0, "failed": 0, "timeout": 0, "not_applicable": 0}
    for record in records:
        if not isinstance(record, dict):
            errors.append("record_not_object")
            derived_classes.append("unknown")
            continue
        raw_command = record.get("command")
        command = [str(value) for value in raw_command] if isinstance(raw_command, list) else []
        step_class = classify_verification_step(command)
        derived_classes.append(step_class)
        if step_class == "unknown":
            errors.append("unknown_step_class")
        if is_noop_verification_step(command):
            errors.append("noop_verification_step")
        status = str(record.get("status") or "")
        if status not in record_status_counts:
            errors.append("invalid_record_status")
        else:
            record_status_counts[status] += 1
        if status == "passed" and step_class in SUBSTANTIVE_STEP_CLASSES:
            passed_substantive.append(step_class)

    declared_classes = payload.get("step_classes")
    normalized_declared = tuple(str(value) for value in declared_classes) if isinstance(declared_classes, list) else ()
    if normalized_declared != tuple(derived_classes):
        errors.append("step_classes_mismatch")
    if records and counts.get("passed") is not None:
        if record_status_counts["passed"] != counts["passed"]:
            errors.append("passed_count_mismatch")
        if record_status_counts["failed"] != counts["failed"]:
            errors.append("failed_count_mismatch")
        if record_status_counts["timeout"] != counts["timed_out"]:
            errors.append("timeout_count_mismatch")
        if record_status_counts["not_applicable"] != counts["not_applicable"]:
            errors.append("not_applicable_count_mismatch")
    if not passed_substantive:
        errors.append("substantive_verification_required")
    acceptance_classes = ACCEPTANCE_STEP_CLASSES | ({"compile"} if allow_compile_only else set())
    if not any(step_class in acceptance_classes for step_class in passed_substantive):
        errors.append("behavioral_verification_required")

    unique_errors = tuple(dict.fromkeys(errors))
    return VerificationBatchValidation(
        valid=not unique_errors,
        errors=unique_errors,
        step_classes=tuple(derived_classes),
        substantive_step_classes=tuple(passed_substantive),
    )


def register_trusted_verification_batch(
    payload: dict[str, Any],
    *,
    workspace_root: str | Path,
) -> str:
    """Register one internally executed batch for this MCP process only."""
    run_id = str(payload.get("run_id") or "")
    launch_id = str(payload.get("launch_id") or "")
    if not run_id:
        raise ValueError("trusted verification requires a run_id")
    if str(payload.get("kind") or "") != PACER_VERIFICATION_BATCH_KIND:
        raise ValueError("trusted verification requires pacer_verification_batch")
    if str(payload.get("source_tool") or "") != PACER_VERIFICATION_SOURCE_TOOL:
        raise ValueError("trusted verification source tool mismatch")
    if _integer(payload.get("policy_version")) != PACER_VERIFICATION_POLICY_VERSION:
        raise ValueError("trusted verification policy version mismatch")

    workspace_identity = _workspace_identity(workspace_root)
    summary_digest = pacer_verification_summary_digest(payload)
    identity = "\0".join(
        ("pacer-verification-receipt-v1", workspace_identity, launch_id, run_id, summary_digest)
    )
    receipt = hmac.new(_TRUST_SECRET, identity.encode("utf-8"), hashlib.sha256).hexdigest()
    key = (workspace_identity, launch_id, run_id)
    with _TRUST_LOCK:
        _TRUSTED_RECEIPTS[key] = (summary_digest, receipt)
        _TRUSTED_RECEIPTS.move_to_end(key)
        while len(_TRUSTED_RECEIPTS) > _MAX_TRUSTED_RECEIPTS:
            _TRUSTED_RECEIPTS.popitem(last=False)
    return receipt


def trusted_verification_receipt_errors(
    payload: dict[str, Any],
    *,
    workspace_root: str | Path | None,
    trusted_receipt: str = "",
) -> tuple[str, ...]:
    if workspace_root is None:
        return ("trusted_workspace_required",)
    if not trusted_receipt:
        return ("trusted_receipt_required",)
    workspace_identity = _workspace_identity(workspace_root)
    launch_id = str(payload.get("launch_id") or "")
    run_id = str(payload.get("run_id") or "")
    key = (workspace_identity, launch_id, run_id)
    with _TRUST_LOCK:
        registered = _TRUSTED_RECEIPTS.get(key)
    if registered is None:
        return ("trusted_receipt_not_registered",)
    registered_digest, registered_receipt = registered
    if not hmac.compare_digest(registered_digest, pacer_verification_summary_digest(payload)):
        return ("trusted_summary_digest_mismatch",)
    if not hmac.compare_digest(registered_receipt, str(trusted_receipt)):
        return ("trusted_receipt_mismatch",)
    return ()


def pacer_verification_summary_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _workspace_identity(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def _integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None
