from __future__ import annotations

import json
from typing import Any

from .codex_check import codex_check_to_markdown, run_codex_check
from .connect import connect_platform, connect_result_to_dict
from .execution_alignment import build_worker_prompt_alignment_check
from .models import to_jsonable
from .workspace import open_workspace


VERIFICATION_COMMANDS = {"verify", "verify-now", "codex-check", "test-plan", "connect"}


def handle_verification_command(args: Any, *, codex_runner: Any = None) -> int:
    if args.command == "verify":
        from .verify import run_verify, verify_to_markdown

        workspace = open_workspace(args.workspace_root)
        tags = tuple(item.strip() for item in str(args.tags).split(",") if item.strip())
        report = run_verify(
            workspace,
            tags=tags or ("verification",),
            workflow_names=tuple(args.workflow or ()),
            max_workflows=args.max_workflows,
            run_profile=args.run_profile,
            wait_lock=args.wait_lock,
            lock_wait_seconds=args.lock_wait_seconds,
            include_slow=args.include_slow,
        )
        if args.format == "markdown":
            print(verify_to_markdown(report))
        else:
            payload = to_jsonable(report)
            payload["verdict"] = report.verdict
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if report.failed == 0 else 1
    if args.command == "verify-now":
        from .verify import run_verify, verify_to_markdown

        workspace = open_workspace(args.workspace_root)
        tags = tuple(item.strip() for item in str(args.tags).split(",") if item.strip())
        run_profile = "supervised" if args.live else args.run_profile
        report = run_verify(
            workspace,
            tags=tags or ("verification",),
            workflow_names=tuple(args.workflow or ()),
            max_workflows=args.max_workflows,
            run_profile=run_profile,
            wait_lock=True,
            lock_wait_seconds=args.lock_wait_seconds,
            include_slow=args.include_slow,
        )
        if args.format == "markdown":
            print(verify_to_markdown(report))
        else:
            payload = to_jsonable(report)
            payload["verdict"] = report.verdict
            payload["entrypoint"] = "verify-now"
            payload["run_profile"] = run_profile
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if report.failed == 0 else 1
    if args.command == "codex-check":
        workspace = open_workspace(args.workspace_root)
        tags = tuple(item.strip() for item in str(args.tags).split(",") if item.strip())
        runner = codex_runner or run_codex_check
        result = runner(
            workspace,
            base=args.base,
            repo_root=args.repo_root,
            include_slow=args.include_slow,
            tags=tags or ("verification",),
            max_workflows=args.max_workflows,
            run_profile=args.run_profile,
            from_step=args.from_step,
        )
        execution_alignment = (
            build_worker_prompt_alignment_check()
            if getattr(args, "strict", False)
            else None
        )
        if args.format == "markdown":
            print(codex_check_to_markdown(result))
            if execution_alignment is not None:
                print(
                    "[codex-check] Worker prompt alignment: "
                    + str(execution_alignment.get("status") or "")
                    + f" ({execution_alignment.get('issue_count') or 0} issue(s))"
                )
                for issue in execution_alignment.get("issues", [])[:5]:
                    if isinstance(issue, dict):
                        print(f"  {issue.get('code')}: {issue.get('message')}")
        else:
            payload = to_jsonable(result)
            payload.update(
                {
                    "passed": result.passed,
                    "inspection_only": result.inspection_only,
                    "failed": result.failed,
                    "total": result.total,
                    "verdict": result.verdict,
                }
            )
            if execution_alignment is not None:
                payload["execution_alignment"] = execution_alignment
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        if result.failed:
            return 1
        if execution_alignment is not None and execution_alignment.get("status") == "blocked":
            return 1
        if getattr(args, "strict", False) and result.verdict == "coverage_gap":
            return 1
        return 0
    if args.command == "test-plan":
        from .verification_profiles import build_test_plan

        plan = build_test_plan(args.repo_root, base=args.base)
        if args.format == "json":
            print(json.dumps(plan, ensure_ascii=False, indent=2))
        else:
            print("## Test Plan")
            print("")
            print(f"Status: `{plan.get('status')}`")
            print(f"Command: `{plan.get('command') or ''}`")
            profiles = plan.get("profiles") if isinstance(plan.get("profiles"), list) else []
            for profile in profiles:
                name = profile.get("name")
                reason = profile.get("reason")
                print(f"- {name}: {reason}")
                targets = profile.get("targets")
                if isinstance(targets, list) and targets:
                    print("  Targets: " + ", ".join(f"`{target}`" for target in targets))
        return 0 if plan.get("command") else 1
    if args.command == "connect":
        result = connect_platform(
            args.platform,
            workspace_root=args.workspace_root,
            repo_root=args.repo_root,
            python=args.python,
            global_config=args.global_config,
        )
        payload = connect_result_to_dict(result)
        if args.format == "markdown":
            print(f"Connected {payload['platform']} to Checkpoint.")
            print(f"Workspace: {payload['workspace_root']}")
            print(f"Config: {payload['config_path']}")
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    raise ValueError(f"Unsupported verification command: {args.command}")
