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

    verdict = str(result.get("verdict") or "")
    failure_kind = str(result.get("failure_kind") or "")
    if verdict == "pass":
        probe = _probe("passed_on_base", reason="gate_green_before_change", base_ref=base, exit_code=result.get("exit_code"))
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
    else:
        probe = _probe("unknown", reason=f"base_verdict_{verdict or 'missing'}", base_ref=base)
    _cache_write(workspace_root, cache_key_base, cmd, probe)
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
