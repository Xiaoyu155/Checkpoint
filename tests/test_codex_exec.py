from __future__ import annotations

import json

from visual_agent.codex_exec import (
    is_resume_unavailable_error,
    is_resume_session_error,
    load_codex_user_defaults,
    parse_codex_jsonl,
    parse_codex_jsonl_evidence,
    read_codex_usage,
)


def _event(payload: dict) -> str:
    return json.dumps(payload)


def test_parse_codex_jsonl_normalizes_one_turn() -> None:
    text = "\n".join(
        [
            _event({"type": "thread.started", "thread_id": "thread-123"}),
            _event(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 120,
                        "cached_input_tokens": 80,
                        "output_tokens": 30,
                        "reasoning_output_tokens": 12,
                    },
                }
            ),
        ]
    )

    assert parse_codex_jsonl(text) == {
        "session_id": "thread-123",
        "input_tokens": 120,
        "cached_input_tokens": 80,
        "output_tokens": 30,
        "reasoning_output_tokens": 12,
        "total_tokens": 150,
        "num_turns": 1,
    }


def test_parse_codex_jsonl_tolerates_mixed_and_bad_lines() -> None:
    text = "\n".join(
        [
            "warning: startup noise",
            "prefix " + _event({"type": "thread.started", "thread_id": "mixed-id"}),
            "{not-json}",
            _event({"type": "item.completed", "item": {"text": "done"}}),
            _event({"type": "turn.completed", "usage": {"prompt_tokens": "7", "completion_tokens": 3}}),
        ]
    )

    usage = parse_codex_jsonl(text)

    assert usage is not None
    assert usage["session_id"] == "mixed-id"
    assert usage["input_tokens"] == 7
    assert usage["output_tokens"] == 3
    assert usage["total_tokens"] == 10


def test_parse_codex_jsonl_sums_multiple_completed_turns() -> None:
    text = "\n".join(
        [
            _event({"type": "thread.started", "thread": {"id": "multi-id"}}),
            _event({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}),
            _event(
                {
                    "type": "turn.completed",
                    "turn": {
                        "usage": {
                            "input_tokens": 20,
                            "cached_input_tokens": 11,
                            "output_tokens": 5,
                            "output_tokens_details": {"reasoning_tokens": 3},
                            "total_tokens": 25,
                        }
                    },
                }
            ),
        ]
    )

    usage = parse_codex_jsonl(text)

    assert usage is not None
    assert usage["input_tokens"] == 30
    assert usage["cached_input_tokens"] == 11
    assert usage["output_tokens"] == 7
    assert usage["reasoning_output_tokens"] == 3
    assert usage["total_tokens"] == 37
    assert usage["num_turns"] == 2


def test_parse_codex_jsonl_evidence_retains_turns_and_reports_bad_lines() -> None:
    text = "\n".join(
        [
            _event({"type": "thread.started", "thread_id": "thread-a"}),
            "startup warning",
            "prefix " + _event({"type": "item.completed", "item": {"text": "done"}}),
            _event({"type": "turn.completed", "usage": {"input_tokens": 4, "output_tokens": 2}}),
            _event(
                {
                    "type": "turn.completed",
                    "turn": {"usage": {"prompt_tokens": 7, "completion_tokens": 3}},
                }
            ),
        ]
    )

    evidence = parse_codex_jsonl_evidence(text)

    assert evidence["session_id"] == "thread-a"
    assert evidence["session_ids"] == ["thread-a"]
    assert evidence["invalid_line_count"] == 2
    assert evidence["event_count"] == 4
    assert evidence["turns"] == [
        {
            "index": 1,
            "usage": {
                "input_tokens": 4,
                "cached_input_tokens": 0,
                "output_tokens": 2,
                "reasoning_output_tokens": 0,
                "total_tokens": 6,
            },
        },
        {
            "index": 2,
            "usage": {
                "input_tokens": 7,
                "cached_input_tokens": 0,
                "output_tokens": 3,
                "reasoning_output_tokens": 0,
                "total_tokens": 10,
            },
        },
    ]
    assert evidence["usage"]["total_tokens"] == 16
    assert evidence["usage"]["num_turns"] == 2


def test_parse_codex_jsonl_handles_missing_and_bad_input() -> None:
    assert parse_codex_jsonl("") is None
    assert parse_codex_jsonl("warning only\n{bad json}") is None
    assert parse_codex_jsonl(_event({"type": "thread.started", "thread_id": "session-only"})) == {
        "session_id": "session-only",
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
        "num_turns": 0,
    }


def test_read_codex_usage_reads_log_and_falls_back_to_stdout(tmp_path) -> None:
    log = tmp_path / "codex.log"
    log.write_text(
        _event({"type": "thread.started", "thread_id": "from-log"})
        + "\n"
        + _event({"type": "turn.completed", "usage": {"input_tokens": 4, "output_tokens": 1}}),
        encoding="utf-8",
    )
    fallback = _event({"type": "thread.started", "thread_id": "from-tail"})

    assert read_codex_usage(log, fallback)["session_id"] == "from-log"
    assert read_codex_usage(tmp_path / "missing.log", fallback)["session_id"] == "from-tail"


def test_load_codex_user_defaults_reads_base_config(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        'model = "gpt-base"\n'
        'model_reasoning_effort = "high"\n'
        'sandbox_mode = "workspace-write"\n'
        'approval_policy = "never"\n'
        'service_tier = "default"\n',
        encoding="utf-8",
    )

    assert load_codex_user_defaults(config) == {
        "model": "gpt-base",
        "reasoning_effort": "high",
        "sandbox": "workspace-write",
        "approval": "never",
    }


def test_load_codex_user_defaults_merges_active_profile_table(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        'model = "gpt-base"\n'
        'model_reasoning_effort = "medium"\n'
        'profile = "review"\n'
        '[profiles.review]\n'
        'model = "gpt-review"\n'
        'model_reasoning_effort = "ultra"\n',
        encoding="utf-8",
    )

    assert load_codex_user_defaults(config) == {
        "model": "gpt-review",
        "reasoning_effort": "ultra",
        "sandbox": "",
        "approval": "",
    }


def test_load_codex_user_defaults_merges_sibling_profile_file(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        'model = "gpt-base"\nprofile = "work"\n',
        encoding="utf-8",
    )
    (tmp_path / "work.config.toml").write_text(
        'model_reasoning_effort = "high"\n',
        encoding="utf-8",
    )

    assert load_codex_user_defaults(config) == {
        "model": "gpt-base",
        "reasoning_effort": "high",
        "sandbox": "",
        "approval": "",
    }


def test_load_codex_user_defaults_tolerates_missing_and_invalid_config(tmp_path) -> None:
    empty = {"model": "", "reasoning_effort": "", "sandbox": "", "approval": ""}
    assert load_codex_user_defaults(tmp_path / "missing.toml") == empty
    invalid = tmp_path / "invalid.toml"
    invalid.write_text("model = [", encoding="utf-8")
    assert load_codex_user_defaults(invalid) == empty


def test_is_resume_session_error_matches_only_session_failures() -> None:
    assert is_resume_session_error("No saved session found with ID abc") is True
    assert is_resume_session_error("failed to load rollout for thread abc") is True
    assert is_resume_session_error("Conversation expired; start a new session") is True
    assert is_resume_session_error("file not found: src/app.py") is False
    assert is_resume_session_error("invalid model name") is False
    assert is_resume_session_error("rate limit exceeded") is False


def test_is_resume_unavailable_error_includes_old_cli_parser_failures() -> None:
    assert is_resume_unavailable_error("error: unrecognized subcommand 'resume'") is True
    assert is_resume_unavailable_error("unexpected argument 'resume' found") is True
    assert is_resume_unavailable_error("No saved session found with ID abc") is True
    assert is_resume_unavailable_error("unexpected argument '--model' found") is False
