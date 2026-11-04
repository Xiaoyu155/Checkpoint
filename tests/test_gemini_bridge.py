from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from visual_agent.gemini_bridge import (
    GeminiResult,
    _invoke_fast_http_api,
    extract_json_from_text,
    find_gemini_executable,
    invoke_gemini,
    is_gemini_available,
)


@pytest.mark.parametrize("data", [
    {"choices": [{"message": {"content": [{"type": "text", "text": "READY"}]}}]},
    {"content": [{"type": "thinking", "text": "private"}, {"type": "text", "text": "READY"}]},
    {"candidates": [{"content": {"parts": [{"thought": True, "text": "private"}, {"text": "READY"}]}}]},
])
def test_response_text_ignores_thoughts_and_accepts_content_blocks(data) -> None:
    from visual_agent.gemini_bridge import _response_text
    assert _response_text(data) == "READY"


def test_response_text_does_not_return_reasoning_as_answer() -> None:
    from visual_agent.gemini_bridge import _response_text
    assert _response_text({"choices": [{"message": {"content": "", "reasoning_content": "private"}}]}) == ""


def _fake_cli_run(*, stdout: str = "", stderr: str = "", returncode: int = 0):
    def run(command, **kwargs):
        is_help = "--help" in command
        out = "" if is_help else stdout
        err = "" if is_help else stderr
        if kwargs.get("stdout") is not None:
            kwargs["stdout"].write(out.encode("utf-8"))
        if kwargs.get("stderr") is not None:
            kwargs["stderr"].write(err.encode("utf-8"))
        return SimpleNamespace(returncode=0 if is_help else returncode)
    return run


def _fake_cli_popen(*, stdout: str = "", stderr: str = "", returncode: int = 0, timeout: bool = False):
    processes = []
    should_timeout = timeout
    exit_code = returncode

    def start(command, **kwargs):
        if kwargs.get("stdout") is not None:
            kwargs["stdout"].write(stdout.encode("utf-8"))
        if kwargs.get("stderr") is not None:
            kwargs["stderr"].write(stderr.encode("utf-8"))

        class Process:
            pid = 12345
            returncode = None if should_timeout else exit_code
            wait_timeout = None

            def wait(self, timeout=None):
                self.wait_timeout = timeout
                if should_timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                self.returncode = exit_code
                return exit_code

            def poll(self):
                return self.returncode

            def kill(self):
                self.returncode = -9

        process = Process()
        processes.append(process)
        return process

    start.processes = processes
    return start


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
    with patch("socket.create_connection", side_effect=OSError), \
         patch("shutil.which", return_value="C:\\bin\\gemini.cmd"):
        assert is_gemini_available() is True
        assert find_gemini_executable() == "C:\\bin\\gemini.cmd"

    with patch("socket.create_connection", side_effect=OSError), \
         patch("shutil.which", return_value=None):
        assert is_gemini_available() is False
        assert find_gemini_executable() is None


def test_invoke_gemini_missing_executable() -> None:
    with patch("socket.create_connection", side_effect=OSError), \
         patch("visual_agent.gemini_bridge.find_gemini_executable", return_value=None):
        res = invoke_gemini("test")
        assert res.success is False
        assert "not found" in res.error.lower()


def test_invoke_gemini_success() -> None:
    fake_json_output = json.dumps({"response": "Hello world from Gemini", "session_id": "sess-123"})
    popen = _fake_cli_popen(stdout=fake_json_output)

    with patch("visual_agent.gemini_bridge.find_gemini_executable", return_value="gemini"), \
         patch("visual_agent.gemini_bridge._local_antigravity_available", return_value=False), \
         patch("subprocess.run", side_effect=_fake_cli_run()) as run, \
         patch("subprocess.Popen", side_effect=popen) as launch:
        res = invoke_gemini("hi", yolo=True, model="gemini-3.8-flash-n")
        assert res.success is True
        assert res.response == "Hello world from Gemini"
        assert res.session_id == "sess-123"
        assert res.parsed_json == {"response": "Hello world from Gemini", "session_id": "sess-123"}
        actual_call = launch.call_args
        assert actual_call.kwargs["stdin"] is subprocess.DEVNULL
        assert actual_call.kwargs["stdout"] is not actual_call.kwargs["stderr"]
        assert popen.processes[0].wait_timeout == pytest.approx(150, abs=0.1)
        command = actual_call.args[0]
        session_index = command.index("--session-id")
        assert len(command[session_index + 1]) == 32
        assert run.call_count >= 1


def test_invoke_gemini_uses_local_antigravity_for_available_gemini38() -> None:
    response = GeminiResult(success=True, response="READY", raw_output="READY")
    with patch.dict(os.environ, {"PACER_FAST_API_BASE": ""}, clear=False), \
         patch("visual_agent.gemini_bridge._local_antigravity_available", return_value=True) as available, \
         patch("visual_agent.gemini_bridge._invoke_fast_http_api", return_value=response) as fast_call:
        result = invoke_gemini("Return exactly READY.", model="gemini-3.8-flash-low")

    assert result.success is True
    assert result.response == "READY"
    available.assert_called_once_with()
    assert fast_call.call_args.kwargs["api_base"] == "http://127.0.0.1:8080/v1"


def test_invoke_gemini_failure() -> None:
    with patch("socket.create_connection", side_effect=OSError), \
         patch("visual_agent.gemini_bridge.find_gemini_executable", return_value="gemini"), \
         patch("subprocess.run", side_effect=_fake_cli_run()), \
         patch("subprocess.Popen", side_effect=_fake_cli_popen(returncode=1, stderr="Quota exceeded")):
        res = invoke_gemini("hi")
        assert res.success is False
        assert "Quota exceeded" in res.error


@pytest.mark.parametrize(("response_data", "expect_json", "error_fragment"), [
    ({"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}, False, "finish_reason=length"),
    ({"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}, False, "incomplete response"),
    ({"choices": [{"message": {"content": "```python\\nprint('partial')"}, "finish_reason": "stop"}]}, False, "incomplete response"),
    ({"choices": [{"message": {"content": '{"ok":'}, "finish_reason": "stop"}]}, True, "incomplete response"),
])
def test_fast_http_rejects_incomplete_responses(response_data, expect_json, error_fragment) -> None:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(response_data).encode("utf-8")
    with patch("visual_agent.gemini_bridge.live_http_call_blocked", return_value=False), \
         patch("visual_agent.gemini_bridge.urllib.request.urlopen", return_value=response):
        result = _invoke_fast_http_api(
            "Return a response", api_base="http://127.0.0.1:8080/v1",
            model="gemini-3.8-flash-low", expect_json=expect_json,
        )
    assert not result.success
    assert error_fragment in result.error


def test_fast_http_sets_gemini38_output_and_thinking_limits() -> None:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        "choices": [{"message": {"content": "READY"}, "finish_reason": "stop"}]
    }).encode("utf-8")
    with patch("visual_agent.gemini_bridge.live_http_call_blocked", return_value=False), \
         patch("visual_agent.gemini_bridge.urllib.request.urlopen", return_value=response) as urlopen:
        result = _invoke_fast_http_api(
            "Return READY", api_base="http://127.0.0.1:8080/v1", model="gemini-3.8-flash-low"
        )
    payload = json.loads(urlopen.call_args.args[0].data)
    assert result.success
    assert payload["max_tokens"] == 4096
    assert payload["maxOutputTokens"] == 4096
    assert payload["thinkingConfig"] == {"thinkingBudget": 0}


def test_fast_http_empty_body_is_rejected_and_cli_fallback_runs() -> None:
    http_response = MagicMock()
    http_response.__enter__.return_value.read.return_value = b""
    popen = _fake_cli_popen(stdout="complete CLI response")
    with patch.dict(os.environ, {"PACER_FAST_API_BASE": "http://127.0.0.1:8080/v1"}, clear=False), \
         patch("visual_agent.gemini_bridge.live_http_call_blocked", return_value=False), \
         patch("visual_agent.gemini_bridge.urllib.request.urlopen", return_value=http_response), \
         patch("visual_agent.gemini_bridge.find_gemini_executable", return_value="gemini"), \
         patch("visual_agent.gemini_bridge._supports_skip_trust", return_value=False), \
         patch("visual_agent.gemini_bridge._supports_plan_approval", return_value=False), \
         patch("subprocess.run", side_effect=_fake_cli_run()), \
         patch("subprocess.Popen", side_effect=popen):
        result = invoke_gemini("return a complete answer", model="gemini-3.8-flash-low")

    assert result.success
    assert result.response == "complete CLI response"
    assert len(result.session_id) == 32


@pytest.mark.parametrize("upstream_response", [
    {"content": [{"type": "text", "text": ""}]},
    OSError("upstream unavailable"),
])
def test_invoke_gemini38_falls_back_to_configured_cli(upstream_response) -> None:
    from visual_agent.gemini_bridge import invoke_gemini38

    node = SimpleNamespace(
        node_id="test-gemini38",
        enabled=True,
        blockers=[],
        api_key="test-key",
        model="gemini-3.8-flash-low",
        provider="anthropic",
        base_url="http://127.0.0.1:8090",
        endpoint="/messages",
        reachable=True,
        cli_available=True,
        cli_fallback=True,
        is_available=lambda: True,
    )
    opener = MagicMock()
    if isinstance(upstream_response, Exception):
        opener.open.side_effect = upstream_response
    else:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(upstream_response).encode()
        opener.open.return_value = response
    cli_result = GeminiResult(True, "SEARCH/REPLACE", "cli output")

    with patch("visual_agent.provider_runtime.antigravity_node", return_value=node), \
         patch("visual_agent.gemini_bridge.urllib.request.build_opener", return_value=opener), \
         patch("visual_agent.gemini_bridge.invoke_gemini", return_value=cli_result) as cli, \
         patch("visual_agent.provider_runtime.note_failure"), \
         patch("visual_agent.provider_runtime.clear_failure") as clear:
        result = invoke_gemini38("change fee rule", cwd="workspace", system_prompt="system")

    assert result is cli_result
    cli.assert_called_once_with(
        "change fee rule",
        model="gemini-3.8-flash-low",
        yolo=False,
        cwd="workspace",
        system_prompt="system",
        timeout_seconds=150,
        expect_json=False,
        _force_cli=True,
    )
    request = opener.open.call_args.args[0]
    payload = json.loads(request.data)
    assert payload["max_tokens"] == 4096
    assert payload["maxOutputTokens"] == 4096
    assert payload["thinking"] == {"type": "disabled"}
    clear.assert_called_once_with("test-gemini38")


def test_invoke_gemini38_falls_back_when_upstream_patch_is_truncated() -> None:
    from visual_agent.gemini_bridge import invoke_gemini38

    node = SimpleNamespace(
        node_id="test-gemini38",
        enabled=True,
        blockers=[],
        api_key="test-key",
        model="gemini-3.8-flash-low",
        provider="anthropic",
        base_url="http://127.0.0.1:8090",
        endpoint="/messages",
        reachable=True,
        cli_available=True,
        cli_fallback=True,
        is_available=lambda: True,
    )
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        "content": [{"type": "text", "text": "tests/test_fee_calc.py\n```python\n<<<<<<< SEARCH\nold\n======="}]
    }).encode()
    cli_result = GeminiResult(True, "tests/test_fee_calc.py\n<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE", "cli output")

    with patch("visual_agent.provider_runtime.antigravity_node", return_value=node), \
         patch("visual_agent.gemini_bridge.urllib.request.build_opener") as build_opener, \
         patch("visual_agent.gemini_bridge.invoke_gemini", return_value=cli_result) as cli, \
         patch("visual_agent.provider_runtime.clear_failure") as clear:
        build_opener.return_value.open.return_value = response
        result = invoke_gemini38("change fee rule", cwd="workspace", system_prompt="system")

    assert result is cli_result
    cli.assert_called_once()
    clear.assert_called_once_with("test-gemini38")


def test_invoke_gemini38_falls_back_when_http_finish_reason_is_length() -> None:
    from visual_agent.gemini_bridge import invoke_gemini38

    node = SimpleNamespace(
        node_id="test-gemini38-length",
        enabled=True,
        blockers=[],
        api_key="test-key",
        model="gemini-3.8-flash-low",
        provider="openai",
        base_url="http://127.0.0.1:8090",
        endpoint="/chat/completions",
        reachable=True,
        cli_available=True,
        cli_fallback=True,
        is_available=lambda: True,
    )
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]
    }).encode()
    opener = MagicMock()
    opener.open.return_value = response
    cli_result = GeminiResult(True, "complete response", "cli output")

    with patch("visual_agent.provider_runtime.antigravity_node", return_value=node), \
         patch("visual_agent.gemini_bridge.urllib.request.build_opener", return_value=opener), \
         patch("visual_agent.gemini_bridge.invoke_gemini", return_value=cli_result) as cli, \
         patch("visual_agent.provider_runtime.note_failure") as note, \
         patch("visual_agent.provider_runtime.clear_failure") as clear:
        result = invoke_gemini38("answer", timeout_seconds=75)

    assert result is cli_result
    assert "finish_reason=length" in note.call_args.args[1]
    assert cli.call_args.kwargs["timeout_seconds"] == 150
    clear.assert_called_once_with("test-gemini38-length")


def test_invoke_gemini38_rejects_incomplete_cli_fallback() -> None:
    from visual_agent.gemini_bridge import invoke_gemini38

    node = SimpleNamespace(
        node_id="test-gemini38-cli-incomplete",
        enabled=True,
        blockers=[],
        api_key="test-key",
        model="gemini-3.8-flash-low",
        provider="anthropic",
        base_url="http://127.0.0.1:8090",
        endpoint="/messages",
        reachable=False,
        cli_available=True,
        cli_fallback=True,
        is_available=lambda: True,
    )
    cli_result = GeminiResult(True, "```python\nprint('cut off')", "cli output")
    with patch("visual_agent.provider_runtime.antigravity_node", return_value=node), \
         patch("visual_agent.gemini_bridge.invoke_gemini", return_value=cli_result):
        result = invoke_gemini38("produce code")

    assert not result.success
    assert result.response == ""
    assert "incomplete response" in result.error


def test_invoke_gemini38_respects_disabled_cli_fallback() -> None:
    from visual_agent.gemini_bridge import invoke_gemini38

    node = SimpleNamespace(
        node_id="test-gemini38",
        enabled=True,
        blockers=[],
        api_key="test-key",
        model="gemini-3.8-flash-low",
        provider="anthropic",
        base_url="http://127.0.0.1:8090",
        endpoint="/messages",
        reachable=False,
        cli_available=True,
        cli_fallback=False,
        is_available=lambda: True,
    )
    with patch("visual_agent.provider_runtime.antigravity_node", return_value=node), \
         patch("visual_agent.gemini_bridge.invoke_gemini") as cli:
        result = invoke_gemini38("test")

    assert not result.success
    assert "unreachable" in result.error
    cli.assert_not_called()


def test_invoke_gemini_force_cli_skips_fast_http_path() -> None:
    popen = _fake_cli_popen(stdout="CLI response")
    with patch("visual_agent.gemini_bridge._invoke_fast_http_api") as fast_api, \
         patch("visual_agent.gemini_bridge.find_gemini_executable", return_value="gemini"), \
         patch("visual_agent.gemini_bridge._supports_skip_trust", return_value=False), \
         patch("visual_agent.gemini_bridge._supports_plan_approval", return_value=False), \
         patch("subprocess.run", side_effect=_fake_cli_run()), \
         patch("subprocess.Popen", side_effect=popen):
        result = invoke_gemini("prompt", model="gemini-3.8-flash-low", _force_cli=True)

    assert result.success
    assert result.response == "CLI response"
    assert len(result.session_id) == 32
    assert popen.processes[0].wait_timeout == pytest.approx(150, abs=0.1)
    fast_api.assert_not_called()


def test_invoke_gemini_cli_timeout_terminates_process_tree() -> None:
    popen = _fake_cli_popen(timeout=True)
    with patch("visual_agent.gemini_bridge._invoke_fast_http_api") as fast_api, \
         patch("visual_agent.gemini_bridge.find_gemini_executable", return_value="gemini"), \
         patch("visual_agent.gemini_bridge._supports_skip_trust", return_value=False), \
         patch("visual_agent.gemini_bridge._supports_plan_approval", return_value=False), \
         patch("subprocess.run", side_effect=_fake_cli_run()), \
         patch("subprocess.Popen", side_effect=popen), \
         patch("visual_agent.gemini_bridge.terminate_process_tree", return_value=True) as terminate:
        result = invoke_gemini("prompt", model="gemini-3.8-flash-low", _force_cli=True)

    assert not result.success
    assert "timed out after 150s" in result.error
    terminate.assert_called_once_with(popen.processes[0], wait_seconds=8.0)
    fast_api.assert_not_called()


def test_forced_cli_uses_read_only_plan_approval_mode() -> None:
    popen = _fake_cli_popen(stdout="SEARCH/REPLACE diff")
    with patch("visual_agent.gemini_bridge._invoke_fast_http_api") as fast_api, \
         patch("visual_agent.gemini_bridge.find_gemini_executable", return_value="gemini"), \
         patch("visual_agent.gemini_bridge._supports_skip_trust", return_value=False), \
         patch("visual_agent.gemini_bridge._supports_plan_approval", return_value=True), \
         patch("subprocess.run", side_effect=_fake_cli_run()), \
         patch("subprocess.Popen", side_effect=popen) as run:
        result = invoke_gemini("produce a diff", model="gemini-3.8-flash-low", _force_cli=True)

    command = run.call_args.args[0]
    assert result.success
    assert "--approval-mode" in command
    assert command[command.index("--approval-mode") + 1] == "plan"
    fast_api.assert_not_called()
