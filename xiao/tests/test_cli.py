from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from visual_agent.cli import main


def test_version_flag_prints_runtime_info(capsys) -> None:
    code = main(["--version"])
    output = capsys.readouterr().out

    assert code == 0
    assert "visual-agent 0.1.2" in output
    assert "Product: Pacer" in output
    assert "Python:" in output
    assert "System:" in output


def test_checkpoint_entrypoint_prints_brand_aligned_version(capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["checkpoint", "--version"])

    code = main()
    output = capsys.readouterr().out

    assert code == 0
    assert "checkpoint 0.1.2" in output
    assert "Product: Pacer" in output
    assert "Package: visual-agent" in output


def test_empty_cli_prints_concise_getting_started(capsys) -> None:
    code = main([])
    output = capsys.readouterr().out

    assert code == 0
    assert "Pacer" in output
    assert "pacer claude" in output
    assert "--help" in output


def test_pacer_entrypoint_delegates_all_arguments_to_codex(monkeypatch) -> None:
    from visual_agent import codex_launcher

    calls = []
    monkeypatch.setattr(sys, "argv", [r"C:\Python\Scripts\pacer.exe", "resume", "--last"])
    monkeypatch.setattr(codex_launcher, "launch_codex", lambda argv: calls.append(argv) or 37)

    assert main() == 37
    assert calls == [["resume", "--last"]]


def test_pacer_code_alias_delegates_to_codex(monkeypatch) -> None:
    from visual_agent import codex_launcher

    calls = []
    monkeypatch.setattr(sys, "argv", [r"C:\Python\Scripts\pacer.exe", "code", "--sandbox", "danger-full-access"])
    monkeypatch.setattr(codex_launcher, "launch_codex", lambda argv: calls.append(argv) or 37)

    assert main() == 37
    assert calls == [["--sandbox", "danger-full-access"]]


def test_pacer_unknown_command_still_reaches_codex(monkeypatch) -> None:
    """Anything Pacer does not own is Codex's argv, unchanged."""
    from visual_agent import codex_launcher

    calls = []
    monkeypatch.setattr(sys, "argv", [r"C:\Python\Scripts\pacer.exe", "exec", "--json", "do the thing"])
    monkeypatch.setattr(codex_launcher, "launch_codex", lambda argv: calls.append(argv) or 5)

    assert main() == 5
    assert calls == [["exec", "--json", "do the thing"]]


def test_pacer_cc_alias_delegates_to_claude(monkeypatch) -> None:
    import visual_agent.cli as cli
    import visual_agent.process_guard as _pg

    calls = []
    monkeypatch.setattr(sys, "argv", [r"C:\Python\Scripts\pacer.exe", "cc", "--dangerously-skip-permissions"])
    monkeypatch.setattr(cli.shutil, "which", lambda name: r"C:\Tools\claude.exe" if name == "claude" else None)

    # By default this is a plain passthrough carrying only the engine's
    # context cap; session rotation is opt-in.
    monkeypatch.delenv("PACER_CLAUDE_ROTATE", raising=False)
    monkeypatch.setenv("PACER_AUTOCOMPACT", "off")

    def fake_guarded_run(command, **kw):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 41)

    monkeypatch.setattr(_pg, "guarded_run", fake_guarded_run)

    assert main() == 41
    assert calls == [[r"C:\Tools\claude.exe", "--permission-mode", "bypassPermissions"]]


def test_pacer_yolo_alias_forces_claude_bypass_permissions(monkeypatch) -> None:
    import visual_agent.cli as cli
    import visual_agent.process_guard as _pg

    calls = []
    monkeypatch.setattr(
        sys,
        "argv",
        [
            r"C:\Python\Scripts\pacer.exe",
            "claude-yolo",
            "--permission-mode",
            "manual",
            "--dangerously-skip-permissions",
            "fix bug",
        ],
    )
    monkeypatch.setattr(cli.shutil, "which", lambda name: r"C:\Tools\claude.exe" if name == "claude" else None)

    monkeypatch.delenv("PACER_CLAUDE_ROTATE", raising=False)
    monkeypatch.setenv("PACER_AUTOCOMPACT", "off")

    def fake_guarded_run(command, **kw):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 43)

    monkeypatch.setattr(_pg, "guarded_run", fake_guarded_run)

    assert main() == 43
    assert calls == [[r"C:\Tools\claude.exe", "--permission-mode", "bypassPermissions", "fix bug"]]


def test_pacer_chat_alias_starts_the_pacer_owned_repl(monkeypatch) -> None:
    import visual_agent.claude_chat as claude_chat

    calls = []
    monkeypatch.setattr(sys, "argv", [r"C:\Python\Scripts\pacer.exe", "chat", "--model", "opus"])
    monkeypatch.setattr(claude_chat, "run_claude_chat", lambda argv: calls.append(list(argv)) or 7)

    assert main() == 7
    assert calls == [["--model", "opus"]]


def test_pacer_gm_alias_delegates_to_gemini(monkeypatch) -> None:
    import visual_agent.cli as cli

    calls = []
    monkeypatch.setattr(sys, "argv", [r"C:\Python\Scripts\pacer.exe", "gm", "--yolo"])
    monkeypatch.setattr(cli.shutil, "which", lambda name: r"C:\Tools\gemini.exe" if name == "gemini" else None)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda argv: calls.append(argv) or subprocess.CompletedProcess(argv, 42),
    )

    assert main() == 42
    assert calls == [[r"C:\Tools\gemini.exe", "--yolo"]]


def test_completion_command_writes_scripts(tmp_path: Path, capsys) -> None:
    code = main(["completion", "--output-dir", str(tmp_path)])
    output = capsys.readouterr().out

    assert code == 0
    for filename in ("pacer.bash", "_pacer", "pacer.ps1"):
        path = tmp_path / filename
        assert path.exists()
        assert "pacer" in path.read_text(encoding="utf-8")
    assert "bash:" in output
