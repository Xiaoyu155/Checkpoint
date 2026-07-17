from __future__ import annotations

import shutil
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
