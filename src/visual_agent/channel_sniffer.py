"""Local AI Channel & Tool Sniffer for Pacer.

Automatically scans and discovers available local AI tools, desktop applications,
CLI utilities, and remote URL+Key credentials on the developer's machine:
- Desktop AI / Code tools (Claude Desktop, Cursor, VS Code, WorkBuddy, Ollama, LM Studio, etc.)
- CLI utilities (codex, claude, gemini, gh, git)
- Local model servers (Ollama on localhost:11434, LM Studio on localhost:1234, vLLM, WorkBuddy on 8000)
- Configured credential channels (Sub2API, OpenAI-compatible, Google Gemini, Anthropic, DeepSeek, Qwen)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import socket
import urllib.request
from typing import Any


def check_tcp_port(host: str, port: int, timeout: float = 0.3) -> bool:
    """Quickly check if a local TCP service is listening."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def sniff_cli_tools() -> list[dict[str, Any]]:
    """Detect available command-line AI coding utilities in system PATH."""
    cli_targets = [
        ("codex", "OpenAI Codex CLI (Official)", "Executable in PATH"),
        ("claude", "Anthropic Claude Code CLI", "Executable in PATH"),
        ("gemini", "Google Gemini CLI", "Executable in PATH"),
        ("gh", "GitHub CLI (gh)", "System tool"),
        ("git", "Git Version Control", "System tool"),
        ("ollama", "Ollama Local LLM Runner", "CLI & Server"),
    ]
    results = []
    for name, title, note in cli_targets:
        exe_path = shutil.which(name)
        if exe_path:
            results.append({
                "id": name,
                "name": title,
                "type": "cli",
                "available": True,
                "path": str(Path(exe_path).resolve()),
                "note": note,
            })
    return results


def sniff_desktop_tools() -> list[dict[str, Any]]:
    """Detect popular AI desktop environments, editors, and background agents."""
    results = []
    is_win = platform.system() == "Windows"
    user_home = Path.home()
    appdata = Path(os.environ.get("APPDATA", user_home / "AppData" / "Roaming"))
    localappdata = Path(os.environ.get("LOCALAPPDATA", user_home / "AppData" / "Local"))

    desktop_checks = [
        {
            "id": "cursor",
            "name": "Cursor IDE",
            "type": "editor",
            "paths": [
                localappdata / "Programs" / "cursor" / "Cursor.exe",
                user_home / ".cursor",
                user_home / "Applications" / "Cursor.app",
            ],
            "note": "AI-first Code Editor",
        },
        {
            "id": "vscode",
            "name": "Visual Studio Code",
            "type": "editor",
            "paths": [
                localappdata / "Programs" / "Microsoft VS Code" / "Code.exe",
                Path("C:/Program Files/Microsoft VS Code/Code.exe"),
                user_home / ".vscode",
            ],
            "note": "Extensible IDE with Copilot / Roo / Cline",
        },
        {
            "id": "claude_desktop",
            "name": "Claude Desktop",
            "type": "desktop_app",
            "paths": [
                appdata / "Claude",
                localappdata / "Programs" / "Claude" / "Claude.exe",
                user_home / "Library" / "Application Support" / "Claude",
            ],
            "note": "Anthropic Claude Desktop Application",
        },
        {
            "id": "workbuddy",
            "name": "WorkBuddy / DeepSeek Desktop",
            "type": "desktop_app",
            "paths": [
                localappdata / "Programs" / "WorkBuddy" / "WorkBuddy.exe",
                user_home / ".workbuddy",
            ],
            "port": 8000,
            "note": "DeepSeek / WorkBuddy desktop agent",
        },
    ]

    for item in desktop_checks:
        found = False
        detected_path = ""
        for p in item.get("paths", []):
            if p.exists():
                found = True
                detected_path = str(p)
                break

        # Also check port if designated
        port = item.get("port")
        if port and check_tcp_port("127.0.0.1", port):
            found = True
            if not detected_path:
                detected_path = f"Active on http://127.0.0.1:{port}"

        if found:
            results.append({
                "id": item["id"],
                "name": item["name"],
                "type": item["type"],
                "available": True,
                "path": detected_path,
                "note": item["note"],
            })
    return results


def sniff_local_llm_servers() -> list[dict[str, Any]]:
    """Detect active local LLM endpoints (Ollama, LM Studio, vLLM, WorkBuddy)."""
    endpoints = [
        {"id": "ollama", "name": "Ollama Server", "host": "127.0.0.1", "port": 11434, "url": "http://127.0.0.1:11434/api/version"},
        {"id": "lm_studio", "name": "LM Studio Local Server", "host": "127.0.0.1", "port": 1234, "url": "http://127.0.0.1:1234/v1/models"},
        {"id": "workbuddy_api", "name": "WorkBuddy Local Gateway", "host": "127.0.0.1", "port": 8000, "url": "http://127.0.0.1:8000/v1/models"},
        {"id": "vllm", "name": "vLLM / Local OpenAI Proxy", "host": "127.0.0.1", "port": 8080, "url": "http://127.0.0.1:8080/v1/models"},
    ]
    results = []
    for ep in endpoints:
        is_open = check_tcp_port(ep["host"], ep["port"])
        if is_open:
            results.append({
                "id": ep["id"],
                "name": ep["name"],
                "type": "local_server",
                "available": True,
                "endpoint": f"http://{ep['host']}:{ep['port']}",
                "note": "Ready for zero-cost offline code generation",
            })
    return results


def sniff_configured_channels() -> list[dict[str, Any]]:
    """Inspect configured API channels (Sub2API, xAI Grok, Gemini, Codex, OpenAI, etc.)."""
    channels = []
    user_home = Path.home()

    # 1. Pacer Global credentials (~/.pacer/credentials.json)
    pacer_cred_file = user_home / ".pacer" / "credentials.json"
    if pacer_cred_file.exists():
        try:
            with open(pacer_cred_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("grok_api_key"):
                base = data.get("sub2api_base", "https://api.x.ai/v1")
                model = data.get("grok_model", "grok-4.7-latest")
                channels.append({
                    "id": "grok_sub2api",
                    "name": f"xAI Grok ({model}) via Sub2API",
                    "type": "cloud_gateway",
                    "available": True,
                    "endpoint": base,
                    "model": model,
                    "note": "Primary ultra-fast 3s coding tier",
                })
        except Exception:
            pass

    # 2. Codex Local OAuth token (~/.codex/auth.json)
    codex_auth_file = user_home / ".codex" / "auth.json"
    if codex_auth_file.exists():
        try:
            with open(codex_auth_file, "r", encoding="utf-8") as f:
                cdata = json.load(f)
            email = cdata.get("email") or cdata.get("user") or "Authenticated"
            channels.append({
                "id": "codex_oauth",
                "name": f"OpenAI Codex CLI (Official OAuth)",
                "type": "cloud_oauth",
                "available": True,
                "endpoint": "https://api.openai.com/v1",
                "model": "codex (ChatGPT Plus subscription)",
                "note": f"Account: {email} (Escalation & heavy repair tier)",
            })
        except Exception:
            pass

    # 3. Environment Variables (GEMINI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, DEEPSEEK_API_KEY)
    env_keys = [
        ("GEMINI_API_KEY", "gemini_cloud", "Google Gemini Flash (Cloud API)", "gemini-1.5-flash / gemini-2.0"),
        ("OPENAI_API_KEY", "openai_cloud", "OpenAI Official Cloud API", "gpt-4o / o1"),
        ("ANTHROPIC_API_KEY", "anthropic_cloud", "Anthropic Claude API", "claude-3-5-sonnet"),
        ("DEEPSEEK_API_KEY", "deepseek_cloud", "DeepSeek Official Cloud API", "deepseek-chat / coder"),
        ("DASHSCOPE_API_KEY", "qwen_cloud", "Alibaba Qwen Cloud API", "qwen-max"),
    ]
    for env_var, cid, title, default_model in env_keys:
        val = os.environ.get(env_var)
        if val:
            masked = f"{val[:6]}...{val[-4:]}" if len(val) > 10 else "***"
            channels.append({
                "id": cid,
                "name": title,
                "type": "cloud_api_key",
                "available": True,
                "endpoint": "Official Endpoint",
                "model": default_model,
                "note": f"Configured via ${env_var} ({masked})",
            })

    return channels


def sniff_all_ai_capabilities() -> dict[str, Any]:
    """Run full scan and aggregate all discovered AI tools and channels."""
    cli_tools = sniff_cli_tools()
    desktop_tools = sniff_desktop_tools()
    local_servers = sniff_local_llm_servers()
    channels = sniff_configured_channels()

    total_discovered = len(cli_tools) + len(desktop_tools) + len(local_servers) + len(channels)

    return {
        "status": "ok",
        "total_capabilities": total_discovered,
        "cli_tools": cli_tools,
        "desktop_tools": desktop_tools,
        "local_servers": local_servers,
        "channels": channels,
    }
