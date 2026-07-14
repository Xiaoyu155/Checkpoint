from __future__ import annotations

import json
from pathlib import Path

import pytest

import visual_agent.rollout_observability as rollout_observability
from visual_agent.rollout_observability import (
    build_launch_observability,
    build_observability_snapshot,
    paginate_timeline,
    read_session_timeline,
)


def _append(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _session(timestamp: str, session_id: str, cwd: Path, *, parent: str = "", depth: int = 0) -> dict:
    source: object = "cli"
    if parent:
        source = {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": depth}}}
    return {
        "timestamp": timestamp,
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "cwd": str(cwd),
            "source": source,
            "model_provider": "custom",
        },
    }


def _token(
    timestamp: str,
    *,
    input_tokens: int,
    cached: int,
    output: int,
    reasoning: int,
    total: int,
) -> dict:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached,
                    "output_tokens": output,
                    "reasoning_output_tokens": reasoning,
                    "total_tokens": total,
                }
            },
        },
    }


def _event(timestamp: str, kind: str, **payload: object) -> dict:
    return {"timestamp": timestamp, "type": "event_msg", "payload": {"type": kind, **payload}}


def test_launch_usage_removes_fork_prefix_and_preserves_raw_ledger(tmp_path: Path) -> None:
    sessions = tmp_path / ".codex" / "sessions" / "2026" / "07" / "13"
    root = sessions / "rollout-root.jsonl"
    child = sessions / "rollout-child.jsonl"
    _append(
        root,
        [
            _session("2026-07-13T00:00:00Z", "root", tmp_path),
            _token(
                "2026-07-13T00:01:00Z",
                input_tokens=100,
                cached=80,
                output=20,
                reasoning=10,
                total=120,
            ),
        ],
    )
    baseline = {str(root.resolve()): root.stat().st_size}
    _append(
        root,
        [
            _token(
                "2026-07-13T00:04:00Z",
                input_tokens=200,
                cached=150,
                output=40,
                reasoning=20,
                total=240,
            )
        ],
    )
    _append(
        child,
        [
            _session("2026-07-13T00:02:00Z", "child", tmp_path, parent="root", depth=1),
            _event("2026-07-13T00:02:01Z", "task_started"),
            _token(
                "2026-07-13T00:02:02Z",
                input_tokens=100,
                cached=80,
                output=20,
                reasoning=10,
                total=120,
            ),
            _token(
                "2026-07-13T00:02:03Z",
                input_tokens=100,
                cached=80,
                output=20,
                reasoning=10,
                total=120,
            ),
            _token(
                "2026-07-13T00:03:00Z",
                input_tokens=150,
                cached=110,
                output=35,
                reasoning=18,
                total=185,
            ),
            _event("2026-07-13T00:03:01Z", "task_complete"),
        ],
    )

    result = build_launch_observability(
        sessions_root=tmp_path / ".codex" / "sessions",
        baseline_files=baseline,
        repo_root=tmp_path,
        launch_id="launch-1",
    )

    assert result["status"] == "ok"
    assert result["usage"]["raw_ledger"] == {
        "input_tokens": 250,
        "cached_input_tokens": 180,
        "output_tokens": 55,
        "reasoning_output_tokens": 28,
        "total_tokens": 305,
    }
    assert result["usage"]["deduplicated_actual"] == {
        "input_tokens": 150,
        "cached_input_tokens": 100,
        "output_tokens": 35,
        "reasoning_output_tokens": 18,
        "total_tokens": 185,
    }
    assert result["usage"]["deduplicated_actual"]["total_tokens"] == 185
    assert result["usage"]["uncached_input_tokens"] == 50
    samples = result["usage"]["usage_samples"]
    assert len(samples) == 4
    assert samples[0]["deduplicated_delta"]["total_tokens"] == 0
    assert samples[1]["deduplicated_delta"]["total_tokens"] == 0
    assert samples[2]["deduplicated_delta"]["total_tokens"] == 65
    assert samples[3]["deduplicated_delta"]["total_tokens"] == 120

    assert {(item["session_id"], item["role"]) for item in result["agents"]} == {
        ("root", "root"),
        ("child", "subagent"),
    }
    child_agent = next(item for item in result["agents"] if item["session_id"] == "child")
    assert child_agent["parent_session_id"] == "root"
    assert child_agent["status"] == "completed"
    assert child_agent["completed_at"] == "2026-07-13T00:03:01Z"
    assert child_agent["turn_count"] == 1
    token_events = [item for item in result["events"] if item["kind"] == "token_count"]
    assert token_events[-1]["usage_delta"]["deduplicated_actual"]["total_tokens"] == 120
    assert any(item["usage_delta"]["deduplicated_actual"]["total_tokens"] == 0 for item in token_events)


def test_resumed_file_baseline_boundary_does_not_recount_old_sample(tmp_path: Path) -> None:
    sessions = tmp_path / ".codex" / "sessions"
    rollout = sessions / "2026" / "07" / "13" / "rollout-resumed.jsonl"
    _append(
        rollout,
        [
            _session("2026-07-13T00:00:00Z", "resumed", tmp_path),
            _token(
                "2026-07-13T00:01:00Z",
                input_tokens=100,
                cached=75,
                output=10,
                reasoning=4,
                total=110,
            ),
        ],
    )
    baseline_size = rollout.stat().st_size
    _append(
        rollout,
        [
            _token(
                "2026-07-13T00:02:00Z",
                input_tokens=130,
                cached=90,
                output=15,
                reasoning=6,
                total=145,
            )
        ],
    )

    result = build_launch_observability(
        sessions_root=sessions,
        baseline_files={str(rollout.resolve()): baseline_size},
        repo_root=tmp_path,
    )

    assert len(result["usage"]["usage_samples"]) == 1
    assert result["usage"]["raw_ledger"] == {
        "input_tokens": 30,
        "cached_input_tokens": 15,
        "output_tokens": 5,
        "reasoning_output_tokens": 2,
        "total_tokens": 35,
    }
    assert result["usage"]["deduplicated_actual"] == result["usage"]["raw_ledger"]


def test_bad_json_unterminated_tail_and_oversized_line_are_marked(tmp_path: Path) -> None:
    sessions = tmp_path / ".codex" / "sessions"
    rollout = sessions / "rollout-broken.jsonl"
    _append(rollout, [_session("2026-07-13T00:00:00Z", "broken", tmp_path)])
    with rollout.open("ab") as handle:
        handle.write(b"{not-json}\n")
        handle.write(b'{"payload":{"content":"' + (b"x" * 1_048_576) + b'"}}\n')
        handle.write(b'{"timestamp":"2026-07-13T00:01:00Z","type":"event_msg"')

    result = build_launch_observability(
        sessions_root=sessions,
        baseline_files={},
        repo_root=tmp_path,
    )

    assert result["status"] == "partial"
    assert result["diagnostics"]["bad_json_lines"] == 1
    assert result["diagnostics"]["oversized_lines"] == 1
    assert result["diagnostics"]["truncated_lines"] == 1
    assert len(result["sessions"]) == 1


def test_prompt_response_arguments_and_output_are_never_returned(tmp_path: Path) -> None:
    sessions = tmp_path / ".codex" / "sessions"
    rollout = sessions / "rollout-private.jsonl"
    secrets = {
        "prompt": "PROMPT_SECRET_123",
        "response": "RESPONSE_SECRET_456",
        "arguments": '{"password":"ARGUMENT_SECRET_789"}',
        "output": "OUTPUT_SECRET_012",
    }
    _append(
        rollout,
        [
            _session("2026-07-13T00:00:00Z", "private", tmp_path),
            _event("2026-07-13T00:00:01Z", "user_message", content=secrets["prompt"]),
            {
                "timestamp": "2026-07-13T00:00:02Z",
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": secrets["response"]},
            },
            {
                "timestamp": "2026-07-13T00:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "run_command",
                    "call_id": "call-1",
                    "arguments": secrets["arguments"],
                },
            },
            {
                "timestamp": "2026-07-13T00:00:04Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": secrets["output"],
                },
            },
        ],
    )

    result = build_launch_observability(
        sessions_root=sessions,
        baseline_files={},
        repo_root=tmp_path,
    )
    serialized = json.dumps(result, ensure_ascii=False)

    assert all(secret not in serialized for secret in secrets.values())
    assert str(tmp_path) not in serialized
    calls = [event for event in result["events"] if event["kind"] == "function_call"]
    assert calls[0]["summary"]["name"] == "run_command"
    assert calls[0]["summary"]["content"]["char_count"] == len(secrets["arguments"])
    assert calls[0]["summary"]["content"]["redacted"] is True
    agent = result["agents"][0]
    assert agent["tool_count"] == 1
    assert "events" not in agent


def test_mcp_timeline_exposes_tool_identity_and_duration_without_payload(tmp_path: Path) -> None:
    sessions = tmp_path / ".codex" / "sessions"
    rollout = sessions / "rollout-mcp.jsonl"
    secret = "MCP_ARGUMENT_SECRET_123"
    _append(
        rollout,
        [
            _session("2026-07-13T00:00:00Z", "mcp-session", tmp_path),
            _event(
                "2026-07-13T00:00:01Z",
                "mcp_tool_call_end",
                call_id="call-mcp",
                invocation={
                    "server": "pacer",
                    "tool": "get_pacer_memory",
                    "arguments": {"query": secret},
                },
                plugin_id="pacer@personal",
                duration={"secs": 1, "nanos": 500_000_000},
                result={"content": secret},
            ),
        ],
    )

    result = build_launch_observability(
        sessions_root=sessions,
        baseline_files={},
        repo_root=tmp_path,
    )

    event = next(item for item in result["events"] if item["kind"] == "mcp_tool_call_end")
    assert event["summary"]["name"] == "get_pacer_memory"
    assert event["summary"]["server"] == "pacer"
    assert event["summary"]["plugin_id"] == "pacer@personal"
    assert event["duration_ms"] == 1500.0
    assert secret not in json.dumps(result, ensure_ascii=False)


def test_path_escape_is_rejected_and_baseline_escape_is_never_opened(tmp_path: Path) -> None:
    sessions = tmp_path / ".codex" / "sessions"
    rollout = sessions / "rollout-safe.jsonl"
    outside = tmp_path / "outside" / "rollout-secret.jsonl"
    _append(rollout, [_session("2026-07-13T00:00:00Z", "safe", tmp_path)])
    _append(outside, [_session("2026-07-13T00:00:00Z", "outside-secret", tmp_path)])

    with pytest.raises(ValueError, match="below sessions root"):
        read_session_timeline(sessions_root=sessions, session_path=outside)

    result = build_launch_observability(
        sessions_root=sessions,
        baseline_files={str(outside): outside.stat().st_size},
        repo_root=tmp_path,
    )
    assert result["diagnostics"]["skipped_paths"] == 1
    assert "outside-secret" not in json.dumps(result)


def test_multiple_changed_roots_for_same_repo_are_ambiguous(tmp_path: Path) -> None:
    sessions = tmp_path / ".codex" / "sessions"
    for index in (1, 2):
        _append(
            sessions / f"rollout-root-{index}.jsonl",
            [
                _session(f"2026-07-13T00:00:0{index}Z", f"root-{index}", tmp_path),
                _token(
                    f"2026-07-13T00:00:1{index}Z",
                    input_tokens=100 * index,
                    cached=0,
                    output=10,
                    reasoning=5,
                    total=100 * index + 10,
                ),
            ],
        )

    result = build_launch_observability(
        sessions_root=sessions,
        baseline_files={},
        repo_root=tmp_path,
    )

    assert result["status"] == "ambiguous"
    assert not any(result["usage"]["deduplicated_actual"].values())
    assert not result["sessions"]
    assert not result["agents"]


def test_completed_launch_window_excludes_later_same_repo_rollout(tmp_path: Path) -> None:
    sessions = tmp_path / ".codex" / "sessions"
    first = sessions / "rollout-launch-a.jsonl"
    later = sessions / "rollout-launch-b.jsonl"
    _append(
        first,
        [
            _session("2026-07-13T00:00:00Z", "launch-a", tmp_path),
            _token(
                "2026-07-13T00:01:00Z",
                input_tokens=100,
                cached=50,
                output=10,
                reasoning=4,
                total=110,
            ),
            _token(
                "2026-07-13T00:10:00Z",
                input_tokens=999,
                cached=500,
                output=99,
                reasoning=40,
                total=1098,
            ),
        ],
    )
    _append(
        later,
        [
            _session("2026-07-13T00:06:00Z", "launch-b", tmp_path),
            _token(
                "2026-07-13T00:07:00Z",
                input_tokens=200,
                cached=100,
                output=20,
                reasoning=8,
                total=220,
            ),
        ],
    )

    result = build_launch_observability(
        sessions_root=sessions,
        baseline_files={},
        repo_root=tmp_path,
        started_at="2026-07-13T00:00:00Z",
        completed_at="2026-07-13T00:05:00Z",
    )

    assert result["status"] == "ok"
    assert [item["session_id"] for item in result["sessions"]] == ["launch-a"]
    assert result["usage"]["deduplicated_actual"]["total_tokens"] == 110
    serialized = json.dumps(result)
    assert "launch-b" not in serialized
    assert "1098" not in serialized


def test_snapshot_and_timeline_pagination_are_metadata_only(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    rollout = codex_home / "sessions" / "rollout-one.jsonl"
    _append(
        rollout,
        [
            _session("2026-07-13T00:00:00Z", "one", tmp_path),
            _event("2026-07-13T00:00:01Z", "task_started"),
            {"timestamp": "2026-07-13T00:00:02Z", "type": "compacted", "payload": {"window_id": "w-1"}},
            _event("2026-07-13T00:00:03Z", "task_complete"),
        ],
    )

    snapshot = build_observability_snapshot(codex_home, repo_root=tmp_path, limit=10)
    first = paginate_timeline(snapshot["events"], limit=2)
    second = paginate_timeline(snapshot["events"], cursor=first["next_cursor"], limit=2)
    direct = read_session_timeline(sessions_root=codex_home / "sessions", session_path=rollout, limit=2)

    assert snapshot["compaction_count"] == 1
    assert first["has_more"] is True
    assert second["has_more"] is False
    assert first["items"] + second["items"] == snapshot["events"]
    assert direct["total"] == 4
    assert direct["next_cursor"] == "2"


def test_identity_prefilter_only_fully_parses_owned_tree_among_large_rollouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / ".codex" / "sessions"
    owned_repo = tmp_path / "owned-repo"
    unrelated_repo = tmp_path / "unrelated-repo"
    owned_repo.mkdir()
    unrelated_repo.mkdir()
    owned = sessions / "rollout-owned.jsonl"
    _append(
        owned,
        [
            _session("2026-07-13T00:00:00Z", "owned", owned_repo),
            _event("2026-07-13T00:00:01Z", "task_started"),
        ],
    )

    large_body = "unrelated-body-" + ("x" * 131_072)
    for index in range(100):
        rollout = sessions / f"rollout-unrelated-{index:03d}.jsonl"
        _append(
            rollout,
            [
                _session(
                    f"2026-07-13T00:01:{index % 60:02d}Z",
                    f"unrelated-{index}",
                    unrelated_repo,
                ),
                {
                    "timestamp": "2026-07-13T00:02:00Z",
                    "type": "response_item",
                    "payload": {"type": "message", "content": large_body},
                },
            ],
        )

    fully_parsed: list[Path] = []
    original_parse = rollout_observability._parse_session

    def recording_parse(path: Path, **kwargs: object):
        fully_parsed.append(path)
        return original_parse(path, **kwargs)

    monkeypatch.setattr(rollout_observability, "_parse_session", recording_parse)

    result = build_launch_observability(
        sessions_root=sessions,
        baseline_files={},
        repo_root=owned_repo,
        limit_sessions=1,
    )

    assert result["status"] == "ok"
    assert [item["session_id"] for item in result["sessions"]] == ["owned"]
    assert fully_parsed == [owned.resolve()]


def test_session_limit_never_truncates_an_owned_lineage(tmp_path: Path) -> None:
    sessions = tmp_path / ".codex" / "sessions"
    _append(
        sessions / "rollout-root.jsonl",
        [_session("2026-07-13T00:00:00Z", "root", tmp_path)],
    )
    _append(
        sessions / "rollout-child-one.jsonl",
        [_session("2026-07-13T00:00:01Z", "child-one", tmp_path, parent="root", depth=1)],
    )
    _append(
        sessions / "rollout-child-two.jsonl",
        [_session("2026-07-13T00:00:02Z", "child-two", tmp_path, parent="root", depth=1)],
    )

    result = build_launch_observability(
        sessions_root=sessions,
        baseline_files={},
        repo_root=tmp_path,
        limit_sessions=1,
    )

    assert result["status"] == "partial"
    assert {item["session_id"] for item in result["sessions"]} == {"root", "child-one", "child-two"}
    assert result["diagnostics"]["skipped_paths"] == 2
