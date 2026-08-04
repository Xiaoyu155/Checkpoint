from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from visual_agent.command_verification import run_command_verification
from visual_agent.shell_dialect import (
    DIALECT_CMD,
    DIALECT_NEUTRAL,
    DIALECT_POWERSHELL,
    detect_dialect,
    prepare_shell_invocation,
    shell_mismatch_hint,
)


@pytest.mark.parametrize(
    "command",
    [
        "$env:NODE_ENV='test'; npm test",
        "Get-ChildItem src | Measure-Object",
        "pwsh ./scripts/verify.ps1",
        "Remove-Item -Recurse -Force dist; npm run build",
    ],
)
def test_powershell_flavoured_commands_are_detected(command: str) -> None:
    assert detect_dialect(command)["dialect"] == DIALECT_POWERSHELL


@pytest.mark.parametrize(
    "command",
    [
        "set NODE_ENV=test&& npm test",
        "cmd /c if exist dist (exit /b 0) else (exit /b 1)",
        "echo %PATH%",
        "scripts\\verify.bat",
    ],
)
def test_cmd_flavoured_commands_are_detected(command: str) -> None:
    assert detect_dialect(command)["dialect"] == DIALECT_CMD


@pytest.mark.parametrize(
    "command",
    ["npm test", "python -m pytest -q", "go test ./...", "cargo test"],
)
def test_ordinary_commands_stay_neutral(command: str) -> None:
    assert detect_dialect(command)["dialect"] == DIALECT_NEUTRAL


def test_mixed_syntax_is_flagged_rather_than_silently_guessed() -> None:
    detection = detect_dialect("$env:PORT='1'; echo %PATH%")

    assert detection["mixed"] is True
    assert detection["powershell_markers"]
    assert detection["cmd_markers"]


@pytest.mark.skipif(os.name != "nt", reason="shell routing only differs on Windows")
def test_powershell_command_is_routed_to_powershell_not_cmd() -> None:
    invocation = prepare_shell_invocation("$env:FOO='1'; python -c \"pass\"")

    assert invocation["shell_used"].startswith(("pwsh", "powershell"))
    assert invocation["use_shell"] is False
    assert isinstance(invocation["argv"], list)


@pytest.mark.skipif(os.name != "nt", reason="shell routing only differs on Windows")
def test_neutral_command_keeps_the_fast_cmd_path() -> None:
    invocation = prepare_shell_invocation("python -m pytest -q")

    assert invocation["shell_used"] == "cmd"
    assert invocation["use_shell"] is True


@pytest.mark.skipif(os.name != "nt", reason="shell routing only differs on Windows")
def test_powershell_verification_command_actually_runs() -> None:
    root = Path(tempfile.mkdtemp())
    (root / "t.py").write_text("print('ok')\n", encoding="utf-8")

    result = run_command_verification(
        command="$env:PACER_SHELL_TEST='1'; python t.py",
        repo_root=root,
        timeout_seconds=120.0,
    )

    # Before shell routing this died in cmd.exe with "is not recognized".
    assert result["verdict"] == "pass"
    assert result["shell_used"].startswith(("pwsh", "powershell"))


def test_verification_records_which_shell_ran_the_gate() -> None:
    root = Path(tempfile.mkdtemp())
    (root / "t.py").write_text("print('ok')\n", encoding="utf-8")

    result = run_command_verification(command="python t.py", repo_root=root, timeout_seconds=120.0)

    assert result["verdict"] == "pass"
    assert result["shell_used"]
    assert result["shell_dialect"] == DIALECT_NEUTRAL


def test_wrong_shell_failure_is_explained_instead_of_left_raw() -> None:
    hint = shell_mismatch_hint(
        command="$env:FOO='1'; npm test",
        shell_used="cmd",
        output="'$env:FOO' is not recognized as an internal or external command",
    )

    assert "PowerShell" in hint


def test_no_hint_when_the_failure_is_not_a_shell_problem() -> None:
    hint = shell_mismatch_hint(
        command="npm test",
        shell_used="cmd",
        output="1 test failed",
    )

    assert hint == ""
