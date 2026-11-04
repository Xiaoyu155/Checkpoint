from pathlib import Path

from visual_agent.model_catalog import select_model
from visual_agent.model_health import check_grok_health
from visual_agent.workbuddy_bridge import _merge_existing_python, extract_and_apply_code_blocks


def test_coder_post_is_workbuddy_then_grok() -> None:
    assert select_model("coder") == "workbuddy"
    assert select_model("coder", workbuddy_available=False) == "grok"
    assert select_model("coder", repeated_failure=True) == "grok"
    assert select_model("planner") == "gemini4_vps"
    assert select_model("coder") not in {"codex", "gemini-3.8", "gemini-3.8-flash-low"}


def test_same_name_function_is_replaced_on_disk(tmp_path: Path) -> None:
    target = tmp_path / "calc.py"
    target.write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n", encoding="utf-8")
    merged = _merge_existing_python(target.read_text(encoding="utf-8"), "def foo():\n    return 9\n")
    assert "return 9" in merged
    assert "def bar" in merged
    written = extract_and_apply_code_blocks(
        "```python\ndef foo():\n    return 9\n```",
        workspace_root=tmp_path,
        allowed_files=["calc.py"],
    )
    assert written == ["calc.py"]
    text = target.read_text(encoding="utf-8")
    assert "return 9" in text
    assert "def bar" in text


def test_grok_401_is_offline(monkeypatch) -> None:
    import urllib.error

    monkeypatch.setattr("visual_agent.model_health.is_grok_available", lambda: True)
    monkeypatch.setattr("visual_agent.grok_bridge.get_grok_api_base", lambda: "https://example.test/v1")

    def boom(*_args, **_kwargs):
        raise urllib.error.HTTPError("https://example.test/models", 401, "Unauthorized", hdrs=None, fp=None)

    monkeypatch.setattr("urllib.request.urlopen", boom)
    status = check_grok_health()
    assert status.status == "offline"
    assert status.error == "401"


def test_grok_reasoning_without_content_is_not_code(monkeypatch) -> None:
    from visual_agent.grok_bridge import invoke_grok

    monkeypatch.setattr("visual_agent.grok_bridge.get_grok_api_key", lambda: "test-key")
    monkeypatch.setattr("visual_agent.grok_bridge.get_grok_model", lambda: "grok-test")
    monkeypatch.setattr("visual_agent.grok_bridge.get_grok_api_base", lambda: "https://example.test/v1")

    class Response:
        def __init__(self) -> None:
            self._lines = [
                b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n',
                b"data: [DONE]\n",
            ]

        def readline(self) -> bytes:
            return self._lines.pop(0) if self._lines else b""

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> bool:
            return False

    monkeypatch.setattr("visual_agent.grok_bridge.urllib.request.urlopen", lambda *_a, **_k: Response())
    result = invoke_grok("write code", retries=0)
    assert result.success is False
    assert result.error == "empty_content"
    assert result.response == ""
