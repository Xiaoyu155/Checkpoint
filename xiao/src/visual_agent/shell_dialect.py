"""Run a command in the shell it was actually written for.

On Windows ``shell=True`` means ``cmd.exe``, but Pacer's users live in
PowerShell — the docs are written in PowerShell, and so are the commands they
hand to ``--test-command``. A PowerShell-flavoured command then dies in cmd with
``'$env:FOO' is not recognized as an internal or external command``, which
reads like a broken project rather than the wrong shell.

This module detects which dialect a command is written in, runs it there, and
names the shell in the evidence so a passing gate says where it passed.
"""

from __future__ import annotations

import os
import re
import shutil
from typing import Any


DIALECT_POWERSHELL = "powershell"
DIALECT_CMD = "cmd"
DIALECT_NEUTRAL = "neutral"

# Verb-Noun cmdlets are the strongest PowerShell tell; the verb list covers what
# realistically shows up in a verification command.
_CMDLET = re.compile(
    r"(?<![\w-])(?:Get|Set|New|Remove|Copy|Move|Test|Select|Where|ForEach|Out|Write|Read|"
    r"Invoke|Start|Stop|Wait|Measure|Compare|Convert|ConvertTo|ConvertFrom|Import|Export|"
    r"Join|Split|Resolve|Push|Pop)-[A-Z][A-Za-z]+",
)

_POWERSHELL_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\$env:", re.IGNORECASE), "$env: variable"),
    (re.compile(r"\$\w+\s*="), "$variable assignment"),
    (_CMDLET, "Verb-Noun cmdlet"),
    (re.compile(r"-ErrorAction\b", re.IGNORECASE), "-ErrorAction"),
    (re.compile(r"-Recurse\b", re.IGNORECASE), "-Recurse"),
    (re.compile(r"\.ps1(?:\s|$|\")"), ".ps1 script"),
    (re.compile(r"@['\"]"), "here-string"),
    (re.compile(r"\bif\s*\(\s*\$\?\s*\)"), "if ($?)"),
    (re.compile(r"\|\s*(?:Select|Where|ForEach|Measure|Out)-"), "pipeline cmdlet"),
)

_CMD_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"%\w+%"), "%VAR% expansion"),
    (re.compile(r"^\s*cmd(?:\.exe)?\s+/c\b", re.IGNORECASE), "cmd /c"),
    (re.compile(r"^\s*set\s+\w+=", re.IGNORECASE | re.MULTILINE), "set VAR="),
    (re.compile(r"\bif\s+exist\b", re.IGNORECASE), "if exist"),
    (re.compile(r"\bexit\s+/b\b", re.IGNORECASE), "exit /b"),
    (re.compile(r"\.(?:bat|cmd)(?:\s|$|\")", re.IGNORECASE), ".bat/.cmd script"),
)


def detect_dialect(command: str) -> dict[str, Any]:
    """Classify which shell a verification command is written for."""

    text = str(command or "")
    powershell = [label for pattern, label in _POWERSHELL_MARKERS if pattern.search(text)]
    cmd = [label for pattern, label in _CMD_MARKERS if pattern.search(text)]
    if powershell and not cmd:
        dialect = DIALECT_POWERSHELL
    elif cmd and not powershell:
        dialect = DIALECT_CMD
    elif powershell and cmd:
        # Mixed syntax cannot run cleanly anywhere; cmd is the historical default
        # so keep it and let the caller warn.
        dialect = DIALECT_CMD
    else:
        dialect = DIALECT_NEUTRAL
    return {
        "dialect": dialect,
        "powershell_markers": powershell,
        "cmd_markers": cmd,
        "mixed": bool(powershell and cmd),
    }


def powershell_executable() -> str:
    """Prefer PowerShell 7 (``pwsh``); fall back to Windows PowerShell."""

    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def prepare_shell_invocation(command: str) -> dict[str, Any]:
    """Decide how to execute ``command`` and say which shell will run it."""

    cmd = str(command or "").strip()
    detection = detect_dialect(cmd)
    warnings: list[str] = []
    if not cmd or os.name != "nt":
        return {
            **detection,
            "shell_used": "sh" if os.name != "nt" else "cmd",
            "argv": cmd,
            "use_shell": True,
            "warnings": warnings,
        }

    if detection["dialect"] == DIALECT_POWERSHELL:
        executable = powershell_executable()
        if not executable:
            warnings.append(
                "命令看起来是 PowerShell 写法，但本机找不到 pwsh/powershell，只能用 cmd 跑，很可能失败。"
            )
            return {**detection, "shell_used": "cmd", "argv": cmd, "use_shell": True, "warnings": warnings}
        if "&&" in cmd and os.path.basename(executable).lower().startswith("powershell"):
            # Windows PowerShell 5.1 has no pipeline chain operators.
            warnings.append(
                "命令里有 `&&`，Windows PowerShell 5.1 不支持它（会报语法错）。改用 `;` 或装 PowerShell 7。"
            )
        return {
            **detection,
            "shell_used": os.path.basename(executable).lower().replace(".exe", ""),
            "argv": [executable, "-NoProfile", "-NonInteractive", "-Command", cmd],
            "use_shell": False,
            "warnings": warnings,
        }

    if detection["mixed"]:
        warnings.append(
            "命令里同时出现 PowerShell 和 cmd 语法，两个 shell 都跑不干净。请挑一种写法。"
        )
    return {**detection, "shell_used": "cmd", "argv": cmd, "use_shell": True, "warnings": warnings}


def shell_mismatch_hint(*, command: str, shell_used: str, output: str) -> str:
    """Explain a 'not recognized' failure that is really a wrong-shell failure."""

    text = str(output or "").lower()
    cmd_rejects = (
        "is not recognized as an internal or external command",
        "不是内部或外部命令",
    )
    powershell_rejects = (
        "is not recognized as the name of a cmdlet",
        "term '",
        "无法将",
    )
    detection = detect_dialect(command)
    if str(shell_used) == "cmd" and any(marker in text for marker in cmd_rejects):
        if detection["dialect"] == DIALECT_POWERSHELL or detection["powershell_markers"]:
            markers = "、".join(detection["powershell_markers"][:3])
            return (
                f"这条验收命令是 PowerShell 写法（{markers}），但它在 cmd.exe 里执行，所以报"
                "「不是内部或外部命令」。Pacer 已能自动识别 PowerShell 命令；如果仍然落到 cmd，"
                "说明本机没装 pwsh/powershell。"
            )
    if str(shell_used).startswith(("pwsh", "powershell")) and any(marker in text for marker in powershell_rejects):
        if detection["cmd_markers"]:
            markers = "、".join(detection["cmd_markers"][:3])
            return (
                f"这条验收命令是 cmd 写法（{markers}），但它在 PowerShell 里执行。"
                "把它改成 PowerShell 语法，或用 `cmd /c \"...\"` 包起来。"
            )
    return ""
