from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .codex_exec import load_codex_user_defaults
from .codex_launcher import DEFAULT_AUTO_COMPACT_TOKEN_LIMIT, _native_codex_command
from .pacer_launch_context import read_active_launch, read_reconciled_active_launch
from .pacer_pillars import assess_five_pillars
from .pacer_verification import audit_pacer_verification_batch
from .security import scrub_secrets
from .user_profile import load_user_profile


_ACCOUNT_CACHE: tuple[float, dict[str, Any]] | None = None
ACCOUNT_CACHE_SECONDS = 15.0


def inspect_codex_account(
    *,
    executable: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout_seconds: float = 5.0,
    use_cache: bool = True,
) -> dict[str, Any]:
    global _ACCOUNT_CACHE
    now = time.monotonic()
    if use_cache and executable is None and _ACCOUNT_CACHE and now - _ACCOUNT_CACHE[0] < ACCOUNT_CACHE_SECONDS:
        return dict(_ACCOUNT_CACHE[1])

    resolved = executable or shutil.which("codex.cmd") or shutil.which("codex")
    base = {
        "installed": bool(resolved),
        "authenticated": False,
        "auth_method": "none",
        "status": "not_installed" if not resolved else "unknown",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    if not resolved:
        return base

    command = _native_codex_command(Path(resolved), ["login", "status"])
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        result = {**base, "status": "timeout"}
    except OSError:
        result = {**base, "status": "probe_failed"}
    else:
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}".lower()
        authenticated = completed.returncode == 0 and "logged in" in output and "not logged in" not in output
        if authenticated and "chatgpt" in output:
            method = "chatgpt_subscription"
        elif authenticated and ("api key" in output or "access token" in output):
            method = "api_key"
        elif authenticated:
            method = "codex_login"
        else:
            method = "none"
        result = {
            **base,
            "authenticated": authenticated,
            "auth_method": method,
            "status": "authenticated" if authenticated else "not_authenticated",
        }
    if use_cache and executable is None:
        _ACCOUNT_CACHE = (now, dict(result))
    return result


def build_pacer_support_snapshot(
    workspace_root: str | Path,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(workspace_root).expanduser().resolve()
    repo = _resolve_support_repo_root(root, repo_root)
    defaults = load_codex_user_defaults()
    account = inspect_codex_account()
    profile = load_user_profile().to_public_dict()
    outcomes, outcome_conflicted_run_ids = _read_native_outcomes(root, repo)
    command_runs, command_conflicted_run_ids = _read_command_runs(root, repo)
    documentation_compile_run_ids = {
        _outcome_run_id(item)
        for item in outcomes
        if _task_review_allows_compile_only(item.get("task_review"))
    }
    command_runs = [
        _reaudit_command_run(item, allow_compile_only=True)
        if str(item.get("run_id") or "") in documentation_compile_run_ids
        else item
        for item in command_runs
    ]
    conflicted_run_ids = sorted(set(outcome_conflicted_run_ids) | set(command_conflicted_run_ids))
    # Older or partially migrated workspaces may only have the durable active
    # pointer, without a per-launch context file. Keep its negative mechanical
    # evidence visible instead of falling back to a falsely green legacy run.
    active_launch = read_reconciled_active_launch(root) or read_active_launch(root)
    active_pillar_assessment = assess_five_pillars(active_launch)
    launches = _read_launches(root, repo)
    storage = _pacer_storage_status(
        root,
        repo,
        conflicted_run_ids=conflicted_run_ids,
        outcome_conflicted_run_ids=outcome_conflicted_run_ids,
        command_conflicted_run_ids=command_conflicted_run_ids,
    )
    conflicted_run_id_set = set(conflicted_run_ids)
    command_runs_by_id = {
        str(item.get("run_id") or ""): item
        for item in command_runs
        if str(item.get("run_id") or "") not in conflicted_run_id_set
        and str(item.get("run_id") or "")
    }
    verified_runs_by_id = {
        run_id: item
        for run_id, item in command_runs_by_id.items()
        if bool(item.get("_verification_valid"))
    }
    verified_run_ids = set(verified_runs_by_id)
    outcomes = [
        _annotate_outcome_evidence(item, command_runs_by_id)
        for item in outcomes
    ]

    completed = sum(1 for item in outcomes if str(item.get("status") or "") == "completed")
    failed = sum(1 for item in outcomes if str(item.get("status") or "") == "failed")
    blocked = sum(1 for item in outcomes if str(item.get("status") or "") == "blocked")
    passed_runs = sum(
        1
        for item in command_runs
        if str(item.get("status") or "") == "passed"
        and str(item.get("run_id") or "") not in conflicted_run_id_set
    )
    verified_runs = len(verified_run_ids)
    invalid_verification_runs = sum(
        1
        for item in command_runs
        if str(item.get("status") or "") == "passed"
        and not bool(item.get("_verification_valid"))
        and str(item.get("run_id") or "") not in conflicted_run_id_set
    )
    failed_runs = sum(1 for item in command_runs if str(item.get("status") or "") in {"failed", "blocked"})
    executed_steps = sum(int(item.get("executed_steps") or 0) for item in command_runs)
    passed_steps = sum(int(item.get("passed") or 0) for item in command_runs)
    elapsed_seconds = round(sum(float(item.get("elapsed_seconds") or 0.0) for item in command_runs), 3)
    latest_outcome = outcomes[-1] if outcomes else {}
    latest_run = command_runs[0] if command_runs else {}
    latest_launch = launches[0] if launches else {}
    telemetry = _compact_rollout_telemetry(latest_launch)
    native_sources = _pacer_native_sources(root, repo)
    history_sources = [
        str(path / "history.jsonl")
        for path in native_sources
        if (path / "history.jsonl").is_file()
    ]
    command_sources = [str(path / "commands") for path in native_sources if (path / "commands").is_dir()]
    launch_sources = [str(path / "launches") for path in native_sources if (path / "launches").is_dir()]

    return scrub_secrets(
        {
            "schema_version": 1,
            "workspace_root": str(root),
            "repo_root": str(repo),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "storage": storage,
            "account": account,
            "profile": profile,
            "runtime": {
                "provider": str(defaults.get("provider") or "inherited"),
                "model": str(defaults.get("model") or "Codex default"),
                "reasoning_effort": str(defaults.get("reasoning_effort") or "default"),
                "auto_compact_limit": DEFAULT_AUTO_COMPACT_TOKEN_LIMIT,
                "native_compaction": True,
            },
            "memory": {
                "total_outcomes": len(outcomes),
                "completed": completed,
                "failed": failed,
                "blocked": blocked,
                "failed_or_blocked": failed + blocked,
                "latest": _compact_outcome(latest_outcome),
                "path": str(root / "pacer_native" / "history.jsonl"),
                "source_paths": history_sources,
            },
            "commands": {
                "total_runs": len(command_runs),
                "passed_runs": passed_runs,
                "verified_runs": verified_runs,
                "invalid_verification_runs": invalid_verification_runs,
                "failed_runs": failed_runs,
                "passed_run_ids": sorted(verified_run_ids),
                "verified_run_ids": sorted(verified_run_ids),
                "conflicted_run_ids": conflicted_run_ids,
                "outcome_conflicted_run_ids": outcome_conflicted_run_ids,
                "command_conflicted_run_ids": command_conflicted_run_ids,
                "executed_steps": executed_steps,
                "passed_steps": passed_steps,
                "elapsed_seconds": elapsed_seconds,
                "latest": _compact_command_run(latest_run),
                "root": str(root / "pacer_native" / "commands"),
                "source_roots": command_sources,
            },
            "launches": {
                "total": len(launches),
                "running": sum(1 for item in launches if str(item.get("status") or "") == "running"),
                "latest": launches[0] if launches else {},
                "recent": launches[:10],
                "active": {
                    "launch_id": str(active_launch.get("launch_id") or ""),
                    "status": str(active_launch.get("status") or ""),
                    "lifecycle_status": str(active_launch.get("status") or ""),
                    "liveness": active_launch.get("liveness")
                    if isinstance(active_launch.get("liveness"), dict)
                    else {},
                    "pillars": active_launch.get("pillars")
                    if isinstance(active_launch.get("pillars"), dict)
                    else {},
                    "assessment": active_pillar_assessment,
                },
                "root": str(root / "pacer_native" / "launches"),
                "source_roots": launch_sources,
            },
            "telemetry": telemetry,
            "recent_outcomes": [_compact_outcome(item) for item in reversed(outcomes[-5:])],
            "recent_command_runs": [_compact_command_run(item) for item in command_runs[:5]],
        }
    )


def support_snapshot_to_markdown(snapshot: dict[str, Any]) -> str:
    storage = snapshot.get("storage") if isinstance(snapshot.get("storage"), dict) else {}
    account = snapshot.get("account") if isinstance(snapshot.get("account"), dict) else {}
    profile = snapshot.get("profile") if isinstance(snapshot.get("profile"), dict) else {}
    runtime = snapshot.get("runtime") if isinstance(snapshot.get("runtime"), dict) else {}
    memory = snapshot.get("memory") if isinstance(snapshot.get("memory"), dict) else {}
    commands = snapshot.get("commands") if isinstance(snapshot.get("commands"), dict) else {}
    launches = snapshot.get("launches") if isinstance(snapshot.get("launches"), dict) else {}
    active_launch = launches.get("active") if isinstance(launches.get("active"), dict) else {}
    latest = memory.get("latest") if isinstance(memory.get("latest"), dict) else {}
    latest_run = commands.get("latest") if isinstance(commands.get("latest"), dict) else {}
    telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
    auth_label = {
        "chatgpt_subscription": "ChatGPT subscription",
        "api_key": "API key / relay token",
        "codex_login": "Codex login",
        "none": "not authenticated",
    }.get(str(account.get("auth_method") or "none"), str(account.get("auth_method") or "unknown"))
    lines = [
        "Pacer status",
        f"- Codex: {'authenticated' if account.get('authenticated') else account.get('status') or 'unknown'} ({auth_label})",
        f"- Provider / model: {runtime.get('provider') or '-'} / {runtime.get('model') or '-'}",
        f"- Pacer profile: {profile.get('email') or 'not bound'}",
        (
            f"- Local memory: {memory.get('total_outcomes') or 0} outcomes, "
            f"{memory.get('completed') or 0} completed, {memory.get('failed') or 0} failed, "
            f"{memory.get('blocked') or 0} blocked"
        ),
        (
            f"- Compact command runs: {commands.get('passed_runs') or 0}/"
            f"{commands.get('total_runs') or 0} passed, {commands.get('failed_runs') or 0} failed, "
            f"{commands.get('passed_steps') or 0}/{commands.get('executed_steps') or 0} steps"
        ),
        f"- Native auto-compaction: {runtime.get('auto_compact_limit') or 0} tokens",
    ]
    liveness = active_launch.get("liveness") if isinstance(active_launch.get("liveness"), dict) else {}
    if active_launch.get("launch_id"):
        lines.append(
            "- Active launch: "
            f"lifecycle={active_launch.get('lifecycle_status') or 'unknown'}, "
            f"liveness={liveness.get('state') or 'unknown'}"
        )
    warning = str(storage.get("warning") or "")
    if warning:
        lines.insert(1, f"WARNING: {warning}")
    if latest:
        lines.append(f"- Latest outcome: {latest.get('status') or '-'} - {latest.get('goal') or '-'}")
        task_review = latest.get("task_review") if isinstance(latest.get("task_review"), dict) else {}
        if task_review:
            from .task_review import task_review_to_markdown

            lines.extend(["", task_review_to_markdown(task_review)])
    if latest_run:
        lines.append(f"- Latest command run: {latest_run.get('status') or '-'} - {latest_run.get('run_id') or '-'}")
    if telemetry:
        usage = telemetry.get("usage") if isinstance(telemetry.get("usage"), dict) else {}
        compactions = telemetry.get("compactions") if isinstance(telemetry.get("compactions"), dict) else {}
        agents = telemetry.get("agents") if isinstance(telemetry.get("agents"), dict) else {}
        lines.append(
            "- Latest Codex rollout: "
            f"{telemetry.get('status') or '-'} ({telemetry.get('attribution_confidence') or 'none'}) · "
            f"input {int(usage.get('input_tokens') or 0):,}, cached {int(usage.get('cached_input_tokens') or 0):,}, "
            f"output {int(usage.get('output_tokens') or 0):,} · "
            f"compactions {int(compactions.get('count') or 0)} · "
            f"agents {int(agents.get('completed') or 0)}/{int(agents.get('total') or 0)} completed"
        )
    lines.extend(
        [
            "",
            "Commands:",
            "  pacer account status",
            '  pacer account bind --email "you@example.com" --name "Your name"',
            "  pacer dashboard",
        ]
    )
    return "\n".join(lines)


def _read_native_outcomes(root: Path, repo: Path) -> tuple[list[dict[str, Any]], list[str]]:
    unbatched_rows: list[dict[str, Any]] = []
    batched: dict[str, tuple[int, float, dict[str, Any], str]] = {}
    conflicted_run_ids: set[str] = set()
    expected_repo = os.path.normcase(str(repo))
    seen: set[str] = set()
    for source_priority, native_root in enumerate(_pacer_native_sources(root, repo)):
        path = native_root / "history.jsonl"
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
        except OSError:
            continue
        for line in lines:
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            payload_repo = (
                os.path.normcase(str(Path(str(payload.get("repo_root") or ".")).expanduser().resolve()))
                if isinstance(payload, dict)
                else ""
            )
            if not isinstance(payload, dict) or payload_repo != expected_repo:
                continue
            identity = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if identity in seen:
                continue
            seen.add(identity)
            run_id = _outcome_run_id(payload)
            if not run_id:
                unbatched_rows.append(payload)
                continue
            fingerprint = _outcome_fingerprint(payload)
            timestamp = _outcome_sort_key(payload)[0]
            previous = batched.get(run_id)
            if previous is not None and previous[0] != source_priority and previous[3] != fingerprint:
                conflicted_run_ids.add(run_id)
            if previous is None or source_priority < previous[0] or (
                source_priority == previous[0] and timestamp >= previous[1]
            ):
                batched[run_id] = (source_priority, timestamp, payload, fingerprint)
    rows = [*unbatched_rows, *(item[2] for item in batched.values())]
    rows.sort(key=_outcome_sort_key)
    return rows, sorted(run_id for run_id in conflicted_run_ids if run_id)


def _read_command_runs(root: Path, repo: Path) -> tuple[list[dict[str, Any]], list[str]]:
    candidates: dict[str, tuple[int, int, dict[str, Any], str]] = {}
    conflicted_run_ids: set[str] = set()
    for source_priority, native_root in enumerate(_pacer_native_sources(root, repo)):
        commands_root = native_root / "commands"
        if not commands_root.is_dir():
            continue
        try:
            paths = list(commands_root.glob("*/summary.json"))
        except OSError:
            continue
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                modified_ns = path.stat().st_mtime_ns
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            run_id = str(payload.get("run_id") or path.parent.name)
            payload = {**payload, "run_id": run_id}
            validation = audit_pacer_verification_batch(
                payload,
                expected_run_id=run_id,
            )
            payload = {
                **payload,
                "_verification_valid": validation.valid,
                "_verification_errors": list(validation.errors),
                "_verification_step_classes": list(validation.step_classes),
                "_verification_substantive_step_classes": list(
                    validation.substantive_step_classes
                ),
            }
            identity = run_id or json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            fingerprint = _command_summary_fingerprint(payload)
            previous = candidates.get(identity)
            if previous is not None and previous[3] != fingerprint:
                conflicted_run_ids.add(run_id)
            if previous is None or source_priority < previous[0] or (
                source_priority == previous[0] and modified_ns >= previous[1]
            ):
                candidates[identity] = (source_priority, modified_ns, payload, fingerprint)
    rows = [item[2] for item in candidates.values()]
    rows.sort(key=_command_run_sort_key, reverse=True)
    return rows[:100], sorted(run_id for run_id in conflicted_run_ids if run_id)


def _read_launches(root: Path, repo: Path) -> list[dict[str, Any]]:
    rows_by_id: dict[str, tuple[int, dict[str, Any]]] = {}
    allowed = {
        "launch_id",
        "status",
        "started_at",
        "completed_at",
        "repo_root",
        "mode",
        "argument_count",
        "prompt_recorded",
        "auto_compact_token_limit",
        "exit_code",
        "elapsed_seconds",
        "rollout_telemetry",
    }
    for native_root in _pacer_native_sources(root, repo):
        launch_root = native_root / "launches"
        if not launch_root.is_dir():
            continue
        try:
            paths = list(launch_root.glob("*.json"))
        except OSError:
            continue
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                modified_ns = path.stat().st_mtime_ns
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            launch_id = str(payload.get("launch_id") or path.stem)
            payload = {**payload, "launch_id": launch_id}
            compact = {key: payload.get(key) for key in allowed if key in payload}
            previous = rows_by_id.get(launch_id)
            if previous is None or modified_ns >= previous[0]:
                rows_by_id[launch_id] = (modified_ns, compact)
    rows = [item[1] for item in rows_by_id.values()]
    rows.sort(
        key=lambda item: (str(item.get("started_at") or ""), str(item.get("launch_id") or "")),
        reverse=True,
    )
    return rows[:100]


def _pacer_native_sources(root: Path, repo: Path) -> list[Path]:
    canonical = root / "pacer_native"
    if not _same_path(root, repo / ".agent-workspace"):
        return [canonical]
    misplaced = repo / "pacer_native"
    if os.path.normcase(str(canonical)) == os.path.normcase(str(misplaced)):
        return [canonical]
    return [canonical, misplaced]


def _native_root_has_data(native_root: Path) -> bool:
    if (native_root / "history.jsonl").is_file():
        return True
    for pattern in ("commands/*/summary.json", "launches/*.json"):
        try:
            if next(native_root.glob(pattern), None) is not None:
                return True
        except OSError:
            continue
    return False


def _pacer_storage_status(
    root: Path,
    repo: Path,
    *,
    conflicted_run_ids: list[str],
    outcome_conflicted_run_ids: list[str] | None = None,
    command_conflicted_run_ids: list[str] | None = None,
) -> dict[str, Any]:
    canonical = root / "pacer_native"
    misplaced = repo / "pacer_native"
    legacy_eligible = _same_path(root, repo / ".agent-workspace")
    canonical_has_data = _native_root_has_data(canonical)
    same_location = os.path.normcase(str(canonical)) == os.path.normcase(str(misplaced))
    misplaced_has_data = legacy_eligible and not same_location and _native_root_has_data(misplaced)
    if conflicted_run_ids:
        status = "inconsistent"
        conflict_text = ", ".join(conflicted_run_ids)
        warning = (
            f"Pacer command evidence conflicts for run_id(s): {conflict_text}. "
            "Canonical summaries were retained, but conflicted runs cannot prove verification."
        )
    elif misplaced_has_data and canonical_has_data:
        status = "split"
        warning = (
            f"Pacer evidence is split between {canonical} and {misplaced}. "
            "Status merged both locations without double-counting; new writes must use the canonical workspace path."
        )
    elif misplaced_has_data:
        status = "misplaced"
        warning = (
            f"Pacer evidence exists outside the canonical workspace at {misplaced}. "
            f"Status included it for recovery; new writes must use {canonical}."
        )
    else:
        status = "healthy"
        warning = ""
    return {
        "status": status,
        "canonical_root": str(canonical),
        "misplaced_root": str(misplaced),
        "canonical_has_data": canonical_has_data,
        "misplaced_has_data": misplaced_has_data,
        "legacy_eligible": legacy_eligible,
        "conflicted_run_ids": conflicted_run_ids,
        "outcome_conflicted_run_ids": list(outcome_conflicted_run_ids or []),
        "command_conflicted_run_ids": list(command_conflicted_run_ids or []),
        "warning": warning,
    }


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _command_summary_fingerprint(payload: dict[str, Any]) -> str:
    location_keys = {"run_dir", "stdout_log", "stderr_log"}

    def normalized(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): normalized(item)
                for key, item in value.items()
                if str(key) not in location_keys
            }
        if isinstance(value, list):
            return [normalized(item) for item in value]
        return value

    return json.dumps(normalized(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _outcome_fingerprint(payload: dict[str, Any]) -> str:
    semantic = {
        key: payload.get(key)
        for key in (
            "repo_root",
            "goal",
            "summary",
            "verification",
            "status",
            "evidence_level",
            "batch_run_id",
            "launch_id",
            "task_review",
        )
    }
    return json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _outcome_run_id(payload: dict[str, Any]) -> str:
    explicit = str(payload.get("batch_run_id") or "").strip()
    if re.fullmatch(r"[0-9]{8}-[0-9]{6}-[A-Za-z0-9_-]+", explicit):
        return explicit
    verification = str(payload.get("verification") or "")
    match = re.search(
        r"(?:run_id\s*=\s*|batch\s+)([0-9]{8}-[0-9]{6}-[A-Za-z0-9_-]+)",
        verification,
    )
    return match.group(1) if match else ""


def _resolve_support_repo_root(root: Path, explicit: str | Path | None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser().resolve()

    candidates: set[str] = set()
    history_path = root / "pacer_native" / "history.jsonl"
    try:
        lines = history_path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
    except OSError:
        lines = []
    for line in lines:
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and str(payload.get("repo_root") or "").strip():
            candidates.add(os.path.normcase(str(Path(str(payload["repo_root"])).expanduser().resolve())))

    launch_root = root / "pacer_native" / "launches"
    try:
        launch_paths = list(launch_root.glob("*.json"))[-100:]
    except OSError:
        launch_paths = []
    for path in launch_paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and str(payload.get("repo_root") or "").strip():
            candidates.add(os.path.normcase(str(Path(str(payload["repo_root"])).expanduser().resolve())))
    if len(candidates) == 1:
        return Path(next(iter(candidates))).resolve()
    return root.parent.resolve()


def _outcome_sort_key(item: dict[str, Any]) -> tuple[float, str]:
    recorded_at = str(item.get("recorded_at") or "")
    return (_timestamp_value(recorded_at), recorded_at)


def _command_run_sort_key(item: dict[str, Any]) -> tuple[float, str]:
    run_id = str(item.get("run_id") or "")
    timestamp = _timestamp_value(str(item.get("started_at") or item.get("completed_at") or ""))
    if not timestamp:
        try:
            timestamp = datetime.strptime(run_id[:15], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            timestamp = 0.0
    return (timestamp, run_id)


def _timestamp_value(value: str) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _compact_outcome(item: dict[str, Any]) -> dict[str, Any]:
    if not item:
        return {}
    result = {
        "recorded_at": str(item.get("recorded_at") or ""),
        "goal": str(item.get("goal") or "")[:300],
        "status": str(item.get("status") or ""),
        "summary": str(item.get("summary") or "")[:500],
        "verification": str(item.get("verification") or "")[:500],
        "evidence_level": str(item.get("evidence_level") or "self_reported"),
        "batch_run_id": str(item.get("batch_run_id") or ""),
        "verification_batch_valid": bool(item.get("verification_batch_valid")),
        "verification_errors": list(item.get("verification_errors") or []),
    }
    task_review = item.get("task_review") if isinstance(item.get("task_review"), dict) else {}
    user_report = task_review.get("user_report") if isinstance(task_review.get("user_report"), dict) else {}
    if task_review:
        result["task_review"] = {
            "schema_version": task_review.get("schema_version"),
            "kind": str(task_review.get("kind") or "pacer_task_review"),
            "valid": bool(task_review.get("valid")),
            "verdict": str(task_review.get("verdict") or ""),
            "trust": str(task_review.get("trust") or ""),
            "evidence_integrity": str(task_review.get("evidence_integrity") or "unknown"),
            "acceptance_adequacy": str(task_review.get("acceptance_adequacy") or "unknown"),
            "product_verdict": str(task_review.get("product_verdict") or "indeterminate"),
            "acceptance_assessment": _compact_acceptance_assessment(
                task_review.get("acceptance_assessment")
            ),
            "errors": list(task_review.get("errors") or [])[:8],
            "warnings": list(task_review.get("warnings") or [])[:8],
            "user_report": {
                "headline": str(user_report.get("headline") or "")[:500],
                "goal": str(user_report.get("goal") or "")[:1000],
                "completed": [str(value)[:300] for value in (user_report.get("completed") or [])[:8]],
                "not_completed": [
                    str(value)[:300] for value in (user_report.get("not_completed") or [])[:8]
                ],
                "evidence": [str(value)[:300] for value in (user_report.get("evidence") or [])[:12]],
                "blocking_issues": [
                    str(value)[:300] for value in (user_report.get("blocking_issues") or [])[:8]
                ],
                "risks": [str(value)[:300] for value in (user_report.get("risks") or [])[:8]],
                "can_trust": str(user_report.get("can_trust") or task_review.get("trust") or "no"),
                "evidence_integrity": str(
                    user_report.get("evidence_integrity")
                    or task_review.get("evidence_integrity")
                    or "unknown"
                ),
                "acceptance_adequacy": str(
                    user_report.get("acceptance_adequacy")
                    or task_review.get("acceptance_adequacy")
                    or "unknown"
                ),
                "product_verdict": str(
                    user_report.get("product_verdict")
                    or task_review.get("product_verdict")
                    or "indeterminate"
                ),
                "next_action": str(user_report.get("next_action") or "")[:500],
            },
        }
    return result


def _compact_acceptance_assessment(value: Any) -> dict[str, Any]:
    assessment = value if isinstance(value, dict) else {}
    if not assessment:
        return {}
    return {
        "schema_version": assessment.get("schema_version"),
        "standard_source": str(assessment.get("standard_source") or "unknown"),
        "standard_digest": str(assessment.get("standard_digest") or "")[:128],
        "digest_verified": bool(assessment.get("digest_verified")),
        "adequacy": str(assessment.get("adequacy") or "insufficient"),
        "final_phase": bool(assessment.get("final_phase")),
        "required_step_classes": [
            str(item)[:80] for item in (assessment.get("required_step_classes") or [])[:10]
        ],
        "observed_step_classes": [
            str(item)[:80] for item in (assessment.get("observed_step_classes") or [])[:10]
        ],
        "missing_step_classes": [
            str(item)[:80] for item in (assessment.get("missing_step_classes") or [])[:10]
        ],
        "missing_commands": [
            str(item)[:300] for item in (assessment.get("missing_commands") or [])[:10]
        ],
        "reason_codes": [str(item)[:120] for item in (assessment.get("reason_codes") or [])[:12]],
    }


def _annotate_outcome_evidence(
    item: dict[str, Any],
    command_runs_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    annotated = dict(item)
    evidence_level = str(annotated.get("evidence_level") or "")
    run_id = _outcome_run_id(annotated)
    command_run = command_runs_by_id.get(run_id)
    validation = (
        audit_pacer_verification_batch(
            command_run,
            expected_launch_id=str(annotated.get("launch_id") or "") or None,
            expected_run_id=run_id,
            allow_compile_only=_task_review_allows_compile_only(annotated.get("task_review")),
        )
        if command_run is not None
        else None
    )
    verified = bool(validation and validation.valid)
    if run_id:
        annotated["batch_run_id"] = run_id
    annotated["verification_batch_valid"] = verified
    if validation is not None:
        annotated["verification_errors"] = list(validation.errors)
    elif run_id:
        annotated["verification_errors"] = ["bound verification batch is missing or invalid"]
    if evidence_level == "verified_batch" and not verified:
        annotated["evidence_level"] = "self_reported"
        return annotated
    if evidence_level:
        return annotated
    if verified and str(annotated.get("status") or "") == "completed":
        annotated["evidence_level"] = "verified_batch"
        return annotated
    annotated["evidence_level"] = "self_reported"
    return annotated


def _task_review_allows_compile_only(task_review: Any) -> bool:
    from .task_review import task_contract_allows_compile_only

    review = task_review if isinstance(task_review, dict) else {}
    return bool(review.get("valid")) and task_contract_allows_compile_only(review.get("task_contract"))


def _reaudit_command_run(
    item: dict[str, Any],
    *,
    allow_compile_only: bool,
) -> dict[str, Any]:
    run_id = str(item.get("run_id") or "")
    validation = audit_pacer_verification_batch(
        item,
        expected_launch_id=str(item.get("launch_id") or "") or None,
        expected_run_id=run_id,
        allow_compile_only=allow_compile_only,
    )
    return {
        **item,
        "_verification_valid": validation.valid,
        "_verification_errors": list(validation.errors),
        "_verification_step_classes": list(validation.step_classes),
        "_verification_substantive_step_classes": list(
            validation.substantive_step_classes
        ),
    }


def _compact_command_run(item: dict[str, Any]) -> dict[str, Any]:
    if not item:
        return {}
    return {
        "run_id": str(item.get("run_id") or ""),
        "status": str(item.get("status") or ""),
        "elapsed_seconds": float(item.get("elapsed_seconds") or 0.0),
        "executed_steps": int(item.get("executed_steps") or 0),
        "passed": int(item.get("passed") or 0),
        "failed": int(item.get("failed") or 0),
        "timed_out": int(item.get("timed_out") or 0),
        "run_dir": str(item.get("run_dir") or ""),
        "verification_valid": bool(item.get("_verification_valid")),
        "verification_errors": list(item.get("_verification_errors") or []),
        "step_classes": list(item.get("_verification_step_classes") or []),
        "substantive_step_classes": list(
            item.get("_verification_substantive_step_classes") or []
        ),
    }


def _compact_rollout_telemetry(launch: dict[str, Any]) -> dict[str, Any]:
    raw = launch.get("rollout_telemetry") if isinstance(launch.get("rollout_telemetry"), dict) else {}
    if not raw:
        return {}
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    compactions = raw.get("compactions") if isinstance(raw.get("compactions"), dict) else {}
    agents = raw.get("agents") if isinstance(raw.get("agents"), dict) else {}
    timeline = agents.get("timeline") if isinstance(agents.get("timeline"), list) else []
    safe_timeline = [
        {
            "depth": int(item.get("depth") or 0),
            "started_at": str(item.get("started_at") or ""),
            "completed_at": str(item.get("completed_at") or ""),
            "elapsed_seconds": item.get("elapsed_seconds"),
            "status": str(item.get("status") or ""),
        }
        for item in timeline[:20]
        if isinstance(item, dict)
    ]
    return {
        "launch_id": str(launch.get("launch_id") or ""),
        "status": str(raw.get("status") or "unavailable"),
        "attribution_confidence": str(raw.get("attribution_confidence") or "none"),
        "source_files": int(raw.get("source_files") or 0),
        "usage": {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "cached_input_tokens": int(usage.get("cached_input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "reasoning_output_tokens": int(usage.get("reasoning_output_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        },
        "compactions": {
            "count": int(compactions.get("count") or 0),
        },
        "agents": {
            "total": int(agents.get("total") or 0),
            "completed": int(agents.get("completed") or 0),
            "interrupted": int(agents.get("interrupted") or 0),
            "active": int(agents.get("active") or 0),
            "timeline": safe_timeline,
        },
    }
