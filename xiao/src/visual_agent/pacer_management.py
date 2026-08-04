from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .codex_launcher import launch_codex
from .cli_quality import QUALITY_COMMANDS
from .pacer_support import build_pacer_support_snapshot, inspect_codex_account, support_snapshot_to_markdown
from .user_profile import LocalUserProfile, load_user_profile, save_user_profile


def handle_pacer_management(argv: Sequence[str]) -> int | None:
    arguments = [str(item) for item in argv]
    if not arguments:
        return None
    # `pacer usage timeline` is how people say it; the parser is flat.
    if len(arguments) >= 2 and arguments[0] == "usage" and arguments[1] == "timeline":
        arguments = ["usage-timeline", *arguments[2:]]
    command = arguments[0]
    from .cli_chief import CHIEF_COMMANDS

    if command in {"doctor", "agents", "init", "quickstart", "app", "chat", "shell", "journey", "usage", "usage-timeline", "worktrees"} or command in CHIEF_COMMANDS:
        if command in {"chat", "shell"}:
            from .interactive_agent import run_interactive_agent

            return run_interactive_agent(repo_root=Path.cwd())
        from .cli import main

        return main(arguments)
    if command == "dashboard" or command in QUALITY_COMMANDS:
        from .cli import main

        return main(arguments)
    if command in {"status", "evidence"}:
        parser = argparse.ArgumentParser(prog=f"pacer {command}")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--workspace-root", default=None)
        parser.add_argument("--repo-root", default=None)
        parsed = parser.parse_args(arguments[1:])
        workspace_root = Path(parsed.workspace_root).expanduser().resolve() if parsed.workspace_root else _workspace_root()
        snapshot = build_pacer_support_snapshot(workspace_root, repo_root=parsed.repo_root)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2) if parsed.json else support_snapshot_to_markdown(snapshot))
        return 0
    if command != "account":
        return None
    return _handle_account(arguments[1:])


def _handle_account(argv: list[str]) -> int:
    action = argv[0] if argv else "status"
    if action == "status":
        payload = {
            "codex": inspect_codex_account(use_cache=False),
            "pacer_profile": load_user_profile().to_public_dict(),
        }
        if "--json" in argv[1:]:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            codex = payload["codex"]
            profile = payload["pacer_profile"]
            print("Pacer account status")
            print(f"- Codex runtime: {codex.get('status')} ({codex.get('auth_method')})")
            print(f"- Pacer local profile: {profile.get('email') or 'not bound'}")
            print("- Note: the local profile does not copy or replace Codex credentials.")
        return 0 if payload["codex"].get("authenticated") else 1
    if action == "bind":
        parser = argparse.ArgumentParser(prog="pacer account bind", description="Save a local Pacer profile; Codex credentials remain managed by Codex.")
        parser.add_argument("--email", required=True)
        parser.add_argument("--name", default="")
        parser.add_argument("--organization", default="")
        args = parser.parse_args(argv[1:])
        path = save_user_profile(LocalUserProfile(email=args.email, display_name=args.name, organization=args.organization))
        print(f"Pacer local profile saved: {path}")
        print("Codex authentication remains separate. Check it with `pacer account status`.")
        return 0
    if action == "login":
        return launch_codex(["login", *argv[1:]])
    if action == "logout":
        return launch_codex(["logout", *argv[1:]])
    print("Usage: pacer account [status [--json] | bind --email EMAIL [--name NAME] [--organization ORG] | login | logout]")
    return 2


def _workspace_root() -> Path:
    cwd = Path.cwd().resolve()
    return cwd if cwd.name == ".agent-workspace" else cwd / ".agent-workspace"
