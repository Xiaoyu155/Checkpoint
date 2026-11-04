"""
Pacer TamperGuard: Physical Read-Only Lock & In-Memory Instant Restoration Guard (v2.1).

Addresses Edge Case 1:
Newly created/untracked test and contract files cannot be rolled back via `git checkout`.
TamperGuard maintains an in-memory byte snapshot (`backup_blobs`) and applies OS-level
read-only file attributes (`stat.S_IREAD`). If a model or rogue process tampers with
or deletes a protected file, TamperGuard detects the SHA256 mismatch and restores the
original file bytes in < 1ms, re-applying the physical read-only lock.
"""

from __future__ import annotations

import ast
from collections import Counter
import contextlib
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any


_EXCLUDED_DIRS = {".git", ".pacer", ".agent-workspace", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"}
_ESCAPE_CALLS = {"pytest.skip", "pytest.xfail", "pytest.mark.skip", "pytest.mark.skipif", "pytest.mark.xfail", "os._exit", "sys.exit"}


def _dotted_name(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join((node.id, *reversed(parts)))
    return ""


def _escape_calls(source: str) -> Counter[str]:
    tree = ast.parse(source)
    module_aliases: dict[str, str] = {}
    symbol_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name.split(".", 1)[0]
                if module in {"pytest", "os", "sys"}:
                    module_aliases[alias.asname or module] = module
        elif isinstance(node, ast.ImportFrom) and node.module in {"pytest", "os", "sys"}:
            if node.module == "pytest":
                allowed = {"skip", "xfail", "mark"}
            elif node.module == "os":
                allowed = {"_exit"}
            else:
                allowed = {"exit"}
            for alias in node.names:
                if alias.name in allowed:
                    symbol_aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    aliases = {**module_aliases, **symbol_aliases}

    def resolve(node: ast.AST) -> str:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
            if len(node.args) == 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                return resolve(node.args[0]) + "." + node.args[1].value
        name = _dotted_name(node)
        first, dot, rest = name.partition(".")
        return aliases.get(first, first) + (dot + rest if dot else "")

    # Resolve simple assignment aliases without executing worker code.
    assignments = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign))]
    for _ in range(len(assignments) + 1):
        changed = False
        for node in assignments:
            value = resolve(node.value) if node.value is not None else ""
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and value and aliases.get(target.id) != value:
                    aliases[target.id] = value
                    changed = True
        if not changed:
            break

    found: Counter[str] = Counter()
    for node in ast.walk(tree):
        function = resolve(node.func) if isinstance(node, ast.Call) else ""
        if function in _ESCAPE_CALLS:
            found[function] += 1
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) and resolve(decorator) in _ESCAPE_CALLS:
                    found[resolve(decorator)] += 1
    return found


class TamperFatalError(RuntimeError):
    """A protected exam file changed and could not be restored."""


class TamperGuard:
    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self.workspace = Path(workspace_root or os.getcwd()).resolve()
        self.protected_files: dict[str, str] = {}  # rel_path -> sha256
        self.backup_blobs: dict[str, bytes] = {}   # rel_path -> raw file bytes
        self.tamper_history: list[dict[str, Any]] = []
        self.acceptance_frozen = False
        self.acceptance_test_paths: frozenset[str] = frozenset()
        self.acceptance_config_paths: frozenset[str] = frozenset()
        self.baseline_test_ids: frozenset[str] = frozenset()
        self._escape_baseline: dict[str, Counter[str]] = {}

    def _python_source_paths(self) -> list[Path]:
        paths = []
        for directory, names, files in os.walk(self.workspace):
            names[:] = [name for name in names if name not in _EXCLUDED_DIRS]
            paths.extend(Path(directory) / name for name in files if name.endswith(".py"))
        return sorted(paths)

    def _test_paths(self) -> list[str]:
        from .command_verification import is_test_path

        result = []
        for directory, names, files in os.walk(self.workspace):
            names[:] = [name for name in names if name not in _EXCLUDED_DIRS and not name.startswith(".pytest_cache")]
            for name in files:
                relative = (Path(directory) / name).relative_to(self.workspace).as_posix()
                if is_test_path(relative):
                    result.append(relative)
        return sorted(set(result))

    def _acceptance_config_paths(self) -> list[str]:
        fixed = {"pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py"}
        result = []
        for directory, names, files in os.walk(self.workspace):
            names[:] = [name for name in names if name not in _EXCLUDED_DIRS]
            result.extend((Path(directory) / name).relative_to(self.workspace).as_posix()
                          for name in files if name in fixed)
        return sorted(set(result))

    def _collect_test_ids(self, *, timeout_seconds: float = 120.0) -> frozenset[str]:
        test_paths = self._test_paths()
        if not test_paths:
            return frozenset()
        from .process_guard import guarded_run
        from .subprocess_window import hidden_subprocess_kwargs

        child_env = os.environ.copy()
        child_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        source_path = str(self.workspace / "src")
        current_pythonpath = child_env.get("PYTHONPATH", "")
        child_env["PYTHONPATH"] = os.pathsep.join(part for part in (source_path, current_pythonpath) if part)
        try:
            completed = guarded_run(
                [sys.executable, "-m", "pytest", "--collect-only", "-q"],
                cwd=str(self.workspace),
                env=child_env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=max(1.0, float(timeout_seconds)),
                check=False,
                **hidden_subprocess_kwargs(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise TamperFatalError(f"cannot freeze pytest collection baseline: {exc}") from exc
        output = "\n".join(part for part in (completed.stdout or "", completed.stderr or "") if part)
        identifiers = frozenset(line.strip() for line in output.splitlines() if "::" in line)
        if completed.returncode not in {0, 5} or (completed.returncode == 0 and not identifiers):
            raise TamperFatalError(
                f"cannot freeze pytest collection baseline (exit {completed.returncode}; IDs {len(identifiers)})"
            )
        return identifiers

    def freeze_acceptance_suite(self, *, collect_timeout_seconds: float = 120.0) -> dict[str, Any]:
        """Freeze protected test bytes, pytest collection IDs, and escape-call counts."""
        if self.acceptance_frozen:
            return self.get_manifest()
        # Restore any files protected before the broader suite was frozen, so
        # collection and its baseline use the captured bytes.
        self.verify_and_enforce()
        test_paths = self._test_paths()
        self.baseline_test_ids = self._collect_test_ids(timeout_seconds=collect_timeout_seconds)
        self.acceptance_test_paths = frozenset(test_paths)
        self.acceptance_config_paths = frozenset(self._acceptance_config_paths())
        self._escape_baseline = self._scan_escape_calls()
        protected = sorted(set(test_paths) | set(self._acceptance_config_paths()))
        if protected:
            self.lock_files(protected)
        self.acceptance_frozen = True
        return self.get_manifest()

    def validate_candidate(self, relative: str, source: str) -> None:
        """Reject protected inputs and new escape calls before any worker bytes land."""
        from .command_verification import is_test_path

        relative = Path(relative).as_posix()
        if relative in self.protected_files or is_test_path(relative) or Path(relative).name in {
            "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py",
        }:
            raise TamperFatalError(f"Worker cannot modify immutable acceptance input: {relative}")
        if relative.endswith(".py"):
            try:
                counts = _escape_calls(source)
            except SyntaxError as exc:
                raise TamperFatalError(f"Worker Python syntax is invalid: {relative}") from exc
            added = counts - self._escape_baseline.get(relative, Counter())
            if added:
                raise TamperFatalError(f"Worker introduced acceptance escape in {relative}: {', '.join(added)}")

    def _scan_escape_calls(self) -> dict[str, Counter[str]]:
        baseline: dict[str, Counter[str]] = {}
        for path in self._python_source_paths():
            relative = path.relative_to(self.workspace).as_posix()
            try:
                baseline[relative] = _escape_calls(path.read_text(encoding="utf-8"))
            except SyntaxError:
                baseline[relative] = Counter()
            except (OSError, UnicodeError) as exc:
                raise TamperFatalError(f"cannot scan Python source for test escapes: {relative}: {exc}") from exc
        return baseline

    def lock_files(self, rel_paths: list[str]) -> dict[str, str]:
        """
        Record file SHA-256 and raw bytes into in-memory storage,
        then apply physical OS-level read-only permissions.
        """
        locked: dict[str, str] = {}
        for rel in rel_paths:
            normalized = str(Path(rel).as_posix())
            full_path = self.workspace / normalized
            if not full_path.resolve().is_relative_to(self.workspace):
                raise TamperFatalError("Protected path escapes workspace")
            if not full_path.exists():
                continue

            if normalized in self.protected_files:
                if hashlib.sha256(full_path.read_bytes()).hexdigest() != self.protected_files[normalized]:
                    raise TamperFatalError(f"Cannot replace immutable baseline for {normalized}")
                continue

            try:
                content = full_path.read_bytes()
                file_hash = hashlib.sha256(content).hexdigest()
                self.protected_files[normalized] = file_hash
                self.backup_blobs[normalized] = content
                locked[normalized] = file_hash

                # Apply OS read-only lock (removes write bits on Windows and Linux)
                mode = full_path.stat().st_mode
                full_path.chmod(mode & ~stat.S_IWRITE & ~stat.S_IWGRP & ~stat.S_IWOTH)
            except OSError:
                if normalized not in self.backup_blobs:
                    raise TamperFatalError(f"cannot snapshot protected file {normalized}")

        self._persist()
        return locked

    def _persist(self) -> None:
        root = self.workspace / ".pacer" / "tamper_guard"
        blob_root = root / "blobs"
        blob_root.mkdir(parents=True, exist_ok=True)
        (root / "manifest.json").write_text(
            json.dumps(self.protected_files, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        for rel, blob in self.backup_blobs.items():
            dest = blob_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(blob)

    def load_persisted(self) -> None:
        root = self.workspace / ".pacer" / "tamper_guard"
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            return
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            return
        self.protected_files = {str(key): str(value) for key, value in manifest.items()}
        for rel in self.protected_files:
            blob = root / "blobs" / rel
            if blob.is_file():
                self.backup_blobs[rel] = blob.read_bytes()

    def unlock_files(self) -> None:
        """Temporarily restore write permissions for all protected files."""
        for rel in self.protected_files.keys():
            full_path = self.workspace / rel
            if full_path.exists():
                try:
                    mode = full_path.stat().st_mode
                    full_path.chmod(mode | stat.S_IWRITE)
                except Exception:
                    pass

    unlock_all = unlock_files

    @contextlib.contextmanager
    def temporary_unlock(self):
        """Context manager for safely performing authorized modifications."""
        self.unlock_files()
        try:
            yield
        finally:
            # Re-lock all protected files
            for rel in self.protected_files.keys():
                full_path = self.workspace / rel
                if full_path.exists():
                    try:
                        mode = full_path.stat().st_mode
                        full_path.chmod(mode & ~stat.S_IWRITE & ~stat.S_IWGRP & ~stat.S_IWOTH)
                    except Exception:
                        pass

    def verify_and_enforce(self) -> tuple[bool, str, list[str]]:
        """
        Physical tamper detection & instant restoration.
        1. Checks whether protected files were modified or deleted;
        2. Restores changed files from captured in-memory byte snapshots;
        3. Re-enforces physical read-only locks.
        """
        t0 = time.perf_counter()
        tampered_files: list[str] = []
        restored_files: list[str] = []

        # Temporarily enable write permissions so we can inspect and restore if needed
        self.unlock_files()

        for rel, orig_hash in self.protected_files.items():
            full_path = self.workspace / rel
            is_deleted = not full_path.exists()
            is_modified = False

            if not is_deleted:
                try:
                    cur_bytes = full_path.read_bytes()
                    cur_hash = hashlib.sha256(cur_bytes).hexdigest()
                    if cur_hash != orig_hash:
                        is_modified = True
                except Exception:
                    is_modified = True

            if is_deleted or is_modified:
                reason = "DELETED" if is_deleted else "MODIFIED"
                tampered_files.append(f"{rel} ({reason})")

                # Edge Case 1: Instant in-memory restoration (works for untracked & new files)
                restored_successfully = False
                if rel in self.backup_blobs:
                    try:
                        full_path.parent.mkdir(parents=True, exist_ok=True)
                        fd, restore_name = tempfile.mkstemp(
                            prefix=f".{full_path.name}.", suffix=".restore", dir=full_path.parent
                        )
                        try:
                            with os.fdopen(fd, "wb") as stream:
                                stream.write(self.backup_blobs[rel])
                                stream.flush()
                                os.fsync(stream.fileno())
                            os.replace(restore_name, full_path)
                        finally:
                            Path(restore_name).unlink(missing_ok=True)
                        # Re-verify byte hash to guarantee authentic restoration
                        if hashlib.sha256(full_path.read_bytes()).hexdigest() == orig_hash:
                            restored_files.append(rel)
                            restored_successfully = True
                    except OSError as exc:
                        raise TamperFatalError(f"cannot restore {rel}: {exc}") from exc

                if not restored_successfully:
                    raise TamperFatalError(f"no valid in-memory snapshot is available to restore {rel}")

        if self.acceptance_frozen:
            current_test_paths = frozenset(self._test_paths())
            for rel in sorted(current_test_paths - self.acceptance_test_paths):
                tampered_files.append(f"{rel} (ADDED TEST FILE)")
            for rel in sorted(self.acceptance_test_paths - current_test_paths):
                tampered_files.append(f"{rel} (MISSING TEST FILE)")
            for rel in sorted(set(self._acceptance_config_paths()) ^ self.acceptance_config_paths):
                tampered_files.append(f"{rel} (CHANGED ACCEPTANCE CONFIG SET)")
            unsafe_source = False
            try:
                current_escapes = self._scan_escape_calls()
            except TamperFatalError as exc:
                tampered_files.append(f"Python escape scan (FAILED: {exc})")
                unsafe_source = True
            else:
                for rel, counts in current_escapes.items():
                    baseline = self._escape_baseline.get(rel, Counter())
                    for call, count in counts.items():
                        if count > baseline.get(call, 0):
                            tampered_files.append(f"{rel} (NEW ESCAPE {call} x{count - baseline.get(call, 0)})")
                            unsafe_source = True
            # Collection imports source: never execute a newly detected exit/skip first.
            if not unsafe_source:
                try:
                    current_ids = self._collect_test_ids()
                except TamperFatalError as exc:
                    tampered_files.append(f"pytest collection (FAILED: {exc})")
                else:
                    if current_ids != self.baseline_test_ids:
                        added = len(current_ids - self.baseline_test_ids)
                        removed = len(self.baseline_test_ids - current_ids)
                        tampered_files.append(f"pytest collection IDs (added={added}, removed={removed})")

        # Re-apply OS read-only locks on all protected files
        for rel in self.protected_files.keys():
            full_path = self.workspace / rel
            if full_path.exists():
                try:
                    mode = full_path.stat().st_mode
                    full_path.chmod(mode & ~stat.S_IWRITE & ~stat.S_IWGRP & ~stat.S_IWOTH)
                except Exception:
                    pass

        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        if tampered_files:
            record = {
                "timestamp": time.time(),
                "tampered_files": tampered_files,
                "restored_files": restored_files,
                "restoration_ms": elapsed_ms,
            }
            self.tamper_history.append(record)
            msg = (
                f"Tamper Guard Triggered! {len(tampered_files)} protected file(s) altered: "
                f"{', '.join(tampered_files)}. Successfully restored in {elapsed_ms:.2f}ms."
            )
            return False, msg, tampered_files

        return True, "Integrity Verified: Zero tamper on protected tests and contracts.", []

    def get_manifest(self) -> dict[str, Any]:
        """Return the current protection manifest for telemetry & audit."""
        return {
            "workspace": str(self.workspace),
            "protected_count": len(self.protected_files),
            "protected_files": list(self.protected_files.keys()),
            "acceptance_frozen": self.acceptance_frozen,
            "test_id_count": len(self.baseline_test_ids),
            "test_ids_sha256": hashlib.sha256("\n".join(sorted(self.baseline_test_ids)).encode("utf-8")).hexdigest(),
            "tamper_events_count": len(self.tamper_history),
            "latest_event": self.tamper_history[-1] if self.tamper_history else None,
        }
