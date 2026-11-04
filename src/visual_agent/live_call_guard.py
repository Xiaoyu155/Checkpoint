"""Fail closed for unmocked model-provider I/O during pytest."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import urllib.request
from typing import Any


_ORIGINAL_POPEN = subprocess.Popen
_ORIGINAL_RUN = subprocess.run
_ORIGINAL_CREATE_CONNECTION = socket.create_connection
_ORIGINAL_URLOPEN = urllib.request.urlopen


def live_model_calls_disabled() -> bool:
    return (
        "PYTEST_CURRENT_TEST" in os.environ
        and os.environ.get("PACER_ALLOW_LIVE_IN_PYTEST") != "1"
    )


def model_cli_launch_blocked(command: Any, launcher: Any) -> bool:
    """Block real Codex/Gemini launchers while allowing patched launch functions."""
    if not live_model_calls_disabled():
        return False
    if launcher is not _ORIGINAL_POPEN and launcher is not _ORIGINAL_RUN:
        return False

    if isinstance(command, (str, bytes)):
        text = command.decode(errors="replace") if isinstance(command, bytes) else command
    else:
        try:
            text = " ".join(str(part) for part in command)
        except TypeError:
            text = str(command)
    return bool(re.search(
        r"(?i)(?:^|[\\/\s\"'])(?:codex(?:\.exe|\.cmd|\.bat)?|gemini(?:\.exe|\.cmd|\.bat)?|gemini-cli)(?=$|[\\/\s\"'])",
        text,
    ))


def live_http_call_blocked() -> bool:
    return live_model_calls_disabled() and urllib.request.urlopen is _ORIGINAL_URLOPEN


def live_socket_probe_blocked() -> bool:
    return live_model_calls_disabled() and socket.create_connection is _ORIGINAL_CREATE_CONNECTION
