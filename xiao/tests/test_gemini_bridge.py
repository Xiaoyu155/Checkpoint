from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from visual_agent.gemini_bridge import (
    extract_json_from_text,
    find_gemini_executable,
    invoke_gemini,
    is_gemini_available,
)


def test_extract_json_from_direct_json() -> None:
    data = {"role": "indexer", "tasks": [1, 2, 3]}
    res = extract_json_from_text(json.dumps(data))
    assert res == data


def test_extract_json_from_markdown_fence() -> None:
    text = """Here is the breakdown:
```json
{
  "id": "task_1",
  "status": "pending"
}
```
Done!"""
    res = extract_json_from_text(text)
    assert res == {"id": "task_1", "status": "pending"}


def test_extract_json_from_mixed_text() -> None:
    text = "Thinking... Some explanation\n[{\"id\": \"t1\"}, {\"id\": \"t2\"}]\nHope this helps!"
    res = extract_json_from_text(text)
    assert res == [{"id": "t1"}, {"id": "t2"}]


def test_extract_json_empty_or_invalid() -> None:
    assert extract_json_from_text("") is None
    assert extract_json_from_text("no json here at all") is None


def test_gemini_availability_check() -> None:
    with patch("shutil.which", return_value="C:\\bin\\gemini.cmd"):
        assert is_gemini_available() is True
        assert find_gemini_executable() == "C:\\bin\\gemini.cmd"

    with patch("shutil.which", return_value=None):
        assert is_gemini_available() is False
        assert find_gemini_executable() is None


def test_invoke_gemini_missing_executable() -> None:
    with patch("visual_agent.gemini_bridge.find_gemini_executable", return_value=None):
        res = invoke_gemini("test")
        assert res.success is False
        assert "not found" in res.error.lower()


def test_invoke_gemini_success() -> None:
    fake_json_output = json.dumps({"response": "Hello world from Gemini", "session_id": "sess-123"})
    fake_proc = MagicMock(returncode=0, stdout=fake_json_output, stderr="")

    with patch("visual_agent.gemini_bridge.find_gemini_executable", return_value="gemini"), \
         patch("subprocess.run", return_value=fake_proc):
        res = invoke_gemini("hi", yolo=True, model="gemini-3.8-flash-n")
        assert res.success is True
        assert res.response == "Hello world from Gemini"
        assert res.session_id == "sess-123"
        assert res.parsed_json == {"response": "Hello world from Gemini", "session_id": "sess-123"}


def test_invoke_gemini_failure() -> None:
    fake_proc = MagicMock(returncode=1, stdout="", stderr="Quota exceeded")

    with patch("visual_agent.gemini_bridge.find_gemini_executable", return_value="gemini"), \
         patch("subprocess.run", return_value=fake_proc):
        res = invoke_gemini("hi")
        assert res.success is False
        assert "Quota exceeded" in res.error
