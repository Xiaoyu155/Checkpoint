"""Does the acceptance command actually test *this* task?

A gate that passes both before and after the change proves nothing about the
objective — it only proves nothing broke. Pacer used to stamp both cases
`verified`, so "append a line to a markdown file" could be accepted by an
unrelated pytest module that was already green.

This module runs the acceptance command once against the worktree's base commit
and grades the evidence:

``verified``          the command failed on base and passes now — it discriminates
``regression_clear``  the command passes either way — only proves no regression
``unverified``        there was no command gate at all

Only ``verified`` may be reported as an accepted objective.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from .command_verification import run_command_verification


TIER_VERIFIED = "verified"
TIER_REGRESSION_CLEAR = "regression_clear"
TIER_UNVERIFIED = "unverified"

# A base run that fails because the environment is broken says nothing about
# discrimination — treating it as "was red before" would manufacture exactly the
# false green this module exists to stop.
_INCONCLUSIVE_FAILURE_KINDS = {
    "test_command_invalid",
    "pytest_not_importable",
    "verification_environment_missing",
    "command_timeout",
    "conditional_test_command_short_circuit",
}

_PROBE_CACHE_NAME = "acceptance_probes.json"
_OVERLAY_SKIP_PARTS = {
    ".git", ".pacer", ".agent-workspace", ".venv", "venv", "node_modules",
    "__pycache__", "dist", "build", "target", ".pytest_cache",
}
_OVERLAY_SECRET_NAMES = {
    ".env", ".env.local", ".env.production", "gemini_keys.json", "credentials.json",
    "secrets.json", "secrets.yaml", "secrets.yml",
}
_OVERLAY_MAX_FILE_BYTES = 16 * 1024 * 1024
_SNAPSHOT_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_SNAPSHOT_MAX_FILES = 25_000


def snapshot_workspace_overlay(
    workspace_root: str | Path,
    *,
    target_prefix: str | Path = ".",
) -> tuple[Callable[[Path], None] | None, str]:
    """Capture dirty tracked/untracked files for a faithful detached-base probe.

    The base worktree must contain the exact pre-task working tree, including
    uncommitted source/tests. Credential stores and generated dependency trees
    are deliberately excluded. The digest scopes the probe cache to this state.
    """
    root = Path(workspace_root).expanduser().resolve()
    prefix = Path(target_prefix)
    if prefix.is_absolute() or ".." in prefix.parts:
        return None, "overlay-target-prefix-unsafe"
    try:
        result = subprocess.run(
            [
                "git", "-C", str(root), "status", "--porcelain=v1", "-z",
                "--untracked-files=all", "--", ".",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "overlay-unavailable"
    if result.returncode != 0:
        return None, "overlay-unavailable"

    prefix_result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-prefix"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
        check=False,
    )
    repo_prefix = prefix_result.stdout.strip().replace("\\", "/").strip("/")
    repo_prefix = f"{repo_prefix}/" if prefix_result.returncode == 0 and repo_prefix else ""

    def workspace_relative(value: str) -> str:
        normalized = value.replace("\\", "/")
        if not repo_prefix:
            return normalized
        return normalized[len(repo_prefix):] if normalized.startswith(repo_prefix) else ""

    raw_entries = iter(result.stdout.split("\0"))
    entries: list[tuple[str, str]] = []
    for raw in raw_entries:
        if len(raw) < 4:
            continue
        status, relative = raw[:2], workspace_relative(raw[3:])
        if "R" in status or "C" in status:
            old_relative = workspace_relative(next(raw_entries, ""))
            if old_relative:
                entries.append(("D", old_relative))
        entries.append((status, relative))

    eligible: list[tuple[str, str]] = []
    digest = hashlib.sha256(b"pacer-workspace-overlay-v1\0")
    for status, relative in entries:
        rel_path = Path(relative)
        if rel_path.is_absolute() or ".." in rel_path.parts or not relative:
            continue
        normalized_parts = {part.lower() for part in rel_path.parts}
        basename = rel_path.name.lower()
        if normalized_parts & _OVERLAY_SKIP_PARTS or basename in _OVERLAY_SECRET_NAMES:
            continue
        if rel_path.suffix.lower() in {".pem", ".p12", ".pfx", ".key"}:
            continue

        source = root / rel_path
        digest.update(status.encode("ascii", errors="ignore") + b"\0")
        digest.update(relative.encode("utf-8", errors="replace") + b"\0")
        if "D" in status or not source.exists():
            digest.update(b"deleted\0")
        elif source.is_symlink():
            # Do not follow links while copying a user's pre-task tree.
            return None, "overlay-contains-symlink"
        elif source.is_file():
            try:
                size = source.stat().st_size
                if size > _OVERLAY_MAX_FILE_BYTES:
                    return None, "overlay-file-too-large"
                content_hash = hashlib.sha256(source.read_bytes()).digest()
            except OSError:
                return None, "overlay-file-unreadable"
            digest.update(size.to_bytes(8, "big") + content_hash)
        else:
            continue
        eligible.append((status, relative))

    baseline_id = digest.hexdigest()

    def prepare_base(target_root: Path) -> None:
        target = Path(target_root).resolve()
        for status, relative in eligible:
            rel_path = Path(relative)
            source = root / rel_path
            destination = target / prefix / rel_path
            relative_destination = destination.relative_to(target)
            cursor = target
            for index, part in enumerate(relative_destination.parts):
                cursor = cursor / part
                if cursor.is_symlink():
                    if index == len(relative_destination.parts) - 1:
                        cursor.unlink()
                    else:
                        raise ValueError(f"workspace overlay would follow a symlink: {relative}")
                elif index < len(relative_destination.parts) - 1 and cursor.exists() and not cursor.is_dir():
                    raise ValueError(f"workspace overlay parent is not a directory: {relative}")
            if "D" in status or not source.exists():
                if destination.is_file() or destination.is_symlink():
                    destination.unlink()
                elif destination.is_dir():
                    raise ValueError(f"workspace overlay deletion points to a directory: {relative}")
                continue
            if not source.is_file() or source.is_symlink():
                raise ValueError(f"workspace overlay is no longer a regular file: {relative}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    return prepare_base, baseline_id

# 门"红"有两种，含义完全相反：
#   (a) 契约先行——功能还没写，所以它红。这是期望状态。
#   (b) 门自己坏了——语法错误、模块加载失败，它根本没在检查。
# 两者在退出码上一模一样，但 (b) 意味着无论 worker 做什么它都红。
# 2026-08-05 实测：一道有语法错误的门被当成 (a) 派了工，worker 为了让它能跑
# 去改了测试文件，触发防篡改，整轮报废——而根因只是那道门从未真正执行过。
_GATE_LOAD_FAILURE_MARKERS = (
    "syntaxerror",
    "err_module_not_found",
    "cannot find module",
    "cannot find package",
    "modulenotfounderror",
    "importerror",
    "error collecting",
    "errors during collection",
    "internalerror",
    "unable to import",
    "failed to load config",
    "cannot resolve entry",
)

# "一个用例都没跑"和"跑了但没过"是两回事：前者说明门没生效。
_ZERO_EXECUTION_MARKERS = (
    "# tests 0",
    "no tests found",
    "no tests ran",
    "collected 0 items",
    "found 0 tests",
    "ran 0 tests",
)


def assess_gate_health(result: dict[str, Any] | None) -> dict[str, Any]:
    """判断验收命令是"跑过并给出结论"还是"根本没跑起来"。

    只在门为红时才有意义：绿的门必然执行过。
    """
    record = result if isinstance(result, dict) else {}
    text = " ".join(
        str(record.get(key) or "")
        for key in ("output_tail", "raw_output_tail", "reason", "markdown")
    ).lower()
    if not text.strip():
        return {"status": "unknown", "reason": "no_output_to_judge"}
    for marker in _GATE_LOAD_FAILURE_MARKERS:
        if marker in text:
            return {"status": "broken", "reason": "gate_failed_to_load", "signal": marker}
    for marker in _ZERO_EXECUTION_MARKERS:
        if marker in text:
            return {"status": "broken", "reason": "gate_executed_nothing", "signal": marker.strip()}
    return {"status": "runnable", "reason": "gate_reported_failures"}


def probe_base_command(
    *,
    command: str,
    repo_root: str | Path,
    base_ref: str,
    timeout_seconds: float = 900.0,
    env: dict[str, str] | None = None,
    verification_env: list[dict[str, Any]] | None = None,
    workspace_root: str | Path | None = None,
    runner: Callable[..., dict[str, Any]] | None = None,
    prepare_base: Callable[[Path], Any] | None = None,
    baseline_id: str = "",
) -> dict[str, Any]:
    """Run ``command`` against ``base_ref`` in a throwaway worktree.

    ``prepare_base`` lets the caller reproduce whatever the worker's worktree
    started from. That matters for repositories with uncommitted work: the
    worker sees ``base_ref`` plus a dirty overlay, so probing bare ``base_ref``
    would compare against a state that never existed and could excuse a
    regression the worker really introduced.
    """

    cmd = str(command or "").strip()
    base = str(base_ref or "").strip()
    if not cmd:
        return _probe("unknown", reason="no_command", base_ref=base)
    if not base:
        return _probe("unknown", reason="no_base_ref", base_ref=base)

    cache_key_base = f"{base}|{baseline_id}" if baseline_id else base
    cached = _cache_read(workspace_root, cache_key_base, cmd)
    if cached is not None:
        return {**cached, "cached": True}

    root = Path(repo_root).expanduser().resolve()
    holder = Path(tempfile.mkdtemp(prefix="pacer-base-probe-"))
    target = holder / "base"
    added = subprocess.run(
        ["git", "worktree", "add", "--detach", str(target), base],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    if added.returncode != 0:
        _discard(holder)
        return _probe(
            "unknown",
            reason="base_worktree_unavailable",
            base_ref=base,
            detail=(added.stderr or added.stdout or "").strip()[:400],
        )
    if prepare_base is not None:
        try:
            prepare_base(target)
        except Exception as exc:  # noqa: BLE001 - a baseline we cannot rebuild is unknown
            _remove_worktree(root, target)
            _discard(holder)
            return _probe("unknown", reason="base_overlay_failed", base_ref=base, detail=str(exc)[:400])
    try:
        execute = runner or run_command_verification
        result = execute(
            command=cmd,
            repo_root=target,
            timeout_seconds=timeout_seconds,
            env=env,
            verification_env=verification_env,
        )
    except Exception as exc:  # noqa: BLE001 - a probe must never fail the mission
        _remove_worktree(root, target)
        _discard(holder)
        return _probe("unknown", reason="base_probe_error", base_ref=base, detail=str(exc)[:400])
    finally:
        _remove_worktree(root, target)
        _discard(holder)

    probe = _grade_probe_result(result, base)
    _cache_write(workspace_root, cache_key_base, cmd, probe)
    return probe


def probe_workspace_command(
    *,
    command: str,
    workspace_root: str | Path,
    timeout_seconds: float = 900.0,
    env: dict[str, str] | None = None,
    verification_env: list[dict[str, Any]] | None = None,
    runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Probe the exact pre-task workspace, whether or not it is a Git repo.

    Git workspaces use HEAD plus their dirty overlay. Non-Git workspaces use a
    bounded local copy, so a missing repository does not prevent the coder from
    starting and concurrent workers cannot change the baseline under the probe.
    """
    env = {**os.environ, **(env or {})}
    env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    root = Path(workspace_root).expanduser().resolve()
    cmd = str(command or "").strip()
    if not cmd:
        return _probe("unknown", reason="no_command", base_ref="WORKTREE")
    try:
        git = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        git = None
    has_git_head = False
    if git is not None and git.returncode == 0:
        try:
            head = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            has_git_head = head.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            has_git_head = False

    if git is not None and git.returncode == 0 and has_git_head:
        git_root = Path(git.stdout.strip()).expanduser().resolve()
        try:
            workspace_prefix = root.relative_to(git_root)
        except ValueError:
            return _probe("unknown", reason="workspace_outside_git_root", base_ref="HEAD")
        prepare_base, baseline_id = snapshot_workspace_overlay(root, target_prefix=workspace_prefix)
        if prepare_base is None:
            return _probe("unknown", reason=baseline_id, base_ref="HEAD")
        base_runner = runner or run_command_verification
        if workspace_prefix.parts:
            def run_from_workspace(**kwargs: Any) -> dict[str, Any]:
                kwargs["repo_root"] = Path(kwargs["repo_root"]) / workspace_prefix
                return base_runner(**kwargs)
        else:
            run_from_workspace = base_runner
        probe = probe_base_command(
            command=cmd,
            repo_root=git_root,
            base_ref="HEAD",
            timeout_seconds=timeout_seconds,
            env=env,
            verification_env=verification_env,
            workspace_root=root,
            runner=run_from_workspace,
            prepare_base=prepare_base,
            baseline_id=baseline_id,
        )
        return _reclassify_missing_target_gate(probe, command=cmd, workspace_root=root)
    holder = Path(tempfile.mkdtemp(prefix="pacer-workspace-probe-"))
    target = holder / "workspace"
    try:
        baseline_id, reason = _copy_workspace_snapshot(root, target)
        if baseline_id is None:
            return _probe("unknown", reason=reason, base_ref="WORKTREE")
        cache_base = f"WORKTREE:{baseline_id}"
        cached = _cache_read(root, cache_base, cmd)
        if cached is not None:
            return {**cached, "cached": True}
        execute = runner or run_command_verification
        result = execute(
            command=cmd,
            repo_root=target,
            timeout_seconds=timeout_seconds,
            env=env,
            verification_env=verification_env,
        )
        probe = _grade_probe_result(result, "WORKTREE")
        _cache_write(root, cache_base, cmd, probe)
        return _reclassify_missing_target_gate(probe, command=cmd, workspace_root=root)
    except Exception as exc:  # noqa: BLE001 - an unusable snapshot is unknown evidence
        return _probe("unknown", reason="workspace_probe_error", base_ref="WORKTREE", detail=str(exc)[:400])
    finally:
        _discard(holder)


def _grade_probe_result(result: dict[str, Any], base: str) -> dict[str, Any]:
    verdict = str(result.get("verdict") or "")
    failure_kind = str(result.get("failure_kind") or "")
    if result.get("exit_code") == 0 or verdict == "pass":
        probe = _probe("passed_on_base", reason="gate_green_before_change", base_ref=base, exit_code=result.get("exit_code"))
        probe["passed"] = True
        probe["failed_on_base"] = False
    elif failure_kind in _INCONCLUSIVE_FAILURE_KINDS:
        probe = _probe("unknown", reason=f"base_run_{failure_kind}", base_ref=base, exit_code=result.get("exit_code"))
    elif verdict == "fail":
        health = assess_gate_health(result)
        if health.get("status") == "broken":
            # 一道跑不起来的门无论如何都红，所以它证明不了"改动前是失败的"，
            # 只证明它自己坏了。当成 failed_on_base 会让人以为契约成立。
            probe = _probe(
                "gate_broken",
                reason=str(health.get("reason") or "gate_not_runnable"),
                base_ref=base,
                exit_code=result.get("exit_code"),
                detail=str(health.get("signal") or "")[:120],
            )
        else:
            probe = _probe(
                "failed_on_base", reason="gate_red_before_change", base_ref=base, exit_code=result.get("exit_code")
            )
            probe["passed"] = False
            probe["failed_on_base"] = True
    else:
        probe = _probe("unknown", reason=f"base_verdict_{verdict or 'missing'}", base_ref=base)
    output = str(result.get("output_tail") or result.get("raw_output_tail") or "")
    probe["output_tail"] = output[-4000:]
    if probe.get("status") == "passed_on_base":
        counts = re.findall(r"(?:^|\s)(\d+)\s+passed\b", output, re.IGNORECASE)
        probe["passed_count"] = int(counts[-1]) if counts else None
    return probe


def _reclassify_missing_target_gate(probe: dict[str, Any], *, command: str, workspace_root: Path) -> dict[str, Any]:
    """Only a failed, un-runnable gate for a not-yet-created target is red-on-base.

    A zero exit code is authoritative and must never be rewritten as a failing
    baseline merely because a planned target is not on disk yet.
    """
    if probe.get("exit_code") in (None, 0) or probe.get("status") != "gate_broken":
        return probe
    paths = re.findall(
        r'''(?<![\w.-])((?:tests?|src|[A-Za-z0-9_.-]+)(?:[/\\][A-Za-z0-9_.-]+)*[/\\](?:test_[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+_test)\.py|(?:tests?|src)[/\\][A-Za-z0-9_.-]+\.py)''',
        str(command or ""),
        flags=re.IGNORECASE,
    )
    missing = []
    for raw in paths:
        candidate = (workspace_root / raw).resolve()
        if candidate.is_relative_to(workspace_root) and not candidate.is_file():
            missing.append(Path(raw).as_posix())
    if not missing:
        return probe
    return {
        **probe,
        "status": "failed_on_base",
        "passed": False,
        "failed_on_base": True,
        "reason": "target_or_test_missing_before_coding",
        "detail": ", ".join(dict.fromkeys(missing)),
    }


def _copy_workspace_snapshot(source_root: Path, target_root: Path) -> tuple[str | None, str]:
    """Copy a bounded source tree and hash it; refuse links and oversized trees."""
    digest = hashlib.sha256(b"pacer-worktree-snapshot-v1\0")
    total_bytes = 0
    file_count = 0

    def copy_directory(source: Path, target: Path) -> None:
        nonlocal total_bytes, file_count
        target.mkdir(parents=True, exist_ok=True)
        try:
            children = sorted(source.iterdir(), key=lambda item: item.name.lower())
        except OSError as exc:
            raise ValueError("workspace-snapshot-unreadable") from exc
        for child in children:
            name = child.name.lower()
            if name in _OVERLAY_SKIP_PARTS:
                continue
            if name in _OVERLAY_SECRET_NAMES or child.suffix.lower() in {".pem", ".p12", ".pfx", ".key"}:
                continue
            if child.is_symlink():
                raise ValueError("workspace-snapshot-contains-symlink")
            destination = target / child.name
            relative = child.relative_to(source_root).as_posix()
            if child.is_dir():
                copy_directory(child, destination)
                continue
            if not child.is_file():
                continue
            file_count += 1
            size = child.stat().st_size
            total_bytes += size
            if size > _OVERLAY_MAX_FILE_BYTES or total_bytes > _SNAPSHOT_MAX_TOTAL_BYTES:
                raise ValueError("workspace-snapshot-too-large")
            if file_count > _SNAPSHOT_MAX_FILES:
                raise ValueError("workspace-snapshot-too-many-files")
            data = child.read_bytes()
            digest.update(relative.encode("utf-8", errors="replace") + b"\0")
            digest.update(size.to_bytes(8, "big") + hashlib.sha256(data).digest())
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, destination)

    try:
        copy_directory(source_root, target_root)
    except (OSError, ValueError) as exc:
        return None, str(exc) or "workspace-snapshot-unavailable"
    return digest.hexdigest(), ""


def classify_acceptance(
    *,
    command_result: dict[str, Any] | None,
    base_probe: dict[str, Any] | None,
) -> dict[str, Any]:
    """Grade acceptance evidence into a tier the product may quote."""

    command = command_result if isinstance(command_result, dict) else {}
    if not str(command.get("command") or "").strip():
        return _tier(
            TIER_UNVERIFIED,
            "acceptance_no_command_gate",
            "没有验收命令，这次改动没有任何独立证据。",
        )
    probe = base_probe if isinstance(base_probe, dict) else {}
    status = str(probe.get("status") or "")

    if str(command.get("verdict") or "") != "pass":
        if status == "gate_broken":
            return _tier(
                TIER_UNVERIFIED,
                "acceptance_gate_broken",
                "验收命令本身跑不起来（不是失败，是没执行），所以它既证明不了做成了，也证明不了没做成。先修好这条命令。",
            )
        if status == "failed_on_base":
            # The gate was already red before the worker touched anything.
            return _tier(
                TIER_UNVERIFIED,
                "acceptance_pre_existing_failure",
                "验收命令在这次改动之前就已经失败了，所以这不是 worker 改坏的——是验收环境本身跑不起来。",
            )
        if status == "passed_on_base":
            return _tier(
                TIER_UNVERIFIED,
                "acceptance_regression_introduced",
                "验收命令在改动前是通过的，改动后失败了——这次改动确实破坏了原有行为。",
            )
        return _tier(
            TIER_UNVERIFIED,
            "acceptance_command_failed",
            "验收命令没有通过。",
        )
    if status == "failed_on_base":
        return _tier(
            TIER_VERIFIED,
            "acceptance_gate_discriminating",
            "验收命令在改动前是失败的、现在通过了，证明这次改动做到了要求的事。",
            discriminating=True,
        )
    if status == "passed_on_base":
        return _tier(
            TIER_REGRESSION_CLEAR,
            "acceptance_gate_not_discriminating",
            "验收命令在改动前就已经通过，它只证明没弄坏别的东西，不能证明目标达成。",
            discriminating=False,
        )
    return _tier(
        TIER_REGRESSION_CLEAR,
        "acceptance_discrimination_unknown",
        "无法在改动前的版本上重跑验收命令，因此只能确认没弄坏，不能确认目标达成。",
        detail=str(probe.get("reason") or ""),
    )


def judge_red_to_green(*, base_passed: bool | None, current_passed: bool) -> dict[str, Any]:
    """Accept only an exam that failed before the edit and passes after it."""
    if not current_passed:
        return {"verdict": "rejected", "reason": "exam_still_failing", "tier": TIER_UNVERIFIED}
    if base_passed is True:
        return {
            "verdict": "rejected",
            "reason": "no red-to-green discrimination",
            "tier": "regression_only_insufficient",
        }
    if base_passed is False:
        return {"verdict": "verified", "reason": "red_to_green", "tier": TIER_VERIFIED}
    return {"verdict": "unknown", "reason": "base_not_probed", "tier": TIER_REGRESSION_CLEAR}


def acceptance_tier_label(tier: str) -> str:
    return {
        TIER_VERIFIED: "已验证达成目标",
        TIER_REGRESSION_CLEAR: "只证明没弄坏",
        TIER_UNVERIFIED: "无验收证据",
    }.get(str(tier or ""), str(tier or ""))


def _tier(
    tier: str,
    reason_code: str,
    message: str,
    *,
    discriminating: bool | None = None,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "tier": tier,
        "reason_code": reason_code,
        "message": message,
        "discriminating": discriminating,
        "detail": detail,
        "label": acceptance_tier_label(tier),
    }


def _probe(
    status: str,
    *,
    reason: str,
    base_ref: str,
    exit_code: Any = None,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": status,
        "reason": reason,
        "base_ref": base_ref,
        "exit_code": exit_code,
        "detail": detail,
        "cached": False,
    }


def _remove_worktree(repo_root: Path, target: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(target)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )


def _discard(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _cache_path(workspace_root: str | Path | None) -> Path | None:
    if not workspace_root:
        return None
    return Path(workspace_root).expanduser().resolve() / ".pacer" / _PROBE_CACHE_NAME


def _cache_key(base_ref: str, command: str) -> str:
    return f"{base_ref}\n{command}"


def _cache_read(workspace_root: str | Path | None, base_ref: str, command: str) -> dict[str, Any] | None:
    path = _cache_path(workspace_root)
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entry = payload.get(_cache_key(base_ref, command)) if isinstance(payload, dict) else None
    return entry if isinstance(entry, dict) else None


def _cache_write(workspace_root: str | Path | None, base_ref: str, command: str, probe: dict[str, Any]) -> None:
    path = _cache_path(workspace_root)
    if path is None:
        return
    payload: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            payload = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            payload = {}
    payload[_cache_key(base_ref, command)] = probe
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return
