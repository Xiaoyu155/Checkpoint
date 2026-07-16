from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import portalocker

from .codex_rollout_telemetry import RolloutSnapshot
from .subprocess_window import hidden_subprocess_kwargs


PROJECT_MARKERS = {
    ".git",
    "Cargo.toml",
    "go.mod",
    "package.json",
    "pubspec.yaml",
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
}
IGNORED_DISCOVERY_DIRS = {
    ".agent-workspace",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "build",
    "dist",
    "node_modules",
    "target",
    "vendor",
}
FORBIDDEN_ALTERNATE_PARTS = {
    ".checkpoint-worktrees",
    "archive",
    "backup",
    "copy",
    "temp",
    "tmp",
    "worktree",
    "备份",
    "副本",
}
MANAGED_PYTHON_ENV = "PACER_PYTHON"
MAX_MANAGED_PYTHON_ROOTS = 8
PYTHON_RUNTIME_CANDIDATES = (
    Path(".venv/Scripts/python.exe"),
    Path(".venv/bin/python"),
    Path("venv/Scripts/python.exe"),
    Path("venv/bin/python"),
)
PYTHON_PATH_COMMANDS = ("python", "python3")
PYTHON_PROBE_TIMEOUT_SECONDS = 2.0
LAUNCH_STATE_LOCK_TIMEOUT_SECONDS = 1.0
ORPHAN_RECONCILE_INTERVAL_SECONDS = 5.0
MAX_ORPHAN_RECONCILE_KEYS = 256
TASK_SOURCE_BASELINE_TRUST_POLICY_VERSION = 1
MAX_TRUSTED_TASK_SOURCE_BASELINES = 256
TASK_CONTRACT_TRUST_POLICY_VERSION = 1
MAX_TRUSTED_TASK_CONTRACTS = 256
PRELAUNCH_TASK_REQUIRED_ENV = "PACER_PRELAUNCH_TASK_REQUIRED"
PRELAUNCH_TASK_CONTRACT_DIGEST_ENV = "PACER_PRELAUNCH_TASK_CONTRACT_DIGEST"
PRELAUNCH_SOURCE_BASELINE_DIGEST_ENV = "PACER_PRELAUNCH_SOURCE_BASELINE_DIGEST"
LAUNCH_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_LAUNCH_STATE_THREAD_LOCKS_GUARD = threading.Lock()
_LAUNCH_STATE_THREAD_LOCKS: dict[str, threading.RLock] = {}
_ORPHAN_RECONCILE_GUARD = threading.Lock()
_ORPHAN_RECONCILE_LAST_CHECK: dict[tuple[str, str, int], float] = {}
_TASK_SOURCE_BASELINE_TRUST_SECRET = secrets.token_bytes(32)
_TASK_SOURCE_BASELINE_TRUST_LOCK = threading.Lock()
_TRUSTED_TASK_SOURCE_BASELINES: OrderedDict[
    tuple[str, str, str], tuple[str, str]
] = OrderedDict()
_TASK_CONTRACT_TRUST_SECRET = secrets.token_bytes(32)
_TASK_CONTRACT_TRUST_LOCK = threading.Lock()
_TRUSTED_TASK_CONTRACTS: OrderedDict[
    tuple[str, str, str], tuple[str, str, str]
] = OrderedDict()
_PYTHON_PROBE_CODE = (
    "import importlib.util,json,sys;"
    "print(json.dumps({'executable':sys.executable,'version':"
    "'.'.join(str(v) for v in sys.version_info[:3]),"
    "'pytest_available':importlib.util.find_spec('pytest') is not None}))"
)


def discover_pacer_runtime_roots(
    *,
    module_path: str | Path | None = None,
    max_parents: int = 6,
) -> list[Path]:
    """Return bounded, package-owned roots that may contain Pacer's venv."""
    source = Path(module_path or __file__).expanduser().resolve()
    roots: list[Path] = []
    for parent in list(source.parents)[: max(0, int(max_parents))]:
        source_checkout = (parent / "pyproject.toml").is_file() and (
            parent / "src" / "visual_agent"
        ).is_dir()
        repository_root = (parent / ".git").exists()
        if source_checkout or repository_root:
            roots.append(parent)
    return _unique_paths(roots, limit=MAX_MANAGED_PYTHON_ROOTS)


def resolve_python_runtime(
    repo_root: str | Path,
    *,
    known_roots: list[str | Path] | tuple[str | Path, ...] = (),
    pacer_executable: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    path_lookup: Callable[[str], str | None] | None = None,
    capability_probe: Callable[[Path], dict[str, Any]] | None = None,
    include_pacer_runtime_roots: bool = True,
) -> dict[str, Any]:
    """Resolve one managed Python without recursive environment discovery."""
    repo = Path(repo_root).expanduser().resolve()
    env = os.environ if environment is None else environment
    lookup = shutil.which if path_lookup is None else path_lookup
    explicit = str(env.get(MANAGED_PYTHON_ENV) or "").strip()

    candidate: Path | None = None
    source = "unavailable"
    trusted_venv = False
    owning_root: Path | None = None
    if explicit:
        candidate = _resolve_explicit_python(explicit, repo=repo, path_lookup=lookup)
        source = "environment"
        if candidate is None:
            return _unavailable_python_runtime(
                source=source,
                probe_status="invalid_explicit_path",
                bound_repo_root=repo,
            )
    else:
        roots = [repo, *[Path(item).expanduser().resolve() for item in known_roots]]
        if include_pacer_runtime_roots:
            roots.extend(discover_pacer_runtime_roots())
        for root in _unique_paths(roots, limit=MAX_MANAGED_PYTHON_ROOTS):
            for relative in PYTHON_RUNTIME_CANDIDATES:
                path = root / relative
                if path.is_file():
                    candidate = path
                    owning_root = root
                    source = "project_venv" if root == repo else "known_root_venv"
                    trusted_venv = True
                    break
            if candidate is not None:
                break
        if candidate is None and pacer_executable is not None:
            current = Path(pacer_executable).expanduser()
            if current.is_file() and current.name.lower().startswith("python"):
                candidate = _absolute_path(current)
                owning_root = candidate.parent.parent
                source = "pacer_launcher"
                trusted_venv = True
        if candidate is None:
            candidate = _resolve_path_python(lookup)
            if candidate is not None:
                source = "path"

    if candidate is None:
        return _unavailable_python_runtime(
            source="unavailable",
            probe_status="not_found",
            bound_repo_root=repo,
        )
    executable = _absolute_path(candidate)
    probe = capability_probe or _probe_python_runtime
    capability = probe(executable)
    return {
        "executable": str(executable),
        "source": source,
        "available": bool(capability.get("available")),
        "pytest_available": bool(capability.get("pytest_available")),
        "version": str(capability.get("version") or ""),
        "probe_status": str(capability.get("probe_status") or "failed"),
        "probe_elapsed_ms": int(capability.get("probe_elapsed_ms") or 0),
        "trusted_venv": trusted_venv,
        "root": str(owning_root) if owning_root is not None else "",
        "bound_repo_root": str(repo),
    }


def _probe_python_runtime(executable: Path) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [str(executable), "-I", "-c", _PYTHON_PROBE_CODE],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=PYTHON_PROBE_TIMEOUT_SECONDS,
            encoding="utf-8",
            errors="replace",
            **hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return {
            "available": False,
            "pytest_available": False,
            "probe_status": "timeout",
            "probe_elapsed_ms": _elapsed_ms(started),
        }
    except OSError:
        return {
            "available": False,
            "pytest_available": False,
            "probe_status": "launch_failed",
            "probe_elapsed_ms": _elapsed_ms(started),
        }
    if completed.returncode != 0:
        return {
            "available": False,
            "pytest_available": False,
            "probe_status": "nonzero_exit",
            "probe_elapsed_ms": _elapsed_ms(started),
        }
    try:
        line = next(line for line in reversed(completed.stdout.splitlines()) if line.strip())
        payload = json.loads(line)
    except (StopIteration, json.JSONDecodeError, TypeError):
        return {
            "available": False,
            "pytest_available": False,
            "probe_status": "invalid_output",
            "probe_elapsed_ms": _elapsed_ms(started),
        }
    return {
        "available": True,
        "pytest_available": bool(payload.get("pytest_available")),
        "version": str(payload.get("version") or ""),
        "probe_status": "ok",
        "probe_elapsed_ms": _elapsed_ms(started),
    }


def _resolve_explicit_python(
    raw: str,
    *,
    repo: Path,
    path_lookup: Callable[[str], str | None],
) -> Path | None:
    path = Path(raw).expanduser()
    if path.is_absolute() or path.parent != Path("."):
        candidate = path if path.is_absolute() else repo / path
        return _absolute_path(candidate) if candidate.is_file() else None
    resolved = path_lookup(raw)
    candidate = Path(resolved) if resolved else repo / path
    return _absolute_path(candidate) if candidate.is_file() else None


def _resolve_path_python(path_lookup: Callable[[str], str | None]) -> Path | None:
    for command in PYTHON_PATH_COMMANDS:
        resolved = path_lookup(command)
        if not resolved:
            continue
        candidate = Path(resolved).expanduser()
        if candidate.is_file() and candidate.name.lower().startswith("python"):
            return _absolute_path(candidate)
    return None


def _unavailable_python_runtime(
    *,
    source: str,
    probe_status: str,
    bound_repo_root: Path,
) -> dict[str, Any]:
    return {
        "executable": "",
        "source": source,
        "available": False,
        "pytest_available": False,
        "version": "",
        "probe_status": probe_status,
        "probe_elapsed_ms": 0,
        "trusted_venv": False,
        "root": "",
        "bound_repo_root": str(bound_repo_root),
    }


def _unique_paths(paths: list[Path], *, limit: int) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        resolved = path.expanduser().resolve()
        identity = os.path.normcase(str(resolved))
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(resolved)
        if len(unique) >= max(0, int(limit)):
            break
    return unique


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(str(path.expanduser())))


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))


def active_launch_path(workspace_root: str | Path) -> Path:
    return Path(workspace_root).expanduser().resolve() / "pacer_native" / "active_launch.json"


def launch_context_path(workspace_root: str | Path, launch_id: str) -> Path:
    launch = _validated_launch_id(launch_id)
    return (
        Path(workspace_root).expanduser().resolve()
        / "pacer_native"
        / "launch-contexts"
        / f"{launch}.json"
    )


def launch_liveness_path(workspace_root: str | Path, launch_id: str) -> Path:
    launch = _validated_launch_id(launch_id)
    return (
        Path(workspace_root).expanduser().resolve()
        / "pacer_native"
        / "liveness"
        / f"{launch}.json"
    )


def recovery_capsule_path(workspace_root: str | Path, launch_id: str) -> Path:
    launch = _validated_launch_id(launch_id)
    return (
        Path(workspace_root).expanduser().resolve()
        / "pacer_native"
        / "recovery"
        / f"{launch}.json"
    )


def _validated_launch_id(value: Any) -> str:
    launch_id = str(value or "").strip()
    if not LAUNCH_ID_PATTERN.fullmatch(launch_id):
        raise ValueError("Pacer launch_id must contain only ASCII letters, digits, '_' or '-'")
    return launch_id


def _launch_manifest_path(workspace_root: str | Path, launch_id: str) -> Path:
    workspace = Path(workspace_root).expanduser().resolve()
    return workspace / "pacer_native" / "launches" / f"{_validated_launch_id(launch_id)}.json"


@contextmanager
def _launch_state_transaction(workspace_root: str | Path) -> Iterator[None]:
    workspace = Path(workspace_root).expanduser().resolve()
    lock_path = workspace / "pacer_native" / ".launch-state.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _workspace_thread_lock(workspace):
        with portalocker.Lock(
            str(lock_path),
            mode="a+b",
            timeout=LAUNCH_STATE_LOCK_TIMEOUT_SECONDS,
            check_interval=0.01,
        ):
            yield


def _workspace_thread_lock(workspace: Path) -> threading.RLock:
    identity = os.path.normcase(str(workspace))
    with _LAUNCH_STATE_THREAD_LOCKS_GUARD:
        return _LAUNCH_STATE_THREAD_LOCKS.setdefault(identity, threading.RLock())


def initialize_active_launch(
    *,
    workspace_root: str | Path,
    manifest_path: str | Path,
    launch: dict[str, Any],
) -> dict[str, Any]:
    workspace = Path(workspace_root).expanduser().resolve()
    launch_id = _validated_launch_id(launch.get("launch_id"))
    expected_manifest = _launch_manifest_path(workspace, launch_id)
    supplied_manifest = Path(manifest_path).expanduser().resolve()
    if os.path.normcase(str(supplied_manifest)) != os.path.normcase(str(expected_manifest)):
        raise ValueError(
            "Pacer launch manifest must be workspace/pacer_native/launches/<launch_id>.json"
        )
    launch_cwd = Path(str(launch.get("repo_root") or ".")).expanduser().resolve()
    process_cwd = Path(str(launch.get("process_cwd") or launch_cwd)).expanduser().resolve()
    effective_repo_root = Path(
        str(launch.get("effective_repo_root") or launch_cwd)
    ).expanduser().resolve()
    launch_goal = str(launch.get("launch_goal") or launch.get("goal") or "").strip()[:2000]
    known_project_roots = discover_existing_project_roots(launch_cwd)
    runtime = launch.get("runtime") if isinstance(launch.get("runtime"), dict) else {}
    python_runtime = runtime.get("python") if isinstance(runtime.get("python"), dict) else {}
    if not python_runtime:
        runtime = {"python": resolve_python_runtime(launch_cwd)}
    else:
        runtime = {
            **runtime,
            "python": {
                **python_runtime,
                "bound_repo_root": str(python_runtime.get("bound_repo_root") or launch_cwd),
            },
        }
    liveness = launch.get("liveness") if isinstance(launch.get("liveness"), dict) else {}
    payload = {
        "schema_version": 1,
        "launch_id": launch_id,
        "status": "running",
        "started_at": _normalized_started_at(launch.get("started_at")),
        "launcher_pid": int(launch.get("launcher_pid") or os.getpid()),
        "workspace_root": str(workspace),
        "launch_cwd": str(launch_cwd),
        "project_root": str(launch_cwd),
        "process_cwd": str(process_cwd),
        "effective_repo_root": str(effective_repo_root),
        "rollout_ownership": (
            dict(launch["rollout_ownership"])
            if isinstance(launch.get("rollout_ownership"), dict)
            else {"scheme": "legacy_cwd_time", "required": False}
        ),
        "launch_goal": launch_goal,
        "current_goal": launch_goal,
        "query_goal": "",
        "manifest_path": str(expected_manifest),
        "auto_compact_token_limit": int(launch.get("auto_compact_token_limit") or 0),
        "known_project_roots": [str(path) for path in known_project_roots],
        "runtime": runtime,
        "liveness": {
            "schema_version": 1,
            "state": str(liveness.get("state") or "idle"),
            "monitoring": bool(liveness.get("monitoring")),
            "lifecycle_status": "running",
            **liveness,
        },
        "source_responsibility": {
            "mode": "in_place",
            "project_existed_at_launch": True,
            "alternate_directory_authorized": False,
            "binding_reason": "launch_cwd",
        },
        "pillars": {
            "routing": {"active": False, "state": "not_observed"},
            "memory": {"active": False, "state": "not_loaded"},
            "managed": {"active": False, "state": "launch_started"},
            "acceptance": {"active": False, "state": "not_verified"},
            "dogfood": {"active": False, "state": "project_not_bound"},
        },
    }
    with _launch_state_transaction(workspace):
        _write_json(launch_liveness_path(workspace, launch_id), dict(payload["liveness"]) | {"launch_id": launch_id})
        current = _read_json(active_launch_path(workspace))
        make_active = not current or _launch_order_key(payload) >= _launch_order_key(current)
        _write_active_launch_unlocked(workspace, payload, make_active=make_active)
    return payload


def discover_existing_project_roots(root: str | Path, *, max_depth: int = 4, max_dirs: int = 2000) -> list[Path]:
    base = Path(root).expanduser().resolve()
    roots = {base}
    visited = 0
    for current, dirs, files in os.walk(base):
        visited += 1
        if visited > max_dirs:
            break
        current_path = Path(current)
        try:
            depth = len(current_path.relative_to(base).parts)
        except ValueError:
            continue
        dirs[:] = [
            name
            for name in dirs
            if name not in IGNORED_DISCOVERY_DIRS
            and not name.endswith(".checkpoint-worktrees")
        ]
        if depth >= max_depth:
            dirs[:] = []
        names = set(dirs) | set(files)
        if names & PROJECT_MARKERS:
            roots.add(current_path.resolve())
    return sorted(roots, key=lambda path: (len(path.parts), os.path.normcase(str(path))))


def read_active_launch(workspace_root: str | Path, *, launch_id: str = "") -> dict[str, Any]:
    workspace = Path(workspace_root).expanduser().resolve()
    selected_value = str(launch_id or os.environ.get("PACER_LAUNCH_ID") or "").strip()
    selected_id = _validated_launch_id(selected_value) if selected_value else ""
    path = launch_context_path(workspace, selected_id) if selected_id else active_launch_path(workspace)
    payload = _read_json(path)
    if not payload:
        return {}
    try:
        payload_launch_id = _validated_launch_id(payload.get("launch_id"))
    except ValueError:
        return {}
    if selected_id and payload_launch_id != selected_id:
        return {}
    payload["manifest_path"] = str(_launch_manifest_path(workspace, payload_launch_id))
    embedded_liveness = payload.get("liveness") if isinstance(payload.get("liveness"), dict) else {}
    sidecar_liveness = read_launch_liveness(workspace, payload_launch_id)
    liveness = {**embedded_liveness, **sidecar_liveness}
    lifecycle_status = str(payload.get("status") or "")
    if lifecycle_status and lifecycle_status != "running":
        stopped_at = str(
            embedded_liveness.get("stopped_at")
            or payload.get("completed_at")
            or sidecar_liveness.get("stopped_at")
            or ""
        )
        liveness.update(
            {
                "monitoring": False,
                "lifecycle_status": lifecycle_status,
            }
        )
        if stopped_at:
            liveness["stopped_at"] = stopped_at
    if liveness:
        payload["liveness"] = liveness
    return payload


def read_reconciled_active_launch(
    workspace_root: str | Path,
    *,
    launch_id: str = "",
    process_probe: Callable[[int], bool] | None = None,
    reconcile_interval_seconds: float = ORPHAN_RECONCILE_INTERVAL_SECONDS,
    monotonic_clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Read one launch and rate-limit a non-destructive orphan check."""
    workspace = Path(workspace_root).expanduser().resolve()
    launch = (
        read_active_launch(workspace, launch_id=launch_id)
        if launch_id
        else _active_pointer_context(workspace)
    )
    return _reconcile_launch_payload(
        workspace,
        launch,
        process_probe=process_probe,
        reconcile_interval_seconds=reconcile_interval_seconds,
        monotonic_clock=monotonic_clock,
    )


def read_launch_liveness(workspace_root: str | Path, launch_id: str) -> dict[str, Any]:
    launch = _validated_launch_id(launch_id)
    payload = _read_json(launch_liveness_path(workspace_root, launch))
    if str(payload.get("launch_id") or launch) != launch:
        return {}
    return payload


def write_launch_liveness(
    workspace_root: str | Path,
    launch_id: str,
    liveness: dict[str, Any],
) -> dict[str, Any]:
    workspace = Path(workspace_root).expanduser().resolve()
    launch = _validated_launch_id(launch_id)
    payload = {**dict(liveness), "launch_id": launch}
    with _launch_state_transaction(workspace):
        _write_json(launch_liveness_path(workspace, launch), payload)
    return payload


def write_active_launch(
    workspace_root: str | Path,
    payload: dict[str, Any],
    *,
    make_active: bool = False,
) -> None:
    workspace = Path(workspace_root).expanduser().resolve()
    with _launch_state_transaction(workspace):
        _write_active_launch_unlocked(workspace, payload, make_active=make_active)


def _write_active_launch_unlocked(
    workspace: Path,
    payload: dict[str, Any],
    *,
    make_active: bool = False,
) -> None:
    launch_id = _validated_launch_id(payload.get("launch_id"))
    stored = dict(payload)
    stored["launch_id"] = launch_id
    stored["manifest_path"] = str(_launch_manifest_path(workspace, launch_id))
    _write_json(launch_context_path(workspace, launch_id), stored)
    path = active_launch_path(workspace)
    current = _read_json(path)
    if not make_active and current and str(current.get("launch_id") or "") != launch_id:
        _sync_launch_manifest(workspace, stored)
        return
    _write_json(path, stored)
    _sync_launch_manifest(workspace, stored)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def find_active_launch(
    *,
    repo_root: str | Path,
    suggested_workspace: str | Path | None = None,
    preferred_launch_id: str = "",
    process_probe: Callable[[int], bool] | None = None,
    reconcile_interval_seconds: float = ORPHAN_RECONCILE_INTERVAL_SECONDS,
) -> tuple[Path | None, dict[str, Any]]:
    repo = Path(repo_root).expanduser().resolve()
    preferred_value = str(preferred_launch_id or os.environ.get("PACER_LAUNCH_ID") or "").strip()
    try:
        preferred = _validated_launch_id(preferred_value) if preferred_value else ""
    except ValueError:
        preferred = ""
    candidates: list[Path] = []
    if suggested_workspace is not None:
        candidates.append(Path(suggested_workspace).expanduser().resolve())
    candidates.append(repo / ".agent-workspace")
    candidates.extend(parent / ".agent-workspace" for parent in (repo, *repo.parents))

    matches: list[tuple[float, str, Path, dict[str, Any]]] = []
    seen: set[str] = set()
    for workspace in candidates:
        identity = os.path.normcase(str(workspace))
        if identity in seen:
            continue
        seen.add(identity)
        if preferred:
            selected = read_active_launch(workspace, launch_id=preferred)
            selected = _reconcile_launch_payload(
                workspace,
                selected,
                process_probe=process_probe,
                reconcile_interval_seconds=reconcile_interval_seconds,
            )
            if _launch_matches_repo(selected, repo):
                return workspace, selected
            # A valid caller-supplied launch ID is an ownership boundary. Search
            # every candidate workspace for that exact ID, but never substitute
            # a different active launch when it is absent or belongs elsewhere.
            continue
        active = _active_pointer_context(workspace)
        active = _reconcile_launch_payload(
            workspace,
            active,
            process_probe=process_probe,
            reconcile_interval_seconds=reconcile_interval_seconds,
        )
        if not _launch_matches_repo(active, repo):
            active = _latest_running_context(
                workspace,
                repo,
                process_probe=process_probe,
                reconcile_interval_seconds=reconcile_interval_seconds,
            )
        if active:
            started_at, launch_id = _launch_order_key(active)
            matches.append((started_at, launch_id, workspace, active))
    if not matches:
        return None, {}
    _, _, workspace, active = max(matches, key=lambda item: (item[0], item[1]))
    return workspace, active


def _active_pointer_context(workspace: Path) -> dict[str, Any]:
    pointer = _read_json(active_launch_path(workspace))
    try:
        launch_id = _validated_launch_id(pointer.get("launch_id"))
    except ValueError:
        return {}
    return read_active_launch(workspace, launch_id=launch_id)


def _latest_running_context(
    workspace: Path,
    repo: Path,
    *,
    process_probe: Callable[[int], bool] | None,
    reconcile_interval_seconds: float,
) -> dict[str, Any]:
    directory = workspace / "pacer_native" / "launch-contexts"
    matches: list[dict[str, Any]] = []
    try:
        paths = list(directory.glob("*.json"))
    except OSError:
        return {}
    for path in paths:
        try:
            launch_id = _validated_launch_id(path.stem)
        except ValueError:
            continue
        launch = read_active_launch(workspace, launch_id=launch_id)
        if str(launch.get("launch_id") or "") != launch_id:
            continue
        if _launch_matches_repo(launch, repo):
            matches.append(launch)
    for launch in sorted(matches, key=_launch_order_key, reverse=True):
        reconciled = _reconcile_launch_payload(
            workspace,
            launch,
            process_probe=process_probe,
            reconcile_interval_seconds=reconcile_interval_seconds,
        )
        if _launch_matches_repo(reconciled, repo):
            return reconciled
    return {}


def _launch_matches_repo(launch: dict[str, Any], repo: Path) -> bool:
    if str(launch.get("status") or "") != "running":
        return False
    raw_launch_cwd = str(launch.get("launch_cwd") or "").strip()
    if not raw_launch_cwd:
        return False
    launch_cwd = Path(raw_launch_cwd).expanduser().resolve()
    # Match the owning launch before validating the requested repository.
    # Otherwise a newly created replacement directory could hide the parent
    # launch and silently fall back to an unconstrained nested workspace.
    return _is_within(repo, launch_cwd)


def bind_active_project(
    *,
    workspace_root: str | Path,
    repo_root: str | Path,
    reason: str,
    launch_id: str = "",
) -> dict[str, Any]:
    workspace = Path(workspace_root).expanduser().resolve()
    repo = Path(repo_root).expanduser().resolve()
    selected_launch_id = _validated_launch_id(launch_id) if launch_id else ""
    with _launch_state_transaction(workspace):
        active = read_active_launch(workspace, launch_id=selected_launch_id)
        if not active:
            return {}
        launch_cwd = Path(str(active.get("launch_cwd") or ".")).expanduser().resolve()
        current = Path(str(active.get("project_root") or launch_cwd)).expanduser().resolve()
        if not repo.is_dir() or not _is_within(repo, launch_cwd):
            raise ValueError("Pacer project root must be an existing directory inside the launch directory")
        known = {
            os.path.normcase(str(Path(str(item)).expanduser().resolve()))
            for item in active.get("known_project_roots") or []
        }
        if os.path.normcase(str(repo)) not in known:
            raise ValueError(
                "Pacer refuses to bind a directory that was not an existing project at launch; "
                "edit the source project in place instead of creating a replacement folder"
            )
        if current != launch_cwd and current != repo:
            raise ValueError(f"Pacer project is already bound to {current}; repository drift is not allowed")
        relative_parts = {part.lower() for part in repo.relative_to(launch_cwd).parts}
        if any(any(marker in part for marker in FORBIDDEN_ALTERNATE_PARTS) for part in relative_parts):
            raise ValueError("Pacer refuses worktree, backup, copy, archive, or temporary project roots by default")

        active["project_root"] = str(repo)
        active["project_bound_at"] = datetime.now(timezone.utc).isoformat()
        responsibility = (
            active.get("source_responsibility")
            if isinstance(active.get("source_responsibility"), dict)
            else {}
        )
        active["source_responsibility"] = {
            **responsibility,
            "mode": "in_place",
            "project_existed_at_launch": True,
            "alternate_directory_authorized": False,
            "binding_reason": str(reason or "pacer_request"),
        }
        pillars = active.get("pillars") if isinstance(active.get("pillars"), dict) else {}
        managed = pillars.get("managed") if isinstance(pillars.get("managed"), dict) else {}
        if not bool(managed.get("active")):
            pillars["managed"] = {
                "active": False,
                "state": "ready_in_place",
                "mode": "native_codex_in_place",
                "project_root": str(repo),
            }
        dogfood = pillars.get("dogfood") if isinstance(pillars.get("dogfood"), dict) else {}
        if not bool(dogfood.get("active")):
            pillars["dogfood"] = {
                "active": False,
                "state": "source_contract_ready",
                "source_mode": "in_place",
                "project_existed_at_launch": True,
                "alternate_directory_authorized": False,
                "project_root": str(repo),
            }
        active["pillars"] = pillars
        _write_active_launch_unlocked(workspace, active)
        return active


def update_active_launch(
    workspace_root: str | Path,
    *,
    expected_launch_id: str = "",
    **updates: Any,
) -> dict[str, Any]:
    workspace = Path(workspace_root).expanduser().resolve()
    selected_launch_id = _validated_launch_id(expected_launch_id) if expected_launch_id else ""
    with _launch_state_transaction(workspace):
        active = read_active_launch(workspace, launch_id=selected_launch_id)
        if not active:
            return {}
        if selected_launch_id and str(active.get("launch_id") or "") != selected_launch_id:
            return active
        proposed_updates = dict(updates)
        if "launch_goal" in proposed_updates:
            proposed_goal = str(proposed_updates.get("launch_goal") or "").strip()[:2000]
            if str(active.get("launch_goal") or "").strip() or not proposed_goal:
                proposed_updates.pop("launch_goal", None)
            else:
                proposed_updates["launch_goal"] = proposed_goal
        for immutable_key in ("task_contract", "task_contract_digest", "task_contract_receipt"):
            if immutable_key in proposed_updates and active.get(immutable_key):
                proposed_updates.pop(immutable_key, None)
        active.update(proposed_updates)
        _write_active_launch_unlocked(workspace, active)
        return active


def register_completion_attempt(
    workspace_root: str | Path,
    *,
    launch_id: str,
    max_attempts: int,
) -> dict[str, Any]:
    """Atomically reserve one bounded completion attempt for a launch."""

    workspace = Path(workspace_root).expanduser().resolve()
    selected_launch_id = _validated_launch_id(launch_id)
    limit = max(1, int(max_attempts))
    with _launch_state_transaction(workspace):
        active = read_active_launch(workspace, launch_id=selected_launch_id)
        if not active or str(active.get("launch_id") or "") != selected_launch_id:
            raise ValueError("completion attempt requires the active Pacer launch")
        control = (
            dict(active.get("completion_control"))
            if isinstance(active.get("completion_control"), dict)
            else {}
        )
        attempt = int(control.get("attempts") or 0) + 1
        control.update(
            {
                "schema_version": 1,
                "attempts": attempt,
                "max_attempts": limit,
                "status": "running" if attempt <= limit else "attempts_exhausted",
                "retryable": attempt < limit,
                "last_attempt_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        active["completion_control"] = control
        _write_active_launch_unlocked(workspace, active)
        return dict(control)


def record_completion_rejection(
    workspace_root: str | Path,
    *,
    launch_id: str,
    reason_codes: list[str],
    retryable: bool,
) -> dict[str, Any]:
    workspace = Path(workspace_root).expanduser().resolve()
    selected_launch_id = _validated_launch_id(launch_id)
    with _launch_state_transaction(workspace):
        active = read_active_launch(workspace, launch_id=selected_launch_id)
        if not active or str(active.get("launch_id") or "") != selected_launch_id:
            raise ValueError("completion rejection requires the active Pacer launch")
        control = (
            dict(active.get("completion_control"))
            if isinstance(active.get("completion_control"), dict)
            else {}
        )
        normalized_codes = list(
            dict.fromkeys(str(code or "completion_rejected")[:120] for code in reason_codes)
        )[:12]
        control.update(
            {
                "status": "correction_required" if retryable else "attempts_exhausted",
                "retryable": bool(retryable),
                "last_rejection_codes": normalized_codes,
                "last_rejected_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        active["completion_control"] = control
        _write_active_launch_unlocked(workspace, active)
        return dict(control)


def update_pillar(
    workspace_root: str | Path,
    pillar: str,
    evidence: dict[str, Any],
    *,
    launch_id: str = "",
) -> dict[str, Any]:
    workspace = Path(workspace_root).expanduser().resolve()
    selected_launch_id = _validated_launch_id(launch_id) if launch_id else ""
    with _launch_state_transaction(workspace):
        active = read_active_launch(workspace, launch_id=selected_launch_id)
        if not active:
            return {}
        pillars = active.get("pillars") if isinstance(active.get("pillars"), dict) else {}
        normalized = dict(evidence)
        from .pacer_pillars import assess_pillar

        normalized["assessment"] = assess_pillar(str(pillar), normalized)
        pillars[str(pillar)] = normalized
        active["pillars"] = pillars
        _write_active_launch_unlocked(workspace, active)
        return active


def save_rollout_baseline(
    *,
    workspace_root: str | Path,
    launch_id: str,
    snapshot: RolloutSnapshot,
) -> Path:
    workspace = Path(workspace_root).expanduser().resolve()
    launch = _validated_launch_id(launch_id)
    path = workspace / "pacer_native" / "launches" / f"{launch}.rollout-baseline.json"
    payload = {
        "schema_version": 1,
        "sessions_root": str(snapshot.sessions_root),
        "captured_at": snapshot.captured_at,
        "files": snapshot.files,
    }
    with _launch_state_transaction(workspace):
        _write_json(path, payload)
        active = read_active_launch(workspace, launch_id=launch)
        if active and str(active.get("launch_id") or "") == launch:
            active["rollout_baseline_path"] = str(path)
            _write_active_launch_unlocked(workspace, active)
    return path


def load_rollout_baseline(
    active: dict[str, Any],
    *,
    workspace_root: str | Path | None = None,
) -> RolloutSnapshot | None:
    try:
        launch_id = _validated_launch_id(active.get("launch_id"))
    except ValueError:
        return None
    raw_workspace = workspace_root if workspace_root is not None else active.get("workspace_root")
    if not raw_workspace:
        return None
    workspace = Path(str(raw_workspace)).expanduser().resolve()
    recorded_workspace = str(active.get("workspace_root") or "").strip()
    if recorded_workspace and os.path.normcase(str(Path(recorded_workspace).expanduser().resolve())) != os.path.normcase(
        str(workspace)
    ):
        return None
    path = workspace / "pacer_native" / "launches" / f"{launch_id}.rollout-baseline.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        sessions_root = Path(str(payload["sessions_root"])).expanduser().resolve()
        files = {str(key): int(value) for key, value in dict(payload.get("files") or {}).items()}
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return RolloutSnapshot(
        sessions_root=sessions_root,
        captured_at=str(payload.get("captured_at") or ""),
        files=files,
    )


def save_task_source_baseline(
    *,
    workspace_root: str | Path,
    launch_id: str,
    baseline: dict[str, Any],
) -> Path:
    workspace = Path(workspace_root).expanduser().resolve()
    launch = _validated_launch_id(launch_id)
    path = workspace / "pacer_native" / "baselines" / f"{launch}.source.json"
    payload = dict(baseline)
    with _launch_state_transaction(workspace):
        _write_json(path, payload)
        active = read_active_launch(workspace, launch_id=launch)
        if active and str(active.get("launch_id") or "") == launch:
            active["source_baseline_path"] = str(path)
            active["source_baseline_kind"] = str(payload.get("kind") or "")
            active["source_baseline_complete"] = bool(payload.get("complete"))
            _write_active_launch_unlocked(workspace, active)
    return path


def task_source_baseline_path(
    workspace_root: str | Path,
    launch_id: str,
) -> Path:
    workspace = Path(workspace_root).expanduser().resolve()
    launch = _validated_launch_id(launch_id)
    return workspace / "pacer_native" / "baselines" / f"{launch}.source.json"


def task_source_baseline_digest(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        raise ValueError("task source baseline must be an object")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def register_trusted_task_source_baseline(
    payload: dict[str, Any],
    *,
    workspace_root: str | Path,
    launch_id: str,
    repo_root: str | Path,
) -> str:
    """Register one MCP-captured source baseline for this process only."""

    workspace_identity, launch, repo_identity = _task_source_baseline_trust_key(
        workspace_root=workspace_root,
        launch_id=launch_id,
        repo_root=repo_root,
    )
    _validate_task_source_baseline_repo(payload, repo_identity)
    digest = task_source_baseline_digest(payload)
    identity = "\0".join(
        (
            "pacer-task-source-baseline-receipt-v1",
            workspace_identity,
            launch,
            repo_identity,
            digest,
        )
    )
    receipt = hmac.new(
        _TASK_SOURCE_BASELINE_TRUST_SECRET,
        identity.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    key = (workspace_identity, launch, repo_identity)
    with _TASK_SOURCE_BASELINE_TRUST_LOCK:
        _TRUSTED_TASK_SOURCE_BASELINES[key] = (digest, receipt)
        _TRUSTED_TASK_SOURCE_BASELINES.move_to_end(key)
        while len(_TRUSTED_TASK_SOURCE_BASELINES) > MAX_TRUSTED_TASK_SOURCE_BASELINES:
            _TRUSTED_TASK_SOURCE_BASELINES.popitem(last=False)
    return receipt


def adopt_prelaunched_task_source_baseline(
    payload: dict[str, Any],
    *,
    workspace_root: str | Path,
    launch_id: str,
    repo_root: str | Path,
    prelaunch_digest: str,
    trusted_receipt: str = "",
) -> str:
    """Adopt launcher-pinned evidence into the current MCP process."""

    workspace_identity, launch, repo_identity = _task_source_baseline_trust_key(
        workspace_root=workspace_root,
        launch_id=launch_id,
        repo_root=repo_root,
    )
    _validate_task_source_baseline_repo(payload, repo_identity)
    digest = task_source_baseline_digest(payload)
    expected = _validated_sha256_digest(prelaunch_digest, "prelaunch source baseline")
    if not hmac.compare_digest(digest, expected):
        raise ValueError("prelaunch source baseline digest mismatch")
    receipt = _validated_or_new_trust_receipt(
        trusted_receipt,
        secret=_TASK_SOURCE_BASELINE_TRUST_SECRET,
        identity="\0".join(
            (
                "pacer-task-source-baseline-receipt-v1",
                workspace_identity,
                launch,
                repo_identity,
                digest,
            )
        ),
    )
    key = (workspace_identity, launch, repo_identity)
    with _TASK_SOURCE_BASELINE_TRUST_LOCK:
        _TRUSTED_TASK_SOURCE_BASELINES[key] = (digest, receipt)
        _TRUSTED_TASK_SOURCE_BASELINES.move_to_end(key)
        while len(_TRUSTED_TASK_SOURCE_BASELINES) > MAX_TRUSTED_TASK_SOURCE_BASELINES:
            _TRUSTED_TASK_SOURCE_BASELINES.popitem(last=False)
    return receipt


def trusted_task_source_baseline_errors(
    payload: dict[str, Any],
    *,
    workspace_root: str | Path | None,
    launch_id: str,
    repo_root: str | Path | None,
    trusted_digest: str = "",
    trusted_receipt: str = "",
) -> tuple[str, ...]:
    errors: list[str] = []
    if workspace_root is None:
        errors.append("trusted_source_baseline_workspace_required")
    if repo_root is None:
        errors.append("trusted_source_baseline_repo_required")
    if not trusted_digest:
        errors.append("trusted_source_baseline_digest_required")
    if not trusted_receipt:
        errors.append("trusted_source_baseline_receipt_required")
    if errors and (workspace_root is None or repo_root is None):
        return tuple(errors)
    try:
        workspace_identity, launch, repo_identity = _task_source_baseline_trust_key(
            workspace_root=workspace_root or ".",
            launch_id=launch_id,
            repo_root=repo_root or ".",
        )
    except ValueError:
        return tuple(dict.fromkeys([*errors, "trusted_source_baseline_identity_invalid"]))
    try:
        _validate_task_source_baseline_repo(payload, repo_identity)
        current_digest = task_source_baseline_digest(payload)
    except ValueError:
        return tuple(dict.fromkeys([*errors, "trusted_source_baseline_payload_invalid"]))

    key = (workspace_identity, launch, repo_identity)
    with _TASK_SOURCE_BASELINE_TRUST_LOCK:
        registered = _TRUSTED_TASK_SOURCE_BASELINES.get(key)
    if registered is None:
        return tuple(dict.fromkeys([*errors, "trusted_source_baseline_not_registered"]))
    registered_digest, registered_receipt = registered
    if trusted_digest and not hmac.compare_digest(str(trusted_digest), current_digest):
        errors.append("trusted_source_baseline_digest_mismatch")
    if not hmac.compare_digest(registered_digest, current_digest):
        errors.append("trusted_source_baseline_registered_digest_mismatch")
    if trusted_receipt and not hmac.compare_digest(registered_receipt, str(trusted_receipt)):
        errors.append("trusted_source_baseline_receipt_mismatch")
    return tuple(dict.fromkeys(errors))


def task_contract_digest(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        raise ValueError("task contract must be an object")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def register_trusted_task_contract(
    payload: dict[str, Any],
    *,
    goal: str,
    workspace_root: str | Path,
    launch_id: str,
    repo_root: str | Path,
) -> str:
    """Bind the immutable goal and task contract to this MCP process."""

    workspace_identity, launch, repo_identity = _task_source_baseline_trust_key(
        workspace_root=workspace_root,
        launch_id=launch_id,
        repo_root=repo_root,
    )
    goal_digest = _task_goal_digest(goal)
    _validate_task_contract(payload, goal_digest)
    digest = task_contract_digest(payload)
    identity = "\0".join(
        (
            "pacer-task-contract-receipt-v1",
            workspace_identity,
            launch,
            repo_identity,
            goal_digest,
            digest,
        )
    )
    receipt = hmac.new(
        _TASK_CONTRACT_TRUST_SECRET,
        identity.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    key = (workspace_identity, launch, repo_identity)
    with _TASK_CONTRACT_TRUST_LOCK:
        _TRUSTED_TASK_CONTRACTS[key] = (goal_digest, digest, receipt)
        _TRUSTED_TASK_CONTRACTS.move_to_end(key)
        while len(_TRUSTED_TASK_CONTRACTS) > MAX_TRUSTED_TASK_CONTRACTS:
            _TRUSTED_TASK_CONTRACTS.popitem(last=False)
    return receipt


def adopt_prelaunched_task_contract(
    payload: dict[str, Any],
    *,
    goal: str,
    workspace_root: str | Path,
    launch_id: str,
    repo_root: str | Path,
    prelaunch_digest: str,
    trusted_receipt: str = "",
) -> str:
    """Adopt a launcher-pinned contract into the current MCP process."""

    workspace_identity, launch, repo_identity = _task_source_baseline_trust_key(
        workspace_root=workspace_root,
        launch_id=launch_id,
        repo_root=repo_root,
    )
    goal_digest = _task_goal_digest(goal)
    _validate_task_contract(payload, goal_digest)
    digest = task_contract_digest(payload)
    expected = _validated_sha256_digest(prelaunch_digest, "prelaunch task contract")
    if not hmac.compare_digest(digest, expected):
        raise ValueError("prelaunch task contract digest mismatch")
    receipt = _validated_or_new_trust_receipt(
        trusted_receipt,
        secret=_TASK_CONTRACT_TRUST_SECRET,
        identity="\0".join(
            (
                "pacer-task-contract-receipt-v1",
                workspace_identity,
                launch,
                repo_identity,
                goal_digest,
                digest,
            )
        ),
    )
    key = (workspace_identity, launch, repo_identity)
    with _TASK_CONTRACT_TRUST_LOCK:
        _TRUSTED_TASK_CONTRACTS[key] = (goal_digest, digest, receipt)
        _TRUSTED_TASK_CONTRACTS.move_to_end(key)
        while len(_TRUSTED_TASK_CONTRACTS) > MAX_TRUSTED_TASK_CONTRACTS:
            _TRUSTED_TASK_CONTRACTS.popitem(last=False)
    return receipt


def trusted_task_contract_errors(
    payload: dict[str, Any],
    *,
    goal: str,
    workspace_root: str | Path | None,
    launch_id: str,
    repo_root: str | Path | None,
    trusted_digest: str = "",
    trusted_receipt: str = "",
) -> tuple[str, ...]:
    errors: list[str] = []
    if workspace_root is None:
        errors.append("trusted_task_contract_workspace_required")
    if repo_root is None:
        errors.append("trusted_task_contract_repo_required")
    if not trusted_digest:
        errors.append("trusted_task_contract_digest_required")
    if not trusted_receipt:
        errors.append("trusted_task_contract_receipt_required")
    if errors and (workspace_root is None or repo_root is None):
        return tuple(errors)
    try:
        workspace_identity, launch, repo_identity = _task_source_baseline_trust_key(
            workspace_root=workspace_root or ".",
            launch_id=launch_id,
            repo_root=repo_root or ".",
        )
        goal_digest = _task_goal_digest(goal)
        _validate_task_contract(payload, goal_digest)
        current_digest = task_contract_digest(payload)
    except ValueError:
        return tuple(dict.fromkeys([*errors, "trusted_task_contract_payload_invalid"]))

    key = (workspace_identity, launch, repo_identity)
    with _TASK_CONTRACT_TRUST_LOCK:
        registered = _TRUSTED_TASK_CONTRACTS.get(key)
    if registered is None:
        return tuple(dict.fromkeys([*errors, "trusted_task_contract_not_registered"]))
    registered_goal_digest, registered_digest, registered_receipt = registered
    if not hmac.compare_digest(registered_goal_digest, goal_digest):
        errors.append("trusted_task_contract_goal_mismatch")
    if trusted_digest and not hmac.compare_digest(str(trusted_digest), current_digest):
        errors.append("trusted_task_contract_digest_mismatch")
    if not hmac.compare_digest(registered_digest, current_digest):
        errors.append("trusted_task_contract_registered_digest_mismatch")
    if trusted_receipt and not hmac.compare_digest(registered_receipt, str(trusted_receipt)):
        errors.append("trusted_task_contract_receipt_mismatch")
    return tuple(dict.fromkeys(errors))


def _task_goal_digest(goal: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", str(goal or "")).split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validated_sha256_digest(value: str, label: str) -> str:
    digest = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"{label} digest is invalid")
    return digest


def _validated_or_new_trust_receipt(value: str, *, secret: bytes, identity: str) -> str:
    receipt = str(value or "").strip().lower()
    if receipt:
        if not re.fullmatch(r"[0-9a-f]{64}", receipt):
            raise ValueError("trusted receipt is invalid")
        return receipt
    return hmac.new(secret, identity.encode("utf-8"), hashlib.sha256).hexdigest()


def _validate_task_contract(payload: dict[str, Any], goal_digest: str) -> None:
    if not isinstance(payload, dict):
        raise ValueError("task contract must be an object")
    try:
        schema_version = int(payload.get("schema_version") or 0)
    except (TypeError, ValueError, OverflowError):
        schema_version = 0
    if schema_version not in {1, 2}:
        raise ValueError("task contract schema version mismatch")
    if not hmac.compare_digest(str(payload.get("goal_digest") or ""), goal_digest):
        raise ValueError("task contract goal digest mismatch")
    requirements = payload.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("task contract requirements missing")
    if schema_version >= 2:
        acceptance_contract = payload.get("acceptance_contract")
        if not isinstance(acceptance_contract, dict):
            raise ValueError("task acceptance contract missing")
        if int(acceptance_contract.get("schema_version") or 0) != 1:
            raise ValueError("task acceptance contract schema version mismatch")
        if not str(acceptance_contract.get("digest") or ""):
            raise ValueError("task acceptance contract digest missing")


def _task_source_baseline_trust_key(
    *,
    workspace_root: str | Path,
    launch_id: str,
    repo_root: str | Path,
) -> tuple[str, str, str]:
    workspace_identity = os.path.normcase(str(Path(workspace_root).expanduser().resolve()))
    launch = _validated_launch_id(launch_id)
    repo_identity = os.path.normcase(str(Path(repo_root).expanduser().resolve()))
    return workspace_identity, launch, repo_identity


def _validate_task_source_baseline_repo(payload: dict[str, Any], repo_identity: str) -> None:
    if not isinstance(payload, dict):
        raise ValueError("task source baseline must be an object")
    try:
        schema_version = int(payload.get("schema_version") or 0)
    except (TypeError, ValueError, OverflowError):
        schema_version = 0
    if schema_version != 1:
        raise ValueError("task source baseline schema version mismatch")
    if str(payload.get("kind") or "") not in {"git", "filesystem"}:
        raise ValueError("task source baseline kind mismatch")
    if not isinstance(payload.get("entries"), dict):
        raise ValueError("task source baseline entries must be an object")
    raw_repo = str(payload.get("repo_root") or "").strip()
    if not raw_repo:
        raise ValueError("task source baseline repo_root is required")
    recorded_identity = os.path.normcase(str(Path(raw_repo).expanduser().resolve()))
    if recorded_identity != repo_identity:
        raise ValueError("task source baseline repo_root mismatch")


def load_task_source_baseline(
    active: dict[str, Any],
    *,
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    try:
        launch_id = _validated_launch_id(active.get("launch_id"))
    except ValueError:
        return {}
    raw_workspace = workspace_root if workspace_root is not None else active.get("workspace_root")
    if not raw_workspace:
        return {}
    workspace = Path(str(raw_workspace)).expanduser().resolve()
    recorded_workspace = str(active.get("workspace_root") or "").strip()
    if recorded_workspace and os.path.normcase(
        str(Path(recorded_workspace).expanduser().resolve())
    ) != os.path.normcase(str(workspace)):
        return {}
    path = task_source_baseline_path(workspace, launch_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_context_recovery_capsule(
    workspace_root: str | Path,
    *,
    launch: dict[str, Any],
    telemetry: dict[str, Any],
) -> dict[str, Any]:
    try:
        launch_id = _validated_launch_id(launch.get("launch_id"))
    except ValueError:
        return {}
    current = telemetry.get("current_context_usage") if isinstance(telemetry.get("current_context_usage"), dict) else {}
    accumulated = telemetry.get("usage") if isinstance(telemetry.get("usage"), dict) else {}
    limit = int(launch.get("auto_compact_token_limit") or 0)
    current_input = int(current.get("input_tokens") or 0)
    if not launch_id or limit <= 0 or current_input < limit:
        return {}
    capsule = {
        "schema_version": 1,
        "status": "pending",
        "source_launch_id": launch_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reason": "abnormal_exit_at_or_above_context_limit",
        "project_root": str(launch.get("project_root") or launch.get("launch_cwd") or ""),
        "goal": str(launch.get("launch_goal") or launch.get("current_goal") or "")[:2000],
        "auto_compact_token_limit": limit,
        "current_context_usage": current,
        "accumulated_usage": accumulated,
        "compactions": telemetry.get("compactions") if isinstance(telemetry.get("compactions"), dict) else {},
        "pillars": launch.get("pillars") if isinstance(launch.get("pillars"), dict) else {},
    }
    with _launch_state_transaction(workspace_root):
        _write_json(recovery_capsule_path(workspace_root, launch_id), capsule)
    return capsule


def latest_pending_recovery_capsule(workspace_root: str | Path, *, repo_root: str | Path) -> dict[str, Any]:
    directory = Path(workspace_root).expanduser().resolve() / "pacer_native" / "recovery"
    expected = os.path.normcase(str(Path(repo_root).expanduser().resolve()))
    matches: list[dict[str, Any]] = []
    try:
        paths = directory.glob("*.json")
        for path in paths:
            payload = _read_json(path)
            if str(payload.get("status") or "") != "pending":
                continue
            project = os.path.normcase(str(Path(str(payload.get("project_root") or ".")).expanduser().resolve()))
            if project == expected:
                matches.append(payload)
    except OSError:
        return {}
    return max(matches, key=lambda item: _timestamp_value(str(item.get("created_at") or "")), default={})


def resolve_recovery_capsule(
    workspace_root: str | Path,
    *,
    source_launch_id: str,
    recovery_launch_id: str,
) -> dict[str, Any]:
    try:
        source = _validated_launch_id(source_launch_id)
        recovery = _validated_launch_id(recovery_launch_id)
    except ValueError:
        return {}
    if not source or not recovery or source == recovery:
        return {}
    path = recovery_capsule_path(workspace_root, source)
    with _launch_state_transaction(workspace_root):
        capsule = _read_json(path)
        if str(capsule.get("status") or "") != "pending":
            return capsule
        capsule.update(
            {
                "status": "resolved",
                "resolved_at": datetime.now(timezone.utc).isoformat(),
                "recovery_launch_id": recovery,
            }
        )
        _write_json(path, capsule)
        return capsule


def _reconcile_launch_payload(
    workspace: Path,
    launch: dict[str, Any],
    *,
    process_probe: Callable[[int], bool] | None,
    reconcile_interval_seconds: float,
    monotonic_clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    if str(launch.get("status") or "") != "running":
        return launch
    try:
        launch_id = _validated_launch_id(launch.get("launch_id"))
        launcher_pid = int(launch.get("launcher_pid") or 0)
    except (TypeError, ValueError):
        return launch
    if launcher_pid <= 0:
        return launch
    clock = monotonic_clock or time.monotonic
    if not _claim_orphan_reconcile_check(
        workspace,
        launch_id,
        launcher_pid,
        checked_at=float(clock()),
        interval_seconds=reconcile_interval_seconds,
    ):
        return launch

    if process_probe is None:
        from .pacer_events import process_exists

        probe = process_exists
    else:
        probe = process_probe
    try:
        launcher_exists = bool(probe(launcher_pid))
    except Exception:  # Probe failures must not mutate or hide a running launch.
        return launch
    if launcher_exists:
        return launch

    ended_at = datetime.now(timezone.utc).isoformat()
    try:
        committed, capsule = _commit_orphaned_launch(
            workspace,
            launch,
            ended_at=ended_at,
            orphaned_pid=launcher_pid,
        )
    except Exception:
        return _orphaned_projection(launch, ended_at=ended_at, orphaned_pid=launcher_pid)
    if not committed or not capsule:
        return read_active_launch(workspace, launch_id=launch_id)
    from .pacer_events import append_pacer_event

    append_pacer_event(
        workspace,
        "launch_orphaned",
        launch_id=launch_id,
        data={"launcher_pid": launcher_pid, "recovery_capsule": True},
    )
    return committed


def _claim_orphan_reconcile_check(
    workspace: Path,
    launch_id: str,
    launcher_pid: int,
    *,
    checked_at: float,
    interval_seconds: float,
) -> bool:
    key = (os.path.normcase(str(workspace)), launch_id, int(launcher_pid))
    interval = max(0.0, float(interval_seconds))
    with _ORPHAN_RECONCILE_GUARD:
        previous = _ORPHAN_RECONCILE_LAST_CHECK.get(key)
        if previous is not None and checked_at - previous < interval:
            return False
        _ORPHAN_RECONCILE_LAST_CHECK[key] = checked_at
        if len(_ORPHAN_RECONCILE_LAST_CHECK) > MAX_ORPHAN_RECONCILE_KEYS:
            oldest = min(_ORPHAN_RECONCILE_LAST_CHECK, key=_ORPHAN_RECONCILE_LAST_CHECK.get)
            _ORPHAN_RECONCILE_LAST_CHECK.pop(oldest, None)
        return True


def _orphaned_projection(
    launch: dict[str, Any],
    *,
    ended_at: str,
    orphaned_pid: int,
) -> dict[str, Any]:
    liveness = launch.get("liveness") if isinstance(launch.get("liveness"), dict) else {}
    return {
        **launch,
        "status": "orphaned",
        "completed_at": ended_at,
        "orphaned_pid": int(orphaned_pid),
        "liveness": {
            **liveness,
            "monitoring": False,
            "lifecycle_status": "orphaned",
            "stopped_at": ended_at,
        },
    }


def recover_orphaned_launches(
    workspace_root: str | Path,
    *,
    process_probe: Any = None,
) -> list[dict[str, Any]]:
    from .pacer_events import append_pacer_event, process_exists

    workspace = Path(workspace_root).expanduser().resolve()
    probe = process_probe or process_exists
    directory = workspace / "pacer_native" / "launch-contexts"
    recovered: list[dict[str, Any]] = []
    try:
        paths = list(directory.glob("*.json"))
    except OSError:
        return recovered
    for path in paths:
        launch = _read_json(path)
        if str(launch.get("status") or "") != "running":
            continue
        try:
            pid = int(launch.get("launcher_pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or probe(pid):
            continue
        ended_at = datetime.now(timezone.utc).isoformat()
        launch, capsule = _commit_orphaned_launch(
            workspace,
            launch,
            ended_at=ended_at,
            orphaned_pid=pid,
        )
        if not launch or not capsule:
            continue
        append_pacer_event(
            workspace,
            "launch_orphaned",
            launch_id=str(launch.get("launch_id") or ""),
            data={"launcher_pid": pid, "recovery_capsule": True},
        )
        recovered.append(launch)
    return recovered


def _commit_orphaned_launch(
    workspace_root: str | Path,
    launch: dict[str, Any],
    *,
    ended_at: str,
    orphaned_pid: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    workspace = Path(workspace_root).expanduser().resolve()
    launch_id = _validated_launch_id(launch.get("launch_id"))
    with _launch_state_transaction(workspace):
        current = _read_json(launch_context_path(workspace, launch_id))
        try:
            latest_pid = int(current.get("launcher_pid") or 0)
        except (TypeError, ValueError):
            return {}, {}
        if str(current.get("status") or "") != "running" or latest_pid != int(orphaned_pid):
            return {}, {}
        current.update({"status": "orphaned", "completed_at": ended_at, "orphaned_pid": int(orphaned_pid)})
        liveness = _read_json(launch_liveness_path(workspace, launch_id))
        liveness.update(
            {
                "launch_id": launch_id,
                "monitoring": False,
                "lifecycle_status": "orphaned",
                "stopped_at": ended_at,
            }
        )
        current["liveness"] = liveness
        _write_json(launch_liveness_path(workspace, launch_id), liveness)
        _write_active_launch_unlocked(workspace, current)
        capsule = {
            "schema_version": 1,
            "status": "pending",
            "source_launch_id": launch_id,
            "created_at": ended_at,
            "reason": "launcher_process_disappeared",
            "project_root": str(current.get("project_root") or current.get("launch_cwd") or ""),
            "goal": str(current.get("launch_goal") or current.get("current_goal") or "")[:2000],
            "auto_compact_token_limit": int(current.get("auto_compact_token_limit") or 0),
            "pillars": current.get("pillars") if isinstance(current.get("pillars"), dict) else {},
        }
        _write_json(recovery_capsule_path(workspace, launch_id), capsule)
        return current, capsule


def _sync_launch_manifest(workspace_root: str | Path, payload: dict[str, Any]) -> None:
    workspace = Path(workspace_root).expanduser().resolve()
    try:
        launch_id = _validated_launch_id(payload.get("launch_id"))
    except ValueError:
        return
    path = _launch_manifest_path(workspace, launch_id)
    if not path.is_file():
        return
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return
    if not isinstance(manifest, dict):
        return
    for key in (
        "project_root",
        "runtime",
        "liveness",
        "source_responsibility",
        "pillars",
        "rollout_telemetry",
        "status",
        "completed_at",
        "elapsed_seconds",
        "exit_code",
    ):
        if key in payload:
            manifest[key] = payload[key]
    try:
        _write_json(path, manifest)
    except OSError:
        pass


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _timestamp_value(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def _normalized_started_at(value: Any) -> str:
    raw = str(value or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.now(timezone.utc)
    else:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _launch_order_key(launch: dict[str, Any]) -> tuple[float, str]:
    try:
        launch_id = _validated_launch_id(launch.get("launch_id"))
    except ValueError:
        launch_id = ""
    return _timestamp_value(str(launch.get("started_at") or "")), launch_id
