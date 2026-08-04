from __future__ import annotations

import shutil
import time
from pathlib import Path

from visual_agent import agent_backends


def test_clear_quota_failure_is_safe_when_state_directory_disappears(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / ".pacer" / "quota_failures.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"codex": 1}\n', encoding="utf-8")
    original_read_text = Path.read_text

    def read_then_remove_parent(self: Path, *args, **kwargs):
        value = original_read_text(self, *args, **kwargs)
        if self == path:
            shutil.rmtree(path.parent)
        return value

    monkeypatch.setattr(Path, "read_text", read_then_remove_parent)

    agent_backends.clear_quota_failure("codex", store_path=path)

    assert path.exists()
    assert path.read_text(encoding="utf-8") == "{}\n"


def test_clear_quota_failure_missing_store_is_a_noop(tmp_path: Path) -> None:
    path = tmp_path / ".pacer" / "quota_failures.json"

    agent_backends.clear_quota_failure("codex", store_path=path)

    assert not path.exists()


def test_recent_quota_failure_ignores_invalid_or_future_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "quota_failures.json"
    path.write_text('{"codex": "invalid"}\n', encoding="utf-8")
    assert agent_backends.has_recent_quota_failure("codex", store_path=path) is False

    path.write_text(f'{{"codex": {time.time() + 7200}}}\n', encoding="utf-8")
    assert agent_backends.has_recent_quota_failure("codex", store_path=path) is False

    path.write_text(f'{{"codex": {time.time()}}}\n', encoding="utf-8")
    assert agent_backends.has_recent_quota_failure("codex", store_path=path) is True


def test_status_code_inside_a_request_id_is_not_quota_exhaustion() -> None:
    from visual_agent.agent_backends import looks_like_quota_exhaustion

    outage = (
        "unexpected status 503 Service Unavailable: Service temporarily unavailable, "
        "url: https://relay.example/responses, request id: c92f7c6e-7e23-4294-a7ef-c0235f14d"
    )

    # "429" as a substring lives inside plenty of hex ids. Reading a provider
    # outage as an exhausted subscription hands the task to a different account
    # and tells the user to go buy credit they do not need.
    assert looks_like_quota_exhaustion(outage) is False


def test_a_real_rate_limit_is_still_detected() -> None:
    from visual_agent.agent_backends import looks_like_quota_exhaustion

    assert looks_like_quota_exhaustion("Error: 429 Too Many Requests") is True
    assert looks_like_quota_exhaustion("http 429: rate limit reached") is True
    assert looks_like_quota_exhaustion("You have hit your usage limit") is True
