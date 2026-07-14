from __future__ import annotations

import json
from pathlib import Path

from visual_agent.codex_rollout_telemetry import (
    RolloutActivityTracker,
    aggregate_rollout_telemetry,
    capture_rollout_snapshot,
    rollout_ownership_marker,
)


def _write(path: Path, events: list[dict], *, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def _session(timestamp: str, thread_id: str, cwd: Path, *, parent: str = "", depth: int = 0) -> dict:
    source: object = "cli"
    if parent:
        source = {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": depth}}}
    return {
        "timestamp": timestamp,
        "type": "session_meta",
        "payload": {"id": thread_id, "cwd": str(cwd), "source": source, "model_provider": "custom"},
    }


def _event(timestamp: str, event_type: str) -> dict:
    return {"timestamp": timestamp, "type": "event_msg", "payload": {"type": event_type}}


def _ownership(timestamp: str, launch_id: str, *, private_text: str = "") -> dict:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "user_message",
            "message": f"{rollout_ownership_marker(launch_id)}\n{private_text}",
        },
    }


def _token(timestamp: str, *, input_tokens: int, cached: int, output: int) -> dict:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached,
                    "output_tokens": output,
                    "reasoning_output_tokens": output // 2,
                    "total_tokens": input_tokens + output,
                },
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached,
                    "output_tokens": output,
                    "reasoning_output_tokens": output // 2,
                    "total_tokens": input_tokens + output,
                }
            },
        },
    }


def _compacted(timestamp: str, window_id: str) -> dict:
    return {"timestamp": timestamp, "type": "compacted", "payload": {"window_id": window_id}}


def test_rollout_telemetry_attributes_root_and_child_without_double_counting_fork_history(tmp_path) -> None:
    codex_home = tmp_path / ".codex"
    sessions = codex_home / "sessions" / "2026" / "07" / "13"
    snapshot = capture_rollout_snapshot(codex_home)
    root = sessions / "rollout-root.jsonl"
    child = sessions / "rollout-child.jsonl"
    root_id = "root-thread"
    launch_id = "launch-root-1"

    _write(
        root,
        [
            _session("2026-07-13T00:00:00.000Z", root_id, tmp_path),
            _ownership("2026-07-13T00:00:00.100Z", launch_id, private_text="private prompt"),
            {"timestamp": "2026-07-13T00:00:00.500Z", "type": "turn_context", "payload": {"model": "gpt-5.6-sol", "effort": "medium"}},
            _token("2026-07-13T00:00:01.000Z", input_tokens=100, cached=80, output=10),
            _compacted("2026-07-13T00:00:02.000Z", "window-1"),
            _token("2026-07-13T00:00:04.000Z", input_tokens=200, cached=150, output=20),
        ],
    )
    _write(
        child,
        [
            _session("2026-07-13T00:00:03.000Z", "child-thread", tmp_path, parent=root_id, depth=1),
            _token("2026-07-13T00:00:03.001Z", input_tokens=100, cached=80, output=10),
            _compacted("2026-07-13T00:00:03.002Z", "window-1"),
            _event("2026-07-13T00:00:03.100Z", "task_started"),
            _token("2026-07-13T00:00:05.000Z", input_tokens=160, cached=115, output=16),
            _compacted("2026-07-13T00:00:05.500Z", "window-2"),
            _event("2026-07-13T00:00:06.000Z", "task_complete"),
        ],
    )

    payload = aggregate_rollout_telemetry(snapshot, repo_root=tmp_path, launch_id=launch_id)

    assert payload["status"] == "captured"
    assert payload["current_context_usage"]["input_tokens"] == 200
    assert payload["runtime"] == {"provider": "custom", "model": "gpt-5.6-sol", "reasoning_effort": "medium"}
    assert payload["attribution_confidence"] == "high"
    assert payload["ownership"] == {
        "scheme": "launch_marker_v1",
        "required": True,
        "matched": True,
    }
    assert payload["usage"] == {
        "input_tokens": 260,
        "cached_input_tokens": 185,
        "output_tokens": 26,
        "reasoning_output_tokens": 13,
        "total_tokens": 286,
    }
    assert payload["compactions"]["count"] == 2
    assert payload["agents"]["total"] == 1
    assert payload["agents"]["completed"] == 1
    assert payload["agents"]["timeline"][0]["elapsed_seconds"] == 3.0
    assert "thread" not in json.dumps(payload["agents"])
    assert "private prompt" not in json.dumps(payload)
    assert rollout_ownership_marker(launch_id) not in json.dumps(payload)


def test_rollout_telemetry_uses_cumulative_delta_for_resumed_rollout(tmp_path) -> None:
    codex_home = tmp_path / ".codex"
    rollout = codex_home / "sessions" / "2026" / "07" / "13" / "rollout-resume.jsonl"
    _write(
        rollout,
        [
            _session("2026-07-12T20:00:00.000Z", "resumed-thread", tmp_path),
            _token("2026-07-12T20:01:00.000Z", input_tokens=100, cached=80, output=10),
            _compacted("2026-07-12T20:02:00.000Z", "old-window"),
        ],
    )
    snapshot = capture_rollout_snapshot(codex_home)
    launch_id = "launch-resume-1"
    _write(
        rollout,
        [
            _ownership("2026-07-13T00:00:30.000Z", launch_id),
            _token("2026-07-13T00:01:00.000Z", input_tokens=160, cached=110, output=20),
            _compacted("2026-07-13T00:02:00.000Z", "new-window"),
        ],
        append=True,
    )

    payload = aggregate_rollout_telemetry(snapshot, repo_root=tmp_path, launch_id=launch_id)

    assert payload["status"] == "captured"
    assert payload["attribution_confidence"] == "high"
    assert payload["usage"] == {
        "input_tokens": 60,
        "cached_input_tokens": 30,
        "output_tokens": 10,
        "reasoning_output_tokens": 5,
        "total_tokens": 70,
    }
    assert payload["compactions"] == {"count": 1, "timestamps": ["2026-07-13T00:02:00.000Z"]}


def test_rollout_telemetry_refuses_to_mix_concurrent_roots_for_same_cwd(tmp_path) -> None:
    codex_home = tmp_path / ".codex"
    sessions = codex_home / "sessions" / "2026" / "07" / "13"
    snapshot = capture_rollout_snapshot(codex_home)
    launch_id = "duplicate-launch"
    for index in (1, 2):
        _write(
            sessions / f"rollout-{index}.jsonl",
            [
                _session(f"2026-07-13T00:00:0{index}.000Z", f"root-{index}", tmp_path),
                _ownership(f"2026-07-13T00:00:0{index}.500Z", launch_id),
                _token(f"2026-07-13T00:00:1{index}.000Z", input_tokens=100 * index, cached=0, output=1),
            ],
        )

    payload = aggregate_rollout_telemetry(snapshot, repo_root=tmp_path, launch_id=launch_id)

    assert payload["status"] == "ambiguous"
    assert payload["attribution_confidence"] == "low"
    assert payload["candidate_roots"] == 2
    assert payload["usage"]["total_tokens"] == 0
    assert payload["source_files"] == 0


def test_rollout_telemetry_ignores_changed_rollout_from_other_cwd(tmp_path) -> None:
    codex_home = tmp_path / ".codex"
    snapshot = capture_rollout_snapshot(codex_home)
    rollout = codex_home / "sessions" / "2026" / "07" / "13" / "rollout-other.jsonl"
    other = tmp_path / "other"
    other.mkdir()
    launch_id = "launch-other-cwd"
    _write(
        rollout,
        [
            _session("2026-07-13T00:00:00.000Z", "other-root", other),
            _ownership("2026-07-13T00:00:00.100Z", launch_id),
        ],
    )

    payload = aggregate_rollout_telemetry(snapshot, repo_root=tmp_path, launch_id=launch_id)

    assert payload["status"] == "no_match"
    assert payload["candidate_roots"] == 0


def test_activity_tracker_only_renews_from_owned_rollout_tree(tmp_path) -> None:
    codex_home = tmp_path / ".codex"
    sessions = codex_home / "sessions" / "2026" / "07" / "13"
    snapshot = capture_rollout_snapshot(codex_home)
    owned = sessions / "rollout-owned.jsonl"
    other = sessions / "rollout-other.jsonl"
    child = sessions / "rollout-child.jsonl"
    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    launch_id = "tracker-owned"
    _write(
        owned,
        [
            _session("2026-07-13T00:00:00.000Z", "owned", tmp_path),
            _ownership("2026-07-13T00:00:00.100Z", launch_id),
        ],
    )
    tracker = RolloutActivityTracker(snapshot, repo_root=tmp_path, launch_id=launch_id)

    first = tracker.poll()
    assert first["status"] == "captured"
    assert first["activity_observed"] is True
    assert first["attribution_confidence"] == "high"

    _write(other, [_session("2026-07-13T00:00:01.000Z", "other", other_cwd)])
    assert tracker.poll()["activity_observed"] is False

    _write(child, [_session("2026-07-13T00:00:02.000Z", "child", tmp_path, parent="owned", depth=1)])
    child_activity = tracker.poll()
    assert child_activity["activity_observed"] is True
    assert child_activity["source_files"] == 2

    _write(owned, [_event("2026-07-13T00:00:03.000Z", "task_started")], append=True)
    assert tracker.poll()["activity_observed"] is True
    assert tracker.poll()["activity_observed"] is False


def test_activity_tracker_refuses_ambiguous_same_cwd_roots(tmp_path) -> None:
    codex_home = tmp_path / ".codex"
    sessions = codex_home / "sessions" / "2026" / "07" / "13"
    snapshot = capture_rollout_snapshot(codex_home)
    launch_id = "tracker-duplicate"
    for index in (1, 2):
        _write(
            sessions / f"rollout-{index}.jsonl",
            [
                _session(f"2026-07-13T00:00:0{index}.000Z", f"root-{index}", tmp_path),
                _ownership(f"2026-07-13T00:00:0{index}.500Z", launch_id),
            ],
        )

    activity = RolloutActivityTracker(
        snapshot,
        repo_root=tmp_path,
        launch_id=launch_id,
    ).poll()

    assert activity["status"] == "ambiguous"
    assert activity["activity_observed"] is False
    assert activity["attribution_confidence"] == "low"
    assert activity["ignored_concurrent_roots"] == 2


def test_activity_tracker_does_not_claim_preexisting_same_cwd_rollout_for_new_exec(tmp_path) -> None:
    codex_home = tmp_path / ".codex"
    sessions = codex_home / "sessions" / "2026" / "07" / "13"
    old = sessions / "rollout-old.jsonl"
    _write(old, [_session("2026-07-13T00:00:00.000Z", "old-root", tmp_path)])
    snapshot = capture_rollout_snapshot(codex_home)
    _write(old, [_event("2026-07-13T00:00:01.000Z", "task_started")], append=True)
    launch_id = "tracker-current"
    tracker = RolloutActivityTracker(snapshot, repo_root=tmp_path, launch_id=launch_id)

    old_activity = tracker.poll()
    assert old_activity["status"] == "ownership_unmatched"
    assert old_activity["activity_observed"] is False
    _write(old, [_event("2026-07-13T00:00:02.000Z", "task_complete")], append=True)
    assert tracker.poll()["activity_observed"] is False

    current = sessions / "rollout-current.jsonl"
    _write(
        current,
        [
            _session("2026-07-13T00:00:03.000Z", "current-root", tmp_path),
            _ownership("2026-07-13T00:00:03.100Z", launch_id),
        ],
    )
    current_activity = tracker.poll()
    assert current_activity["status"] == "captured"
    assert current_activity["activity_observed"] is True
    assert current_activity["attribution_confidence"] == "high"


def test_activity_tracker_requires_explicit_opt_in_for_resumed_rollout(tmp_path) -> None:
    codex_home = tmp_path / ".codex"
    rollout = codex_home / "sessions" / "2026" / "07" / "13" / "rollout-resumed.jsonl"
    _write(rollout, [_session("2026-07-13T00:00:00.000Z", "resumed-root", tmp_path)])
    snapshot = capture_rollout_snapshot(codex_home)
    launch_id = "tracker-resumed"
    _write(
        rollout,
        [
            _ownership("2026-07-13T00:00:00.500Z", launch_id),
            _event("2026-07-13T00:00:01.000Z", "task_started"),
        ],
        append=True,
    )

    resumed = RolloutActivityTracker(
        snapshot,
        repo_root=tmp_path,
        launch_id=launch_id,
        allow_preexisting_root=True,
    ).poll()

    assert resumed["status"] == "captured"
    assert resumed["activity_observed"] is True
    assert resumed["attribution_confidence"] == "high"


def test_rollout_telemetry_counts_only_owned_root_when_plain_codex_shares_cwd(tmp_path) -> None:
    codex_home = tmp_path / ".codex"
    sessions = codex_home / "sessions" / "2026" / "07" / "13"
    snapshot = capture_rollout_snapshot(codex_home)
    launch_id = "owned-concurrent"
    _write(
        sessions / "rollout-plain.jsonl",
        [
            _session("2026-07-13T00:00:00.000Z", "plain", tmp_path),
            _token("2026-07-13T00:00:01.000Z", input_tokens=900, cached=0, output=90),
        ],
    )
    _write(
        sessions / "rollout-owned.jsonl",
        [
            _session("2026-07-13T00:00:00.100Z", "owned", tmp_path),
            _ownership("2026-07-13T00:00:00.200Z", launch_id, private_text="do not expose"),
            _token("2026-07-13T00:00:01.100Z", input_tokens=100, cached=50, output=10),
        ],
    )

    payload = aggregate_rollout_telemetry(snapshot, repo_root=tmp_path, launch_id=launch_id)

    assert payload["status"] == "captured"
    assert payload["candidate_roots"] == 1
    assert payload["usage"]["input_tokens"] == 100
    assert payload["usage"]["output_tokens"] == 10
    assert "do not expose" not in json.dumps(payload)


def test_rollout_telemetry_does_not_claim_plain_same_cwd_session(tmp_path) -> None:
    codex_home = tmp_path / ".codex"
    sessions = codex_home / "sessions" / "2026" / "07" / "13"
    snapshot = capture_rollout_snapshot(codex_home)
    _write(
        sessions / "rollout-plain.jsonl",
        [
            _session("2026-07-13T00:00:00.000Z", "plain", tmp_path),
            _token("2026-07-13T00:00:01.000Z", input_tokens=900, cached=0, output=90),
        ],
    )

    payload = aggregate_rollout_telemetry(
        snapshot,
        repo_root=tmp_path,
        launch_id="absent-owner",
    )

    assert payload["status"] == "ownership_unmatched"
    assert payload["usage"]["total_tokens"] == 0
    assert payload["ownership"]["matched"] is False


def test_legacy_rollout_fallback_is_explicitly_low_confidence(tmp_path) -> None:
    codex_home = tmp_path / ".codex"
    sessions = codex_home / "sessions" / "2026" / "07" / "13"
    snapshot = capture_rollout_snapshot(codex_home)
    _write(
        sessions / "rollout-legacy.jsonl",
        [
            _session("2026-07-13T00:00:00.000Z", "legacy", tmp_path),
            _token("2026-07-13T00:00:01.000Z", input_tokens=100, cached=0, output=10),
        ],
    )

    payload = aggregate_rollout_telemetry(snapshot, repo_root=tmp_path)

    assert payload["status"] == "captured_legacy"
    assert payload["attribution_confidence"] == "low"
    assert payload["ownership"] == {
        "scheme": "legacy_cwd_time",
        "required": False,
        "matched": False,
    }


def test_activity_tracker_reports_scan_failure_as_unobservable(tmp_path, monkeypatch) -> None:
    snapshot = capture_rollout_snapshot(tmp_path / ".codex")
    tracker = RolloutActivityTracker(snapshot, repo_root=tmp_path)
    monkeypatch.setattr(tracker, "_current_changed_files", lambda: ("unavailable", {}))

    observation = tracker.poll()

    assert observation["status"] == "unavailable"
    assert observation["observable"] is False
    assert observation["activity_observed"] is False
