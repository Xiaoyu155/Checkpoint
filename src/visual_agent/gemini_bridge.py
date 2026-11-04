"""Gemini CLI & Fast Local HTTP Proxy Bridge.

Provides fast, zero-hang execution for Gemini Flash:
1. Direct HTTP Fast-Path: If `PACER_FAST_API_BASE` (or local Antigravity OpenAI-compatible
   endpoint) is configured, bypasses Node.js CLI startup completely (0ms process overhead).
2. Windows `cmd.exe` Bypass: Automatically resolves npm `.cmd` shims to their underlying
   `node <script.js>` entrypoint and enforces `stdin=subprocess.DEVNULL` so Windows pipes
   never deadlock or wait for interactive input.
3. Single-Shot Text Mode: Supports `output_format="text"` with `yolo=False` for Stateless
   Canvas `SEARCH/REPLACE` generation, eliminating Gemini CLI's 60s+ internal tool loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import threading
from contextlib import nullcontext
import time
from typing import Any
from urllib.parse import urlsplit
import uuid
import urllib.error
import urllib.request
from .live_call_guard import live_http_call_blocked, live_socket_probe_blocked, model_cli_launch_blocked
from .subprocess_window import isolated_process_group_kwargs, terminate_process_tree


@dataclass
class GeminiResult:
    success: bool
    response: str
    raw_output: str
    parsed_json: dict[str, Any] | list[Any] | None = None
    session_id: str = ""
    error: str | None = None
    elapsed_seconds: float = 0.0


_LOCAL_ANTIGRAVITY_BASE = "http://127.0.0.1:8080/v1"
_GEMINI38_MODEL_PREFIXES = ("gemini-3.8", "gemini-3-8")
_GEMINI38_CLI_FALLBACK_TIMEOUT_SECONDS = 150
_GEMINI38_MAX_OUTPUT_TOKENS = 4096
_GEMINI38_HTTP_SEMAPHORE = threading.BoundedSemaphore(2)


def _response_text(data: dict) -> str:
    def blocks(value: Any) -> str:
        if isinstance(value, str):
            return value
        if not isinstance(value, list):
            return ""
        return "".join(
            block.get("text", "") for block in value
            if isinstance(block, dict) and isinstance(block.get("text"), str)
            and not block.get("thought") and block.get("type", "text") in {"text", "output_text"}
        )

    choices = data.get("choices") or []
    if choices:
        return blocks((choices[0].get("message") or {}).get("content"))
    candidates = data.get("candidates") or []
    if candidates:
        return blocks((candidates[0].get("content") or {}).get("parts"))
    return blocks(data.get("content"))


def _has_incomplete_search_replace(text: str) -> bool:
    if not re.search(r"(?m)^\s*(?:<{5,9}\s*SEARCH|>{5,9}\s*REPLACE)\b", text):
        return False
    state = "outside"
    for line in str(text or "").splitlines():
        if re.match(r"^\s*<{5,9}\s*SEARCH\s*$", line):
            if state != "outside":
                return True
            state = "search"
        elif re.match(r"^\s*={5,9}\s*$", line):
            if state != "search":
                return True
            state = "replace"
        elif re.match(r"^\s*>{5,9}\s*REPLACE\s*$", line):
            if state != "replace":
                return True
            state = "outside"
    return state != "outside"


def _has_unclosed_code_fence(text: str) -> bool:
    active_char = ""
    active_length = 0
    for line in str(text or "").splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if not match:
            continue
        marker, trailing = match.groups()
        if not active_char:
            active_char = marker[0]
            active_length = len(marker)
        elif marker[0] == active_char and len(marker) >= active_length and not trailing.strip():
            active_char = ""
            active_length = 0
    return bool(active_char)


def _response_is_incomplete(text: str, *, expect_json: bool = False) -> bool:
    content = str(text or "")
    if not content.strip() or _has_unclosed_code_fence(content) or _has_incomplete_search_replace(content):
        return True
    # A valid nested object must not hide an unfinished outer JSON document.
    json_blocks = re.findall(r"(?ms)^\s*```json\s*\n(.*?)^\s*```\s*$", content, re.IGNORECASE)
    for block in json_blocks:
        try:
            json.loads(block)
        except (TypeError, ValueError):
            return True
    if expect_json:
        start = re.search(r"[\[{]", content)
        if start:
            try:
                json.JSONDecoder().raw_decode(content, start.start())
            except ValueError:
                return True
    return expect_json and extract_json_from_text(content) is None


def _resolve_gemini_executable() -> str | None:
    """Resolve the Gemini CLI without invoking it."""
    return shutil.which("gemini") or shutil.which("gemini.cmd") or shutil.which("gemini.exe")


def find_gemini_executable() -> str | None:
    """Find the gemini CLI executable on PATH."""
    return _resolve_gemini_executable()


def _local_antigravity_available() -> bool:
    """Check whether the local OpenAI-compatible Antigravity gateway is listening."""
    if live_socket_probe_blocked():
        return False
    try:
        with socket.create_connection(("127.0.0.1", 8080), timeout=0.15):
            return True
    except (OSError, socket.timeout):
        return False


def is_gemini_available() -> bool:
    """Check if Gemini CLI or fast HTTP endpoint is accessible."""
    if os.environ.get("PACER_FAST_API_BASE") or _local_antigravity_available():
        return True
    # Keep availability detection a side-effect-free PATH probe. Execution uses
    # find_gemini_executable() directly, which callers/tests may override.
    return _resolve_gemini_executable() is not None


def is_gemini38_available() -> bool:
    from .provider_runtime import antigravity_node
    return antigravity_node().is_available()


def invoke_gemini38(prompt: str, *, cwd=None, system_prompt: str | None = None,
                    timeout_seconds: int = 30, expect_json: bool = False, **_kwargs) -> GeminiResult:
    """Use the configured local upstream, then its configured Gemini CLI fallback."""
    from .provider_runtime import antigravity_node, clear_failure, note_failure
    node = antigravity_node()
    timeout_seconds = max(timeout_seconds, _GEMINI38_CLI_FALLBACK_TIMEOUT_SECONDS)

    def cli_fallback(reason: str) -> GeminiResult:
        if not (node.enabled and not node.blockers and node.cli_fallback and node.cli_available):
            return GeminiResult(False, "", "", error=reason)
        result = invoke_gemini(
            prompt,
            model=node.model,
            yolo=False,
            cwd=cwd,
            system_prompt=system_prompt,
            timeout_seconds=max(timeout_seconds, _GEMINI38_CLI_FALLBACK_TIMEOUT_SECONDS),
            expect_json=expect_json,
            _force_cli=True,
        )
        if result.success and result.response.strip():
            if _response_is_incomplete(result.response, expect_json=expect_json):
                return GeminiResult(
                    False,
                    "",
                    result.raw_output,
                    error=f"{reason}; configured Gemini CLI fallback returned an incomplete response",
                    elapsed_seconds=result.elapsed_seconds,
                )
            clear_failure(node.node_id)
            return result
        fallback_error = result.error or "Gemini CLI returned empty content"
        return GeminiResult(
            False,
            "",
            result.raw_output,
            error=f"{reason}; configured Gemini CLI fallback failed: {fallback_error}",
            elapsed_seconds=result.elapsed_seconds,
        )

    if not node.enabled or node.blockers or not node.api_key:
        return GeminiResult(False, "", "", error="Gemini 3.8 disabled or unconfigured")
    if not node.is_available():
        return cli_fallback("Gemini 3.8 upstream is cooling down or unavailable")
    if not node.reachable:
        return cli_fallback("Gemini 3.8 upstream is unreachable")
    started = time.monotonic()
    base = node.base_url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    messages = [{"role": "user", "content": prompt}]
    payload = {
        "model": node.model,
        "messages": messages,
        "max_tokens": _GEMINI38_MAX_OUTPUT_TOKENS,
        "maxOutputTokens": _GEMINI38_MAX_OUTPUT_TOKENS,
    }
    if node.provider == "anthropic":
        payload["thinking"] = {"type": "disabled"}
    else:
        payload["thinkingConfig"] = {"thinkingBudget": 0}
    if system_prompt:
        if node.provider == "anthropic":
            payload["system"] = system_prompt
        else:
            messages.insert(0, {"role": "system", "content": system_prompt})
    headers = {"Content-Type": "application/json", "User-Agent": "Pacer-Client/1.0",
               "x-api-key": node.api_key, "Authorization": "Bearer " + node.api_key,
               "anthropic-version": "2023-06-01"}
    request = urllib.request.Request(base + node.endpoint, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        # Never send local requests through the user's external proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with _GEMINI38_HTTP_SEMAPHORE:
            with opener.open(request, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        if not raw.strip():
            reason = "Gemini 3.8 returned an empty HTTP response body"
            note_failure(node.node_id, reason)
            return cli_fallback(reason)
        data = json.loads(raw)
        if node.provider == "anthropic":
            text = _response_text(data)
            finish_reason = data.get("stop_reason")
            truncated = str(finish_reason or "").lower() in {"max_tokens", "length"}
        else:
            choices = data.get("choices") or []
            choice = choices[0] if choices else {}
            text = _response_text(data)
            finish_reason = choice.get("finish_reason")
            truncated = str(finish_reason or "").lower() == "length"
        if truncated:
            reason = f"Gemini 3.8 HTTP response was truncated (finish_reason={finish_reason})"
            note_failure(node.node_id, reason)
            return cli_fallback(reason)
        if _response_is_incomplete(text, expect_json=expect_json):
            reason = "Gemini 3.8 returned an incomplete HTTP response"
            note_failure(node.node_id, reason)
            return cli_fallback(reason)
        clear_failure(node.node_id)
        return GeminiResult(True, text, raw, parsed_json=extract_json_from_text(text) if expect_json else None,
                            elapsed_seconds=time.monotonic() - started)
    except Exception as exc:
        error = str(exc).replace(node.api_key, "[REDACTED]")
        note_failure(node.node_id, error)
        return cli_fallback("Gemini 3.8 request failed: " + error)


@lru_cache(maxsize=4)
def _resolve_node_cli_argv(executable: str) -> tuple[str, ...]:
    """On Windows, bypass `gemini.cmd` wrapper by resolving `node.exe` + the JS entrypoint directly."""
    exe_path = Path(executable)
    if exe_path.suffix.lower() in (".cmd", ".bat") and exe_path.is_file():
        try:
            content = exe_path.read_text(encoding="utf-8", errors="ignore")
            # Match typical npm cmd shim pattern: "%_prog%" ... "%dp0%\node_modules\...\index.js"
            match = re.search(r'"%dp0%\\([^"]+?\.m?js)"', content, re.IGNORECASE)
            if match:
                rel_js = match.group(1).replace("\\", "/")
                js_path = (exe_path.parent / rel_js).resolve()
                node_bin = shutil.which("node") or shutil.which("node.exe")
                if node_bin and js_path.is_file():
                    return (node_bin, "--no-warnings", str(js_path))
        except Exception:
            pass
    return (executable,)


@lru_cache(maxsize=4)
def _supports_skip_trust(executable: str) -> bool:
    """Probe whether the installed Gemini CLI supports --skip-trust."""
    try:
        base_argv = list(_resolve_node_cli_argv(executable))
        if model_cli_launch_blocked([*base_argv, "--help"], subprocess.run):
            return False
        proc = subprocess.run(
            [*base_argv, "--help"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        help_text = (proc.stdout or "") + (proc.stderr or "")
        return "--skip-trust" in help_text
    except Exception:
        return False


@lru_cache(maxsize=4)
def _supports_plan_approval(executable: str) -> bool:
    """Check whether the installed CLI can run tool calls without interactive approval."""
    try:
        command = [*_resolve_node_cli_argv(executable), "--help"]
        if model_cli_launch_blocked(command, subprocess.run):
            return False
        proc = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        help_text = (proc.stdout or "") + (proc.stderr or "")
        return "--approval-mode" in help_text
    except Exception:
        return False


def extract_json_from_text(text: str) -> dict[str, Any] | list[Any] | None:
    """Extract a JSON object or array from potentially mixed model output."""
    raw = str(text or "").strip()
    if not raw:
        return None

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, (dict, list)):
            return parsed
    except Exception:
        pass

    fence_pattern = re.compile(r"```(?:json)?\s*\n([\s\S]*?)\n```", re.IGNORECASE)
    matches = fence_pattern.findall(raw)
    for block in matches:
        try:
            parsed = json.loads(block.strip())
            if isinstance(parsed, (dict, list)):
                return parsed
        except Exception:
            continue

    decoder = json.JSONDecoder()
    candidates: list[tuple[int, int, dict[str, Any] | list[Any]]] = []
    for idx, ch in enumerate(raw):
        if ch not in ("{", "["):
            continue
        try:
            obj, end_idx = decoder.raw_decode(raw, idx)
            if isinstance(obj, (dict, list)):
                candidates.append((end_idx - idx, -idx, obj))
        except Exception:
            continue
    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return candidates[0][2]

    pairs = []
    for start_char, end_char in (("{", "}"), ("[", "]")):
        s_idx = raw.find(start_char)
        e_idx = raw.rfind(end_char)
        if s_idx != -1 and e_idx != -1 and e_idx > s_idx:
            pairs.append((s_idx, e_idx, start_char, end_char))
    pairs.sort(key=lambda item: item[0])
    for s_idx, e_idx, _, _ in pairs:
        try:
            parsed = json.loads(raw[s_idx : e_idx + 1])
            if isinstance(parsed, (dict, list)):
                return parsed
        except Exception:
            continue

    return None


def _invoke_fast_http_api(
    full_prompt: str,
    *,
    api_base: str,
    model: str | None = None,
    expect_json: bool = False,
    timeout_seconds: int = 60,
) -> GeminiResult:
    """Zero-startup-overhead HTTP call to local Antigravity / OpenAI-compatible endpoint."""
    t0 = time.time()
    url = api_base.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = f"{url}/v1/chat/completions" if not url.endswith("/v1") else f"{url}/chat/completions"

    api_key = os.environ.get("PACER_FAST_API_KEY", "").strip()
    if not api_key:
        host = urlsplit(url).hostname
        api_key = "sk-local" if host in {"127.0.0.1", "localhost", "::1"} else (
            os.environ.get("OPENAI_API_KEY") or "sk-local"
        )
    target_model = model or os.environ.get("PACER_FAST_MODEL") or "gemini-3.8-flash-low"

    payload = {
        "model": target_model,
        "messages": [{"role": "user", "content": full_prompt}],
        "temperature": 0.1,
    }
    if target_model.lower().startswith(_GEMINI38_MODEL_PREFIXES):
        payload.update({
            "max_tokens": _GEMINI38_MAX_OUTPUT_TOKENS,
            "maxOutputTokens": _GEMINI38_MAX_OUTPUT_TOKENS,
            "thinkingConfig": {"thinkingBudget": 0},
        })
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        if live_http_call_blocked():
            raise RuntimeError("Live model HTTP calls are disabled during pytest.")
        limiter = (_GEMINI38_HTTP_SEMAPHORE if target_model.lower().startswith(_GEMINI38_MODEL_PREFIXES)
                   and urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"} else nullcontext())
        with limiter:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            if not raw.strip():
                raise ValueError("empty HTTP response body")
            data = json.loads(raw)
            choice = (data.get("choices") or [{}])[0]
            finish_reason = choice.get("finish_reason")
            if str(finish_reason or "").lower() == "length":
                return GeminiResult(
                    success=False,
                    response="",
                    raw_output=raw,
                    error="Fast HTTP API returned a truncated response (finish_reason=length)",
                    elapsed_seconds=time.time() - t0,
                )
            content = _response_text(data).strip()
            if _response_is_incomplete(content, expect_json=expect_json):
                return GeminiResult(
                    success=False,
                    response="",
                    raw_output=raw,
                    error="Fast HTTP API returned an incomplete response",
                    elapsed_seconds=time.time() - t0,
                )
            parsed = extract_json_from_text(content) if expect_json else None
            return GeminiResult(
                success=True,
                response=content,
                raw_output=raw,
                parsed_json=parsed,
                elapsed_seconds=time.time() - t0,
            )
    except Exception as exc:
        return GeminiResult(
            success=False,
            response="",
            raw_output="",
            error=f"Fast HTTP API call failed: {exc}",
            elapsed_seconds=time.time() - t0,
        )


def invoke_gemini(
    prompt: str,
    *,
    model: str | None = None,
    yolo: bool = False,
    output_format: str = "text",
    expect_json: bool = False,
    cwd: str | Path | None = None,
    timeout_seconds: int = 25,
    system_prompt: str | None = None,
    _force_cli: bool = False,
) -> GeminiResult:
    """Invoke Gemini headlessly with zero Windows pipe hang and minimal tool overhead."""
    full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
    target_model = model or os.environ.get("PACER_FAST_MODEL") or "gemini-3.8-flash-low"
    is_gemini38 = target_model.lower().startswith(_GEMINI38_MODEL_PREFIXES)
    if is_gemini38:
        timeout_seconds = max(timeout_seconds, _GEMINI38_CLI_FALLBACK_TIMEOUT_SECONDS)

    # Explicitly configured gateways take precedence. When a Gemini 3.8 planning /
    # review call is requested, use the local Antigravity pool if it proves it has
    # that exact model; otherwise retain the Gemini CLI fallback.
    fast_api_base = os.environ.get("PACER_FAST_API_BASE", "").strip()
    if (
        not _force_cli
        and not fast_api_base
        and model
        and model.lower().startswith(_GEMINI38_MODEL_PREFIXES)
        and _local_antigravity_available()
    ):
        fast_api_base = _LOCAL_ANTIGRAVITY_BASE
    if fast_api_base and not _force_cli:
        http_res = _invoke_fast_http_api(
            full_prompt,
            api_base=fast_api_base,
            model=model,
            expect_json=expect_json,
            timeout_seconds=timeout_seconds,
        )
        if http_res.success:
            return http_res

    # 2. Local Gemini CLI path (with direct node.exe resolution & stdin=DEVNULL)
    executable = find_gemini_executable()
    if not executable:
        return GeminiResult(
            success=False,
            response="",
            raw_output="",
            error="Gemini CLI executable not found on PATH. Install with: npm install -g @google/gemini-cli",
        )

    cmd = list(_resolve_node_cli_argv(executable))
    if model_cli_launch_blocked(cmd, subprocess.Popen):
        return GeminiResult(False, "", "", error="Live Gemini CLI calls are disabled during pytest.")
    if _supports_skip_trust(executable):
        cmd.append("--skip-trust")
    if _force_cli and not yolo and _supports_plan_approval(executable):
        cmd.extend(["--approval-mode", "plan"])
    cmd.extend(["-p", full_prompt])
    if yolo:
        cmd.append("-y")
    if model:
        cmd.extend(["-m", model])
    cli_session_id = uuid.uuid4().hex
    cmd.extend(["--session-id", cli_session_id])
    # Keep the caller's response text API while retaining real model/usage receipts.
    if output_format == "json" or expect_json or is_gemini38:
        cmd.extend(["-o", "json"])

    t0 = time.monotonic()
    cli_timeout_seconds = (
        max(timeout_seconds, _GEMINI38_CLI_FALLBACK_TIMEOUT_SECONDS)
        if model and model.lower().startswith(_GEMINI38_MODEL_PREFIXES)
        else timeout_seconds
    )
    run_env = dict(os.environ)
    run_env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
    stdout_file = tempfile.TemporaryFile(mode="w+b")
    stderr_file = tempfile.TemporaryFile(mode="w+b")

    def read_output(handle) -> str:
        handle.flush()
        handle.seek(0)
        return handle.read().decode("utf-8", errors="replace")

    proc = None
    deadline = t0 + cli_timeout_seconds
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            cwd=str(cwd) if cwd else None,
            env=run_env,
            **isolated_process_group_kwargs(),
        )
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            terminate_process_tree(proc, wait_seconds=8.0)
            raw_stdout = read_output(stdout_file)
            raw_stderr = read_output(stderr_file)
            return GeminiResult(
                success=False,
                response="",
                raw_output=raw_stdout or raw_stderr,
                error=f"Gemini execution timed out after {cli_timeout_seconds}s",
                elapsed_seconds=time.monotonic() - t0,
            )
        elapsed = time.monotonic() - t0
        raw_stdout = read_output(stdout_file)
        raw_stderr = read_output(stderr_file)

        if proc.returncode != 0:
            return GeminiResult(
                success=False,
                response="",
                raw_output=raw_stderr,
                error=f"Gemini CLI exited with code {proc.returncode}: {raw_stderr[:500]}",
                elapsed_seconds=elapsed,
            )

        parsed = extract_json_from_text(raw_stdout)
        session_id = ""
        response_text = raw_stdout

        if isinstance(parsed, dict):
            if "response" in parsed and isinstance(parsed["response"], str):
                response_text = parsed["response"]
                session_id = str(parsed.get("session_id") or "")
            elif "text" in parsed and isinstance(parsed["text"], str):
                response_text = parsed["text"]

        inner_json = extract_json_from_text(response_text) if expect_json else None
        effective_json = (
            inner_json
            if isinstance(inner_json, (dict, list))
            else (parsed if isinstance(parsed, (dict, list)) else None)
        )

        if _response_is_incomplete(response_text, expect_json=expect_json):
            return GeminiResult(
                success=False,
                response="",
                raw_output=raw_stdout,
                error="Gemini CLI returned an incomplete response",
                elapsed_seconds=elapsed,
            )

        return GeminiResult(
            success=True,
            response=response_text.strip(),
            raw_output=raw_stdout,
            parsed_json=effective_json if expect_json else (
                effective_json
                if isinstance(effective_json, dict)
                else ({"items": effective_json} if isinstance(effective_json, list) else None)
            ),
            session_id=session_id or cli_session_id,
            elapsed_seconds=elapsed,
        )
    except Exception as exc:
        if proc is not None and proc.poll() is None:
            terminate_process_tree(proc, wait_seconds=8.0)
        return GeminiResult(
            success=False,
            response="",
            raw_output="",
            error=f"Gemini execution failed: {exc}",
            elapsed_seconds=time.monotonic() - t0,
        )
    finally:
        stdout_file.close()
        stderr_file.close()
