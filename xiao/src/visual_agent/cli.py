from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__


DEFAULT_CLI_NAME = "visual-agent"
CLI_ALIASES = {"visual-agent", "checkpoint", "pacer"}
PACER_CODEX_COMPAT_COMMANDS = {"resume"}


def current_cli_name() -> str:
    stem = Path(sys.argv[0]).stem.lower()
    return stem if stem in CLI_ALIASES else DEFAULT_CLI_NAME


def launch_claude_code(argv: list[str]) -> int:
    executable = shutil.which("claude") or shutil.which("claude.cmd")
    if not executable:
        print("Pacer requires Claude Code CLI on PATH. Install it with: npm install -g @anthropic-ai/claude-code")
        return 127
    from .subprocess_window import prepare_subprocess_command
    from .process_guard import guarded_run
    from .claude_rotation import (
        argv_with_autocompact,
        autocompact_setting,
        is_interactive_launch,
        rotation_enabled,
        run_claude_with_rotation,
    )

    argv = normalize_claude_passthrough_argv(argv)
    if is_interactive_launch(argv):
        # Opt-in only (PACER_AUTOCOMPACT=<tokens>): forcing a small window
        # made the engine stall on minutes-long compaction; default is the
        # engine's own behaviour, quota saving lives in `pacer chat`.
        argv = argv_with_autocompact(argv, autocompact_setting())
    if rotation_enabled() and is_interactive_launch(argv):
        # Opt-in backstop: rotate the billed session once it crosses the token
        # threshold, carrying a distilled handoff forward.
        return run_claude_with_rotation(executable, argv)
    # Guarded so a hard-killed Pacer cannot leave Claude's MCP stdio servers
    # running as orphans; see .process_guard.
    completed = guarded_run(prepare_subprocess_command([executable, *argv]))
    return int(completed.returncode)


def normalize_claude_passthrough_argv(argv: list[str]) -> list[str]:
    if "--dangerously-skip-permissions" in argv:
        return claude_yolo_argv(argv)
    return list(argv)


def claude_yolo_argv(argv: list[str]) -> list[str]:
    cleaned: list[str] = []
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token == "--permission-mode":
            skip_next = True
            continue
        if token.startswith("--permission-mode="):
            continue
        if token in {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"}:
            continue
        cleaned.append(token)
    return ["--permission-mode", "bypassPermissions", *cleaned]


def launch_claude_code_yolo(argv: list[str]) -> int:
    return launch_claude_code(claude_yolo_argv(argv))


def launch_gemini(argv: list[str]) -> int:
    executable = shutil.which("gemini") or shutil.which("gemini.cmd")
    if not executable:
        print("Pacer requires Gemini CLI on PATH. Install it with: npm install -g @google/gemini-cli")
        return 127
    from .subprocess_window import prepare_subprocess_command

    completed = subprocess.run(prepare_subprocess_command([executable, *argv]))
    return int(completed.returncode)


def build_version_message(cli_name: str = DEFAULT_CLI_NAME) -> str:
    return "\n".join(
        [
            f"{cli_name} {__version__}",
            "Product: Pacer",
            "Package: visual-agent",
            f"Python: {platform.python_version()}",
            f"System: {platform.platform()}",
            f"Executable: {sys.executable}",
        ]
    )


def build_welcome_message(cli_name: str = "checkpoint") -> str:
    """Pacer is a launcher for coding agents, plus the checks the agents skip."""
    return "\n".join(
        [
            "Pacer — 帮你把编程助手交的活验一遍的工具。",
            "",
            "启动开发工具（原生窗口，/model 等命令照常可用）：",
            "  pacer claude          # Claude Code 原生窗口",
            "  pacer yolo            # Claude Code 全权限交互模式",
            "  pacer codex           # 把终端交给 Codex 本尊",
            "  pacer gemini          # Gemini CLI",
            "  pacer task            # 每个任务一个原生窗口：按内容自动选模型，退出即交接开下一个",
            "",
            "省额度：",
            "  --autocompact 注入已改为可选（PACER_AUTOCOMPACT=<tokens> 开启）。",
            "  杀进程轮转默认关闭，需要 PACER_CLAUDE_ROTATE=1。",
            "",
            f"  {cli_name} --help",
        ]
    )


def _subcommand_names(parser: argparse.ArgumentParser) -> list[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    return []


def build_parser(prog: str = DEFAULT_CLI_NAME) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Pacer — launch coding agents and check what they hand back.")
    parser.add_argument("--version", "-V", action="store_true", help="Print version information and exit.")
    subparsers = parser.add_subparsers(dest="command")
    completion = subparsers.add_parser("completion", help="Write shell completion scripts into a directory.")
    completion.add_argument("--output-dir", default=".", help="Directory to write completion scripts into. Default: current directory.")
    return parser


def build_completion_script(shell: str, commands: list[str]) -> str:
    names = " ".join(sorted(commands))
    if shell == "bash":
        return "\n".join(
            [
                "_pacer_completion() {",
                f'    COMPREPLY=( $(compgen -W "{names}" -- "${{COMP_WORDS[1]}}") )',
                "}",
                "complete -F _pacer_completion pacer",
                "",
            ]
        )
    if shell == "zsh":
        return "\n".join([f"#compdef pacer", f'_arguments "1:command:({names})"', ""])
    if shell == "powershell":
        quoted = ", ".join(f"'{name}'" for name in sorted(commands))
        return "\n".join(
            [
                "Register-ArgumentCompleter -Native -CommandName pacer -ScriptBlock {",
                "    param($wordToComplete, $commandAst, $cursorPosition)",
                f"    @({quoted}) | Where-Object {{ $_ -like \"$wordToComplete*\" }} | ForEach-Object {{",
                "        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)",
                "    }",
                "}",
                "",
            ]
        )
    raise ValueError(f"Unsupported shell: {shell}")


def write_completion_scripts(output_dir: str | Path = ".") -> dict[str, str]:
    commands = _subcommand_names(build_parser(prog="pacer"))
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for shell, filename in (("bash", "pacer.bash"), ("zsh", "_pacer"), ("powershell", "pacer.ps1")):
        path = target / filename
        path.write_text(build_completion_script(shell, commands), encoding="utf-8")
        written[shell] = str(path)
    return written


def quote_cli_arg(value: str) -> str:
    text = str(value)
    if not text or any(char.isspace() for char in text):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def format_cli_error(exc: Exception | str, *, command: str = "") -> str:
    message = str(exc).strip()
    return f"Error: {message}"


def main(argv: list[str] | None = None) -> int:
    system_invocation = argv is None
    prog = current_cli_name() if argv is None else DEFAULT_CLI_NAME
    welcome_cli = "pacer" if prog == "pacer" else "checkpoint"
    if argv is None:
        argv = sys.argv[1:]
    # Meta flags must work for every entry name (pacer/checkpoint/visual-agent)
    # before any Codex handoff. Otherwise `pacer --version` becomes Codex's help.
    if argv and argv[0] in {"help", "-h", "--help"}:
        print(build_welcome_message(welcome_cli))
        print("")
        build_parser(prog=prog).print_help()
        return 0
    if argv and (argv[0] in {"--version", "-V"} or "--version" in argv):
        print(build_version_message(prog))
        return 0
    if system_invocation and prog == "pacer":
        if not argv:
            print(build_welcome_message(welcome_cli))
            return 0
        # Escape hatch: pacer codex/code ... hands the terminal to Codex unchanged.
        if argv[0] in {"codex", "code", "--codex"}:
            from .codex_launcher import launch_codex

            return launch_codex(argv[1:])
        # Claude Code passthrough for users who want the native interactive CLI.
        if argv[0] in {"claude", "cc", "--claude"}:
            return launch_claude_code(argv[1:])
        # One real native Claude window per task: Pacer picks the model up
        # front from the task's content and stitches a handoff between
        # windows by reading Claude's own on-disk session transcript --
        # never intercepts the native TUI itself (see pacer_task.py).
        if argv[0] in {"task", "--task", "chat", "--chat"}:
            from .pacer_task import run_pacer_task

            return run_pacer_task(argv[1:])
        # Explicit Claude Code YOLO shortcut: remove conflicting permission flags
        # and force Claude's native bypassPermissions mode.
        if argv[0] in {"yolo", "claude-yolo", "cc-yolo", "--claude-yolo"}:
            return launch_claude_code_yolo(argv[1:])
        # Gemini passthrough for users who want the native interactive CLI.
        if argv[0] in {"gemini", "gm", "--gemini"}:
            return launch_gemini(argv[1:])
        if argv[0] in PACER_CODEX_COMPAT_COMMANDS:
            from .codex_launcher import launch_codex

            return launch_codex(argv)
        if argv[0] not in _subcommand_names(build_parser(prog="pacer")):
            from .codex_launcher import launch_codex

            return launch_codex(argv)
    if not argv:
        print(build_welcome_message(welcome_cli))
        return 0
    parser = build_parser(prog=prog)
    args = parser.parse_args(argv)
    if args.command == "completion":
        written = write_completion_scripts(args.output_dir)
        for shell, path in written.items():
            print(f"{shell}: {path}")
        return 0
    print(build_welcome_message(welcome_cli))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
