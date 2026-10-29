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
import subprocess
import time
from typing import Any
import urllib.error
import urllib.request


@dataclass
class GeminiResult:
    success: bool
    response: str
    raw_output: str
    parsed_json: dict[str, Any] | list[Any] | None = None
    session_id: str = ""
    error: str | None = None
    elapsed_seconds: float = 0.0


def find_gemini_executable() -> str | None:
    """Find the gemini CLI executable on PATH."""
    return shutil.which("gemini") or shutil.which("gemini.cmd") or shutil.which("gemini.exe")


def is_gemini_available() -> bool:
    """Check if Gemini CLI or fast HTTP endpoint is accessible."""
    if os.environ.get("PACER_FAST_API_BASE"):
        return True
    return find_gemini_executable() is not None


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

    api_key = os.environ.get("PACER_FAST_API_KEY") or os.environ.get("OPENAI_API_KEY") or "sk-local"
    target_model = model or os.environ.get("PACER_FAST_MODEL") or "gemini-2.5-flash"

    payload = {
        "model": target_model,
        "messages": [{"role": "user", "content": full_prompt}],
        "temperature": 0.1,
    }
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
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            content = str(data["choices"][0]["message"]["content"]).strip()
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
    timeout_seconds: int = 90,
    system_prompt: str | None = None,
) -> GeminiResult:
    """Invoke Gemini headlessly with zero Windows pipe hang and minimal tool overhead."""
    full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt

    # 1. Fast HTTP API path (0ms Node.js cold start if PACER_FAST_API_BASE is set)
    fast_api_base = os.environ.get("PACER_FAST_API_BASE", "").strip()
    if fast_api_base:
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
    if _supports_skip_trust(executable):
        cmd.append("--skip-trust")
    cmd.extend(["-p", full_prompt])
    if yolo:
        cmd.append("-y")
    if model:
        cmd.extend(["-m", model])
    if output_format == "json" or expect_json:
        cmd.extend(["-o", "json"])

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd) if cwd else None,
            timeout=timeout_seconds,
        )
        elapsed = time.time() - t0
        raw_stdout = proc.stdout or ""
        raw_stderr = proc.stderr or ""

        if proc.returncode != 0 and not raw_stdout:
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

        return GeminiResult(
            success=True,
            response=response_text.strip(),
            raw_output=raw_stdout,
            parsed_json=effective_json if expect_json else (
                effective_json
                if isinstance(effective_json, dict)
                else ({"items": effective_json} if isinstance(effective_json, list) else None)
            ),
            session_id=session_id,
            elapsed_seconds=elapsed,
        )
    except subprocess.TimeoutExpired:
        return GeminiResult(
            success=False,
            response="",
            raw_output="",
            error=f"Gemini execution timed out after {timeout_seconds}s",
            elapsed_seconds=time.time() - t0,
        )
    except Exception as exc:
        return GeminiResult(
            success=False,
            response="",
            raw_output="",
            error=f"Gemini execution failed: {exc}",
            elapsed_seconds=time.time() - t0,
        )