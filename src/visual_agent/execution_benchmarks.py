"""Load and validate the execution-layer benchmark task contracts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


_TASK_ID = re.compile(r"^B[1-5]$")
_QUIET_FLAG = re.compile(r"(?:^|\s)-q(?:\s|$)")
_GIT_REVISION = re.compile(r"^[0-9a-fA-F]{40}$")
_REPOSITORY_TARGET_TASKS = {"B1", "B2", "B3", "B4"}
_OPERATOR_VERIFIER_TASKS = {"B2", "B3", "B4"}


def default_execution_tasks_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "tests" / "benchmarks" / "execution_tasks"


def load_execution_benchmark_tasks(tasks_dir: str | Path | None = None) -> list[dict[str, Any]]:
    directory = Path(tasks_dir).expanduser().resolve() if tasks_dir is not None else default_execution_tasks_dir()
    repo_root = directory.parents[2]
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Benchmark task must be a JSON object: {path}")
        validate_execution_benchmark_task(payload, repo_root=repo_root, source_path=path)
        task_id = str(payload.get("id") or "")
        if task_id in seen:
            raise ValueError(f"Duplicate benchmark task id: {task_id}")
        seen.add(task_id)
        tasks.append(payload)
    if seen != {f"B{index}" for index in range(1, 6)}:
        raise ValueError(f"Execution benchmark suite must define exactly B1-B5; found {sorted(seen)}")
    return tasks


def validate_execution_benchmark_task(
    task: dict[str, Any],
    *,
    repo_root: str | Path,
    source_path: str | Path | None = None,
) -> None:
    label = str(source_path or task.get("id") or "benchmark task")
    task_id = str(task.get("id") or "")
    if int(task.get("schema_version") or 0) != 1 or not _TASK_ID.fullmatch(task_id):
        raise ValueError(f"Invalid execution benchmark schema/id in {label}")
    if not str(task.get("objective") or "").strip():
        raise ValueError(f"Benchmark objective is required in {label}")
    criteria = task.get("acceptance_criteria") if isinstance(task.get("acceptance_criteria"), list) else []
    if not criteria or not all(str(item).strip() for item in criteria):
        raise ValueError(f"Benchmark acceptance_criteria must be non-empty in {label}")
    protocol = task.get("protocol") if isinstance(task.get("protocol"), dict) else {}
    if not isinstance(protocol.get("allow_test_edits"), bool):
        raise ValueError(f"Benchmark protocol must explicitly declare allow_test_edits in {label}")
    commands = execution_benchmark_commands(task)
    if not commands:
        raise ValueError(f"Benchmark verification commands are required in {label}")
    if any(_QUIET_FLAG.search(command) for command in commands):
        raise ValueError(f"Quiet -q verification is prohibited in {label}; keep commands observable and split")

    root = Path(repo_root).expanduser().resolve()
    target = task.get("target") if isinstance(task.get("target"), dict) else {}
    base_revision = str(target.get("base_revision") or "").strip()
    seed_patch = str(target.get("seed_patch") or "").strip()
    seed_patch_hash = str(target.get("seed_patch_sha256") or "").strip()
    if task_id in _REPOSITORY_TARGET_TASKS and not (base_revision and seed_patch and seed_patch_hash):
        raise ValueError(f"Repository benchmark target is incomplete in {label}")
    if base_revision or seed_patch or seed_patch_hash:
        if not _GIT_REVISION.fullmatch(base_revision):
            raise ValueError(f"Benchmark base_revision must be a full commit hash in {label}")
        patch_path = _repo_artifact(root, seed_patch, label=label)
        _verify_sha256(patch_path, seed_patch_hash, label=label)

    seed_dir = str(target.get("source_dir") or "").strip()
    seed_hashes = target.get("seed_files_sha256") if isinstance(target.get("seed_files_sha256"), dict) else {}
    if seed_dir or seed_hashes:
        directory = _repo_artifact(root, seed_dir, label=label)
        if not directory.is_dir() or not seed_hashes:
            raise ValueError(f"Benchmark seed fixture is incomplete in {label}")
        for relative, expected in seed_hashes.items():
            path = _child_artifact(directory, str(relative), label=label)
            _verify_sha256(path, str(expected), label=label)

    operator = task.get("operator_verification") if isinstance(task.get("operator_verification"), dict) else {}
    _validate_private_verifier(
        operator,
        repo_root=root,
        label=label,
        required=task_id in _OPERATOR_VERIFIER_TASKS,
    )
    repair = task.get("repair_injection") if isinstance(task.get("repair_injection"), dict) else {}
    _validate_private_verifier(repair, repo_root=root, label=label, required=task_id == "B5")


def execution_benchmark_commands(task: dict[str, Any]) -> list[str]:
    verification = task.get("verification") if isinstance(task.get("verification"), dict) else {}
    commands: list[str] = []
    for key in ("commands", "round_one_commands", "final_commands"):
        values = verification.get(key) if isinstance(verification.get(key), list) else []
        commands.extend(str(item).strip() for item in values if str(item).strip())
    operator = task.get("operator_verification") if isinstance(task.get("operator_verification"), dict) else {}
    operator_command = str(operator.get("command_template") or "").strip()
    if operator_command:
        commands.append(operator_command)
    repair = task.get("repair_injection") if isinstance(task.get("repair_injection"), dict) else {}
    repair_command = str(repair.get("command_template") or "").strip()
    if repair_command:
        commands.append(repair_command)
    return commands


def render_execution_benchmark_worker_prompt(task: dict[str, Any]) -> str:
    """Render only worker-visible fields; verification and hidden evidence stay operator-side."""
    lines = ["Objective: " + str(task.get("objective") or "").strip()]
    criteria = task.get("acceptance_criteria") if isinstance(task.get("acceptance_criteria"), list) else []
    if criteria:
        lines.append("Acceptance criteria:")
        lines.extend(f"- {str(item).strip()}" for item in criteria if str(item).strip())
    return "\n".join(lines)


def _repo_artifact(repo_root: Path, relative: str, *, label: str) -> Path:
    path = _child_artifact(repo_root, relative, label=label)
    if not path.exists():
        raise ValueError(f"Benchmark artifact does not exist in {label}: {relative}")
    return path


def _validate_private_verifier(
    contract: dict[str, Any],
    *,
    repo_root: Path,
    label: str,
    required: bool,
) -> None:
    verifier = str(contract.get("verifier") or "").strip()
    verifier_hash = str(contract.get("verifier_sha256") or "").strip()
    command = str(contract.get("command_template") or "").strip()
    if not contract and not required:
        return
    if str(contract.get("kind") or "") != "operator_private_verifier":
        raise ValueError(f"Benchmark private verifier kind is invalid in {label}")
    if not verifier or not verifier_hash or not command:
        raise ValueError(f"Benchmark private verifier contract is incomplete in {label}")
    if "{target_root}" not in command or Path(verifier).name not in command:
        raise ValueError(f"Benchmark private verifier command is invalid in {label}")
    path = _repo_artifact(repo_root, verifier, label=label)
    _verify_sha256(path, verifier_hash, label=label)


def _child_artifact(root: Path, relative: str, *, label: str) -> Path:
    candidate = (root / str(relative)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"Benchmark artifact escapes its root in {label}: {relative}") from exc
    return candidate


def _verify_sha256(path: Path, expected: str, *, label: str) -> None:
    if not path.is_file() or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise ValueError(f"Benchmark artifact hash is missing or invalid in {label}: {path}")
    # Git may materialize text artifacts as CRLF on Windows. Hash their
    # canonical LF form so checkout policy does not invalidate a frozen task.
    canonical = path.read_bytes().replace(b"\r\n", b"\n")
    actual = hashlib.sha256(canonical).hexdigest()
    if actual.lower() != expected.lower():
        raise ValueError(f"Benchmark artifact hash mismatch in {label}: {path}")
