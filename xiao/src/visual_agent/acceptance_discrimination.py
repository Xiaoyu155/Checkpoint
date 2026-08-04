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

import json
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
) -> dict[str, Any]:
    """Run ``command`` against ``base_ref`` in a throwaway worktree."""

    cmd = str(command or "").strip()
    base = str(base_ref or "").strip()
    if not cmd:
        return _probe("unknown", reason="no_command", base_ref=base)
    if not base:
        return _probe("unknown", reason="no_base_ref", base_ref=base)

    cached = _cache_read(workspace_root, base, cmd)
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

    verdict = str(result.get("verdict") or "")
    failure_kind = str(result.get("failure_kind") or "")
    if verdict == "pass":
        probe = _probe("passed_on_base", reason="gate_green_before_change", base_ref=base, exit_code=result.get("exit_code"))
    elif failure_kind in _INCONCLUSIVE_FAILURE_KINDS:
        probe = _probe("unknown", reason=f"base_run_{failure_kind}", base_ref=base, exit_code=result.get("exit_code"))
    elif verdict == "fail":
        probe = _probe("failed_on_base", reason="gate_red_before_change", base_ref=base, exit_code=result.get("exit_code"))
    else:
        probe = _probe("unknown", reason=f"base_verdict_{verdict or 'missing'}", base_ref=base)
    _cache_write(workspace_root, base, cmd, probe)
    return probe


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
    if str(command.get("verdict") or "") != "pass":
        return _tier(
            TIER_UNVERIFIED,
            "acceptance_command_failed",
            "验收命令没有通过。",
        )

    probe = base_probe if isinstance(base_probe, dict) else {}
    status = str(probe.get("status") or "")
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
    return Path(workspace_root).expanduser().resolve() / _PROBE_CACHE_NAME


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
