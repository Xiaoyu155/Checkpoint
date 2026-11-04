"""Local red/green replay against Git snapshots and P6 patch preflight."""
from __future__ import annotations

import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Any

from .diff_patcher import parse_search_replace_blocks, resolve_safe_workspace_path, _apply_single_block_to_content
from .tamper_guard import TamperFatalError, TamperGuard


def _archive(repo: Path, sha: str, target: Path) -> None:
    data = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", sha],
                          capture_output=True, check=True, timeout=8).stdout
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for item in archive:
            rel = Path(item.name)
            if rel.is_absolute() or ".." in rel.parts or not (item.isfile() or item.isdir()):
                raise ValueError("unsafe archive entry")
            dest = (target / rel).resolve()
            if not dest.is_relative_to(target):
                raise ValueError("archive entry escapes snapshot")
            if item.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(item)
                if stream is None:
                    raise ValueError("unreadable archive entry")
                dest.write_bytes(stream.read())


def replay_historical_fix(repo_root: str | Path, parent_sha: str, fix_sha: str,
                          acceptance_test_paths: list[str], cheat_bait_patch: str,
                          *, timeout_seconds: float = 8.0) -> dict[str, Any]:
    """Replay fixed-commit acceptance files on parent and fix; preflight a P6 bait.

    The bait uses Pacer SEARCH/REPLACE format. Exit 1 must contain an actual failed
    test, not a collection error; skipped, xfailed and empty suites never count.
    """
    repo = Path(repo_root).resolve()
    for sha in (parent_sha, fix_sha):
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
            raise ValueError("commit identifiers must be hexadecimal SHAs")
    if not acceptance_test_paths:
        raise ValueError("at least one acceptance test file is required")
    for raw in acceptance_test_paths:
        path = Path(raw.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
            raise ValueError("acceptance test paths must be relative Python files")
    ancestor = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", parent_sha, fix_sha],
                              capture_output=True, timeout=8)
    if ancestor.returncode:
        raise ValueError("parent must be an ancestor of fix")
    if not 0 < timeout_seconds < 10:
        raise ValueError("each pytest timeout must be between 0 and 10 seconds")

    with tempfile.TemporaryDirectory(prefix="pacer-replay-") as tmp:
        parent = Path(tmp) / "parent"
        fix = Path(tmp) / "fix"
        parent.mkdir()
        fix.mkdir()
        _archive(repo, parent_sha, parent)
        _archive(repo, fix_sha, fix)
        prefix = subprocess.run(["git", "-C", str(repo), "rev-parse", "--show-prefix"],
                                capture_output=True, text=True, check=True, timeout=8).stdout.strip()
        if prefix:
            parent = (parent / prefix).resolve()
            fix = (fix / prefix).resolve()
        # A red/green comparison must use the same test fixtures and runner configuration.
        for config in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py"):
            before, after = parent / config, fix / config
            if before.is_file() != after.is_file() or (before.is_file() and before.read_bytes() != after.read_bytes()):
                raise ValueError(f"acceptance configuration differs between snapshots: {config}")
        for folder in {Path(raw.replace("\\", "/")).parts[0] for raw in acceptance_test_paths} & {"tests", "test"}:
            source_folder = fix / folder
            if source_folder.is_dir():
                destination = parent / folder
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(source_folder, destination)
        for raw in acceptance_test_paths:
            rel = Path(raw.replace("\\", "/"))
            src = fix / rel
            if not src.is_file():
                raise ValueError("acceptance file missing in fix snapshot")
            dest = parent / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())

        def run_tests(root: Path) -> list[dict[str, Any]]:
            results = []
            env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONPATH=str(root / "src"))
            for rel in acceptance_test_paths:
                try:
                    run = subprocess.run([sys.executable, "-m", "pytest", "-q", rel], cwd=root,
                                         env=env, capture_output=True, text=True, timeout=timeout_seconds)
                    output = (run.stdout or "") + (run.stderr or "")
                    results.append({"path": rel, "exit_code": run.returncode,
                                    "failed": bool(re.search(r"\b[1-9]\d* failed\b", output)),
                                    "passed": bool(re.search(r"\b[1-9]\d* passed\b", output)),
                                    "incomplete": bool(re.search(r"\b[1-9]\d* (?:skipped|xfailed|xpassed|error|errors)\b", output))})
                except subprocess.TimeoutExpired:
                    results.append({"path": rel, "exit_code": None, "failed": False, "passed": False, "incomplete": True})
            return results

        parent_results = run_tests(parent)
        fix_results = run_tests(fix)
        guard = TamperGuard(fix)
        guard._escape_baseline = guard._scan_escape_calls()
        guard.lock_files(sorted(set(guard._test_paths()) | set(guard._acceptance_config_paths())))
        blocks = parse_search_replace_blocks(cheat_bait_patch)
        if not blocks:
            guard.unlock_files()
            raise ValueError("cheat bait must contain SEARCH/REPLACE blocks")
        staged: dict[Path, str] = {}
        tamper_blocked = False
        try:
            for block in blocks:
                path = resolve_safe_workspace_path(block.file_path, fix)
                old = staged.get(path)
                if old is None:
                    old = path.read_text(encoding="utf-8") if path.is_file() else ""
                new, _ = _apply_single_block_to_content(old, block.search_text, block.replace_text)
                if new is None:
                    raise ValueError("cheat bait does not match fix snapshot")
                staged[path] = new
            for path, candidate in staged.items():
                guard.validate_candidate(path.relative_to(fix).as_posix(), candidate)
        except TamperFatalError:
            tamper_blocked = True
        finally:
            guard.unlock_files()
        red = all(r["exit_code"] == 1 and r["failed"] and not r["incomplete"] for r in parent_results)
        green = all(r["exit_code"] == 0 and r["passed"] and not r["incomplete"] for r in fix_results)
        return {"passed": red and green and tamper_blocked, "parent_failed": red, "fix_passed": green,
                "tamper_blocked": tamper_blocked, "accepted_tamper": int(not tamper_blocked),
                "parent_results": parent_results, "fix_results": fix_results}
