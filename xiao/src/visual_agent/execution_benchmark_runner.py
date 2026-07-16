"""Reproducible direct-Codex execution benchmark harness.

The harness keeps target source, task definitions, model policy, worker output,
operator verification, and final diff evidence separate. All subprocess work is
behind an injectable runner so unit tests never need a model or external tool.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from .codex_exec import load_codex_user_defaults, parse_codex_jsonl_evidence
from .execution_benchmarks import render_execution_benchmark_worker_prompt, validate_execution_benchmark_task
from .security import redact_secret_text
from .subprocess_window import isolated_process_group_kwargs, prepare_subprocess_command, terminate_process_tree


ProcessRunner = Callable[..., dict[str, Any]]
TargetMaterializer = Callable[..., dict[str, Any]]
DiffCollector = Callable[..., dict[str, Any]]

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_SAFE_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SAFE_SERVICE_TIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PROVIDER_OPTION_ORDER = ("name", "base_url", "wire_api", "requires_openai_auth")
_TERMINAL_STATUSES = frozenset({"INVALID_SETUP", "INTERRUPTED", "PASS", "FAIL"})


class BenchmarkSetupError(RuntimeError):
    """Raised when a run cannot be compared to its frozen context."""


def run_external_process(
    command: list[str] | str,
    *,
    cwd: Path,
    timeout_seconds: float,
    stdin_text: str | None = None,
    shell: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Default subprocess adapter used by git, Codex, and operator gates."""
    started = monotonic()
    try:
        launch_command = prepare_subprocess_command(command) if isinstance(command, list) and not shell else command
        process = subprocess.Popen(
            launch_command,
            cwd=str(cwd),
            stdin=subprocess.PIPE if stdin_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=shell,
            env=({**os.environ, **{str(key): str(value) for key, value in env.items()}} if env else None),
            **isolated_process_group_kwargs(),
        )
        try:
            stdout, stderr = process.communicate(input=stdin_text, timeout=float(timeout_seconds))
        except subprocess.TimeoutExpired as exc:
            partial_stdout = _decode(exc.stdout)
            partial_stderr = _decode(exc.stderr)
            terminate_process_tree(process)
            try:
                stdout, stderr = process.communicate(timeout=5.0)
            except subprocess.TimeoutExpired as drain_exc:
                stdout = _decode(drain_exc.stdout) or partial_stdout
                stderr = _decode(drain_exc.stderr) or partial_stderr
            return {
                "exit_code": 124,
                "stdout": stdout or partial_stdout,
                "stderr": (stderr or partial_stderr) + f"\nTimed out after {float(timeout_seconds):.0f}s",
                "elapsed_seconds": round(monotonic() - started, 6),
                "timed_out": True,
            }
        return {
            "exit_code": int(process.returncode or 0),
            "stdout": stdout or "",
            "stderr": stderr or "",
            "elapsed_seconds": round(monotonic() - started, 6),
            "timed_out": False,
        }
    except OSError as exc:
        return {
            "exit_code": 127,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": round(monotonic() - started, 6),
            "timed_out": False,
        }


def freeze_execution_benchmark_context(
    *,
    target_repo: str | Path,
    output_dir: str | Path,
    task_files: Iterable[str | Path],
    operator_root: str | Path | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    sandbox: str | None = None,
    approval: str | None = None,
    ignore_user_config: bool = True,
    model_provider: str | None = None,
    model_provider_options: Mapping[str, Any] | None = None,
    service_tier: str | None = None,
    windows_sandbox: str | None = None,
    trust_target: bool = False,
    codex_executable: str = "codex",
    codex_version: str | None = None,
    config_path: str | Path | None = None,
    common_seed_patch: str | Path | None = None,
    project_subdir: str | Path = ".",
    operator_gates_by_task: Mapping[str, list[dict[str, Any]]] | None = None,
    process_runner: ProcessRunner = run_external_process,
    context_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Freeze comparison inputs without mutating the target repository."""
    if not isinstance(ignore_user_config, bool):
        raise ValueError("Frozen Codex policy ignore_user_config must be a boolean")
    provider_policy = _normalize_codex_provider_policy(
        model_provider=model_provider,
        model_provider_options=model_provider_options,
        service_tier=service_tier,
    )
    runtime_policy = _normalize_codex_runtime_policy(
        windows_sandbox=windows_sandbox,
        trust_target=trust_target,
    )
    repo = Path(target_repo).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"Target repository does not exist: {repo}")
    root = Path(operator_root).expanduser().resolve() if operator_root else repo
    frozen_project_subdir = _normalize_project_subdir(project_subdir)
    source_project_root = repo if frozen_project_subdir == "." else _safe_child(repo, frozen_project_subdir)
    if not source_project_root.is_dir():
        raise ValueError(f"Target project subdirectory does not exist: {source_project_root}")
    sources = [Path(path).expanduser().resolve() for path in task_files]
    if not sources:
        raise ValueError("At least one task definition is required")

    tasks: list[tuple[Path, dict[str, Any], bytes]] = []
    seen: set[str] = set()
    for source in sources:
        raw = source.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Task definition must be a JSON object: {source}")
        validate_execution_benchmark_task(payload, repo_root=root, source_path=source)
        task_id = str(payload.get("id") or "")
        if task_id in seen:
            raise ValueError(f"Duplicate benchmark task: {task_id}")
        seen.add(task_id)
        tasks.append((source, payload, raw))

    defaults = load_codex_user_defaults(config_path)
    pinned = {
        "model": str(model or defaults.get("model") or "").strip(),
        "reasoning_effort": str(reasoning_effort or defaults.get("reasoning_effort") or "").strip(),
        "sandbox": str(sandbox or defaults.get("sandbox") or "").strip(),
        "approval": str(approval or defaults.get("approval") or "").strip(),
    }
    missing = [key for key, value in pinned.items() if not value or value.startswith("inherited(")]
    if missing:
        raise ValueError("Frozen Codex policy requires explicit values: " + ", ".join(missing))

    base_commit = _git_text(process_runner, repo, ["git", "rev-parse", "HEAD"])
    base_tree = _git_text(process_runner, repo, ["git", "rev-parse", "HEAD^{tree}"])
    for _source, task, _raw in tasks:
        target = task.get("target") if isinstance(task.get("target"), dict) else {}
        declared_base = str(target.get("base_revision") or "").strip()
        if declared_base and declared_base != base_commit:
            raise ValueError(
                f"Task {task.get('id')} base_revision {declared_base} does not match frozen HEAD {base_commit}"
            )
    tracked_paths = _git_nul_paths(
        process_runner,
        repo,
        ["git", "-c", "core.quotePath=false", "ls-tree", "-r", "--name-only", "-z", "HEAD"],
    )
    untracked_paths = _git_nul_paths(
        process_runner,
        repo,
        ["git", "-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard", "-z"],
    )
    declared_patch_paths = {
        str((task.get("target") or {}).get("seed_patch") or "").strip()
        for _source, task, _raw in tasks
        if isinstance(task.get("target"), dict) and str((task.get("target") or {}).get("seed_patch") or "").strip()
    }
    if common_seed_patch is None and declared_patch_paths:
        if len(declared_patch_paths) != 1:
            raise ValueError("Repository benchmark tasks do not share one common seed patch")
        patch_bytes = _safe_child(root, next(iter(declared_patch_paths))).read_bytes()
    elif common_seed_patch is None:
        patch_result = _call_runner(
            process_runner,
            ["git", "-c", "core.quotePath=false", "diff", "--binary", "--full-index", "HEAD", "--"],
            cwd=repo,
            timeout_seconds=60.0,
        )
        _require_success(patch_result, "capture common seed patch")
        patch_bytes = str(patch_result.get("stdout") or "").encode("utf-8")
    else:
        patch_bytes = Path(common_seed_patch).expanduser().resolve().read_bytes()
    patch_bytes = patch_bytes.replace(b"\r\n", b"\n")
    frozen_patch_sha256 = hashlib.sha256(patch_bytes).hexdigest()
    for _source, task, _raw in tasks:
        target = task.get("target") if isinstance(task.get("target"), dict) else {}
        declared_patch_hash = str(target.get("seed_patch_sha256") or "").lower()
        if declared_patch_hash and declared_patch_hash != frozen_patch_sha256:
            raise ValueError(
                f"Task {task.get('id')} seed patch does not match the frozen common seed patch"
            )

    version = str(codex_version or "").strip()
    if not version:
        version_result = _call_runner(
            process_runner,
            [codex_executable, "--version"],
            cwd=repo,
            timeout_seconds=30.0,
        )
        _require_success(version_result, "read Codex version")
        version = str(version_result.get("stdout") or "").strip()
    if not version:
        raise ValueError("Codex version could not be frozen")
    if ignore_user_config:
        capability_probes = (
            ([codex_executable, "exec", "--ignore-user-config", "--help"], "initial"),
            ([codex_executable, "exec", "--ignore-user-config", "resume", "--help"], "resume"),
        )
        for command, invocation_kind in capability_probes:
            probe_result = _call_runner(
                process_runner,
                command,
                cwd=repo,
                timeout_seconds=30.0,
            )
            _require_success(
                probe_result,
                f"validate Codex {invocation_kind} --ignore-user-config support",
            )

    destination = Path(output_dir).expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"Frozen context directory already exists: {destination}")
    destination.mkdir(parents=True)

    common_files_dir = destination / "targets" / "common" / "files"
    base_worktree = destination / ".freeze-base-worktree"
    add_worktree = _call_runner(
        process_runner,
        ["git", "worktree", "add", "--detach", str(base_worktree), base_commit],
        cwd=repo,
        timeout_seconds=120.0,
    )
    _require_success(add_worktree, "create clean frozen base worktree")
    try:
        common_manifest = _freeze_files(
            base_worktree,
            common_files_dir,
            [(path, True) for path in tracked_paths],
        )
    finally:
        remove_worktree = _call_runner(
            process_runner,
            ["git", "worktree", "remove", "--force", str(base_worktree)],
            cwd=repo,
            timeout_seconds=120.0,
        )
        _require_success(remove_worktree, "remove temporary frozen base worktree")
    seed_files_dir = destination / "targets" / "common" / "seed_files"
    seed_files_manifest = _freeze_files(
        repo,
        seed_files_dir,
        [(path, False) for path in untracked_paths],
        allow_empty=True,
    )
    patch_path = destination / "targets" / "common" / "common_seed.patch"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_bytes(patch_bytes)
    files_manifest_path = destination / "targets" / "common" / "files_manifest.json"
    _write_json_exclusive(files_manifest_path, common_manifest)
    seed_files_manifest_path = destination / "targets" / "common" / "seed_files_manifest.json"
    _write_json_exclusive(seed_files_manifest_path, seed_files_manifest)
    patch_sha256 = frozen_patch_sha256
    common_snapshot_sha = _canonical_sha256(
        {
            "base_files": common_manifest,
            "seed_patch_sha256": patch_sha256,
            "seed_files": seed_files_manifest,
        }
    )

    context_tasks: dict[str, Any] = {}
    for source, task, raw in tasks:
        task_id = str(task["id"])
        frozen_task = destination / "task_definitions" / f"{task_id}.json"
        frozen_task.parent.mkdir(parents=True, exist_ok=True)
        frozen_task.write_bytes(raw)
        target_record: dict[str, Any] = {
            "kind": "common",
            "snapshot_sha256": common_snapshot_sha,
        }
        target = task.get("target") if isinstance(task.get("target"), dict) else {}
        seed_dir_text = str(target.get("source_dir") or "").strip()
        if seed_dir_text:
            seed_root = _safe_child(root, seed_dir_text)
            seed_hashes = target.get("seed_files_sha256") if isinstance(target.get("seed_files_sha256"), dict) else {}
            fixture_dir = destination / "targets" / task_id / "files"
            fixture_manifest = _freeze_declared_files(seed_root, fixture_dir, seed_hashes)
            fixture_manifest_path = destination / "targets" / task_id / "files_manifest.json"
            _write_json_exclusive(fixture_manifest_path, fixture_manifest)
            target_record = {
                "kind": "fixture",
                "files_dir": _relative(destination, fixture_dir),
                "files_manifest": _relative(destination, fixture_manifest_path),
                "snapshot_sha256": _canonical_sha256({"files": fixture_manifest}),
            }

        verifier_record: dict[str, str] = {}
        repair = task.get("repair_injection") if isinstance(task.get("repair_injection"), dict) else {}
        verifier_text = str(repair.get("verifier") or "").strip()
        if verifier_text:
            verifier = _safe_child(root, verifier_text)
            expected = str(repair.get("verifier_sha256") or "").lower()
            if _sha256_canonical_file(verifier) != expected:
                raise ValueError(f"Private verifier changed while freezing {task_id}: {verifier}")
            frozen_verifier = destination / "operator_artifacts" / task_id / verifier.name
            frozen_verifier.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(verifier, frozen_verifier)
            verifier_record = {
                "source": verifier_text,
                "path": _relative(destination, frozen_verifier),
                "sha256": expected,
            }

        operator_verifier_record = _freeze_operator_verifier(
            task_id=task_id,
            task=task,
            operator_root=root,
            destination=destination,
        )

        overrides = operator_gates_by_task or {}
        gates = (
            [dict(item) for item in overrides.get(task_id, [])]
            if task_id in overrides
            else _derive_operator_gates(task, verifier_record, operator_verifier_record)
        )
        if not gates:
            raise ValueError(f"No operator gates were frozen for {task_id}")
        context_tasks[task_id] = {
            "definition_path": _relative(destination, frozen_task),
            "definition_sha256": hashlib.sha256(raw).hexdigest(),
            "objective": str(task.get("objective") or ""),
            "target": target_record,
            "operator_gates": gates,
            "max_worker_turns": max(1, int((task.get("protocol") or {}).get("max_worker_turns") or 1)),
            "require_change": True,
        }

    cid = context_id or datetime.now(timezone.utc).strftime("execution-%Y%m%dT%H%M%SZ")
    _validate_id(cid, field="context_id")
    context = {
        "schema_version": 1,
        "context_id": cid,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "harness": _harness_source_manifest(),
        "target": {
            "source_repo": str(repo),
            "project_subdir": frozen_project_subdir,
            "base_commit": base_commit,
            "base_tree": base_tree,
            "files_dir": _relative(destination, common_files_dir),
            "files_manifest": _relative(destination, files_manifest_path),
            "snapshot_sha256": common_snapshot_sha,
            "seed_patch_path": _relative(destination, patch_path),
            "seed_patch_sha256": patch_sha256,
            "seed_files_dir": _relative(destination, seed_files_dir),
            "seed_files_manifest": _relative(destination, seed_files_manifest_path),
        },
        "codex": {
            "executable": codex_executable,
            "version": version,
            "ignore_user_config": ignore_user_config,
            "provider": provider_policy,
            "runtime": runtime_policy,
            **pinned,
        },
        "orchestrator": {
            "root": str(root),
            "revision": _git_text(process_runner, root, ["git", "rev-parse", "HEAD"], required=False),
        },
        "tasks": context_tasks,
    }
    context_path = destination / "context.json"
    _write_json_exclusive(context_path, context)
    return {**context, "path": str(context_path), "context_sha256": _sha256_file(context_path)}


def load_frozen_execution_context(path: str | Path) -> tuple[dict[str, Any], Path]:
    context_path = Path(path).expanduser().resolve()
    payload = json.loads(context_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != 1:
        raise BenchmarkSetupError(f"Invalid frozen context: {context_path}")
    if not isinstance(payload.get("tasks"), dict) or not isinstance(payload.get("codex"), dict):
        raise BenchmarkSetupError(f"Incomplete frozen context: {context_path}")
    return payload, context_path


def materialize_frozen_target(
    *,
    context: dict[str, Any],
    context_root: Path,
    task_id: str,
    run_dir: Path,
    process_runner: ProcessRunner = run_external_process,
) -> dict[str, Any]:
    """Copy one frozen snapshot into a fresh standalone Git repository."""
    task_record = _context_task(context, task_id)
    target_record = task_record.get("target") if isinstance(task_record.get("target"), dict) else {}
    common = context.get("target") if isinstance(context.get("target"), dict) else {}
    is_fixture = target_record.get("kind") == "fixture"
    selected = target_record if is_fixture else common
    files_dir = _context_artifact(context_root, str(selected.get("files_dir") or ""))
    manifest_path = _context_artifact(context_root, str(selected.get("files_manifest") or ""))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_snapshot = str(selected.get("snapshot_sha256") or "")
    seed_manifest: list[dict[str, Any]] = []
    seed_files_dir: Path | None = None
    patch_text = ""
    if is_fixture:
        actual_snapshot = _canonical_sha256({"files": manifest})
    else:
        seed_manifest_path = _context_artifact(context_root, str(common.get("seed_files_manifest") or ""))
        raw_seed_manifest = json.loads(seed_manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw_seed_manifest, list):
            raise BenchmarkSetupError("Frozen common seed files manifest must be a list")
        seed_manifest = raw_seed_manifest
        seed_files_dir = _context_artifact(context_root, str(common.get("seed_files_dir") or ""))
        patch_path = _context_artifact(context_root, str(common.get("seed_patch_path") or ""))
        patch_bytes = patch_path.read_bytes()
        if hashlib.sha256(patch_bytes).hexdigest() != str(common.get("seed_patch_sha256") or ""):
            raise BenchmarkSetupError("Frozen common seed patch hash mismatch")
        patch_text = patch_bytes.decode("utf-8", errors="strict")
        actual_snapshot = _canonical_sha256(
            {
                "base_files": manifest,
                "seed_patch_sha256": hashlib.sha256(patch_bytes).hexdigest(),
                "seed_files": seed_manifest,
            }
        )
    if actual_snapshot != expected_snapshot:
        raise BenchmarkSetupError(f"Frozen target manifest hash mismatch for {task_id}")

    target_root = run_dir / "target"
    if target_root.exists():
        raise BenchmarkSetupError(f"Fresh target path already exists: {target_root}")
    shutil.copytree(files_dir, target_root, symlinks=True)
    _verify_materialized_files(target_root, manifest)

    init = _call_runner(process_runner, ["git", "init", "--quiet"], cwd=target_root, timeout_seconds=60.0)
    _require_success(init, "initialize frozen target")
    if not is_fixture and patch_text:
        apply_result = _call_runner(
            process_runner,
            ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
            cwd=target_root,
            timeout_seconds=60.0,
            stdin_text=patch_text,
        )
        _require_success(apply_result, "apply frozen common seed patch")
    if not is_fixture and seed_files_dir is not None:
        _copy_manifest_files(seed_files_dir, target_root, seed_manifest)
        _verify_materialized_files(target_root, seed_manifest)

    commands = [
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.email=benchmark@example.local",
            "-c",
            "user.name=Execution Benchmark",
            "commit",
            "--quiet",
            "-m",
            "Frozen execution benchmark baseline",
        ],
    ]
    for command in commands:
        result = _call_runner(process_runner, command, cwd=target_root, timeout_seconds=60.0)
        _require_success(result, "materialize frozen target")
    baseline_commit = _git_text(process_runner, target_root, ["git", "rev-parse", "HEAD"])
    baseline_tree = _git_text(process_runner, target_root, ["git", "rev-parse", "HEAD^{tree}"])
    status = _git_text(
        process_runner,
        target_root,
        ["git", "-c", "core.quotePath=false", "status", "--porcelain"],
        required=False,
    )
    if status:
        raise BenchmarkSetupError(f"Materialized target is not clean: {status[:200]}")
    project_subdir = "." if is_fixture else _normalize_project_subdir(
        str(common.get("project_subdir") or ".")
    )
    project_root = target_root if project_subdir == "." else _safe_child(target_root, project_subdir)
    if not project_root.is_dir():
        raise BenchmarkSetupError(f"Materialized project root is missing: {project_root}")
    return {
        "root": str(target_root),
        "project_root": str(project_root),
        "project_subdir": project_subdir,
        "kind": str(target_record.get("kind") or "common"),
        "snapshot_sha256": expected_snapshot,
        "baseline_commit": baseline_commit,
        "baseline_tree": baseline_tree,
        "common_seed_patch_sha256": str(common.get("seed_patch_sha256") or ""),
    }


def _normalize_codex_provider_policy(
    *,
    model_provider: Any,
    model_provider_options: Mapping[str, Any] | None,
    service_tier: Any,
) -> dict[str, Any]:
    provider_id = str(model_provider or "").strip()
    if provider_id and not _SAFE_PROVIDER_ID.fullmatch(provider_id):
        raise ValueError("model_provider must be a simple TOML key")

    if model_provider_options is None:
        raw_options: Mapping[str, Any] = {}
    elif isinstance(model_provider_options, Mapping):
        raw_options = model_provider_options
    else:
        raise ValueError("model_provider_options must be an object")
    unsupported = sorted(str(key) for key in raw_options if str(key) not in _PROVIDER_OPTION_ORDER)
    if unsupported:
        raise ValueError(
            "unsupported model provider options (secret-bearing fields are not frozen): "
            + ", ".join(unsupported)
        )
    if raw_options and not provider_id:
        raise ValueError("model_provider is required when provider options are frozen")

    options: dict[str, Any] = {}
    for key in _PROVIDER_OPTION_ORDER:
        if key not in raw_options:
            continue
        value = raw_options[key]
        if key == "requires_openai_auth":
            if not isinstance(value, bool):
                raise ValueError("requires_openai_auth must be a boolean")
            options[key] = value
            continue
        text = str(value or "").strip()
        if not text or any(ord(char) < 32 for char in text):
            raise ValueError(f"{key} must be a non-empty printable string")
        if "'" in text:
            raise ValueError(f"{key} must not contain a single quote")
        if key == "wire_api" and text not in {"responses", "chat"}:
            raise ValueError("wire_api must be responses or chat")
        if key == "base_url":
            parsed = urlsplit(text)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("base_url must be an absolute HTTP(S) URL")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("base_url must not contain credentials, query parameters, or fragments")
        options[key] = text

    tier = str(service_tier or "").strip()
    if tier and not _SAFE_SERVICE_TIER.fullmatch(tier):
        raise ValueError("service_tier contains unsupported characters")
    return {"id": provider_id, "options": options, "service_tier": tier}


def _normalize_codex_runtime_policy(*, windows_sandbox: Any, trust_target: Any) -> dict[str, Any]:
    if not isinstance(trust_target, bool):
        raise ValueError("trust_target must be a boolean")
    sandbox = str(windows_sandbox or "").strip()
    if sandbox:
        if not _SAFE_SERVICE_TIER.fullmatch(sandbox) or "'" in sandbox:
            raise ValueError("windows_sandbox contains unsupported characters")
    return {"windows_sandbox": sandbox, "trust_target": trust_target}


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "'" + str(value) + "'"


def build_direct_codex_argv(
    context: Mapping[str, Any],
    *,
    resume_session_id: str | None = None,
    trusted_project_root: str | Path | None = None,
) -> list[str]:
    """Build a fully explicit direct-Codex command; the prompt stays on stdin."""
    codex = context.get("codex") if isinstance(context.get("codex"), dict) else {}
    required = ("executable", "model", "reasoning_effort", "sandbox", "approval")
    missing = [key for key in required if not str(codex.get(key) or "").strip()]
    if missing:
        raise BenchmarkSetupError("Frozen Codex policy is incomplete: " + ", ".join(missing))
    ignore_user_config = codex.get("ignore_user_config")
    if not isinstance(ignore_user_config, bool):
        raise BenchmarkSetupError("Frozen Codex policy is incomplete: ignore_user_config")
    provider = codex.get("provider")
    if not isinstance(provider, dict):
        raise BenchmarkSetupError("Frozen Codex policy is incomplete: provider")
    try:
        provider_policy = _normalize_codex_provider_policy(
            model_provider=provider.get("id"),
            model_provider_options=provider.get("options"),
            service_tier=provider.get("service_tier"),
        )
    except ValueError as exc:
        raise BenchmarkSetupError(f"Frozen Codex provider policy is invalid: {exc}") from exc
    try:
        runtime_policy = _normalize_codex_runtime_policy(
            windows_sandbox=(codex.get("runtime") or {}).get("windows_sandbox")
            if isinstance(codex.get("runtime"), dict)
            else "",
            trust_target=(codex.get("runtime") or {}).get("trust_target")
            if isinstance(codex.get("runtime"), dict)
            else False,
        )
    except ValueError as exc:
        raise BenchmarkSetupError(f"Frozen Codex runtime policy is invalid: {exc}") from exc
    argv = [
        str(codex["executable"]),
        "--ask-for-approval",
        str(codex["approval"]),
        "--sandbox",
        str(codex["sandbox"]),
        "exec",
    ]
    if ignore_user_config:
        argv.append("--ignore-user-config")
    if resume_session_id:
        argv.append("resume")
    argv.extend(
        [
            "--json",
            "--model",
            str(codex["model"]),
            "-c",
            f"model_reasoning_effort={codex['reasoning_effort']}",
        ]
    )
    provider_id = str(provider_policy["id"])
    if provider_id:
        argv.extend(["-c", f"model_provider={_toml_scalar(provider_id)}"])
        options = provider_policy["options"]
        for key in _PROVIDER_OPTION_ORDER:
            if key in options:
                argv.extend(
                    [
                        "-c",
                        f"model_providers.{provider_id}.{key}={_toml_scalar(options[key])}",
                    ]
                )
    if provider_policy["service_tier"]:
        argv.extend(["-c", f"service_tier={_toml_scalar(provider_policy['service_tier'])}"])
    if runtime_policy["windows_sandbox"]:
        argv.extend(["-c", f"windows.sandbox={_toml_scalar(runtime_policy['windows_sandbox'])}"])
    if runtime_policy["trust_target"]:
        if trusted_project_root is None:
            raise BenchmarkSetupError("Frozen Codex runtime trust_target requires a trusted project root")
        trusted_path = str(Path(trusted_project_root).expanduser().resolve())
        if "'" in trusted_path or any(ord(char) < 32 for char in trusted_path):
            raise BenchmarkSetupError("Trusted project root cannot be represented as a TOML key")
        argv.extend(["-c", f"projects.{_toml_scalar(trusted_path)}.trust_level='trusted'"])
    if resume_session_id:
        argv.append(str(resume_session_id))
    argv.append("-")
    return argv


def run_direct_codex_benchmark(
    *,
    context_path: str | Path,
    task_id: str,
    artifact_root: str | Path,
    run_id: str | None = None,
    timeout_seconds: float = 1800.0,
    gate_timeout_seconds: float = 900.0,
    process_runner: ProcessRunner = run_external_process,
    target_materializer: TargetMaterializer = materialize_frozen_target,
    diff_collector: DiffCollector | None = None,
    human_interventions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run one frozen task through direct ``codex exec --json``."""
    context, frozen_context_path = load_frozen_execution_context(context_path)
    _validate_id(task_id, field="task_id")
    rid = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:10]
    _validate_id(rid, field="run_id")
    root = Path(artifact_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / "runs" / rid
    run_dir.mkdir(parents=True, exist_ok=False)
    started = monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    task: dict[str, Any] | None = None
    target: dict[str, Any] | None = None
    invocations: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "run_id": rid,
        "lane": "direct-codex",
        "transport": "exec-jsonl",
        "status": "INVALID_SETUP",
        "started_at": started_at,
        "context": {
            "context_id": str(context.get("context_id") or ""),
            "path": str(frozen_context_path),
            "sha256": _sha256_file(frozen_context_path),
        },
        "task_id": task_id,
        "codex": {
            "version": str((context.get("codex") or {}).get("version") or ""),
            "model": str((context.get("codex") or {}).get("model") or ""),
            "reasoning_effort": str((context.get("codex") or {}).get("reasoning_effort") or ""),
            "sandbox": str((context.get("codex") or {}).get("sandbox") or ""),
            "approval": str((context.get("codex") or {}).get("approval") or ""),
            "ignore_user_config": bool((context.get("codex") or {}).get("ignore_user_config")),
            "provider": dict((context.get("codex") or {}).get("provider") or {}),
            "invocations": invocations,
        },
        "human_interventions": list(human_interventions or []),
    }

    try:
        task, task_record = _load_frozen_task(context, frozen_context_path.parent, task_id)
        manifest["task"] = {
            "definition_path": str(_context_artifact(frozen_context_path.parent, task_record["definition_path"])),
            "definition_sha256": str(task_record.get("definition_sha256") or ""),
            "objective": str(task.get("objective") or ""),
        }
        baseline_target = target_materializer(
            context=context,
            context_root=frozen_context_path.parent,
            task_id=task_id,
            run_dir=run_dir / "baseline-probe",
            process_runner=process_runner,
        )
        baseline_project_root = Path(
            str(baseline_target.get("project_root") or baseline_target.get("root") or "")
        ).expanduser().resolve()
        if not baseline_project_root.is_dir():
            raise BenchmarkSetupError("Baseline materializer did not create a project directory")
        baseline_verification = _run_operator_gate_phase(
            context=context,
            context_root=frozen_context_path.parent,
            task_id=task_id,
            target_root=baseline_project_root,
            run_dir=run_dir,
            phase="baseline",
            timeout_seconds=gate_timeout_seconds,
            process_runner=process_runner,
        )
        manifest["baseline_probe_target"] = baseline_target
        manifest["baseline_verification"] = baseline_verification
        if baseline_verification.get("verdict") != "FAIL":
            raise BenchmarkSetupError("Frozen operator baseline gates must fail before a worker run")

        target = target_materializer(
            context=context,
            context_root=frozen_context_path.parent,
            task_id=task_id,
            run_dir=run_dir,
            process_runner=process_runner,
        )
        manifest["target"] = target
        git_target_root = Path(str(target.get("root") or "")).expanduser().resolve()
        project_root = Path(str(target.get("project_root") or "")).expanduser().resolve()
        if not git_target_root.is_dir():
            raise BenchmarkSetupError("Target materializer did not create a target directory")
        if not project_root.is_dir():
            raise BenchmarkSetupError("Target materializer did not create the frozen project subdirectory")

        prompt = render_execution_benchmark_worker_prompt(task)
        initial = _run_codex_invocation(
            context=context,
            target_root=project_root,
            trusted_project_root=git_target_root,
            run_dir=run_dir,
            index=1,
            label="initial",
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            process_runner=process_runner,
        )
        invocations.append(initial)
        first_verification = _run_operator_gate_phase(
            context=context,
            context_root=frozen_context_path.parent,
            task_id=task_id,
            target_root=project_root,
            run_dir=run_dir,
            phase="first",
            timeout_seconds=gate_timeout_seconds,
            process_runner=process_runner,
        )
        manifest["first_verification"] = first_verification
        final_verification = first_verification

        repair = task.get("repair_injection") if isinstance(task.get("repair_injection"), dict) else {}
        max_turns = int(task_record.get("max_worker_turns") or 1)
        if (
            first_verification.get("verdict") == "FAIL"
            and repair
            and max_turns >= 2
            and int(initial.get("exit_code") or 0) == 0
        ):
            session_id = str((initial.get("jsonl") or {}).get("session_id") or "")
            if not session_id:
                raise BenchmarkSetupError("Repair task requires a thread_id from the initial JSONL stream")
            repair_prompt = _build_repair_prompt(
                task,
                first_verification,
                context_root=frozen_context_path.parent,
                target_root=project_root,
            )
            repair_invocation = _run_codex_invocation(
                context=context,
                target_root=project_root,
                trusted_project_root=git_target_root,
                run_dir=run_dir,
                index=2,
                label="repair",
                prompt=repair_prompt,
                timeout_seconds=timeout_seconds,
                process_runner=process_runner,
                resume_session_id=session_id,
            )
            invocations.append(repair_invocation)
            resumed_session = str((repair_invocation.get("jsonl") or {}).get("session_id") or "")
            if resumed_session and resumed_session != session_id:
                raise BenchmarkSetupError(
                    f"Repair JSONL changed thread id: expected {session_id}, received {resumed_session}"
                )
            final_verification = _run_operator_gate_phase(
                context=context,
                context_root=frozen_context_path.parent,
                task_id=task_id,
                target_root=project_root,
                run_dir=run_dir,
                phase="final",
                timeout_seconds=gate_timeout_seconds,
                process_runner=process_runner,
            )
        manifest["final_verification"] = final_verification

        collector = diff_collector or capture_diff_evidence
        diff = collector(
            target_root=git_target_root,
            baseline_commit=str(target.get("baseline_commit") or ""),
            run_dir=run_dir,
            process_runner=process_runner,
        )
        manifest["diff"] = diff
        manifest["usage"] = _aggregate_invocation_usage(invocations)
        manifest["first_verdict"] = str(first_verification.get("verdict") or "FAIL")
        manifest["final_verdict"] = str(final_verification.get("verdict") or "FAIL")

        interrupted = any(bool(item.get("timed_out")) for item in invocations)
        interrupted = interrupted or any(
            bool(gate.get("timed_out"))
            for phase in (first_verification, final_verification)
            for gate in (phase.get("gates") or [])
            if isinstance(gate, dict)
        )
        last_worker_ok = bool(invocations) and int(invocations[-1].get("exit_code") or 0) == 0
        changed = diff.get("changed_files") if isinstance(diff.get("changed_files"), list) else []
        require_change = bool(task_record.get("require_change", True))
        if interrupted:
            manifest["status"] = "INTERRUPTED"
            manifest["reason"] = "worker_or_verification_timeout"
        elif final_verification.get("verdict") == "PASS" and last_worker_ok and (changed or not require_change):
            manifest["status"] = "PASS"
        else:
            manifest["status"] = "FAIL"
            if not last_worker_ok:
                manifest["reason"] = "codex_exec_failed"
            elif final_verification.get("verdict") != "PASS":
                manifest["reason"] = "operator_verification_failed"
            else:
                manifest["reason"] = "no_target_changes"
    except BenchmarkSetupError as exc:
        manifest["status"] = "INVALID_SETUP"
        manifest["reason"] = str(exc)
    except KeyboardInterrupt:
        manifest["status"] = "INTERRUPTED"
        manifest["reason"] = "operator_interrupted"

    manifest["elapsed_seconds"] = round(monotonic() - started, 6)
    return append_execution_benchmark_manifest(root, run_dir, manifest)


def capture_diff_evidence(
    *,
    target_root: Path,
    baseline_commit: str,
    run_dir: Path,
    process_runner: ProcessRunner = run_external_process,
) -> dict[str, Any]:
    """Persist reviewable tracked and untracked changes relative to the seed."""
    if not baseline_commit:
        raise BenchmarkSetupError("Diff evidence requires a synthetic baseline commit")
    commands = {
        "status": ["git", "-c", "core.quotePath=false", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        "patch": ["git", "-c", "core.quotePath=false", "diff", "--binary", "--full-index", baseline_commit, "--"],
        "names": ["git", "-c", "core.quotePath=false", "diff", "--name-status", baseline_commit, "--"],
        "numstat": ["git", "-c", "core.quotePath=false", "diff", "--numstat", baseline_commit, "--"],
        "untracked": ["git", "-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard", "-z"],
    }
    outputs: dict[str, str] = {}
    for name, command in commands.items():
        result = _call_runner(process_runner, command, cwd=target_root, timeout_seconds=60.0)
        _require_success(result, f"capture diff {name}")
        outputs[name] = str(result.get("stdout") or "")

    diff_dir = run_dir / "diff"
    diff_dir.mkdir(parents=True, exist_ok=True)
    status_path = diff_dir / "status.porcelain"
    patch_path = diff_dir / "final.patch"
    status_path.write_text(outputs["status"], encoding="utf-8")
    patch_path.write_text(outputs["patch"], encoding="utf-8")

    stats: dict[str, tuple[int, int]] = {}
    for line in outputs["numstat"].splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            added = int(parts[0]) if parts[0].isdigit() else 0
            removed = int(parts[1]) if parts[1].isdigit() else 0
            stats[parts[-1].replace("\\", "/")] = (added, removed)
    changed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in outputs["names"].splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        path = parts[-1].replace("\\", "/")
        added, removed = stats.get(path, (0, 0))
        changed.append(
            {
                "path": path,
                "status": parts[0],
                "lines_added": added,
                "lines_removed": removed,
            }
        )
        seen.add(path)

    untracked_manifest: list[dict[str, Any]] = []
    untracked_dir = diff_dir / "untracked"
    for relative in _split_nul(outputs["untracked"]):
        source = _safe_child(target_root, relative)
        if not source.is_file():
            continue
        destination = untracked_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        entry = {
            "path": relative.replace("\\", "/"),
            "sha256": _sha256_file(source),
            "size": source.stat().st_size,
        }
        untracked_manifest.append(entry)
        if entry["path"] not in seen:
            line_count = _text_line_count(source)
            changed.append(
                {
                    "path": entry["path"],
                    "status": "A",
                    "lines_added": line_count,
                    "lines_removed": 0,
                }
            )
            seen.add(entry["path"])
    untracked_manifest.sort(key=lambda item: item["path"])
    untracked_path = diff_dir / "untracked_manifest.json"
    _write_json_exclusive(untracked_path, untracked_manifest)
    changed.sort(key=lambda item: item["path"])
    return {
        "baseline_commit": baseline_commit,
        "status_path": _relative(run_dir, status_path),
        "status_sha256": _sha256_file(status_path),
        "patch_path": _relative(run_dir, patch_path),
        "patch_sha256": _sha256_file(patch_path),
        "untracked_manifest_path": _relative(run_dir, untracked_path),
        "untracked_manifest_sha256": _sha256_file(untracked_path),
        "changed_files": changed,
        "file_count": len(changed),
        "lines_added": sum(int(item.get("lines_added") or 0) for item in changed),
        "lines_removed": sum(int(item.get("lines_removed") or 0) for item in changed),
    }


def append_execution_benchmark_manifest(
    artifact_root: str | Path,
    run_dir: str | Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Write one immutable terminal manifest and append its index record."""
    root = Path(artifact_root).expanduser().resolve()
    directory = Path(run_dir).expanduser().resolve()
    status = str(manifest.get("status") or "")
    if status not in _TERMINAL_STATUSES:
        raise ValueError(f"Benchmark manifest is not terminal: {status}")
    terminal = dict(manifest)
    terminal.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
    manifest_path = directory / "manifest.json"
    _write_json_exclusive(manifest_path, terminal)
    digest = _sha256_file(manifest_path)
    index_entry = {
        "schema_version": 1,
        "run_id": str(terminal.get("run_id") or directory.name),
        "status": status,
        "task_id": str(terminal.get("task_id") or ""),
        "lane": str(terminal.get("lane") or ""),
        "manifest_path": _relative(root, manifest_path),
        "manifest_sha256": digest,
        "recorded_at": terminal["recorded_at"],
    }
    index_path = root / "runs.jsonl"
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(index_entry, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return {
        **terminal,
        "manifest_path": str(manifest_path),
        "manifest_sha256": digest,
        "index_path": str(index_path),
    }


def _run_codex_invocation(
    *,
    context: Mapping[str, Any],
    target_root: Path,
    trusted_project_root: Path | None = None,
    run_dir: Path,
    index: int,
    label: str,
    prompt: str,
    timeout_seconds: float,
    process_runner: ProcessRunner,
    resume_session_id: str | None = None,
) -> dict[str, Any]:
    directory = run_dir / "codex" / f"{index:02d}-{label}"
    directory.mkdir(parents=True, exist_ok=False)
    prompt_path = directory / "prompt.txt"
    stdout_path = directory / "stdout.jsonl"
    stderr_path = directory / "stderr.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    argv = build_direct_codex_argv(
        context,
        resume_session_id=resume_session_id,
        trusted_project_root=trusted_project_root,
    )
    result = _call_runner(
        process_runner,
        argv,
        cwd=target_root,
        timeout_seconds=timeout_seconds,
        stdin_text=prompt,
    )
    stdout = redact_secret_text(str(result.get("stdout") or ""))
    stderr = redact_secret_text(str(result.get("stderr") or ""))
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    evidence = parse_codex_jsonl_evidence(stdout)
    return {
        "index": index,
        "label": label,
        "session_mode": "resume" if resume_session_id else "new",
        "resume_session_id": str(resume_session_id or ""),
        "argv": [redact_secret_text(str(item)) for item in argv],
        "cwd": str(target_root),
        "exit_code": int(result.get("exit_code") or 0),
        "elapsed_seconds": float(result.get("elapsed_seconds") or 0.0),
        "timed_out": bool(result.get("timed_out")),
        "prompt_path": _relative(run_dir, prompt_path),
        "prompt_sha256": _sha256_file(prompt_path),
        "stdout_jsonl_path": _relative(run_dir, stdout_path),
        "stdout_jsonl_sha256": _sha256_file(stdout_path),
        "stderr_path": _relative(run_dir, stderr_path),
        "stderr_sha256": _sha256_file(stderr_path),
        "stderr_tail": stderr[-2000:],
        "jsonl": evidence,
    }


def _run_operator_gate_phase(
    *,
    context: Mapping[str, Any],
    context_root: Path,
    task_id: str,
    target_root: Path,
    run_dir: Path,
    phase: str,
    timeout_seconds: float,
    process_runner: ProcessRunner,
) -> dict[str, Any]:
    task_record = _context_task(context, task_id)
    raw_gates = task_record.get("operator_gates") if isinstance(task_record.get("operator_gates"), list) else []
    gates: list[dict[str, Any]] = []
    for gate in raw_gates:
        if not isinstance(gate, dict):
            continue
        phases = [str(item) for item in (gate.get("phases") or ["first"])]
        if phase in phases or (phase == "baseline" and "baseline" not in phases and "first" in phases):
            gates.append(gate)
    if not gates:
        raise BenchmarkSetupError(f"No frozen operator gates apply to {task_id} phase {phase}")
    phase_dir = run_dir / "verification" / phase
    phase_dir.mkdir(parents=True, exist_ok=False)
    records: list[dict[str, Any]] = []
    for index, gate in enumerate(gates, start=1):
        gate_id = str(gate.get("id") or f"gate-{index}")
        safe_gate_id = re.sub(r"[^A-Za-z0-9._-]+", "-", gate_id).strip("-") or f"gate-{index}"
        directory = phase_dir / f"{index:02d}-{safe_gate_id}"
        directory.mkdir(parents=True, exist_ok=False)
        command = _render_gate_command(
            gate,
            context_root=context_root,
            target_root=target_root,
        )
        cwd_kind = str(gate.get("cwd") or "target")
        cwd = context_root if cwd_kind == "context" else target_root
        result = _call_runner(
            process_runner,
            command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            shell=True,
        )
        stdout = redact_secret_text(str(result.get("stdout") or ""))
        stderr = redact_secret_text(str(result.get("stderr") or ""))
        stdout_path = directory / "stdout.txt"
        stderr_path = directory / "stderr.txt"
        command_path = directory / "command.txt"
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        command_path.write_text(redact_secret_text(command), encoding="utf-8")
        exit_code = int(result.get("exit_code") if result.get("exit_code") is not None else -1)
        records.append(
            {
                "id": gate_id,
                "operator_only": bool(gate.get("operator_only", True)),
                "command": redact_secret_text(command),
                "cwd": str(cwd),
                "exit_code": exit_code,
                "verdict": "PASS" if exit_code == 0 else "FAIL",
                "elapsed_seconds": float(result.get("elapsed_seconds") or 0.0),
                "timed_out": bool(result.get("timed_out")),
                "command_path": _relative(run_dir, command_path),
                "command_sha256": _sha256_file(command_path),
                "stdout_path": _relative(run_dir, stdout_path),
                "stdout_sha256": _sha256_file(stdout_path),
                "stderr_path": _relative(run_dir, stderr_path),
                "stderr_sha256": _sha256_file(stderr_path),
                "stdout_tail": stdout[-4000:],
                "stderr_tail": stderr[-4000:],
            }
        )
    return {
        "phase": phase,
        "verdict": "PASS" if all(record["verdict"] == "PASS" for record in records) else "FAIL",
        "gates": records,
    }


def _build_repair_prompt(
    task: Mapping[str, Any],
    verification: Mapping[str, Any],
    *,
    context_root: Path,
    target_root: Path,
) -> str:
    repair = task.get("repair_injection") if isinstance(task.get("repair_injection"), dict) else {}
    evidence_chunks: list[str] = []
    for gate in verification.get("gates") or []:
        if not isinstance(gate, dict) or gate.get("verdict") != "FAIL":
            continue
        output = "\n".join(
            item for item in (str(gate.get("stdout_tail") or ""), str(gate.get("stderr_tail") or "")) if item
        )
        if output:
            evidence_chunks.append(output)
    evidence = redact_secret_text("\n\n".join(evidence_chunks))
    evidence = evidence.replace(str(context_root), "[operator-context]").replace(str(target_root), "[target]")
    evidence = _tail_utf8(evidence, 32768)
    lines = [
        "Repair the implementation so the operator verification failure is resolved.",
        "Continue in the same target and preserve useful diagnostic context.",
    ]
    additional = str(repair.get("additional_acceptance") or "").strip()
    if additional:
        lines.extend(["Additional acceptance requirement:", additional])
    if evidence:
        lines.extend(["Operator verification evidence:", evidence])
    return "\n".join(lines)


def _aggregate_invocation_usage(invocations: list[dict[str, Any]]) -> dict[str, Any]:
    keys = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    buckets: dict[str, dict[str, int]] = {}
    session_ids: list[str] = []
    turns: list[dict[str, Any]] = []
    invalid_line_count = 0
    for invocation in invocations:
        evidence = invocation.get("jsonl") if isinstance(invocation.get("jsonl"), dict) else {}
        for session_id in evidence.get("session_ids") or []:
            value = str(session_id or "")
            if value and value not in session_ids:
                session_ids.append(value)
        usage = evidence.get("usage") if isinstance(evidence.get("usage"), dict) else {}
        session_id = str(evidence.get("session_id") or "").strip()
        bucket_key = "session:" + session_id if session_id else f"invocation:{invocation.get('index')}"
        bucket = buckets.setdefault(bucket_key, {key: 0 for key in keys})
        for key in keys:
            bucket[key] = max(bucket[key], int(usage.get(key) or 0))
        for turn in evidence.get("turns") or []:
            if isinstance(turn, dict):
                turns.append({"invocation": invocation.get("index"), **turn})
        invalid_line_count += int(evidence.get("invalid_line_count") or 0)
    totals = {key: sum(bucket[key] for bucket in buckets.values()) for key in keys}
    return {
        "status": "AVAILABLE" if turns else "UNAVAILABLE",
        "session_ids": session_ids,
        "turns": turns,
        **totals,
        "num_turns": len(turns),
        "invalid_line_count": invalid_line_count,
    }


def _derive_operator_gates(
    task: Mapping[str, Any],
    verifier: Mapping[str, str],
    operator_verifier: Mapping[str, Any],
) -> list[dict[str, Any]]:
    verification = task.get("verification") if isinstance(task.get("verification"), dict) else {}
    gates: list[dict[str, Any]] = []
    for index, command in enumerate(verification.get("commands") or [], start=1):
        if str(command).strip():
            gates.append(
                {
                    "id": f"command-{index}",
                    "command": str(command).strip(),
                    "cwd": "target",
                    "phases": ["first", "final"],
                    "operator_only": True,
                }
            )
    for index, command in enumerate(verification.get("round_one_commands") or [], start=1):
        if str(command).strip():
            gates.append(
                {
                    "id": f"round-one-{index}",
                    "command": str(command).strip(),
                    "cwd": "target",
                    "phases": ["first"],
                    "operator_only": True,
                }
            )
    for index, command in enumerate(verification.get("final_commands") or [], start=1):
        if str(command).strip():
            gates.append(
                {
                    "id": f"final-{index}",
                    "command": str(command).strip(),
                    "cwd": "target",
                    "phases": ["final"],
                    "operator_only": True,
                }
            )
    repair = task.get("repair_injection") if isinstance(task.get("repair_injection"), dict) else {}
    private_command = str(repair.get("command_template") or "").strip()
    if private_command:
        gates.append(
            {
                "id": "private-verifier",
                "command": private_command,
                "cwd": "context",
                "phases": ["first", "final"],
                "operator_only": True,
                "artifact_source": str(verifier.get("source") or ""),
                "artifact_path": str(verifier.get("path") or ""),
                "artifact_sha256": str(verifier.get("sha256") or ""),
            }
        )
    if operator_verifier:
        gates.append(
            {
                "id": "operator-verifier",
                "command": str(
                    operator_verifier.get("command")
                    or "python {operator_artifact} {target_root}"
                ),
                "cwd": "context",
                "phases": list(operator_verifier.get("phases") or ["first", "final"]),
                "operator_only": True,
                "artifact_source": str(operator_verifier.get("source") or ""),
                "artifact_path": str(operator_verifier.get("path") or ""),
                "artifact_sha256": str(operator_verifier.get("sha256") or ""),
            }
        )
    return gates


def _freeze_operator_verifier(
    *,
    task_id: str,
    task: Mapping[str, Any],
    operator_root: Path,
    destination: Path,
) -> dict[str, Any]:
    verification = task.get("verification") if isinstance(task.get("verification"), dict) else {}
    top_level = task.get("operator_verification") if isinstance(task.get("operator_verification"), dict) else {}
    if top_level:
        source_text = str(top_level.get("verifier") or top_level.get("path") or "").strip()
        expected = str(top_level.get("verifier_sha256") or top_level.get("sha256") or "").lower()
        command = str(top_level.get("command_template") or top_level.get("command") or "").strip()
        phases = [str(item) for item in (top_level.get("phases") or ["first", "final"])]
        raw: Any = None
    else:
        raw = verification.get("operator_verifier")
    if not raw:
        if not top_level:
            return {}
    elif isinstance(raw, dict):
        source_text = str(raw.get("path") or raw.get("source") or raw.get("verifier") or "").strip()
        expected = str(raw.get("sha256") or verification.get("operator_verifier_sha256") or "").lower()
        command = str(raw.get("command_template") or raw.get("command") or "").strip()
        phases = [str(item) for item in (raw.get("phases") or ["first", "final"])]
    elif raw:
        source_text = str(raw).strip()
        expected = str(verification.get("operator_verifier_sha256") or "").lower()
        command = str(
            verification.get("operator_verifier_command_template")
            or verification.get("operator_verifier_command")
            or ""
        ).strip()
        phases = [str(item) for item in (verification.get("operator_verifier_phases") or ["first", "final"])]
    if not source_text or not expected:
        raise ValueError(f"Task {task_id} operator_verifier requires a path and sha256")
    source = _safe_child(operator_root, source_text)
    if _sha256_canonical_file(source) != expected:
        raise ValueError(f"Operator verifier changed while freezing {task_id}: {source}")
    frozen = destination / "operator_artifacts" / task_id / ("operator-" + source.name)
    frozen.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, frozen)
    return {
        "source": source_text,
        "path": _relative(destination, frozen),
        "sha256": expected,
        "command": command,
        "phases": phases,
    }


def _render_gate_command(gate: Mapping[str, Any], *, context_root: Path, target_root: Path) -> str:
    command = str(gate.get("command") or "").strip()
    if not command:
        raise BenchmarkSetupError("Operator gate command is empty")
    artifact_path = str(gate.get("artifact_path") or "").strip()
    artifact_source = str(gate.get("artifact_source") or "").strip()
    if artifact_path:
        artifact = _context_artifact(context_root, artifact_path)
        expected = str(gate.get("artifact_sha256") or "").lower()
        if expected and _sha256_canonical_file(artifact) != expected:
            raise BenchmarkSetupError(f"Frozen operator artifact hash mismatch: {artifact}")
        if artifact_source:
            command = command.replace(artifact_source, _quote_shell(artifact))
        command = command.replace("{operator_artifact}", _quote_shell(artifact))
    command = command.replace("{target_root}", _quote_shell(target_root))
    command = command.replace("{operator_root}", _quote_shell(context_root))
    return command


def _load_frozen_task(
    context: Mapping[str, Any], context_root: Path, task_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    record = _context_task(context, task_id)
    path = _context_artifact(context_root, str(record.get("definition_path") or ""))
    expected = str(record.get("definition_sha256") or "").lower()
    if not expected or _sha256_file(path) != expected:
        raise BenchmarkSetupError(f"Frozen task definition hash mismatch: {task_id}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or str(payload.get("id") or "") != task_id:
        raise BenchmarkSetupError(f"Frozen task definition is invalid: {task_id}")
    if str(payload.get("objective") or "") != str(record.get("objective") or ""):
        raise BenchmarkSetupError(f"Frozen task objective changed: {task_id}")
    return payload, record


def _context_task(context: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    tasks = context.get("tasks") if isinstance(context.get("tasks"), dict) else {}
    record = tasks.get(task_id)
    if not isinstance(record, dict):
        raise BenchmarkSetupError(f"Task is not present in frozen context: {task_id}")
    return record


def _freeze_files(
    source_root: Path,
    destination_root: Path,
    paths: Iterable[tuple[str, bool]],
    *,
    allow_empty: bool = False,
) -> list[dict[str, Any]]:
    destination_root.mkdir(parents=True, exist_ok=True)
    selected: dict[str, bool] = {}
    for raw, tracked in paths:
        normalized = str(raw).replace("\\", "/").strip().lstrip("/")
        if normalized:
            selected[normalized] = bool(tracked or selected.get(normalized))
    manifest: list[dict[str, Any]] = []
    for relative in sorted(selected):
        source = _safe_child(source_root, relative)
        if not source.exists() and not source.is_symlink():
            continue
        destination = destination_root / relative
        entry = _copy_snapshot_file(source, destination, relative)
        entry["tracked"] = selected[relative]
        manifest.append(entry)
    if not manifest and not allow_empty:
        raise ValueError(f"Target snapshot is empty: {source_root}")
    return manifest


def _freeze_declared_files(
    source_root: Path,
    destination_root: Path,
    expected_hashes: Mapping[str, Any],
) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    for raw_relative, raw_expected in sorted(expected_hashes.items()):
        relative = str(raw_relative).replace("\\", "/").strip().lstrip("/")
        source = _safe_child(source_root, relative)
        expected = str(raw_expected or "").lower()
        if _sha256_canonical_file(source) != expected:
            raise ValueError(f"Frozen fixture hash mismatch: {source}")
        entry = _copy_snapshot_file(source, destination_root / relative, relative)
        entry["tracked"] = True
        manifest.append(entry)
    if not manifest:
        raise ValueError(f"Fixture snapshot is empty: {source_root}")
    return manifest


def _copy_snapshot_file(source: Path, destination: Path, relative: str) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        link_target = os.readlink(source)
        destination.symlink_to(link_target)
        return {
            "path": relative,
            "kind": "symlink",
            "link_target": link_target,
            "sha256": hashlib.sha256(("symlink:" + link_target).encode("utf-8")).hexdigest(),
            "size": len(link_target.encode("utf-8")),
        }
    if not source.is_file():
        raise ValueError(f"Frozen snapshot entry is not a file: {source}")
    shutil.copy2(source, destination)
    return {
        "path": relative,
        "kind": "file",
        "sha256": _sha256_file(source),
        "size": source.stat().st_size,
        "mode": source.stat().st_mode & 0o777,
    }


def _verify_materialized_files(root: Path, manifest: Any) -> None:
    if not isinstance(manifest, list):
        raise BenchmarkSetupError("Frozen files manifest must be a list")
    for entry in manifest:
        if not isinstance(entry, dict):
            raise BenchmarkSetupError("Frozen files manifest entry must be an object")
        path = _safe_child(root, str(entry.get("path") or ""))
        if entry.get("kind") == "symlink":
            if not path.is_symlink() or os.readlink(path) != str(entry.get("link_target") or ""):
                raise BenchmarkSetupError(f"Materialized symlink mismatch: {path}")
            actual = hashlib.sha256(("symlink:" + os.readlink(path)).encode("utf-8")).hexdigest()
        else:
            if not path.is_file():
                raise BenchmarkSetupError(f"Materialized file is missing: {path}")
            actual = _sha256_file(path)
        if actual != str(entry.get("sha256") or "").lower():
            raise BenchmarkSetupError(f"Materialized file hash mismatch: {path}")


def _copy_manifest_files(source_root: Path, target_root: Path, manifest: list[dict[str, Any]]) -> None:
    for entry in manifest:
        relative = str(entry.get("path") or "")
        source = _safe_child(source_root, relative)
        destination = target_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if entry.get("kind") == "symlink":
            destination.symlink_to(os.readlink(source))
        else:
            shutil.copy2(source, destination)


def _call_runner(
    runner: ProcessRunner,
    command: list[str] | str,
    *,
    cwd: Path,
    timeout_seconds: float,
    stdin_text: str | None = None,
    shell: bool = False,
) -> dict[str, Any]:
    started = monotonic()
    result = runner(
        command,
        cwd=cwd,
        timeout_seconds=float(timeout_seconds),
        stdin_text=stdin_text,
        shell=shell,
    )
    if not isinstance(result, dict):
        raise BenchmarkSetupError("Injected process runner must return a dict")
    exit_value = result.get("exit_code", result.get("returncode", 0))
    normalized = dict(result)
    normalized["exit_code"] = int(exit_value if exit_value is not None else -1)
    normalized["stdout"] = str(result.get("stdout", result.get("stdout_tail", "")) or "")
    normalized["stderr"] = str(result.get("stderr", result.get("stderr_tail", "")) or "")
    normalized.setdefault("elapsed_seconds", round(monotonic() - started, 6))
    normalized.setdefault("timed_out", normalized["exit_code"] == 124)
    return normalized


def _require_success(result: Mapping[str, Any], action: str) -> None:
    if int(result.get("exit_code") if result.get("exit_code") is not None else -1) != 0:
        detail = str(result.get("stderr") or result.get("stdout") or "").strip()[:500]
        raise BenchmarkSetupError(f"Could not {action}: {detail or 'command failed'}")


def _git_text(
    runner: ProcessRunner,
    cwd: Path,
    command: list[str],
    *,
    required: bool = True,
) -> str:
    result = _call_runner(runner, command, cwd=cwd, timeout_seconds=60.0)
    if int(result.get("exit_code") or 0) != 0:
        if required:
            _require_success(result, "run git command")
        return ""
    value = str(result.get("stdout") or "").strip()
    if required and not value:
        raise BenchmarkSetupError(f"Git command returned no value: {' '.join(command)}")
    return value


def _git_nul_paths(runner: ProcessRunner, cwd: Path, command: list[str]) -> list[str]:
    result = _call_runner(runner, command, cwd=cwd, timeout_seconds=60.0)
    _require_success(result, "enumerate frozen target files")
    return _split_nul(str(result.get("stdout") or ""))


def _split_nul(value: str) -> list[str]:
    return [
        item.replace("\\", "/").strip().lstrip("/")
        for item in str(value or "").split("\0")
        if item.strip()
    ]


def _context_artifact(context_root: Path, relative: str) -> Path:
    if not str(relative or "").strip():
        raise BenchmarkSetupError("Frozen context artifact path is empty")
    try:
        path = _safe_child(context_root, relative)
    except ValueError as exc:
        raise BenchmarkSetupError(str(exc)) from exc
    if not path.exists() and not path.is_symlink():
        raise BenchmarkSetupError(f"Frozen context artifact is missing: {path}")
    return path


def _safe_child(root: Path, relative: str) -> Path:
    base = root.expanduser().resolve()
    text = str(relative or "").replace("\\", "/").strip()
    if not text or Path(text).is_absolute():
        raise ValueError(f"Unsafe relative path: {relative!r}")
    candidate = (base / text).resolve(strict=False)
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"Path escapes its root: {relative!r}") from exc
    return candidate


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_canonical_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _harness_source_manifest() -> dict[str, Any]:
    directory = Path(__file__).resolve().parent
    names = (
        "execution_benchmark_runner.py",
        "execution_benchmarks.py",
        "codex_exec.py",
        "security.py",
        "subprocess_window.py",
    )
    files = [{"name": name, "sha256": _sha256_file(directory / name)} for name in names]
    return {"files": files, "bundle_sha256": _canonical_sha256(files)}


def _write_json_exclusive(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _validate_id(value: str, *, field: str) -> None:
    if not _SAFE_ID.fullmatch(str(value or "")):
        raise ValueError(f"Unsafe {field}: {value!r}")


def _normalize_project_subdir(value: str | Path) -> str:
    text = str(value or ".").replace("\\", "/").strip().strip("/")
    if not text or text == ".":
        return "."
    if Path(text).is_absolute() or any(part in {"", ".", ".."} for part in text.split("/")):
        raise ValueError(f"Unsafe project_subdir: {value!r}")
    return text


def _quote_shell(path: str | Path) -> str:
    value = str(path)
    return subprocess.list2cmdline([value]) if os.name == "nt" else shlex.quote(value)


def _tail_utf8(value: str, max_bytes: int) -> str:
    encoded = str(value or "").encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return str(value or "")
    return encoded[-max_bytes:].decode("utf-8", errors="ignore")


def _text_line_count(path: Path) -> int:
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    if not data or b"\0" in data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
