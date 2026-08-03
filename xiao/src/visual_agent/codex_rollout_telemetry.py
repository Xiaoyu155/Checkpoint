from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
PACER_LAUNCH_OWNERSHIP_PREFIX = "PACER_LAUNCH_OWNERSHIP_V1:"
IDENTITY_SCAN_LIMIT_BYTES = 1_048_576


@dataclass(frozen=True)
class RolloutSnapshot:
    sessions_root: Path
    captured_at: str
    files: dict[str, int]


@dataclass(frozen=True)
class _RolloutIdentity:
    path: Path
    thread_id: str
    cwd: str
    parent_thread_id: str
    preexisting: bool
    ownership_matched: bool


class RolloutActivityTracker:
    """Track file growth owned by one uniquely attributable Codex rollout tree."""

    def __init__(
        self,
        snapshot: RolloutSnapshot,
        *,
        repo_root: str | Path,
        launch_id: str = "",
        allow_preexisting_root: bool = False,
    ) -> None:
        self._snapshot = snapshot
        self._expected_cwd = _normalized_path(repo_root)
        self._ownership_marker = rollout_ownership_marker(launch_id)
        self._allow_preexisting_root = bool(allow_preexisting_root)
        self._identities: dict[str, _RolloutIdentity] = {}
        self._root_thread_id = ""
        self._root_path = ""
        self._attribution_confidence = "none"
        self._largest_sizes: dict[str, int] = {}

    def poll(self) -> dict[str, Any]:
        """Return prompt-free activity without accepting unrelated rollout growth."""
        observed_at = datetime.now(timezone.utc).isoformat()
        scan_status, current_sizes = self._current_changed_files()
        if scan_status != "ok":
            return self._observation("unavailable", observed_at=observed_at, observable=False)
        identities = [self._identity(Path(path)) for path in current_sizes]
        if any(item is None for item in identities):
            return self._observation("identity_unavailable", observed_at=observed_at, observable=False)
        identities = [item for item in identities if item is not None]
        matching_cwd_roots = [
            item
            for item in identities
            if not item.parent_thread_id and _normalized_path(item.cwd) == self._expected_cwd
        ]
        matching_roots = (
            [item for item in matching_cwd_roots if item.ownership_matched]
            if self._ownership_marker
            else matching_cwd_roots
        )
        roots = [item for item in matching_roots if self._allow_preexisting_root or not item.preexisting]

        if not self._root_thread_id:
            if not current_sizes:
                return self._observation("no_rollout", observed_at=observed_at)
            if not roots:
                if matching_roots and all(item.preexisting for item in matching_roots):
                    return self._observation("preexisting_only", observed_at=observed_at)
                if self._ownership_marker and matching_cwd_roots:
                    return self._observation(
                        "ownership_unmatched",
                        observed_at=observed_at,
                        ignored_concurrent_roots=len(matching_cwd_roots),
                    )
                return self._observation("no_match", observed_at=observed_at)
            if len(roots) > 1:
                return self._observation(
                    "ambiguous",
                    observed_at=observed_at,
                    attribution_confidence="low",
                    ignored_concurrent_roots=len(roots),
                )
            root = roots[0]
            self._root_thread_id = root.thread_id
            self._root_path = str(root.path.resolve())
            if self._ownership_marker:
                self._attribution_confidence = "high"
            else:
                self._attribution_confidence = "low"

        selected_ids = {self._root_thread_id}
        selected: list[_RolloutIdentity] = []
        remaining = list(identities)
        changed = True
        while changed:
            changed = False
            for item in list(remaining):
                if item.thread_id in selected_ids or item.parent_thread_id in selected_ids:
                    selected.append(item)
                    selected_ids.add(item.thread_id)
                    remaining.remove(item)
                    changed = True

        activity_observed = False
        selected_paths: set[str] = set()
        for item in selected:
            path_key = str(item.path.resolve())
            selected_paths.add(path_key)
            size = int(current_sizes.get(path_key, 0))
            previous = int(self._largest_sizes.get(path_key, self._snapshot.files.get(path_key, 0)))
            if size > previous:
                activity_observed = True
                self._largest_sizes[path_key] = size

        ignored_roots = sum(
            1 for item in matching_cwd_roots if str(item.path.resolve()) != self._root_path
        )
        return self._observation(
            "captured",
            observed_at=observed_at,
            activity_observed=activity_observed,
            source_files=len(selected_paths),
            attribution_confidence=self._attribution_confidence,
            ignored_concurrent_roots=ignored_roots,
        )

    def _current_changed_files(self) -> tuple[str, dict[str, int]]:
        root = self._snapshot.sessions_root
        try:
            root.stat()
        except FileNotFoundError:
            return "ok", {}
        except OSError:
            return "unavailable", {}
        if not root.is_dir():
            return "unavailable", {}
        result: dict[str, int] = {}
        try:
            candidates = root.glob("**/rollout-*.jsonl")
            for path in candidates:
                try:
                    resolved = str(path.resolve())
                    size = path.stat().st_size
                except OSError:
                    return "unavailable", {}
                if size > int(self._snapshot.files.get(resolved, 0)):
                    result[resolved] = size
        except OSError:
            return "unavailable", {}
        return "ok", result

    def _identity(self, path: Path) -> _RolloutIdentity | None:
        key = str(path.resolve())
        cached = self._identities.get(key)
        if cached is not None and (not self._ownership_marker or cached.ownership_matched):
            return cached
        baseline_size = int(self._snapshot.files.get(key, 0))
        identity = _read_rollout_identity(
            path,
            baseline_size=baseline_size,
            ownership_marker=self._ownership_marker,
        )
        if identity is not None:
            self._identities[key] = identity
        return identity

    @staticmethod
    def _observation(
        status: str,
        *,
        observed_at: str,
        activity_observed: bool = False,
        source_files: int = 0,
        attribution_confidence: str = "none",
        ignored_concurrent_roots: int = 0,
        observable: bool = True,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "activity_observed": activity_observed,
            "observed_at": observed_at,
            "source_files": source_files,
            "attribution_confidence": attribution_confidence,
            "ignored_concurrent_roots": ignored_concurrent_roots,
            "observable": observable,
        }


@dataclass
class _RolloutEvidence:
    path: Path
    preexisting: bool
    thread_id: str
    cwd: str
    parent_thread_id: str
    depth: int
    session_started_at: str
    baseline_usage: dict[str, int]
    final_usage: dict[str, int]
    current_usage: dict[str, int]
    usage_timeline: list[tuple[str, dict[str, int]]]
    baseline_compactions: set[str]
    compactions: dict[str, str]
    activity_started_at: str
    terminal_timeline: list[tuple[str, str]]
    provider: str
    model: str
    reasoning_effort: str
    ownership_matched: bool

    @property
    def usage_delta(self) -> dict[str, int]:
        return {
            field: max(0, int(self.final_usage.get(field) or 0) - int(self.baseline_usage.get(field) or 0))
            for field in TOKEN_FIELDS
        }


def capture_rollout_snapshot(codex_home: str | Path | None = None) -> RolloutSnapshot:
    """Capture only rollout file sizes; no conversation content is retained."""
    home = _codex_home(codex_home)
    sessions_root = home / "sessions"
    files: dict[str, int] = {}
    if sessions_root.is_dir():
        try:
            candidates = sessions_root.glob("**/rollout-*.jsonl")
            for path in candidates:
                try:
                    files[str(path.resolve())] = path.stat().st_size
                except OSError:
                    continue
        except OSError:
            pass
    return RolloutSnapshot(
        sessions_root=sessions_root.resolve(),
        captured_at=datetime.now(timezone.utc).isoformat(),
        files=files,
    )


def _read_rollout_identity(
    path: Path,
    *,
    baseline_size: int,
    ownership_marker: str = "",
) -> _RolloutIdentity | None:
    """Read only bounded session metadata; never retain prompt or response content."""
    thread_id = ""
    cwd = ""
    parent_thread_id = ""
    ownership_matched = False
    try:
        with path.open("rb") as handle:
            scanned = 0
            while scanned < IDENTITY_SCAN_LIMIT_BYTES:
                raw_line = handle.readline(IDENTITY_SCAN_LIMIT_BYTES - scanned + 1)
                if not raw_line:
                    break
                scanned += len(raw_line)
                if scanned > IDENTITY_SCAN_LIMIT_BYTES:
                    break
                try:
                    event = json.loads(raw_line.decode("utf-8", errors="replace"))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if not isinstance(event, dict) or str(event.get("type") or "") != "session_meta":
                    continue
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
                subagent = source.get("subagent") if isinstance(source.get("subagent"), dict) else {}
                spawn = subagent.get("thread_spawn") if isinstance(subagent.get("thread_spawn"), dict) else {}
                thread_id = str(payload.get("session_id") or payload.get("id") or "")
                if not thread_id:
                    return None
                cwd = str(payload.get("cwd") or "")
                parent_thread_id = str(spawn.get("parent_thread_id") or "")
                break
            if ownership_marker:
                marker = ownership_marker.encode("ascii")
                handle.seek(max(0, int(baseline_size)))
                remaining = IDENTITY_SCAN_LIMIT_BYTES
                overlap = b""
                while remaining > 0:
                    chunk = handle.read(min(65_536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    searchable = overlap + chunk
                    if marker in searchable:
                        ownership_matched = True
                        break
                    overlap = searchable[-max(0, len(marker) - 1) :]
    except OSError:
        return None
    if not thread_id:
        return None
    return _RolloutIdentity(
        path=path,
        thread_id=thread_id,
        cwd=cwd,
        parent_thread_id=parent_thread_id,
        preexisting=baseline_size > 0,
        ownership_matched=ownership_matched,
    )


def aggregate_rollout_telemetry(
    snapshot: RolloutSnapshot,
    *,
    repo_root: str | Path,
    launch_id: str = "",
    completed_at: str | None = None,
) -> dict[str, Any]:
    """Return prompt-free telemetry for one uniquely attributable Codex launch."""
    completed = completed_at or datetime.now(timezone.utc).isoformat()
    base = {
        "schema_version": 1,
        "status": "unavailable",
        "attribution_confidence": "none",
        "captured_at": snapshot.captured_at,
        "completed_at": completed,
        "candidate_roots": 0,
        "source_files": 0,
        "usage": _zero_usage(),
        "current_context_usage": _zero_usage(),
        "compactions": {"count": 0, "timestamps": []},
        "agents": {"total": 0, "completed": 0, "interrupted": 0, "active": 0, "timeline": []},
        "warnings": [],
        "runtime": {"provider": "", "model": "", "reasoning_effort": ""},
    }
    ownership_marker = rollout_ownership_marker(launch_id)
    base["ownership"] = {
        "scheme": "launch_marker_v1" if ownership_marker else "legacy_cwd_time",
        "required": bool(ownership_marker),
        "matched": False,
    }
    evidence = _changed_rollouts(snapshot, ownership_marker=ownership_marker)
    if not evidence:
        return {**base, "status": "no_rollout", "warnings": ["no rollout changed during this launch"]}

    expected_cwd = _normalized_path(repo_root)
    cwd_roots = [
        item for item in evidence if not item.parent_thread_id and _normalized_path(item.cwd) == expected_cwd
    ]
    roots = (
        [item for item in cwd_roots if item.ownership_matched]
        if ownership_marker
        else cwd_roots
    )
    base["candidate_roots"] = len(roots)
    if not roots:
        if ownership_marker and cwd_roots:
            return {
                **base,
                "status": "ownership_unmatched",
                "warnings": ["changed same-cwd rollouts did not contain this Pacer launch marker"],
            }
        return {**base, "status": "no_match", "warnings": ["no changed root rollout matched the launch cwd"]}
    if len(roots) > 1:
        return {
            **base,
            "status": "ambiguous",
            "attribution_confidence": "low",
            "warnings": ["multiple root rollouts matched this cwd; usage was not attributed"],
        }

    root = roots[0]
    selected = _descendants(root, evidence)
    by_thread = {item.thread_id: item for item in selected}
    for item in sorted(selected, key=lambda candidate: candidate.depth):
        if item.parent_thread_id and not any(item.baseline_usage.values()):
            parent = by_thread.get(item.parent_thread_id)
            if parent is not None:
                _maximize_usage(item.baseline_usage, _usage_at(parent, item.session_started_at))
    usage = _zero_usage()
    compactions: dict[str, str] = {}
    baseline_compactions = set().union(*(item.baseline_compactions for item in selected))
    for item in selected:
        for field, value in item.usage_delta.items():
            usage[field] += value
        for key, timestamp in item.compactions.items():
            if key not in baseline_compactions:
                compactions.setdefault(key, timestamp)

    agents = [_agent_timeline(item) for item in selected if item.parent_thread_id]
    confidence = "high" if ownership_marker else "low"
    status = "captured" if ownership_marker else "captured_legacy"
    warnings = (
        []
        if ownership_marker
        else ["legacy launch lacks an ownership marker; attribution used only cwd/time-window evidence"]
    )
    return {
        **base,
        "status": status,
        "attribution_confidence": confidence,
        "ownership": {
            **base["ownership"],
            "matched": bool(ownership_marker),
        },
        "source_files": len(selected),
        "sessions": [
            {
                "path": str(item.path.resolve()),
                "session_id": item.thread_id,
                "parent_session_id": item.parent_thread_id,
                "cwd": item.cwd,
                "depth": item.depth,
                "started_at": item.session_started_at,
            }
            for item in selected
        ],
        "usage": usage,
        "current_context_usage": dict(root.current_usage),
        "compactions": {
            "count": len(compactions),
            "timestamps": sorted(timestamp for timestamp in compactions.values() if timestamp),
        },
        "agents": {
            "total": len(agents),
            "completed": sum(1 for item in agents if item["status"] == "completed"),
            "interrupted": sum(1 for item in agents if item["status"] == "interrupted"),
            "active": sum(1 for item in agents if item["status"] == "active"),
            "timeline": agents,
        },
        "warnings": warnings,
        "runtime": {
            "provider": root.provider,
            "model": root.model,
            "reasoning_effort": root.reasoning_effort,
        },
    }


def _changed_rollouts(
    snapshot: RolloutSnapshot,
    *,
    ownership_marker: str = "",
) -> list[_RolloutEvidence]:
    root = snapshot.sessions_root
    if not root.is_dir():
        return []
    evidence: list[_RolloutEvidence] = []
    try:
        candidates = root.glob("**/rollout-*.jsonl")
        for path in candidates:
            try:
                resolved = str(path.resolve())
                old_size = int(snapshot.files.get(resolved, 0))
                if path.stat().st_size <= old_size:
                    continue
                item = _parse_rollout(
                    path,
                    baseline_size=old_size,
                    ownership_marker=ownership_marker,
                )
            except OSError:
                continue
            if item.thread_id:
                evidence.append(item)
    except OSError:
        return []
    return evidence


def _parse_rollout(
    path: Path,
    *,
    baseline_size: int,
    ownership_marker: str = "",
) -> _RolloutEvidence:
    thread_id = ""
    cwd = ""
    parent_thread_id = ""
    depth = 0
    session_started_at = ""
    baseline_usage = _zero_usage()
    final_usage = _zero_usage()
    current_usage = _zero_usage()
    baseline_compactions: set[str] = set()
    compactions: dict[str, str] = {}
    activity_started_at = ""
    usage_timeline: list[tuple[str, dict[str, int]]] = []
    terminal_timeline: list[tuple[str, str]] = []
    provider = ""
    model = ""
    reasoning_effort = ""
    ownership_matched = False
    ownership_bytes = ownership_marker.encode("ascii") if ownership_marker else b""
    offset = 0

    with path.open("rb") as handle:
        for raw_line in handle:
            offset += len(raw_line)
            if ownership_bytes and offset > baseline_size and ownership_bytes in raw_line:
                ownership_matched = True
            try:
                event = json.loads(raw_line.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            timestamp = str(event.get("timestamp") or "")
            before_launch = offset <= baseline_size

            if not thread_id and str(event.get("type") or "") == "session_meta":
                thread_id = str(payload.get("session_id") or payload.get("id") or "")
                cwd = str(payload.get("cwd") or "")
                session_started_at = timestamp or str(payload.get("timestamp") or "")
                source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
                subagent = source.get("subagent") if isinstance(source.get("subagent"), dict) else {}
                spawn = subagent.get("thread_spawn") if isinstance(subagent.get("thread_spawn"), dict) else {}
                parent_thread_id = str(spawn.get("parent_thread_id") or "")
                depth = _integer(spawn.get("depth"))
                provider = str(payload.get("model_provider") or "")

            if str(event.get("type") or "") == "turn_context":
                model = str(payload.get("model") or model)
                reasoning_effort = str(payload.get("effort") or reasoning_effort)

            payload_type = str(payload.get("type") or "")
            if payload_type == "token_count":
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                raw_usage = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
                normalized = {field: _integer(raw_usage.get(field)) for field in TOKEN_FIELDS}
                raw_current = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
                if raw_current:
                    current_usage = {field: _integer(raw_current.get(field)) for field in TOKEN_FIELDS}
                usage_timeline.append((timestamp, normalized))
                _maximize_usage(final_usage, normalized)
                if before_launch:
                    _maximize_usage(baseline_usage, normalized)

            if str(event.get("type") or "") == "compacted":
                key = str(payload.get("window_id") or "").strip() or f"{thread_id}:{timestamp}"
                compactions.setdefault(key, timestamp)
                if before_launch:
                    baseline_compactions.add(key)

            if not before_launch and payload_type == "task_started" and not activity_started_at:
                activity_started_at = timestamp
            if not before_launch and payload_type in {"task_complete", "turn_aborted"}:
                terminal_timeline.append((timestamp, payload_type))

    if baseline_size == 0 and parent_thread_id:
        activity_started_at = session_started_at or activity_started_at
    return _RolloutEvidence(
        path=path,
        preexisting=baseline_size > 0,
        thread_id=thread_id,
        cwd=cwd,
        parent_thread_id=parent_thread_id,
        depth=depth,
        session_started_at=session_started_at,
        baseline_usage=baseline_usage,
        final_usage=final_usage,
        current_usage=current_usage,
        usage_timeline=usage_timeline,
        baseline_compactions=baseline_compactions,
        compactions=compactions,
        activity_started_at=activity_started_at,
        terminal_timeline=terminal_timeline,
        provider=provider,
        model=model,
        reasoning_effort=reasoning_effort,
        ownership_matched=ownership_matched,
    )


def rollout_ownership_marker(launch_id: str) -> str:
    """Return a non-secret marker only for a valid Pacer launch identifier."""
    value = str(launch_id or "").strip()
    if not value or len(value) > 128:
        return ""
    if any(not (character.isascii() and (character.isalnum() or character in "_-")) for character in value):
        return ""
    return f"{PACER_LAUNCH_OWNERSHIP_PREFIX}{value}"


def _descendants(root: _RolloutEvidence, evidence: list[_RolloutEvidence]) -> list[_RolloutEvidence]:
    selected = [root]
    selected_ids = {root.thread_id}
    remaining = [item for item in evidence if item is not root]
    changed = True
    while changed:
        changed = False
        for item in list(remaining):
            if item.parent_thread_id in selected_ids:
                selected.append(item)
                selected_ids.add(item.thread_id)
                remaining.remove(item)
                changed = True
    return selected


def _agent_timeline(item: _RolloutEvidence) -> dict[str, Any]:
    first_owned_usage = next(
        (
            timestamp
            for timestamp, usage in item.usage_timeline
            if any(int(usage.get(field) or 0) > int(item.baseline_usage.get(field) or 0) for field in TOKEN_FIELDS)
        ),
        "",
    )
    terminal = next(
        (
            (timestamp, kind)
            for timestamp, kind in reversed(item.terminal_timeline)
            if first_owned_usage and timestamp >= first_owned_usage
        ),
        ("", ""),
    )
    completed_at, terminal_kind = terminal
    if terminal_kind == "task_complete":
        status = "completed"
    elif terminal_kind == "turn_aborted":
        status = "interrupted"
    else:
        status = "active"
    elapsed = _elapsed_seconds(item.activity_started_at, completed_at)
    return {
        "depth": item.depth,
        "started_at": item.activity_started_at,
        "completed_at": completed_at,
        "elapsed_seconds": elapsed,
        "status": status,
    }


def _usage_at(item: _RolloutEvidence, timestamp: str) -> dict[str, int]:
    result = _zero_usage()
    for sample_timestamp, usage in item.usage_timeline:
        if timestamp and sample_timestamp > timestamp:
            continue
        _maximize_usage(result, usage)
    return result


def _elapsed_seconds(started_at: str, completed_at: str) -> float | None:
    if not started_at or not completed_at:
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return round(max(0.0, (completed - started).total_seconds()), 3)


def _maximize_usage(target: dict[str, int], sample: dict[str, int]) -> None:
    for field in TOKEN_FIELDS:
        target[field] = max(int(target.get(field) or 0), int(sample.get(field) or 0))


def _zero_usage() -> dict[str, int]:
    return {field: 0 for field in TOKEN_FIELDS}


def _codex_home(value: str | Path | None) -> Path:
    if value is not None:
        return Path(value).expanduser().resolve()
    configured = os.environ.get("CODEX_HOME")
    return (Path(configured).expanduser() if configured else Path.home() / ".codex").resolve()


def _normalized_path(value: str | Path) -> str:
    try:
        resolved = Path(value).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        resolved = Path(str(value))
    return os.path.normcase(str(resolved))


def _integer(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0
