from __future__ import annotations

import argparse
import platform
import json
import os
import sys
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Any
from importlib.metadata import PackageNotFoundError, version as package_version
from threading import Event, Thread

from . import __version__
from .capabilities import build_atomic_capability_manifest, build_capability_manifest
from .ci_templates import ci_template_install_to_dict, install_ci_templates
from .ci_templates import ci_workflow_template
from .cli_chief import CHIEF_COMMANDS
from .cli_cloud import CLOUD_COMMANDS
from .cli_external_samples import EXTERNAL_SAMPLE_COMMANDS, add_external_sample_parsers
from .cli_quality import QUALITY_COMMANDS, add_quality_parsers
from .cli_repair import REPAIR_COMMANDS
from .cli_runner import RUNNER_COMMANDS
from .cli_runtime import RUNTIME_COMMANDS
from .cli_verification import VERIFICATION_COMMANDS
from .cli_workflow import WORKFLOW_COMMANDS, add_workflow_parsers, detect_framework_from_dir, generate_from_diff_cli_markdown as generate_from_diff_cli_markdown, verify_impl_cli_markdown as verify_impl_cli_markdown
from .cli_workspace import (
    WORKSPACE_MANAGE_COMMANDS,
    WORKSPACE_QUEUE_COMMANDS,
    WORKSPACE_READ_COMMANDS,
    WORKSPACE_RECORD_COMMANDS,
    WORKSPACE_RUN_COMMANDS,
)
from .integrations import (
    export_workflow_to_playwright,
    integration_snippets_to_dict,
    install_integration_snippets,
    playwright_export_to_dict,
)
from .codex_check import run_codex_check
from .fixtures import FIXTURE_TYPES, fixture_template_payload, render_fixture_template
from .github_pr import github_event_pr_number, github_repository_from_env, github_run_url_from_env, post_pr_comment, pr_failure_comment_result
from .gui import (
    build_gui_action_history_index,
    build_gui_action_history_report,
    build_gui_action_history_risk_summary,
    gui_action_history_index_to_markdown,
    gui_action_history_report_to_markdown,
    gui_action_history_risk_to_markdown,
    open_workspace_window,
)
from .models import to_jsonable
from .notifications import build_event_notification, notification_config_template, send_email_notification
from .playwright_env import PLAYWRIGHT_INSTALL_HINT, playwright_runtime_status
from .quality import run_release_trial
from .recorder import record_browser_session
from .reports import (
    build_run_history_report,
    build_run_history_share_payload,
    build_run_history_ai_summary,
    run_history_report_to_markdown,
    write_run_history_report,
)
from .run_profile import RUN_PROFILE_CHOICES, SAFE_RUN_PROFILE_CHOICES
from .dynamic_model_selector import select_model_for_task, selection_to_dict, selection_to_markdown
from .vlm import detect_cloud_vision_backend, detect_vlm_backend, public_engine_status, vlm_doctor_summary
from .workbench_audit import handle_workbench_audit_command
from .workspace import (
    build_workspace_risk_policy_template,
    build_workspace_risk_policy_apply_plan,
    init_workspace,
    load_workspace_gui_action_history_risk_config,
    open_workspace,
    validate_workspace_risk_policy,
    workspace_status,
)


DEFAULT_CLI_NAME = "visual-agent"
CLI_ALIASES = {"visual-agent", "checkpoint", "pacer"}

if TYPE_CHECKING:
    from .workflow import WorkflowRuntime


def current_cli_name() -> str:
    stem = Path(sys.argv[0]).stem.lower()
    return stem if stem in CLI_ALIASES else DEFAULT_CLI_NAME


def build_version_message(cli_name: str = DEFAULT_CLI_NAME) -> str:
    try:
        playwright_version = package_version("playwright")
    except PackageNotFoundError:
        playwright_version = "not installed"
    except Exception:
        playwright_version = "unavailable"
    lines = [
        f"{cli_name} {__version__}",
        "Product: Checkpoint",
    ]
    if cli_name != DEFAULT_CLI_NAME:
        lines.append(f"Package: {DEFAULT_CLI_NAME}")
    lines.extend(
        [
            f"Python: {platform.python_version()}",
            f"Playwright: {playwright_version}",
            f"System: {platform.platform()}",
            f"Executable: {sys.executable}",
        ]
    )
    return "\n".join(lines)


def build_welcome_message(cli_name: str = "checkpoint") -> str:
    return "\n".join(
        [
            "DevPacer / Checkpoint — AI coding assistant orchestrator.",
            "",
            "直接开发：",
            f"  {cli_name}",
            "  Pacer> 修复登录错误并运行测试",
            "  Pacer> 继续补边界测试",
            "",
            "Start here (one-time per project):",
            f"  {cli_name} init --root .agent-workspace",
            f"  {cli_name} agents doctor",
            "",
            "Run a task (safe preview — no code changed):",
            f'  {cli_name} mission start --goal "Add a slugify function and tests" \\',
            "    --test-command \"pytest -q\" --agent codex",
            "",
            "Add --execute to actually run, --merge to auto-merge when verified:",
            f'  {cli_name} mission start --goal "..." --test-command "pytest -q" \\',
            "    --agent codex --execute --merge",
            "",
            "Queue & watch multiple tasks:",
            f"  {cli_name} mission worker --watch",
            f"  {cli_name} dashboard",
            "",
            "Inspect previous missions:",
            f"  {cli_name} mission list",
            f"  {cli_name} quota",
            "",
            "  See docs/五分钟上手.md for a 5-minute walkthrough.",
            f"  Run {cli_name} --help for the full command list.",
        ]
    )


def add_init_workspace_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".agent-workspace", help="Workspace root directory. Default: .agent-workspace.")
    parser.add_argument("--no-demo", action="store_true", help="Do not copy demo workflow/assets.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite demo files if they already exist.")
    parser.add_argument("--auto-detect", action="store_true", help="Scan a project root and generate matching fixture/workflow examples.")
    parser.add_argument("--repo-root", default=".", help="Project root to scan for --auto-detect. Default: current directory.")


def build_run_progress_message(progress_state: dict[str, Any]) -> str:
    workflow_name = str(progress_state.get("workflow_name") or "workflow")
    stage = str(progress_state.get("stage") or "running")
    current_step = str(progress_state.get("current_step") or "")
    current_index = int(progress_state.get("current_index") or 0)
    total_steps = int(progress_state.get("total_steps") or 0)
    message = str(progress_state.get("message") or "")
    if total_steps > 0 and current_step:
        prefix = f"Step {min(current_index + 1, total_steps)}/{total_steps}: {current_step}"
    elif current_step:
        prefix = current_step
    else:
        prefix = workflow_name
    return f"{prefix} [{stage}]" + (f" {message}" if message else "")


def run_workflow_with_progress(
    runtime: WorkflowRuntime,
    workflow: Any,
    *,
    progress_interval_seconds: float = 5.0,
    **run_kwargs: Any,
):
    progress_state: dict[str, Any] = {
        "workflow_name": getattr(workflow, "name", "workflow"),
        "stage": "starting",
        "current_step": "",
        "current_index": -1,
        "total_steps": len(getattr(workflow, "steps", ()) or ()),
        "message": "",
        "done": False,
    }
    stop = Event()
    last_message = {"value": ""}

    def reporter() -> None:
        while not stop.wait(progress_interval_seconds):
            if progress_state.get("done"):
                return
            message = build_run_progress_message(progress_state)
            if message != last_message["value"]:
                print(message, file=sys.stderr)
                last_message["value"] = message

    thread = Thread(target=reporter, name="visual-agent-progress", daemon=True)
    thread.start()
    try:
        return runtime.run(workflow, progress_state=progress_state, **run_kwargs)
    finally:
        progress_state["done"] = True
        stop.set()
        thread.join(timeout=0.1)


def _subcommand_names(parser: argparse.ArgumentParser) -> list[str]:
    names: list[str] = []
    for action in parser._actions:  # argparse internals, used to build completion scripts.
        if getattr(action, "choices", None) and isinstance(action.choices, dict):
            names.extend(str(name) for name in action.choices.keys())
    return sorted(dict.fromkeys(names))


def expand_natural_language_task_argv(argv: list[str], commands: list[str]) -> tuple[list[str], bool]:
    """Turn `pacer "goal"` into the safe, fully managed mission command."""
    if not argv or argv[0].startswith("-") or argv[0] in commands:
        return list(argv), False
    if any(item.startswith("-") for item in argv[1:]):
        return list(argv), False
    goal = " ".join(str(item).strip() for item in argv if str(item).strip())
    if not goal:
        return list(argv), False
    return [
        "mission",
        "start",
        "--goal",
        goal,
        "--workspace-root",
        ".agent-workspace",
        "--repo-root",
        ".",
        "--agent",
        "codex",
        "--execute",
        "--merge",
        "--allow-dirty",
        "--allow-coverage-gap",
        "--run-profile",
        "supervised",
        "--dispatch-mode",
        "delegated",
        "--prompt-style",
        "expanded",
        "--repair-strategy",
        "resume",
        "--max-rounds",
        "8",
        "--max-repair-rounds",
        "7",
    ], True


def build_completion_script(shell: str, commands: list[str]) -> str:
    command_words = " ".join(sorted(commands))
    if shell == "bash":
        return f"""# Checkpoint bash completion
_visual_agent_complete() {{
  local cur prev words cword
  _init_completion -n : || return

  if [[ $cword -eq 1 ]]; then
    COMPREPLY=( $(compgen -W \"{command_words}\" -- \"$cur\") )
    return 0
  fi

  case \"${{words[1]}}\" in
    generate-workflow)
      COMPREPLY=( $(compgen -W \"--description --output --workspace-root --model --page-type --url --from-existing --variant --from-sitemap --limit --dry-run --format\" -- \"$cur\") )
      return 0
      ;;
    run-workflow)
      COMPREPLY=( $(compgen -W \"--workflow --file --output-dir --inputs --inputs-file --sensitive-fields --resume-from --allow-click --run-profile --from-step --skip-preflight --strict-preflight --allow-high-risk --no-lock --lock-ttl-seconds --wait-lock --queue-when-locked --lock-wait-seconds --lock-poll-seconds --synthetic-on-capture-fail\" -- \"$cur\") )
      return 0
      ;;
    init|init-workspace)
      COMPREPLY=( $(compgen -W \"--root --no-demo --overwrite --auto-detect --repo-root\" -- \"$cur\") )
      return 0
      ;;
  esac
}}
complete -F _visual_agent_complete visual-agent
"""
    if shell == "zsh":
        return f"""#compdef visual-agent

_visual_agent_complete() {{
  local -a commands
  commands=({command_words})
  if (( CURRENT == 2 )); then
    _describe 'command' commands
    return
  fi

  case $words[2] in
    generate-workflow)
      _arguments \
        '--description[workflow description]:description:' \
        '--output[output YAML file]:output file:_files' \
        '--workspace-root[workspace root]:workspace root:_files -/' \
        '--model[LLM model]:model:' \
        '--page-type[page type hint]:page type:(auth form list detail ecommerce)' \
        '--url[entry URL]:url:' \
        '--from-existing[existing workflow]:workflow:' \
        '--variant[variant]:variant:(mobile)' \
        '--from-sitemap[sitemap XML path]:sitemap:_files' \
        '--limit[maximum sitemap URLs]:limit:' \
        '--dry-run[print without saving]' \
        '--format[output format]:format:(json yaml)'
      ;;
    run-workflow)
      _arguments \
        '--workflow[workflow file]:workflow file:_files' \
        '--file[deprecated alias for --workflow]:workflow file:_files' \
        '--output-dir[output directory]:output directory:_files -/' \
        '--inputs[workflow input JSON string]:inputs:' \
        '--inputs-file[workflow input JSON file]:input file:_files' \
        '--sensitive-fields[comma-separated input paths]:fields:' \
        '--resume-from[existing run directory]:run dir:_files -/' \
        '--allow-click[allow real click actions]' \
        '--run-profile[execution profile]:profile:(dry-run supervised semi-auto approved)' \
        '--from-step[start from step id]:step:' \
        '--skip-preflight[skip runtime preflight]' \
        '--strict-preflight[strict preflight]' \
        '--allow-high-risk[allow high-risk actions]' \
        '--no-lock[disable run lock]' \
        '--lock-ttl-seconds[lock TTL]:seconds:' \
        '--wait-lock[wait for lock]' \
        '--queue-when-locked[queue when locked]' \
        '--lock-wait-seconds[lock wait seconds]:seconds:' \
        '--lock-poll-seconds[lock poll seconds]:seconds:' \
        '--synthetic-on-capture-fail[use synthetic image on capture fail]'
      ;;
    init|init-workspace)
      _arguments \
        '--root[workspace root]:workspace root:_files -/' \
        '--no-demo[do not copy demo assets]' \
        '--overwrite[overwrite existing demo files]' \
        '--auto-detect[auto-detect project type]' \
        '--repo-root[project root to scan]:project root:_files -/'
      ;;
  esac
}}

compdef _visual_agent_complete visual-agent
"""
    raise ValueError(f"Unsupported shell: {shell}")


def write_completion_scripts(output_dir: str | Path = ".") -> dict[str, str]:
    parser = build_parser()
    commands = _subcommand_names(parser)
    output_path = Path(output_dir).resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    bash_path = output_path / "_visual_agent_completion.sh"
    zsh_path = output_path / "_visual_agent_completion.zsh"
    bash_path.write_text(build_completion_script("bash", commands), encoding="utf-8")
    zsh_path.write_text(build_completion_script("zsh", commands), encoding="utf-8")
    return {"bash": str(bash_path), "zsh": str(zsh_path)}


class _CoreHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Hide the full subcommand wall from --help; the curated list in the
    description is what new users should see. All commands still work."""

    def _format_action(self, action: argparse.Action) -> str:
        if action.__class__.__name__ == "_ChoicesPseudoAction":
            return ""
        return super()._format_action(action)


def build_parser(prog: str = DEFAULT_CLI_NAME) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        formatter_class=_CoreHelpFormatter,
        description=(
            "Pacer — 本地 AI 编码任务调度器（旧名 checkpoint / visual-agent，命令仍兼容）。\n"
            "\n"
            "常用命令（黄金路径）：\n"
            "  init          在当前项目初始化工作空间\n"
            "  dashboard     启动 Web 工作台（浏览器看板）\n"
            "  app           启动桌面工作台窗口\n"
            "  mission       派发/管理 AI 编码任务（start/list/queue/worker）\n"
            "  agents        检测已安装的 AI 工具（agents doctor）\n"
            "  doctor        全面健康检查\n"
            "  quickstart    五分钟上手向导\n"
            "\n"
            "其余命令为高级/内部用途，用 `<命令> --help` 查看详情。"
        ),
    )
    parser.add_argument("--version", action="store_true", help="Show version and runtime information, then exit.")
    # metavar hides the 200+ subcommand wall from --help; the full list still
    # works and is discoverable via shell completion.
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    click = subparsers.add_parser("click", help="Locate a target on screen and click it.")
    click.add_argument("--target", required=True, help="Visible UI target name, for example 登录.")
    click.add_argument("--provider", default="mock", help="Vision provider name. Default: mock.")
    click.add_argument("--output-dir", default=".runs", help="Directory for screenshots and run artifacts.")
    click.add_argument("--dry-run", action="store_true", help="Locate target without clicking.")
    click.add_argument(
        "--synthetic-on-capture-fail",
        action="store_true",
        help="Use a generated image if desktop screenshot capture is blocked.",
    )

    inspect_dom = subparsers.add_parser("inspect-dom", help="Inspect structured DOM elements from a web page.")
    inspect_dom.add_argument("--url", required=True, help="Page URL to inspect.")
    inspect_dom.add_argument("--headed", action="store_true", help="Show the browser instead of headless mode.")
    inspect_dom.add_argument("--limit", type=int, default=30, help="Maximum elements to print.")

    resolve_dom = subparsers.add_parser("resolve-dom", help="Resolve a target from structured DOM elements.")
    resolve_dom.add_argument("--url", required=True, help="Page URL to inspect.")
    resolve_dom.add_argument("--target", required=True, help="Target text, label, or accessible name.")
    resolve_dom.add_argument("--role", help="Expected DOM role, for example button or link.")
    resolve_dom.add_argument("--headed", action="store_true", help="Show the browser instead of headless mode.")

    browser_smoke = subparsers.add_parser("browser-smoke", help="Run a real browser smoke check against a URL.")
    browser_smoke.add_argument("--url", required=True, help="Page URL to open.")
    browser_smoke.add_argument("--output-dir", default=".runs", help="Directory for smoke screenshots and artifacts.")
    browser_smoke.add_argument("--headed", action="store_true", help="Show the browser instead of headless mode.")
    browser_smoke.add_argument("--timeout-ms", type=int, default=10_000)
    browser_smoke.add_argument("--wait-until", default="domcontentloaded", choices=["domcontentloaded", "load", "networkidle"])
    browser_smoke.add_argument("--min-text-length", type=int, default=1)
    browser_smoke.add_argument("--min-interactive", type=int, default=0)
    browser_smoke.add_argument("--expect-text", action="append", default=[], help="Required visible text. Can be repeated.")
    browser_smoke.add_argument("--expect-url-contains", action="append", default=[], help="Required initial URL fragment. Can be repeated.")
    browser_smoke.add_argument("--fill", action="append", default=[], help="Fill input by semantic label, formatted as label=value. Can be repeated.")
    browser_smoke.add_argument("--fill-selector", action="append", default=[], help="Fill input by CSS selector, formatted as selector=value. Can be repeated.")
    browser_smoke.add_argument("--click-text", default=None, help="Visible button/link text to click once.")
    browser_smoke.add_argument("--click-selector", default=None, help="CSS selector to click once.")
    browser_smoke.add_argument("--require-change-after-click", action="store_true", help="Fail if click does not change URL, visible text, or interactive element count.")
    browser_smoke.add_argument("--wait-for-text-after", action="append", default=[], help="Text to wait for after click before final screenshot. Can be repeated.")
    browser_smoke.add_argument("--wait-for-url-contains-after", action="append", default=[], help="URL fragment to wait for after click before final screenshot. Can be repeated.")
    browser_smoke.add_argument("--wait-timeout-seconds", type=float, default=5.0)
    browser_smoke.add_argument("--expect-text-after", action="append", default=[], help="Required visible text after click. Can be repeated.")
    browser_smoke.add_argument("--expect-url-contains-after", action="append", default=[], help="Required URL fragment after click. Can be repeated.")
    browser_smoke.add_argument("--wait-after-seconds", type=float, default=0.5)
    browser_smoke.add_argument("--save-workflow", help="Save a reusable workflow YAML generated from this smoke configuration.")
    browser_smoke.add_argument("--overwrite-workflow", action="store_true", help="Overwrite --save-workflow if it already exists.")
    browser_smoke.add_argument("--format", choices=["json", "markdown"], default="markdown")

    browser_smoke_suite = subparsers.add_parser("browser-smoke-suite", help="Run a browser smoke suite from a JSON/YAML file.")
    browser_smoke_suite.add_argument("--file", required=True, help="Suite JSON/YAML path.")
    browser_smoke_suite.add_argument("--output-dir", default=".runs", help="Directory for suite reports and case artifacts.")
    browser_smoke_suite.add_argument("--headed", action="store_true", help="Force headed browser mode for all cases.")
    browser_smoke_suite.add_argument("--format", choices=["json", "markdown"], default="markdown")

    inspect_uia = subparsers.add_parser("inspect-uia", help="Inspect structured Windows UI Automation controls.")
    inspect_uia.add_argument("--max-depth", type=int, default=4, help="Maximum UIA tree depth to inspect.")
    inspect_uia.add_argument("--limit", type=int, default=50, help="Maximum controls to print.")

    resolve_uia = subparsers.add_parser("resolve-uia", help="Resolve a target from Windows UI Automation controls.")
    resolve_uia.add_argument("--target", required=True, help="Target text, control name, or automation id.")
    resolve_uia.add_argument("--role", help="Expected control type, for example button or edit.")
    resolve_uia.add_argument("--max-depth", type=int, default=4, help="Maximum UIA tree depth to inspect.")

    run_workflow = subparsers.add_parser("run-workflow", help="Run an audited workflow file.")
    run_workflow.add_argument("--workflow", default=None, help="Workflow YAML or JSON file. Recommended.")
    run_workflow.add_argument("--file", default=None, help="Deprecated alias for --workflow.")
    run_workflow.add_argument("--output-dir", default=".runs", help="Directory for workflow run artifacts.")
    run_workflow.add_argument("--inputs", help="Workflow input JSON string.")
    run_workflow.add_argument("--inputs-file", help="Workflow input JSON file.")
    run_workflow.add_argument("--sensitive-fields", help="Comma-separated input paths to hash in audit logs.")
    run_workflow.add_argument("--resume-from", help="Existing run directory to resume from checkpoint.")
    run_workflow.add_argument("--allow-click", action="store_true", help="Allow real click actions. Default is dry-run.")
    run_workflow.add_argument(
        "--run-profile",
        choices=RUN_PROFILE_CHOICES,
        default="dry-run",
        help="Execution permission profile. Default is dry-run.",
    )
    run_workflow.add_argument("--from-step", default=None, help="Start execution from the named step id.")
    run_workflow.add_argument("--skip-preflight", action="store_true", help="Skip runtime preflight checks.")
    run_workflow.add_argument("--strict-preflight", action="store_true", help="Apply strict validation during preflight.")
    run_workflow.add_argument("--allow-high-risk", action="store_true", help="Allow high-risk actions during strict preflight.")
    run_workflow.add_argument("--no-lock", action="store_true", help="Disable run lock for controlled debugging.")
    run_workflow.add_argument("--lock-ttl-seconds", type=float, default=3600.0, help="Run lock TTL. Default: 3600.")
    run_workflow.add_argument("--wait-lock", action="store_true", help="Wait for the run lock instead of failing immediately.")
    run_workflow.add_argument("--queue-when-locked", action="store_true", help="Wait for the run lock instead of failing immediately.")
    run_workflow.add_argument("--lock-wait-seconds", type=float, default=30.0, help="Maximum seconds to wait when queued. Default: 30.")
    run_workflow.add_argument("--lock-poll-seconds", type=float, default=0.5, help="Seconds between lock checks when queued. Default: 0.5.")
    run_workflow.add_argument(
        "--synthetic-on-capture-fail",
        action="store_true",
        help="Use a generated image if desktop screenshot capture is blocked.",
    )

    preflight = subparsers.add_parser("preflight-workflow", help="Run validation and capability checks without executing.")
    preflight.add_argument("--file", required=True, help="Workflow YAML or JSON file.")
    preflight.add_argument("--workspace-root", default=None, help="Optional workspace root for environment checks and status updates.")
    preflight.add_argument("--strict", action="store_true", help="Apply production-oriented validation rules.")
    preflight.add_argument("--allow-high-risk", action="store_true", help="Allow high-risk actions in strict preflight.")

    env_check = subparsers.add_parser("env-check", help="Run environment checks without executing a workflow.")
    env_check.add_argument("--workspace-root", required=True, help="Workspace root or project root to inspect.")
    env_check.add_argument("--host", default="127.0.0.1", help="Host to probe for the dev server. Default: 127.0.0.1.")
    env_check.add_argument("--port", type=int, default=None, help="Optional explicit port to probe.")
    env_check.add_argument("--dist-dir", default=None, help="Optional explicit build output directory to inspect.")
    env_check.add_argument("--max-age-minutes", type=int, default=10, help="Maximum build age in minutes. Default: 10.")
    env_check.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    validate = subparsers.add_parser("validate-workflow", help="Validate a workflow file without running it.")
    validate.add_argument("--file", required=True, help="Workflow YAML or JSON file.")
    validate.add_argument("--strict", action="store_true", help="Apply production-oriented validation rules.")
    validate.add_argument("--allow-high-risk", action="store_true", help="Allow high-risk actions in strict validation.")

    list_runs = subparsers.add_parser("list-runs", help="List audited workflow runs.")
    list_runs.add_argument("--output-dir", default=".runs", help="Directory containing workflow run artifacts.")
    list_runs.add_argument("--limit", type=int, default=20, help="Maximum runs to list.")

    show_run = subparsers.add_parser("show-run", help="Show one audited workflow run summary.")
    show_run.add_argument("--run-dir", required=True, help="Run directory containing workflow_result.json.")

    report_run = subparsers.add_parser("report-run", help="Show a detailed audited run report.")
    report_run.add_argument("--run-dir", required=True, help="Run directory containing workflow_result.json.")
    report_run.add_argument("--format", choices=["json", "markdown"], default="json")

    generate_report = subparsers.add_parser("generate-report", help="Generate a static HTML report from local run history.")
    generate_report.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing run_history.jsonl.")
    generate_report.add_argument("--output", default=None, help="Output HTML file. Default: <workspace-root>/reports/run_history_report.html.")
    generate_report.add_argument("--limit", type=int, default=20, help="Maximum recent runs to include.")
    generate_report.add_argument("--open", action="store_true", help="Open the generated report in the default browser.")
    generate_report.add_argument("--share", action="store_true", help="Print a share payload with local URL and cloud placeholder.")
    generate_report.add_argument(
        "--summary-provider",
        choices=["none", "anthropic", "openai"],
        default="none",
        help="Optional model provider for the summary paragraph. Default: none.",
    )
    generate_report.add_argument("--summary-model", default=None, help="Optional model name for --summary-provider.")
    generate_report.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    generate_fixture = subparsers.add_parser("generate-fixture", help="Generate a reusable fixture template.")
    generate_fixture.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root used to place the fixture template.")
    generate_fixture.add_argument("--page", required=True, help="Page path or URL the fixture should describe.")
    generate_fixture.add_argument("--name", default=None, help="Fixture name. Default is derived from --page.")
    generate_fixture.add_argument("--type", choices=FIXTURE_TYPES, default="standard", help="Fixture type. Default: standard.")
    generate_fixture.add_argument("--output", default=None, help="Optional fixture file path. Default: <workspace-root>/fixtures/<name>.yaml.")
    generate_fixture.add_argument("--format", choices=["yaml", "json"], default="yaml", help="Output format. Default: yaml.")

    subparsers.add_parser("capabilities", help="List framework capabilities and dependency status.")
    subparsers.add_parser("atomic-capabilities", help="List planner-visible atomic capabilities.")
    doctor = subparsers.add_parser("doctor", help="Check missing capabilities.")
    doctor.add_argument("--strict", action="store_true", help="Treat missing optional capabilities as failures.")
    real_readiness = subparsers.add_parser("real-acceptance-readiness", help="Check whether this machine can run live real-acceptance workflows.")
    real_readiness.add_argument("--workspace-root", default=".agent-workspace")
    real_readiness.add_argument("--format", choices=["json", "markdown"], default="json")

    add_quality_parsers(subparsers)

    context_snapshot = subparsers.add_parser("context-snapshot", help="Print compact AI context for the workspace.")
    context_snapshot.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing agent_session.json.")
    context_snapshot.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    show_status = subparsers.add_parser("show-status", help="Print the project .visual-agent-status.md file.")
    show_status.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root used to infer the project root.")
    show_status.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    stats = subparsers.add_parser("stats", help="Show local workflow run statistics from run_history.jsonl.")
    stats.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing run_history.jsonl.")
    stats.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    export_runs = subparsers.add_parser("export-runs", help="Export local run history as JSON or CSV.")
    export_runs.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing run_history.jsonl.")
    export_runs.add_argument("--output", required=True, help="Output file path.")
    export_runs.add_argument("--format", choices=["json", "csv"], default="json", help="Export format. Default: json.")

    usage_status = subparsers.add_parser("usage-status", help="Show local usage counters and license feature boundaries.")
    usage_status.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing agent_session.json.")
    usage_status.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    usage = subparsers.add_parser("usage", help="Show local usage counters and license feature boundaries.")
    usage.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing agent_session.json.")
    usage.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    activate = subparsers.add_parser("activate", help="Activate a local license key.")
    activate.add_argument("--key", required=True, help="License key to write locally.")
    activate.add_argument("--tier", choices=["pro", "team", "enterprise"], default="pro", help="License tier to write. Default: pro.")
    activate.add_argument("--seats", type=int, default=1, help="Licensed seats. Default: 1.")
    activate.add_argument("--license-file", default=None, help="Optional path to write the license JSON file.")
    activate.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    cloud_run_plan = subparsers.add_parser("cloud-run-plan", help="Preview a cloud workflow request without sending network traffic.")
    cloud_run_plan.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root.")
    cloud_run_plan.add_argument("--workflow", required=True, help="Workflow name to run remotely in the future.")
    cloud_run_plan.add_argument("--workflow-id", default=None, help="Optional marketplace workflow id to resolve into YAML before planning.")
    cloud_run_plan.add_argument("--run-profile", choices=RUN_PROFILE_CHOICES, default="dry-run")
    cloud_run_plan.add_argument("--inputs-file", default=None, help="Optional inputs file name to reference without reading its contents.")
    cloud_run_plan.add_argument("--marketplace-endpoint", default="", help="Marketplace API endpoint used to resolve workflow ids.")
    cloud_run_plan.add_argument("--marketplace-api-key", default="", help="Optional bearer token for marketplace API requests.")
    cloud_run_plan.add_argument("--marketplace-org", default="", help="Optional marketplace org header value.")
    cloud_run_plan.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    cloud_run = subparsers.add_parser("cloud-run", help="Plan a cloud workflow run; use --execute to request execution.")
    cloud_run.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root.")
    cloud_run.add_argument("--workflow", required=True, help="Workflow name to run remotely.")
    cloud_run.add_argument("--workflow-id", default=None, help="Optional marketplace workflow id to resolve into YAML before execution.")
    cloud_run.add_argument("--run-profile", choices=RUN_PROFILE_CHOICES, default="dry-run")
    cloud_run.add_argument("--inputs-file", default=None, help="Optional inputs file name to reference without reading its contents.")
    cloud_run.add_argument("--marketplace-endpoint", default="", help="Marketplace API endpoint used to resolve workflow ids.")
    cloud_run.add_argument("--marketplace-api-key", default="", help="Optional bearer token for marketplace API requests.")
    cloud_run.add_argument("--marketplace-org", default="", help="Optional marketplace org header value.")
    cloud_run.add_argument("--execute", action="store_true", help="Explicitly request remote execution.")
    cloud_run.add_argument("--transport", choices=["none", "http"], default="none", help="Remote transport. Default: none.")
    cloud_run.add_argument("--timeout-seconds", type=float, default=30.0, help="HTTP transport timeout when --transport http is used.")
    cloud_run.add_argument("--max-retries", type=int, default=0, help="Retry count for retryable HTTP transport responses. Default: 0.")
    cloud_run.add_argument("--retry-backoff-seconds", type=float, default=0.0, help="Initial retry backoff for HTTP transport. Default: 0.")
    cloud_run.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    cloud_pull = subparsers.add_parser("cloud-pull-workflow", help="Download a public marketplace workflow into the local workspace.")
    cloud_pull.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root to receive the workflow.")
    cloud_pull.add_argument("--workflow-id", required=True, help="Marketplace workflow id or name to download.")
    cloud_pull.add_argument("--marketplace-endpoint", default="", help="Marketplace API endpoint. Defaults to VISUAL_AGENT_CLOUD_MARKETPLACE_ENDPOINT.")
    cloud_pull.add_argument("--marketplace-api-key", default="", help="Optional bearer token for marketplace API requests.")
    cloud_pull.add_argument("--marketplace-org", default="", help="Optional marketplace org header value.")
    cloud_pull.add_argument("--overwrite", action="store_true", help="Overwrite the destination file if it already exists.")
    cloud_pull.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    cloud_server = subparsers.add_parser("cloud-server", help="Run a minimal local Checkpoint cloud execution server.")
    cloud_server.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root served by the cloud server.")
    cloud_server.add_argument("--host", default="127.0.0.1", help="Host to bind. Default: 127.0.0.1.")
    cloud_server.add_argument("--port", type=int, default=7890, help="Port to bind. Default: 7890.")
    cloud_server.add_argument("--run-profile", choices=RUN_PROFILE_CHOICES, default="dry-run", help="Default run profile for requests.")
    cloud_server.add_argument("--api-key", default="", help="Optional bearer token required for cloud-server requests. Prefer --api-key-env.")
    cloud_server.add_argument("--api-key-env", default="CHECKPOINT_CLOUD_SERVER_API_KEY", help="Environment variable containing bearer token. Default: CHECKPOINT_CLOUD_SERVER_API_KEY.")
    cloud_server.add_argument("--required-org", default="", help="Optional required X-Visual-Agent-Org header value.")
    cloud_server.add_argument("--audit-log", default="", help="Optional redacted JSONL request audit log path.")
    cloud_server.add_argument("--retention-max-reports", type=int, default=0, help="Keep only the newest N workspace reports. Default: 0 disables count retention.")
    cloud_server.add_argument("--retention-days", type=float, default=0.0, help="Delete workspace reports older than this many days. Default: 0 disables age retention.")

    save_task = subparsers.add_parser("save-task-context", help="Save AI task state before switching windows.")
    save_task.add_argument("--task", required=True, help="Current task description.")
    save_task.add_argument("--files", nargs="*", default=[], help="Files already analyzed.")
    save_task.add_argument("--root-cause", default="", help="Current root-cause hypothesis.")
    save_task.add_argument("--plan", default="", help="Next-step plan.")
    save_task.add_argument("--tried", nargs="*", default=[], help="Approaches already tried.")
    save_task.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing agent_session.json.")
    save_task.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    summarize_failure = subparsers.add_parser("summarize-latest-failure", help="Print a compact latest-failure summary.")
    summarize_failure.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing agent_session.json.")
    summarize_failure.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    diagnose_failure = subparsers.add_parser("diagnose-latest-failure", help="Build an AI-readable evidence pack for the latest workflow failure.")
    diagnose_failure.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing workflow reports.")
    diagnose_failure.add_argument("--run-id", default=None, help="Specific run id to diagnose. Default: latest failed run.")
    diagnose_failure.add_argument("--max-chars", type=int, default=12000, help="Maximum JSON evidence budget. Default: 12000.")
    diagnose_failure.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    repair_workflow = subparsers.add_parser("repair-workflow", help="Suggest a safe workflow or app repair from failure evidence.")
    repair_workflow.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing workflow reports.")
    repair_workflow.add_argument("--run-id", default=None, help="Specific run id to repair. Default: latest failed run.")
    repair_workflow.add_argument("--provider", choices=["none", "anthropic", "openai"], default="none", help="Model provider. Default: none.")
    repair_workflow.add_argument("--model", default=None, help="Model name for provider-backed repair.")
    repair_workflow.add_argument("--max-chars", type=int, default=12000, help="Maximum JSON evidence budget. Default: 12000.")
    repair_workflow.add_argument("--apply", action="store_true", help="Apply a high-confidence deterministic workflow patch and create a backup.")
    repair_workflow.add_argument("--min-confidence", type=float, default=0.75, help="Minimum confidence required for --apply. Default: 0.75.")
    repair_workflow.add_argument("--verify", action="store_true", help="Rerun the repaired workflow after --apply. Default run profile: dry-run.")
    repair_workflow.add_argument("--verify-run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")
    repair_workflow.add_argument("--inputs-file", default=None, help="Optional workspace inputs file for verification rerun.")
    repair_workflow.add_argument("--rollback-on-fail", action="store_true", help="Restore the workflow backup when --verify fails.")
    repair_workflow.add_argument("--candidate-id", default=None, help="Repair candidate id to apply. Default: deterministic workflow patch when available.")
    repair_workflow.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    auto_repair = subparsers.add_parser("auto-repair", help="Diagnose, apply a safe deterministic repair, verify, and rollback on failure.")
    auto_repair.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing workflow reports.")
    auto_repair.add_argument("--run-id", default=None, help="Specific run id to repair. Default: latest failed run.")
    auto_repair.add_argument("--max-chars", type=int, default=12000, help="Maximum JSON evidence budget. Default: 12000.")
    auto_repair.add_argument("--min-confidence", type=float, default=0.75, help="Minimum confidence required for auto apply. Default: 0.75.")
    auto_repair.add_argument("--verify-run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")
    auto_repair.add_argument("--inputs-file", default=None, help="Optional workspace inputs file for verification rerun.")
    auto_repair.add_argument("--candidate-id", default=None, help="Repair candidate id to apply. Default: deterministic workflow patch when available.")
    auto_repair.add_argument("--dry-run", action="store_true", help="Preview the selected repair candidate without applying or verifying.")
    auto_repair.add_argument("--force", action="store_true", help="Apply even when repair health is high risk.")
    auto_repair.add_argument("--promote-regression", action="store_true", help="After verified auto repair, export and promote the failed run as a regression test.")
    auto_repair.add_argument("--overwrite-regression", action="store_true", help="Overwrite existing regression export/test when promoting.")
    auto_repair.add_argument("--run-regression", action="store_true", help="Run workspace regression tests after promotion.")
    auto_repair.add_argument("--regression-timeout-seconds", type=float, default=120.0, help="Timeout for --run-regression. Default: 120.")
    auto_repair.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    repair_history = subparsers.add_parser("repair-history", help="List recent workflow repair attempts.")
    repair_history.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing repair_history.jsonl.")
    repair_history.add_argument("--limit", type=int, default=20, help="Maximum entries to return. Default: 20.")
    repair_history.add_argument("--workflow", default=None, help="Filter by workflow name.")
    repair_history.add_argument("--status", default=None, help="Filter by repair status.")
    repair_history.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    repair_health = subparsers.add_parser("repair-health", help="Summarize repair reliability and rollback risk.")
    repair_health.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing repair_history.jsonl.")
    repair_health.add_argument("--limit", type=int, default=50, help="Maximum entries to analyze. Default: 50.")
    repair_health.add_argument("--workflow", default=None, help="Filter by workflow name.")
    repair_health.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    repair_rollback = subparsers.add_parser("repair-rollback", help="Rollback a workflow from a recorded repair backup.")
    repair_rollback.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing repair_history.jsonl.")
    repair_rollback.add_argument("--history-id", default=None, help="Specific repair history id. Default: latest repair with backup.")
    repair_rollback.add_argument("--workflow", default=None, help="Filter rollback candidate by workflow name.")
    repair_rollback.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    benchmarks = subparsers.add_parser("benchmarks", help="List public reference benchmarks for real-world Checkpoint testing.")
    benchmarks.add_argument("--category", default=None, help="Optional benchmark category filter.")
    benchmarks.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    benchmark_plan = subparsers.add_parser("benchmark-plan", help="Create an executable Checkpoint benchmark coverage plan.")
    benchmark_plan.add_argument("--category", default=None, help="Optional benchmark category filter.")
    benchmark_plan.add_argument("--benchmark-id", default=None, help="Optional benchmark id filter.")
    benchmark_plan.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    benchmark_draft = subparsers.add_parser("benchmark-draft", help="Generate a local workflow YAML draft for one benchmark scenario.")
    benchmark_draft.add_argument("--scenario-id", required=True, help="Scenario id or workflow name from benchmark-plan.")
    benchmark_draft.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root used for saving drafts.")
    benchmark_draft.add_argument("--output", default=None, help="Optional workspace-relative output path.")
    benchmark_draft.add_argument("--save", action="store_true", help="Write the workflow draft to the workspace.")
    benchmark_draft.add_argument("--overwrite", action="store_true", help="Overwrite existing workflow draft.")
    benchmark_draft.add_argument("--format", choices=["json", "markdown", "yaml"], default="markdown", help="Output format. Default: markdown.")

    subparsers.add_parser("quickstart", help="Print the shortest Checkpoint getting-started path.")

    slash = subparsers.add_parser("slash", help="Run a slash-style shortcut command, e.g. slash plan --goal ...")
    slash.add_argument("slash_command", help="Shortcut name: plan, memory, test, risk, verify, status.")
    slash.add_argument("slash_args", nargs=argparse.REMAINDER, help="Arguments passed to the target command.")

    chief_plan = subparsers.add_parser("chief-plan", help="Build a chief-engineer task plan for AI coding agents.")
    chief_plan.add_argument("--goal", required=True, help="User objective to turn into an executable engineering plan.")
    chief_plan.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing Checkpoint workflows.")
    chief_plan.add_argument("--repo-root", default=".", help="Git repository root. Default: current directory.")
    chief_plan.add_argument("--base", default="HEAD", help="Git base ref for diff routing. Default: HEAD.")
    chief_plan.add_argument("--agent", action="append", default=[], help="Worker agent label, for example codex or claude-code. Can be repeated.")
    chief_plan.add_argument("--max-workflows", type=int, default=5, help="Maximum affected workflows to include. Default: 5.")
    chief_plan.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")
    chief_plan.add_argument("--include-slow", action="store_true", help="Include workflows tagged 'slow'. Default: skipped.")
    chief_plan.add_argument("--interview", action="store_true", help="Surface clarifying questions to sharpen the objective before dispatch.")
    chief_plan.add_argument("--answer", action="append", default=[], help="Answer to a clarifying question. Repeat to add several; counts as clarification.")
    chief_plan.add_argument("--save", action="store_true", help="Persist the plan under <workspace>/chief_plans/<plan_id>/plan.json.")
    chief_plan.add_argument("--output", default=None, help="Optional file path to write the plan.")
    chief_plan.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    chief_plans = subparsers.add_parser("chief-plans", help="List or show saved chief-engineer plans.")
    chief_plans.add_argument("action", choices=["list", "show"], help="list saved plans, or show one by id.")
    chief_plans.add_argument("plan_id", nargs="?", default=None, help="Plan id (required for show).")
    chief_plans.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing chief_plans.")
    chief_plans.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    chief_dispatch = subparsers.add_parser("chief-dispatch", help="Preview or execute one saved chief-engineer worker dispatch.")
    chief_dispatch.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing chief_plans.")
    chief_dispatch.add_argument("--plan", required=True, help="Saved chief plan id.")
    chief_dispatch.add_argument("--track-id", default=None, help="Optional worker track id. Default: first Codex implementation track.")
    chief_dispatch.add_argument("--dry-run", action="store_true", help="Preview only. This is also the default unless --execute is passed.")
    chief_dispatch.add_argument("--execute", action="store_true", help="Actually create a worktree and run the Codex worker adapter.")
    chief_dispatch.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")
    chief_dispatch.add_argument("--include-slow", action="store_true", help="Include slow verification workflows after worker exit.")
    chief_dispatch.add_argument("--max-workflows", type=int, default=10, help="Maximum workflows to verify after worker exit. Default: 10.")
    chief_dispatch.add_argument("--timeout-seconds", type=float, default=1800.0, help="Worker timeout. Default: 1800.")
    chief_dispatch.add_argument("--allow-dirty", action="store_true", help="Allow executing from a dirty repository. Default: blocked.")
    chief_dispatch.add_argument("--allow-coverage-gap", action="store_true", help="Allow dispatch when plan status is needs_workflow_coverage.")
    chief_dispatch.add_argument("--test-command", default=None, help="Run this test/build command as the acceptance gate (works on any project, no workflow needed), e.g. 'pytest -q' or 'npm test'.")
    chief_dispatch.add_argument("--allow-test-edits", action="store_true", help="Let the worker modify test files without failing verification (only for tasks explicitly about changing tests).")
    chief_dispatch.add_argument("--merge", action="store_true", help="After verification passes, merge the worker's isolated branch back into the current branch. Only merges when verified; aborts on conflict.")
    chief_dispatch.add_argument("--auto-repair-once", action="store_true", help="Compatibility alias for --max-repair-rounds 1.")
    chief_dispatch.add_argument("--max-repair-rounds", type=int, default=None, help="Maximum repair rounds. Default: 2; use 0 to disable repair.")
    chief_dispatch.add_argument("--reasoning-effort", default=None, help="Codex reasoning override. Default: inherit config.toml.")
    chief_dispatch.add_argument("--dispatch-mode", choices=["tracked", "delegated"], default="tracked", help="Worker prompt mode. Default: tracked.")
    chief_dispatch.add_argument("--prompt-style", choices=["expanded", "legacy"], default="expanded", help="Prompt compatibility style. Default: expanded.")
    chief_dispatch.add_argument("--repair-strategy", choices=["resume", "fresh"], default="resume", help="Repair session strategy. Default: resume.")
    chief_dispatch.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    chief_run = subparsers.add_parser("chief-run", help="Run a bounded DevPacer mission around planning, dispatch, verification, and repair.")
    chief_run.add_argument("--goal", default=None, help="User objective. Required unless --plan is supplied.")
    chief_run.add_argument("--plan", default=None, help="Saved chief plan id to run instead of building a new plan.")
    chief_run.add_argument("--mission-id", default=None, help="Optional mission id. Default: timestamp plus objective hash.")
    chief_run.add_argument("--resume", default=None, help="Resume a saved mission id instead of creating a new mission.")
    chief_run.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing Checkpoint workflows.")
    chief_run.add_argument("--repo-root", default=".", help="Git repository root. Default: current directory.")
    chief_run.add_argument("--base", default="HEAD", help="Git base ref for diff routing. Default: HEAD.")
    chief_run.add_argument("--agent", action="append", default=[], help="Worker/inspection agent label, for example codex or gemini. Can be repeated.")
    chief_run.add_argument("--answer", action="append", default=[], help="Answer to a clarifying question. Repeat to add several.")
    chief_run.add_argument("--interview", action="store_true", help="Surface clarifying questions when the goal is vague.")
    chief_run.add_argument("--max-rounds", type=int, default=3, help="Maximum worker/verification rounds. Default: 3 (initial plus two repairs).")
    chief_run.add_argument("--max-repair-rounds", type=int, default=2, help="Maximum repair rounds within --max-rounds. Default: 2.")
    chief_run.add_argument("--max-wall-minutes", type=int, default=60, help="Mission wall-clock budget. Default: 60.")
    chief_run.add_argument("--max-worker-minutes", type=int, default=45, help="Worker process budget per attempt. Default: 45.")
    chief_run.add_argument("--dry-run", action="store_true", help="Preview only. This is also the default unless --execute is passed.")
    chief_run.add_argument("--execute", action="store_true", help="Actually launch the Codex worker adapter under the mission budget.")
    chief_run.add_argument("--background", action="store_true", help="Start/resume the mission in a background process and return immediately.")
    chief_run.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")
    chief_run.add_argument("--include-slow", action="store_true", help="Include slow verification workflows.")
    chief_run.add_argument("--max-workflows", type=int, default=10, help="Maximum workflows to verify after worker exit. Default: 10.")
    chief_run.add_argument("--timeout-seconds", type=float, default=1800.0, help="Worker timeout cap. Default: 1800.")
    chief_run.add_argument("--allow-dirty", action="store_true", help="Allow executing from a dirty repository. Default: blocked.")
    chief_run.add_argument("--allow-coverage-gap", action="store_true", help="Allow running even when plan coverage is weak.")
    chief_run.add_argument("--test-command", default=None, help="Run this test/build command as the acceptance gate (works on any project, no workflow needed), e.g. 'pytest -q' or 'npm test'.")
    chief_run.add_argument("--require-env", action="append", default=[], help="Require an environment variable before running the verification command. Can be repeated.")
    chief_run.add_argument("--allow-test-edits", action="store_true", help="Let the worker modify test files without failing verification (only for tasks explicitly about changing tests).")
    chief_run.add_argument("--merge", action="store_true", help="After verification passes, merge the worker's isolated branch back into the current branch. Only merges when verified; aborts on conflict.")
    chief_run.add_argument("--reasoning-effort", default=None, help="Codex reasoning override. Default: inherit config.toml.")
    chief_run.add_argument("--dispatch-mode", choices=["tracked", "delegated"], default=None, help="Worker prompt mode. Default: tracked or the saved mission value.")
    chief_run.add_argument("--prompt-style", choices=["expanded", "legacy"], default=None, help="Prompt compatibility style. Default: expanded.")
    chief_run.add_argument("--repair-strategy", choices=["resume", "fresh"], default=None, help="Repair session strategy. Default: resume.")
    chief_run.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    chief_run_demo = subparsers.add_parser("chief-run-demo", help="Run a deterministic DevPacer checkout mission in an isolated demo git repo.")
    chief_run_demo.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root used to store demo repos. Default: .agent-workspace.")
    chief_run_demo.add_argument("--demo-root", default=None, help="Optional directory where the isolated demo repo is created.")
    chief_run_demo.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    chief_missions = subparsers.add_parser("chief-missions", help="List or show saved DevPacer missions.")
    chief_missions.add_argument("action", choices=["list", "show"], help="list saved missions, or show one by id.")
    chief_missions.add_argument("mission_id", nargs="?", default=None, help="Mission id (required for show).")
    chief_missions.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing missions.")
    chief_missions.add_argument("--limit", type=int, default=None, help="Maximum missions to list.")
    chief_missions.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    chief_memory = subparsers.add_parser("chief-memory", help="Summarize evidence-derived DevPacer project memory.")
    chief_memory.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing missions and plans.")
    chief_memory.add_argument("--repo-root", default=".", help="Repository root for PACER.md/.pacer instruction memory. Default: current directory.")
    chief_memory.add_argument("--goal", default=None, help="Optional goal to rank relevant previous missions.")
    chief_memory.add_argument("--limit", type=int, default=8, help="Maximum memory entries. Default: 8.")
    chief_memory.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    repo_map = subparsers.add_parser("repo-map", help="Build/refresh the zero-token repository architecture map.")
    repo_map.add_argument("--repo-root", default=".", help="Repository root to index. Default: current directory.")
    repo_map.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root holding the map cache.")
    repo_map.add_argument("--goal", default="", help="Optional objective to focus the rendered excerpt.")
    repo_map.add_argument("--max-lines", type=int, default=60, help="Line budget for the rendered excerpt. Default: 60.")
    repo_map.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    refine_goal_cmd = subparsers.add_parser("refine-goal", help="Sharpen a vague goal via a cheap model (receptionist); falls back to rule-based questions offline.")
    refine_goal_cmd.add_argument("--goal", required=True, help="The rough goal to sharpen.")
    refine_goal_cmd.add_argument("--answer", action="append", default=[], help="Answer to a clarifying question. Can be repeated.")
    refine_goal_cmd.add_argument("--model", default=None, help="Intake model id. Default: auto-resolve a cheap backend (e.g. DeepSeek) or a small model.")
    refine_goal_cmd.add_argument("--base-url", default=None, help="Override the intake endpoint base URL (for a custom cheap endpoint).")
    refine_goal_cmd.add_argument("--endpoint", default=None, help="Override the intake endpoint path (default: /chat/completions).")
    refine_goal_cmd.add_argument("--no-model", action="store_true", help="Skip the model; use deterministic rules only.")
    refine_goal_cmd.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    quota = subparsers.add_parser("quota", help="Show subscription quota snapshots for Codex/Claude (not API spend).")
    quota.add_argument("--refresh-codex", action="store_true", help="Run PACER_CODEX_STATUS_COMMAND and parse Codex /usage or /status output.")
    quota.add_argument("--codex-command", default=None, help="Command that prints Codex /usage or /status output. Overrides PACER_CODEX_STATUS_COMMAND.")
    quota.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    model_select = subparsers.add_parser("model-select", help="Dynamically choose a model from configured candidates for one task.")
    model_select.add_argument("--goal", required=True, help="Task objective.")
    model_select.add_argument("--changed-file", action="append", default=[], help="Changed or expected file path. Can be repeated.")
    model_select.add_argument("--acceptance", action="append", default=[], help="Acceptance criterion. Can be repeated.")
    model_select.add_argument("--repeated-failure", action="store_true", help="Escalate because a previous attempt failed.")
    model_select.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing model_pool.json and quota context.")
    model_select.add_argument("--model-pool", default=None, help="Optional model pool JSON. Default: <workspace-root>/model_pool.json.")
    model_select.add_argument("--credential-source", default=None, help="Optional credential source for auto-discovered providers.")
    model_select.add_argument("--quota-file", default=None, help="Optional subscription quota snapshot JSON.")
    model_select.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    permission_plan_cmd = subparsers.add_parser("permission-plan", help="Assess command risk before an agent or automation run.")
    permission_plan_cmd.add_argument("--command", dest="assess_command", action="append", required=True, help="Command to assess. Can be repeated.")
    permission_plan_cmd.add_argument("--repo-root", default=".", help="Repository root for path-safety checks. Default: current directory.")
    permission_plan_cmd.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    hourly_plan = subparsers.add_parser("hourly-plan", help="Plan ready tasks inside the current subscription quota window.")
    hourly_plan.add_argument("--tasks-file", required=True, help="JSON file containing a list of tasks or {tasks:[...]}.")
    hourly_plan.add_argument("--quota-file", default=None, help="Optional subscription quota snapshot JSON.")
    hourly_plan.add_argument("--hours", type=float, default=5.0, help="Planning window hours. Default: 5.")
    hourly_plan.add_argument("--reserve-minutes", type=int, default=45, help="Strong-model reserve minutes. Default: 45.")
    hourly_plan.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    notify = subparsers.add_parser("notify", help="Build or send an email notification for mission/quota events.")
    notify.add_argument("--event", default="mission_stopped", help="Event name, e.g. mission_verified, quota_warning, worker_error.")
    notify.add_argument("--payload", default=None, help="Notification payload JSON string.")
    notify.add_argument("--payload-file", default=None, help="Notification payload JSON file.")
    notify.add_argument("--config", default=None, help="Notification config JSON. Default: ~/.checkpoint/notifications.json.")
    notify.add_argument("--send", action="store_true", help="Actually send email. Default is dry-run.")
    notify.add_argument("--template", action="store_true", help="Print a notification config template.")
    notify.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    subparsers.add_parser(
        "quota-statusline",
        help="Claude Code statusLine command: reads session JSON on stdin, records rate_limits, prints a status line.",
    )

    mission = subparsers.add_parser("mission", help="High-level DevPacer mission workflow.")
    mission.add_argument(
        "action",
        choices=["start", "resume", "status", "list", "show", "queue", "worker", "memory", "import"],
        help="Mission workflow action.",
    )
    mission.add_argument("--goal", default=None, help="User objective for mission start.")
    mission.add_argument("--plan", default=None, help="Saved chief plan id to run instead of building a new plan.")
    mission.add_argument("--file", default=None, help="Development plan file for mission import.")
    mission.add_argument("--mission", default=None, help="Mission id for resume/status/show/queue.")
    mission.add_argument("--mission-id", default=None, help="Optional mission id when starting a new mission.")
    mission.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing missions.")
    mission.add_argument("--repo-root", default=".", help="Git repository root. Default: current directory.")
    mission.add_argument("--base", default="HEAD", help="Git base ref for diff routing. Default: HEAD.")
    mission.add_argument("--agent", action="append", default=[], help="Worker/inspection agent label. Can be repeated.")
    mission.add_argument("--answer", action="append", default=[], help="Answer to a clarifying question. Can be repeated.")
    mission.add_argument("--interview", action="store_true", help="Surface clarifying questions when the goal is vague.")
    mission.add_argument("--max-rounds", type=int, default=3, help="Maximum worker/verification rounds. Default: 3.")
    mission.add_argument("--max-repair-rounds", type=int, default=2, help="Maximum repair rounds. Default: 2.")
    mission.add_argument("--max-wall-minutes", type=int, default=60, help="Mission wall-clock budget. Default: 60.")
    mission.add_argument("--max-worker-minutes", type=int, default=45, help="Worker process budget per attempt. Default: 45.")
    mission.add_argument("--execute", action="store_true", help="Actually launch the Codex worker adapter.")
    mission.add_argument("--background", action="store_true", help="Run the mission in a background process.")
    mission.add_argument("--create", action="store_true", help="Import action: create preview missions from extracted drafts.")
    mission.add_argument("--queue", action="store_true", help="Import action: submit created preview missions to the mission queue.")
    mission.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")
    mission.add_argument("--include-slow", action="store_true", help="Include slow verification workflows.")
    mission.add_argument("--max-workflows", type=int, default=10, help="Maximum workflows to verify. Default: 10.")
    mission.add_argument("--timeout-seconds", type=float, default=1800.0, help="Worker timeout cap. Default: 1800.")
    mission.add_argument("--allow-dirty", action="store_true", help="Allow executing from a dirty repository.")
    mission.add_argument("--allow-coverage-gap", action="store_true", help="Allow running even when plan coverage is weak.")
    mission.add_argument("--test-command", default=None, help="Run this test/build command as the acceptance gate (works on any project, no workflow needed), e.g. 'pytest -q' or 'npm test'.")
    mission.add_argument("--allow-test-edits", action="store_true", help="Let the worker modify test files without failing verification (only for tasks explicitly about changing tests).")
    mission.add_argument("--merge", action="store_true", help="After verification passes, merge the worker's isolated branch back into the current branch. Only merges when verified; aborts on conflict.")
    mission.add_argument("--reasoning-effort", default=None, help="Codex reasoning override. Default: inherit config.toml.")
    mission.add_argument("--dispatch-mode", choices=["tracked", "delegated"], default=None, help="Worker prompt mode.")
    mission.add_argument("--prompt-style", choices=["expanded", "legacy"], default=None, help="Prompt compatibility style.")
    mission.add_argument("--repair-strategy", choices=["resume", "fresh"], default=None, help="Repair session strategy.")
    mission.add_argument("--merge-policy", choices=["manual", "auto", "never"], default="manual", help="Import/queue merge behavior after verification. Default: manual.")
    mission.add_argument("--priority", type=int, default=0, help="Queue priority for mission queue. Default: 0.")
    mission.add_argument("--force", action="store_true", help="Force queueing a non-runnable mission after review.")
    mission.add_argument("--run-once", action="store_true", help="Worker action: process one queued mission and exit.")
    mission.add_argument("--watch", action="store_true", help="Worker action: keep polling for queued missions.")
    mission.add_argument("--poll-seconds", type=float, default=5.0, help="Worker action: polling delay. Default: 5.")
    mission.add_argument("--max-items", type=int, default=None, help="Worker action: maximum queued missions to process.")
    mission.add_argument("--max-seconds", type=float, default=None, help="Worker action: wall-clock cap.")
    mission.add_argument("--limit", type=int, default=8, help="Memory/import action: maximum entries. Default: 8.")
    mission.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    program = subparsers.add_parser("program", help="Project Autopilot program workflow.")
    program.add_argument("action", choices=["create", "plan", "start", "status", "list", "report"], help="Program action.")
    program.add_argument("--program", default=None, help="Program id.")
    program.add_argument("--file", default=None, help="Development plan file for program create.")
    program.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing programs.")
    program.add_argument("--repo-root", default=".", help="Git repository root. Default: current directory.")
    program.add_argument("--objective", default=None, help="Optional program objective/title.")
    program.add_argument("--hours", type=float, default=5.0, help="Planning window in hours. Default: 5.")
    program.add_argument("--agent", default="codex", help="Default coding worker for program tasks. Default: codex.")
    program.add_argument("--test-command", default="auto", help="Default acceptance command for program tasks. Default: auto.")
    program.add_argument("--limit", type=int, default=12, help="Maximum tasks to import. Default: 12.")
    program.add_argument("--parallel", action="store_true", help="Do not chain imported tasks sequentially.")
    program.add_argument(
        "--autonomous",
        action="store_true",
        help="Let Pacer schedule all imported tasks, use delegated workers, and allocate quota without conservative reserves.",
    )
    program.add_argument("--allow-dirty", action="store_true", help="Explicitly overlay the current dirty checkout into autonomous worktrees.")
    program.add_argument("--model", default=None, help="Explicit Codex model for every program task, for example gpt-5.5.")
    program.add_argument("--strong-model", default=None, help="Model override for strong-worker tasks.")
    program.add_argument("--cheap-model", default=None, help="Model override for cheap-worker tasks. Autonomous default: gpt-5.6-luna.")
    program.add_argument("--research-model", default=None, help="Model override for research/doc tasks. Autonomous default: gpt-5.6-luna.")
    program.add_argument("--codex-provider", default=None, help="Codex provider override, for example openai or custom.")
    program.add_argument("--codex-failover-provider", default=None, help="Alternate Codex provider used after quota/rate-limit failure.")
    program.add_argument("--memory-mode", choices=["enabled", "disabled"], default="enabled", help="Inject local project memory into workers. Default: enabled.")
    program.add_argument("--acceptance-policy", choices=["strict", "standard"], default=None, help="Acceptance strength. Autonomous default: strict.")
    program.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    autopilot = subparsers.add_parser("autopilot", help="Create and start a Project Autopilot program from a plan file.")
    autopilot.add_argument("--file", required=True, help="Development plan file.")
    autopilot.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root.")
    autopilot.add_argument("--repo-root", default=".", help="Git repository root. Default: current directory.")
    autopilot.add_argument("--objective", default=None, help="Optional program objective/title.")
    autopilot.add_argument("--hours", type=float, default=5.0, help="Planning window in hours. Default: 5.")
    autopilot.add_argument("--agent", default="codex", help="Default coding worker. Default: codex.")
    autopilot.add_argument("--test-command", default="auto", help="Default acceptance command. Default: auto.")
    autopilot.add_argument("--limit", type=int, default=12, help="Maximum tasks to import. Default: 12.")
    autopilot.add_argument("--parallel", action="store_true", help="Do not chain imported tasks sequentially.")
    autopilot.add_argument(
        "--autonomous",
        action="store_true",
        help="Enable unrestricted Pacer orchestration while keeping verification, external-access, and merge gates.",
    )
    autopilot.add_argument("--allow-dirty", action="store_true", help="Explicitly overlay the current dirty checkout into autonomous worktrees.")
    autopilot.add_argument("--model", default=None, help="Explicit Codex model for every program task, for example gpt-5.5.")
    autopilot.add_argument("--strong-model", default=None, help="Model override for strong-worker tasks.")
    autopilot.add_argument("--cheap-model", default=None, help="Model override for cheap-worker tasks. Autonomous default: gpt-5.6-luna.")
    autopilot.add_argument("--research-model", default=None, help="Model override for research/doc tasks. Autonomous default: gpt-5.6-luna.")
    autopilot.add_argument("--codex-provider", default=None, help="Codex provider override, for example openai or custom.")
    autopilot.add_argument("--codex-failover-provider", default=None, help="Alternate Codex provider used after quota/rate-limit failure.")
    autopilot.add_argument("--memory-mode", choices=["enabled", "disabled"], default="enabled", help="Inject local project memory into workers. Default: enabled.")
    autopilot.add_argument("--acceptance-policy", choices=["strict", "standard"], default=None, help="Acceptance strength. Autonomous default: strict.")
    autopilot.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    chief_status = subparsers.add_parser("chief-status", help="Show the current status of a saved DevPacer mission.")
    chief_status.add_argument("--mission", required=True, help="Mission id to inspect.")
    chief_status.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing missions.")
    chief_status.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    chief_queue = subparsers.add_parser("chief-queue", help="Submit and inspect queued DevPacer missions.")
    chief_queue.add_argument("action", choices=["submit", "list", "show"], help="submit a mission, list queue items, or show one item.")
    chief_queue.add_argument("--mission", default=None, help="Mission id to submit.")
    chief_queue.add_argument("--queue-id", default=None, help="Queue item id for show.")
    chief_queue.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing missions and mission_queue.")
    chief_queue.add_argument("--priority", type=int, default=0, help="Higher priority runs first. Default: 0.")
    chief_queue.add_argument("--status", default=None, help="Optional status filter for list.")
    chief_queue.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")
    chief_queue.add_argument("--include-slow", action="store_true", help="Include slow verification workflows when the worker runs this mission.")
    chief_queue.add_argument("--max-workflows", type=int, default=10, help="Maximum workflows to verify. Default: 10.")
    chief_queue.add_argument("--timeout-seconds", type=float, default=1800.0, help="Worker timeout cap. Default: 1800.")
    chief_queue.add_argument("--allow-dirty", action="store_true", help="Allow the mission worker to execute from a dirty repository.")
    chief_queue.add_argument("--allow-coverage-gap", action="store_true", help="Allow executing a mission whose plan coverage is weak.")
    chief_queue.add_argument("--agent", default=None, help="Coding worker for this queued mission, e.g. codex or claude-code.")
    chief_queue.add_argument("--test-command", default=None, help="Test/build command to use as this queued mission's acceptance gate.")
    chief_queue.add_argument("--allow-test-edits", action="store_true", help="Let the worker modify test files for this queued mission.")
    chief_queue.add_argument("--merge-policy", choices=["manual", "auto", "never"], default="manual", help="Queue merge behavior after verification. Default: manual.")
    chief_queue.add_argument("--reasoning-effort", default=None, help="Codex reasoning override for this queue item.")
    chief_queue.add_argument("--dispatch-mode", choices=["tracked", "delegated"], default=None, help="Worker prompt mode for this queue item.")
    chief_queue.add_argument("--prompt-style", choices=["expanded", "legacy"], default=None, help="Prompt style for this queue item.")
    chief_queue.add_argument("--repair-strategy", choices=["resume", "fresh"], default=None, help="Repair strategy for this queue item.")
    chief_queue.add_argument("--force", action="store_true", help="Submit a mission even if its current status is not normally runnable.")
    chief_queue.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    chief_worker = subparsers.add_parser("chief-worker", help="Run queued DevPacer missions.")
    chief_worker.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing mission_queue.")
    chief_worker.add_argument("--run-once", action="store_true", help="Claim at most one pending mission and exit. This is the default.")
    chief_worker.add_argument("--watch", action="store_true", help="Keep polling for queued missions until stopped or limits are reached.")
    chief_worker.add_argument("--poll-seconds", type=float, default=5.0, help="Polling delay in watch mode. Default: 5.")
    chief_worker.add_argument("--max-items", type=int, default=None, help="Optional maximum queued missions to process.")
    chief_worker.add_argument("--max-seconds", type=float, default=None, help="Optional worker wall-clock cap.")
    chief_worker.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    chief_background_worker = subparsers.add_parser("chief-background-worker", help=argparse.SUPPRESS)
    chief_background_worker.add_argument("--mission", required=True, help=argparse.SUPPRESS)
    chief_background_worker.add_argument("--workspace-root", default=".agent-workspace", help=argparse.SUPPRESS)
    chief_background_worker.add_argument("--agent", action="append", default=[], help=argparse.SUPPRESS)
    chief_background_worker.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run", help=argparse.SUPPRESS)
    chief_background_worker.add_argument("--include-slow", action="store_true", help=argparse.SUPPRESS)
    chief_background_worker.add_argument("--max-workflows", type=int, default=10, help=argparse.SUPPRESS)
    chief_background_worker.add_argument("--timeout-seconds", type=float, default=1800.0, help=argparse.SUPPRESS)
    chief_background_worker.add_argument("--allow-dirty", action="store_true", help=argparse.SUPPRESS)
    chief_background_worker.add_argument("--allow-coverage-gap", action="store_true", help=argparse.SUPPRESS)
    chief_background_worker.add_argument("--test-command", default=None, help=argparse.SUPPRESS)
    chief_background_worker.add_argument("--allow-test-edits", action="store_true", help=argparse.SUPPRESS)
    chief_background_worker.add_argument("--merge", action="store_true", help=argparse.SUPPRESS)
    chief_background_worker.add_argument("--format", choices=["json", "markdown"], default="markdown", help=argparse.SUPPRESS)

    agents = subparsers.add_parser("agents", help="Inspect coding-agent capability profiles (Codex, Claude Code).")
    agents.add_argument("action", choices=["doctor", "show", "list"], help="doctor probes installed agents; show prints one profile; list names profiles.")
    agents.add_argument("agent", nargs="?", default=None, help="Agent name for show (codex or claude-code).")
    agents.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    dashboard_cmd = subparsers.add_parser("dashboard", help="Serve a local web dashboard: missions, plans, verification status, installed agents.")
    dashboard_cmd.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root to visualize.")
    dashboard_cmd.add_argument(
        "--host",
        choices=["127.0.0.1", "localhost", "::1"],
        default="127.0.0.1",
        help="Local-only bind host. Default: 127.0.0.1.",
    )
    dashboard_cmd.add_argument("--port", type=int, default=8787, help="Bind port. Default: 8787.")
    dashboard_cmd.add_argument("--no-open", action="store_true", help="Do not auto-open the browser.")

    portfolio_cmd = subparsers.add_parser("portfolio-dashboard", help="Serve xiao's multi-project dashboard.")
    portfolio_cmd.add_argument("--project", action="append", required=True, help="Project root to visualize. Can be repeated.")
    portfolio_cmd.add_argument("--host", default="127.0.0.1", help="Bind host. Default: 127.0.0.1 (local only).")
    portfolio_cmd.add_argument("--port", type=int, default=8797, help="Bind port. Default: 8797.")
    portfolio_cmd.add_argument("--no-open", action="store_true", help="Do not auto-open the browser.")

    portfolio_worker = subparsers.add_parser("portfolio-worker", help="Run queued DevPacer missions across multiple projects concurrently.")
    portfolio_worker.add_argument("--project", action="append", required=True, help="Project root whose .agent-workspace mission queue should be processed. Can be repeated.")
    portfolio_worker.add_argument("--workspace-name", default=".agent-workspace", help="Workspace directory name inside each project. Default: .agent-workspace.")
    portfolio_worker.add_argument("--max-workers", type=int, default=2, help="Maximum projects to process concurrently. Default: 2.")
    portfolio_worker.add_argument("--max-items-per-project", type=int, default=None, help="Maximum queued missions to process per project. Default: 1 per project, or unlimited with --watch.")
    portfolio_worker.add_argument("--poll-seconds", type=float, default=0.5, help="Idle poll delay used inside each project worker. Default: 0.5.")
    portfolio_worker.add_argument("--watch", action="store_true", help="Keep polling each project queue until stopped, --max-seconds is reached, or --max-items-per-project is reached.")
    portfolio_worker.add_argument("--max-seconds", type=float, default=None, help="Optional portfolio worker wall-clock cap, e.g. 3600 for one hour.")
    portfolio_worker.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    app_cmd = subparsers.add_parser("app", help="Open the DevPacer desktop workbench (a real window: pick a project, type a goal, run).")
    app_cmd.add_argument("--project", default=None, help="Optional project folder to preselect.")

    workbench_audit = subparsers.add_parser(
        "workbench-audit",
        help="Run a 10-round natural-language audit of the workbench entry chain and write a dedicated review document.",
    )
    workbench_audit.add_argument("--project-dir", default=".", help="Project directory that the workbench should validate.")
    workbench_audit.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root used for the audit report.")
    workbench_audit.add_argument("--output", default=None, help="Output markdown or JSON file. Default: <workspace-root>/reports/workbench_entry_audit_<project>_<timestamp>.md.")
    workbench_audit.add_argument("--no-model", action="store_true", help="Disable model-assisted intake and use deterministic fallback only.")
    workbench_audit.add_argument("--backend-order", default="mimo,deepseek", help="Comma-separated backend preference for model-assisted intake.")
    workbench_audit.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    verify = subparsers.add_parser("verify", help="Run verification-tagged workspace workflows.")
    verify.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing workflows.")
    verify.add_argument("--tags", default="verification", help="Comma-separated workflow tags to run. Default: verification.")
    verify.add_argument("--workflow", action="append", default=[], help="Workflow name or workspace-relative path to verify. Can be used multiple times.")
    verify.add_argument("--max-workflows", type=int, default=10, help="Maximum matching workflows to run. Default: 10.")
    verify.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")
    verify.add_argument("--wait-lock", action="store_true", help="Wait for workflow locks instead of failing immediately.")
    verify.add_argument("--lock-wait-seconds", type=float, default=30.0, help="Maximum seconds to wait when queued. Default: 30.")
    verify.add_argument("--include-slow", action="store_true", help="Include workflows tagged 'slow'. Default: skipped.")
    verify.add_argument("--for", dest="target_agent", default="codex", help="Target coding agent label.")
    verify.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    verify_now = subparsers.add_parser("verify-now", help="Run the default Checkpoint verification path.")
    verify_now.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing workflows.")
    verify_now.add_argument("--tags", default="verification", help="Comma-separated workflow tags to run. Default: verification.")
    verify_now.add_argument("--workflow", action="append", default=[], help="Workflow name or workspace-relative path to verify. Can be used multiple times.")
    verify_now.add_argument("--max-workflows", type=int, default=5, help="Maximum matching workflows to run. Default: 5.")
    verify_now.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="supervised", help="Execution profile. Default: supervised.")
    verify_now.add_argument("--live", action="store_true", help="Shortcut for --run-profile supervised when verifying local demo or supervised-safe workflows.")
    verify_now.add_argument("--lock-wait-seconds", type=float, default=30.0, help="Maximum seconds to wait for workflow locks. Default: 30.")
    verify_now.add_argument("--include-slow", action="store_true", help="Include workflows tagged 'slow'. Default: skipped.")
    verify_now.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    add_workflow_parsers(subparsers)

    codex_check = subparsers.add_parser("codex-check", help="Smart check for Codex/Claude Code: git-diff-aware, fast by default.")
    codex_check.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing workflows.")
    codex_check.add_argument("--repo-root", default=".", help="Git repository root. Default: current directory.")
    codex_check.add_argument("--base", default="HEAD", help="Git base ref for diff. Default: HEAD.")
    codex_check.add_argument("--tags", default="verification", help="Comma-separated workflow tags to run. Default: verification.")
    codex_check.add_argument("--max-workflows", type=int, default=10, help="Maximum workflows to run. Default: 10.")
    codex_check.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="supervised")
    codex_check.add_argument("--include-slow", action="store_true", help="Include workflows tagged 'slow'. Default: skipped.")
    codex_check.add_argument("--strict", action="store_true", help="Exit non-zero on a coverage gap (uncovered or fallback-only changes), not just on failures.")
    codex_check.add_argument("--from-step", default=None, help="Start each selected workflow from the named step id.")
    codex_check.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    test_plan = subparsers.add_parser("test-plan", help="Print the auto-selected local test command without running it.")
    test_plan.add_argument("--repo-root", default=".", help="Git repository root. Default: current directory.")
    test_plan.add_argument("--base", default="HEAD", help="Git base ref for changed-file selection. Default: HEAD.")
    test_plan.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    connect = subparsers.add_parser("connect", help="Connect Checkpoint to an AI coding platform.")
    connect.add_argument("platform", choices=["claude-code", "cursor", "codex"])
    connect.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root to initialize/connect.")
    connect.add_argument("--repo-root", default=".", help="Repository root. Default: current directory.")
    connect.add_argument("--python", default="python", help="Python executable used by MCP clients. Default: python.")
    connect.add_argument("--global", dest="global_config", action="store_true", help="Write user-level config where supported.")
    connect.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    ci_templates = subparsers.add_parser("install-ci-templates", help="Install CI/local quality gate templates.")
    ci_templates.add_argument("--root", default=".", help="Repository root to receive generated templates. Default: current directory.")
    ci_templates.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root used by generated quality gates.")
    ci_templates.add_argument("--overwrite", action="store_true", help="Overwrite existing CI template files.")

    generate_ci = subparsers.add_parser("generate-ci", help="Generate a GitHub Actions CI workflow YAML.")
    generate_ci.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root used in generated commands.")
    generate_ci.add_argument("--python-version", default="3.11", help="Python version to use in CI.")
    generate_ci.add_argument("--node-version", default="20", help="Node version to use for extension compilation.")
    generate_ci.add_argument("--output", default=None, help="Optional file path to write the workflow YAML.")
    generate_ci.add_argument("--format", choices=["yaml", "json"], default="yaml", help="Output format. Default: yaml.")

    generate_integrations = subparsers.add_parser("generate-integrations", help="Generate editor and IDE integration files for Checkpoint.")
    generate_integrations.add_argument("--root", default=".", help="Repository root to receive generated integration files.")
    generate_integrations.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root used in generated command snippets.")
    generate_integrations.add_argument("--overwrite", action="store_true", help="Overwrite changed integration files.")
    generate_integrations.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    export_playwright = subparsers.add_parser("export-to-playwright", help="Export a workflow YAML file to a Playwright Test spec.")
    export_playwright.add_argument("workflow", help="Workflow YAML file to export.")
    export_playwright.add_argument("--output", default=None, help="Optional output .spec.ts file.")
    export_playwright.add_argument("--spec-name", default=None, help="Optional Playwright test title override.")
    export_playwright.add_argument("--format", choices=["ts", "json"], default="ts", help="Output format. Default: ts.")

    github_pr_comment = subparsers.add_parser("github-pr-comment", help="Build or post a GitHub PR failure comment from the latest failed run.")
    github_pr_comment.add_argument("--report-root", default=".runs", help="Workspace run directory root containing workflow reports.")
    github_pr_comment.add_argument("--quality-root", default=".runs/quality_gates", help="Quality gate report root.")
    github_pr_comment.add_argument("--artifact-url", default="", help="Uploaded artifact URL to reference in the comment.")
    github_pr_comment.add_argument("--run-url", default="", help="Workflow run URL to reference in the comment.")
    github_pr_comment.add_argument("--event-path", default=None, help="GitHub event JSON path. Defaults to GITHUB_EVENT_PATH.")
    github_pr_comment.add_argument("--repository", default=None, help="GitHub repository slug. Defaults to GITHUB_REPOSITORY.")
    github_pr_comment.add_argument("--token-env", default="GITHUB_TOKEN", help="Environment variable that stores the GitHub token.")
    github_pr_comment.add_argument("--max-screenshots", type=int, default=3, help="Maximum screenshots to mention. Default: 3.")
    github_pr_comment.add_argument("--dry-run", action="store_true", help="Build the comment without posting it.")
    github_pr_comment.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    add_external_sample_parsers(subparsers)

    auth_plan = subparsers.add_parser("auth-state-plan", help="Plan a redacted storage_state import.")
    auth_plan.add_argument("--source", required=True, help="Existing Playwright storage_state JSON.")
    auth_plan.add_argument("--name", required=True, help="Safe account alias for .agent-auth/<name>.json.")
    auth_plan.add_argument("--workspace-root", default=".", help="Project/workspace root. Default: current directory.")
    auth_plan.add_argument("--overwrite", action="store_true", help="Allow replacing an existing auth state.")

    auth_import = subparsers.add_parser("auth-state-import", help="Import Playwright storage_state JSON into .agent-auth.")
    auth_import.add_argument("--source", required=True, help="Existing Playwright storage_state JSON.")
    auth_import.add_argument("--name", required=True, help="Safe account alias for .agent-auth/<name>.json.")
    auth_import.add_argument("--workspace-root", default=".", help="Project/workspace root. Default: current directory.")
    auth_import.add_argument("--overwrite", action="store_true", help="Replace an existing auth state.")

    auth_inspect = subparsers.add_parser("auth-state-inspect", help="Inspect storage_state metadata without printing secrets.")
    auth_inspect.add_argument("--path", required=True, help="Storage state JSON path.")

    auth_probe = subparsers.add_parser("auth-state-probe", help="Load storage_state in a browser context and verify domain/session readiness without printing secrets.")
    auth_probe.add_argument("--path", required=True, help="Storage state JSON path.")
    auth_probe.add_argument("--url", required=True, help="HTTPS URL whose domain should match the storage_state.")
    auth_probe.add_argument("--allowed-domain", help="Expected allowed domain. Defaults to the URL host.")
    auth_probe.add_argument("--headed", action="store_true", help="Run browser headed.")
    auth_probe.add_argument("--timeout-ms", type=int, default=10_000)
    auth_probe.add_argument("--format", choices=["json", "markdown"], default="json")

    model_creds = subparsers.add_parser("model-credentials-inspect", help="Inspect local model API credentials without printing secrets.")
    model_creds.add_argument("--source", default=None, help="Credential file path. Defaults to VISUAL_AGENT_MODEL_CREDENTIAL_FILE or model_api_keys.txt.")
    model_creds.add_argument("--preferred", default=None, help="Preferred provider. Defaults to VISUAL_AGENT_MODEL_PROVIDER or openai.")
    model_creds.add_argument("--format", choices=["json", "markdown"], default="json")

    model_probe = subparsers.add_parser("model-api-probe-plan", help="Plan a redacted model API readiness probe without sending secrets.")
    model_probe.add_argument("--source", default=None, help="Credential file path. Defaults to VISUAL_AGENT_MODEL_CREDENTIAL_FILE or model_api_keys.txt.")
    model_probe.add_argument("--preferred", default=None, help="Preferred provider. Defaults to VISUAL_AGENT_MODEL_PROVIDER or openai.")
    model_probe.add_argument("--base-url", default=None, help="Provider base URL for a future read-only probe.")
    model_probe.add_argument("--endpoint", default=None, help="Read-only endpoint path for a future probe.")
    model_probe.add_argument("--model", default=None, help="Optional model name for the future probe.")
    model_probe.add_argument("--run", action="store_true", help="Execute one low-cost probe request. Without this flag, only a plan is generated.")
    model_probe.add_argument("--timeout-seconds", type=float, default=15.0)
    model_probe.add_argument("--max-completion-tokens", type=int, default=64)
    model_probe.add_argument("--format", choices=["json", "markdown"], default="json")

    init_cmd = subparsers.add_parser("init", help="Initialize a visual-agent workspace.")
    add_init_workspace_arguments(init_cmd)

    init_ws = subparsers.add_parser("init-workspace", help="Initialize a visual-agent workspace.")
    add_init_workspace_arguments(init_ws)

    completions = subparsers.add_parser("generate-completions", help="Generate bash and zsh completion scripts.")
    completions.add_argument("--output-dir", default=".", help="Directory to write completion scripts. Default: current directory.")

    ws_status = subparsers.add_parser("workspace-status", help="Show workspace status.")
    ws_status.add_argument("--root", required=True, help="Workspace root directory.")

    subparsers.add_parser(
        "workspace-risk-policy-template",
        help="Print a copyable workspace.json quality policy template.",
    )
    ws_risk_policy_check = subparsers.add_parser(
        "workspace-risk-policy-check",
        help="Validate the workspace.json quality risk policy.",
    )
    ws_risk_policy_check.add_argument("--root", required=True, help="Workspace root directory.")
    ws_risk_policy_plan = subparsers.add_parser(
        "workspace-risk-policy-plan",
        help="Preview or apply a workspace.json quality risk policy patch.",
    )
    ws_risk_policy_plan.add_argument("--root", required=True, help="Workspace root directory.")
    ws_risk_policy_plan.add_argument("--overwrite", action="store_true", help="Let template defaults replace existing risk policy values.")
    ws_risk_policy_plan.add_argument("--apply", action="store_true", help="Write the proposed quality policy into workspace.json.")

    ws_dashboard = subparsers.add_parser("workspace-dashboard", help="Show a compact workspace console dashboard.")
    ws_dashboard.add_argument("--root", required=True, help="Workspace root directory.")
    ws_dashboard.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")
    ws_dashboard.add_argument("--limit", type=int, default=5, help="Maximum recent items per section. Default: 5.")

    ws_gui = subparsers.add_parser("workspace-gui", help="Open the read-only workspace desktop console.")
    ws_gui.add_argument("--root", required=True, help="Workspace root directory.")
    ws_gui.add_argument("--run-id", help="Optional run id to show first.")
    ws_gui.add_argument("--limit", type=int, default=10, help="Maximum recent reports to load. Default: 10.")

    ws_gui_actions = subparsers.add_parser("workspace-gui-actions", help="Export recent workspace GUI action history.")
    ws_gui_actions.add_argument("--root", required=True, help="Workspace root directory.")
    ws_gui_actions.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")
    ws_gui_actions.add_argument("--limit", type=int, default=20, help="Maximum events to load. Default: 20.")
    ws_gui_actions.add_argument("--action", help="Filter by GUI action id.")
    ws_gui_actions.add_argument("--status", choices=["success", "error"], help="Filter by action status.")

    ws_gui_action_index = subparsers.add_parser(
        "workspace-gui-action-index",
        help="Summarize recent workspace GUI action history for Planner/CI reads.",
    )
    ws_gui_action_index.add_argument("--root", required=True, help="Workspace root directory.")
    ws_gui_action_index.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")
    ws_gui_action_index.add_argument("--limit", type=int, default=100, help="Maximum recent events to summarize. Default: 100.")
    ws_gui_action_index.add_argument("--recent-error-limit", type=int, default=5, help="Maximum recent errors to include. Default: 5.")
    ws_gui_action_index.add_argument("--risk", action="store_true", help="Export risk summary with remediation checklist instead of the raw index.")

    ws_list = subparsers.add_parser("workspace-list", help="List workflows in a workspace.")
    ws_list.add_argument("--root", required=True, help="Workspace root directory.")
    ws_list.add_argument("--include-slow", action="store_true", help="Include workflows tagged 'slow'. Default: skipped.")

    ws_validate = subparsers.add_parser("workspace-validate", help="Validate all workflows in a workspace.")
    ws_validate.add_argument("--root", required=True, help="Workspace root directory.")
    ws_validate.add_argument("--strict", action="store_true", help="Apply production-oriented validation rules.")
    ws_validate.add_argument("--allow-high-risk", action="store_true", help="Allow high-risk actions in strict validation.")

    ws_run = subparsers.add_parser("workspace-run", help="Run a workflow from a workspace.")
    ws_run.add_argument("--root", required=True, help="Workspace root directory.")
    ws_run.add_argument("--workflow", required=True, help="Workflow name or relative path.")
    ws_run.add_argument("--inputs", help="Workflow input JSON string.")
    ws_run.add_argument("--inputs-file", help="Workflow input JSON file or name under inputs/.")
    ws_run.add_argument("--sensitive-fields", help="Comma-separated input paths to hash in audit logs.")
    ws_run.add_argument("--resume-from", help="Existing run directory to resume from checkpoint.")
    ws_run.add_argument("--allow-click", action="store_true", help="Allow real click/input actions. Default is dry-run.")
    ws_run.add_argument(
        "--run-profile",
        choices=RUN_PROFILE_CHOICES,
        default="dry-run",
        help="Execution permission profile. Default is dry-run.",
    )
    ws_run.add_argument("--skip-preflight", action="store_true", help="Skip runtime preflight checks.")
    ws_run.add_argument("--strict-preflight", action="store_true", help="Apply strict validation during preflight.")
    ws_run.add_argument("--allow-high-risk", action="store_true", help="Allow high-risk actions during strict preflight.")
    ws_run.add_argument("--no-lock", action="store_true", help="Disable run lock for controlled debugging.")
    ws_run.add_argument("--lock-ttl-seconds", type=float, default=3600.0, help="Run lock TTL. Default: 3600.")
    ws_run.add_argument("--wait-lock", action="store_true", help="Wait for the run lock instead of failing immediately.")
    ws_run.add_argument("--queue-when-locked", action="store_true", help="Wait for the run lock instead of failing immediately.")
    ws_run.add_argument("--lock-wait-seconds", type=float, default=30.0, help="Maximum seconds to wait when queued. Default: 30.")
    ws_run.add_argument("--lock-poll-seconds", type=float, default=0.5, help="Seconds between lock checks when queued. Default: 0.5.")
    ws_run.add_argument("--no-report-export", action="store_true", help="Do not export JSON/Markdown reports to workspace reports/.")
    ws_run.add_argument("--synthetic-on-capture-fail", action="store_true")
    ws_run.add_argument("--from-step", default=None, help="Start execution from the named step id.")
    ws_run.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    ws_runs = subparsers.add_parser("workspace-runs", help="List workspace run summaries.")
    ws_runs.add_argument("--root", required=True, help="Workspace root directory.")
    ws_runs.add_argument("--limit", type=int, default=20)

    ws_reports = subparsers.add_parser("workspace-reports", help="List exported workspace reports.")
    ws_reports.add_argument("--root", required=True, help="Workspace root directory.")

    ws_report_index = subparsers.add_parser("workspace-report-index", help="Build or query the workspace report index.")
    ws_report_index.add_argument("--root", required=True, help="Workspace root directory.")
    ws_report_index.add_argument("--rebuild", action="store_true", help="Rebuild reports/index.json before reading.")
    ws_report_index.add_argument("--status", choices=["success", "failed"], help="Filter by report status.")
    ws_report_index.add_argument("--workflow", help="Filter by workflow name.")
    ws_report_index.add_argument("--failed-only", action="store_true", help="Return only failed reports.")

    ws_report_detail = subparsers.add_parser("workspace-report-detail", help="Show one workspace report detail.")
    ws_report_detail.add_argument("--root", required=True, help="Workspace root directory.")
    ws_report_detail.add_argument("--run-id", required=True, help="Run id to inspect.")
    ws_report_detail.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    ws_tag_report = subparsers.add_parser("workspace-tag-report", help="Annotate a workspace run report.")
    ws_tag_report.add_argument("--root", required=True, help="Workspace root directory.")
    ws_tag_report.add_argument("--run-id", required=True, help="Run id to annotate.")
    ws_tag_report.add_argument(
        "--review-status",
        choices=["unreviewed", "reviewed", "needs_fix", "regression_ready", "ignored"],
        help="Human review status.",
    )
    ws_tag_report.add_argument("--tag", action="append", default=[], help="Tag to attach. Can be used multiple times.")
    ws_tag_report.add_argument("--note", help="Human review note.")
    ws_tag_report.add_argument("--regression-candidate", action="store_true", help="Mark as regression test candidate.")
    ws_tag_report.add_argument("--clear-regression-candidate", action="store_true", help="Clear regression test candidate flag.")

    ws_report_tags = subparsers.add_parser("workspace-report-tags", help="Show workspace report annotations.")
    ws_report_tags.add_argument("--root", required=True, help="Workspace root directory.")

    ws_product_issues = subparsers.add_parser("workspace-product-issues", help="Summarize failed reports as product issue groups.")
    ws_product_issues.add_argument("--root", required=True, help="Workspace root directory.")
    ws_product_issues.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")
    ws_product_issues.add_argument("--write", action="store_true", help="Write reports/product_issues.json before printing.")

    ws_export_regression = subparsers.add_parser("workspace-export-regression-fixture", help="Export a failed run report into a regression fixture draft.")
    ws_export_regression.add_argument("--root", required=True, help="Workspace root directory.")
    ws_export_regression.add_argument("--run-id", required=True, help="Failed run id to export.")
    ws_export_regression.add_argument("--allow-success", action="store_true", help="Allow exporting a successful run.")
    ws_export_regression.add_argument("--overwrite", action="store_true", help="Overwrite an existing export.")

    ws_promote_regression = subparsers.add_parser("workspace-promote-regression", help="Promote a regression fixture draft into workspace regression_tests/.")
    ws_promote_regression.add_argument("--root", required=True, help="Workspace root directory.")
    ws_promote_regression.add_argument("--run-id", required=True, help="Run id to promote.")
    ws_promote_regression.add_argument("--overwrite", action="store_true", help="Overwrite an existing promoted test.")

    ws_regression_tests = subparsers.add_parser("workspace-regression-tests", help="List promoted workspace regression tests.")
    ws_regression_tests.add_argument("--root", required=True, help="Workspace root directory.")

    ws_run_regression = subparsers.add_parser("workspace-run-regression-tests", help="Run workspace regression_tests and write a report.")
    ws_run_regression.add_argument("--root", required=True, help="Workspace root directory.")
    ws_run_regression.add_argument("--timeout-seconds", type=float, default=120.0, help="Maximum pytest runtime. Default: 120.")
    ws_run_regression.add_argument("--pytest-arg", action="append", default=[], help="Extra pytest argument. Can be used multiple times.")

    ws_queue_submit = subparsers.add_parser("workspace-queue-submit", help="Submit a workflow task to the workspace queue.")
    ws_queue_submit.add_argument("--root", required=True, help="Workspace root directory.")
    ws_queue_submit.add_argument("--workflow", required=True, help="Workflow name or relative path.")
    ws_queue_submit.add_argument("--inputs", help="Workflow input JSON string.")
    ws_queue_submit.add_argument("--inputs-file", help="Workflow input JSON file or name under inputs/.")
    ws_queue_submit.add_argument("--priority", type=int, default=0, help="Higher priority runs first. Default: 0.")
    ws_queue_submit.add_argument("--max-retries", type=int, default=0, help="Retry count after failures. Default: 0.")
    ws_queue_submit.add_argument(
        "--run-profile",
        choices=RUN_PROFILE_CHOICES,
        default="dry-run",
        help="Execution permission profile. Default is dry-run.",
    )
    ws_queue_submit.add_argument("--allow-click", action="store_true", help="Allow real click/input actions when the task runs.")

    ws_queue_list = subparsers.add_parser("workspace-queue-list", help="List workspace queue tasks.")
    ws_queue_list.add_argument("--root", required=True, help="Workspace root directory.")
    ws_queue_list.add_argument("--status", choices=["pending", "running", "success", "failed", "canceled"], help="Filter by task status.")

    ws_queue_cancel = subparsers.add_parser("workspace-queue-cancel", help="Cancel a pending workspace queue task.")
    ws_queue_cancel.add_argument("--root", required=True, help="Workspace root directory.")
    ws_queue_cancel.add_argument("--task-id", required=True, help="Task id to cancel.")
    ws_queue_cancel.add_argument("--reason", help="Optional cancel reason.")

    ws_queue_retry = subparsers.add_parser("workspace-queue-retry", help="Requeue a failed or canceled workspace queue task.")
    ws_queue_retry.add_argument("--root", required=True, help="Workspace root directory.")
    ws_queue_retry.add_argument("--task-id", required=True, help="Task id to retry.")

    ws_queue_run_next = subparsers.add_parser("workspace-queue-run-next", help="Run the next pending workspace queue task.")
    ws_queue_run_next.add_argument("--root", required=True, help="Workspace root directory.")

    ws_queue_worker = subparsers.add_parser("workspace-queue-worker", help="Continuously run pending workspace queue tasks.")
    ws_queue_worker.add_argument("--root", required=True, help="Workspace root directory.")
    ws_queue_worker.add_argument("--poll-seconds", type=float, default=1.0, help="Seconds to wait between idle polls. Default: 1.")
    ws_queue_worker.add_argument("--max-tasks", type=int, help="Stop after this many tasks have run.")
    ws_queue_worker.add_argument("--max-seconds", type=float, help="Stop after this many seconds.")
    ws_queue_worker.add_argument("--stop-file", help="Stop when this file exists. Default: queue/worker.stop.")
    ws_queue_worker.add_argument("--once", action="store_true", help="Run at most one pending task and exit.")

    ws_queue_migrate_sqlite = subparsers.add_parser("workspace-queue-migrate-sqlite", help="Migrate JSON workspace queue tasks into SQLite.")
    ws_queue_migrate_sqlite.add_argument("--root", required=True, help="Workspace root directory.")
    ws_queue_migrate_sqlite.add_argument("--no-backup", action="store_true", help="Do not create a tasks.json backup before migration.")
    ws_queue_migrate_sqlite.add_argument("--no-set-backend", action="store_true", help="Do not switch workspace queue backend to sqlite.")

    ws_queue_rollback_json = subparsers.add_parser("workspace-queue-rollback-json", help="Export SQLite queue tasks back to JSON tasks.json.")
    ws_queue_rollback_json.add_argument("--root", required=True, help="Workspace root directory.")
    ws_queue_rollback_json.add_argument("--no-backup", action="store_true", help="Do not backup the existing tasks.json before rollback.")
    ws_queue_rollback_json.add_argument("--no-set-backend", action="store_true", help="Do not switch workspace queue backend to json.")

    ws_record_browser = subparsers.add_parser("workspace-record-browser", help="Record a headed browser session into a workflow draft.")
    ws_record_browser.add_argument("--root", required=True, help="Workspace root directory.")
    ws_record_browser.add_argument("--url", required=True, help="Initial page URL to open for recording.")
    ws_record_browser.add_argument("--save-as", required=True, help="Workflow name or relative path under workflows/.")
    ws_record_browser.add_argument("--timeout-seconds", type=float, default=0.0, help="Optional automatic stop timeout. Default: wait until browser closes.")
    ws_record_browser.add_argument("--headless", action="store_true", help="Run headless for automation tests. Default is headed.")
    ws_record_browser.add_argument("--assert-text", help="Explicit success text to append as an assert_text step.")
    ws_record_browser.add_argument("--no-auto-assert", action="store_true", help="Do not infer an assert_text step from the final page.")
    ws_record_browser.add_argument("--save-auth-state", help="Append a confirmed save_storage_state step under .agent-auth/<name>.json.")
    ws_record_browser.add_argument("--no-check", action="store_true", help="Skip validation/preflight after saving the recorded workflow.")
    ws_record_browser.add_argument("--preview-run", action="store_true", help="Run the saved workflow once in dry-run mode after recording.")
    ws_record_browser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing recorded workflow file.")
    ws_record_browser.add_argument("--queue", action="store_true", help="Submit the recorded workflow to the workspace queue in dry-run mode.")
    ws_record_browser.add_argument("--queue-priority", type=int, default=0, help="Priority for --queue. Higher runs first.")
    ws_record_browser.add_argument("--queue-max-retries", type=int, default=0, help="Max retries for --queue.")
    ws_record_browser.add_argument("--format", choices=["json", "markdown"], default="json")

    ws_planner = subparsers.add_parser("workspace-planner-context", help="Show planner-safe workspace context.")
    ws_planner.add_argument("--root", required=True, help="Workspace root directory.")
    ws_planner.add_argument("--run-limit", type=int, default=5)

    ws_check_plan = subparsers.add_parser("workspace-check-plan", help="Validate a planner draft without executing it.")
    ws_check_plan.add_argument("--root", required=True, help="Workspace root directory.")
    ws_check_plan.add_argument("--file", required=True, help="Workflow draft YAML or JSON file.")
    ws_check_plan.add_argument("--allow-high-risk", action="store_true", help="Allow high-risk capabilities in the draft.")

    ws_generate_plan = subparsers.add_parser("workspace-planner-draft", help="Generate a workflow draft with the configured model, then check it without executing.")
    ws_generate_plan.add_argument("--root", required=True, help="Workspace root directory.")
    ws_generate_plan.add_argument("--instruction", required=True, help="Natural-language automation request.")
    ws_generate_plan.add_argument("--source", default=None, help="Model credential file.")
    ws_generate_plan.add_argument("--preferred", default=None, help="Preferred model provider. Defaults to VISUAL_AGENT_MODEL_PROVIDER or openai.")
    ws_generate_plan.add_argument("--model", default=None, help="Optional model override.")
    ws_generate_plan.add_argument("--run", action="store_true", help="Call the model. Without this flag, only build the prompt and API plan.")
    ws_generate_plan.add_argument("--save-as", default=None, help="Save a valid generated draft under workspace workflows/. Requires --run.")
    ws_generate_plan.add_argument("--preview-save", action="store_true", help="Show the save target and diff for --save-as without writing the file.")
    ws_generate_plan.add_argument("--overwrite", action="store_true", help="Allow --save-as to replace an existing workflow file.")
    ws_generate_plan.add_argument("--timeout-seconds", type=float, default=30.0)
    ws_generate_plan.add_argument("--max-completion-tokens", type=int, default=1200)
    ws_generate_plan.add_argument("--format", choices=["json", "markdown"], default="json")

    subparsers.add_parser("templates", help="List available workflow templates.")

    install_template_cmd = subparsers.add_parser("install-template", help="Install a template into a workspace.")
    install_template_cmd.add_argument("--root", required=True, help="Workspace root directory.")
    install_template_cmd.add_argument("--template", required=True, help="Template id.")
    install_template_cmd.add_argument("--overwrite", action="store_true")
    return parser


def _build_perception_status(
    manifest: Any,
    vlm_summary: dict[str, Any],
    playwright_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarise which perception providers are actually usable right now."""
    available_names = {
        str(getattr(c, "name", ""))
        for c in manifest.capabilities
        if getattr(c, "available", False)
    }
    dom_ok = "observe_browser" in available_names or "observe_dom" in available_names
    uia_ok = "observe_uia" in available_names
    ocr_ok = "observe_ocr" in available_names and ("screen_ocr" in available_names or "pytesseract" in available_names)
    vlm_ok = bool(vlm_summary.get("ok")) or (
        vlm_summary.get("cloud", {}).get("available") is True
        or any(
            v.get("available") for v in vlm_summary.get("local", {}).values()
            if isinstance(v, dict)
        )
    )

    warnings: list[str] = []
    if not dom_ok:
        detail = ""
        if isinstance(playwright_status, dict) and playwright_status.get("error"):
            detail = f" ({playwright_status.get('error')})"
        warnings.append(f"Browser/DOM provider unavailable{detail}. Run: {PLAYWRIGHT_INSTALL_HINT}")
    if not vlm_ok:
        warnings.append(
            "No VLM (visual fallback) is configured. "
            "Options: (A) set an API key in model_api_keys.txt for cloud VLM, "
            "(B) install torch/transformers and a local model for on-device VLM, "
            "(C) workflows that only use DOM/UIA work without VLM."
        )
    if not ocr_ok:
        warnings.append(
            "OCR provider unavailable (optional). "
            "Install screen-ocr[winrt] on Windows, or install pytesseract and the Tesseract binary."
        )

    return {
        "dom_browser": dom_ok,
        "windows_uia": uia_ok,
        "ocr": ocr_ok,
        "vlm": vlm_ok,
        "ready_for_dom_workflows": dom_ok,
        "ready_for_visual_workflows": dom_ok and vlm_ok,
        "warnings": warnings,
    }


def doctor_recommendations(missing: list[Any], *, strict: bool = False) -> list[dict[str, Any]]:
    priority_rank = {"P0": 0, "P1": 1, "P2": 2}
    recommendations = []
    for capability in missing:
        name = str(getattr(capability, "name", "") or "")
        dependency = str(getattr(capability, "dependency", "") or name)
        required = bool(getattr(capability, "required", False))
        install_hint = getattr(capability, "install_hint", None)
        priority = doctor_priority(name, dependency=dependency, required=required, strict=strict)
        recommendations.append(
            {
                "priority": priority,
                "code": f"missing_{name}",
                "name": name,
                "dependency": dependency or None,
                "required": required,
                "message": f"{priority}: {name} is unavailable.",
                "install_hint": install_hint,
            }
        )
    return sorted(recommendations, key=lambda item: (priority_rank.get(str(item.get("priority")), 99), str(item.get("name") or "")))


def doctor_priority(name: str, *, dependency: str = "", required: bool = False, strict: bool = False) -> str:
    key = (dependency or name).lower()
    if required or strict or key in {"playwright", "mcp"}:
        return "P0"
    if key in {"uiautomation", "pytesseract", "tesseract"}:
        return "P1"
    return "P2"


def main(argv: list[str] | None = None) -> int:
    system_invocation = argv is None
    prog = current_cli_name() if argv is None else DEFAULT_CLI_NAME
    welcome_cli = "pacer" if prog == "pacer" else "checkpoint"
    if argv is None:
        argv = sys.argv[1:]
    if system_invocation and prog == "pacer":
        from .pacer_management import handle_pacer_management

        managed = handle_pacer_management(argv)
        if managed is not None:
            return managed
        from .codex_launcher import launch_codex

        return launch_codex(argv)
    if not argv:
        print(build_welcome_message(welcome_cli))
        return 0
    if "--version" in argv:
        print(build_version_message(prog))
        return 0
    parser = build_parser(prog=prog)
    argv, simple_task = expand_natural_language_task_argv(argv, _subcommand_names(parser))
    if simple_task:
        from .simple_task import run_simple_managed_task, simple_result_to_markdown

        payload = run_simple_managed_task(argv[3])
        print(simple_result_to_markdown(payload))
        return 0 if str(payload.get("status") or "") == "completed" else 1
    args = parser.parse_args(argv)
    if args.command in RUNNER_COMMANDS:
        from .cli_runner import handle_runner_command

        return handle_runner_command(
            args,
            load_inputs_func=load_inputs,
            format_error=format_cli_error,
            parse_csv_set_func=parse_csv_set,
            run_progress_func=run_workflow_with_progress,
        )
    if args.command in RUNTIME_COMMANDS:
        from .cli_runtime import handle_runtime_command

        return handle_runtime_command(args)
    if args.command == "generate-report":
        report = build_run_history_report(args.workspace_root, limit=args.limit)
        report["ai_summary"] = build_run_history_ai_summary(report, provider=args.summary_provider, model=args.summary_model)
        output_path = write_run_history_report(
            args.workspace_root,
            args.output,
            limit=args.limit,
            summary_provider=args.summary_provider,
            summary_model=args.summary_model,
        )
        report["output_path"] = str(output_path.resolve())
        share_payload = build_run_history_share_payload(args.workspace_root, output_path, report=report)
        if args.open:
            webbrowser.open(output_path.resolve().as_uri())
        payload = {
            "schema_version": report.get("schema_version", 1),
            "workspace": report.get("workspace"),
            "generated_at": report.get("generated_at"),
            "output_path": str(output_path.resolve()),
            "summary": report.get("summary"),
            "ai_summary": report.get("ai_summary"),
            "recent_run_count": len(report.get("recent_runs") or []),
            "share": share_payload,
        }
        if args.format == "markdown":
            print(run_history_report_to_markdown(report))
        else:
            if args.share:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                payload.pop("share", None)
                print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "generate-fixture":
        workspace_root = Path(args.workspace_root).resolve()
        workspace_root.mkdir(parents=True, exist_ok=True)
        fixture_name = args.name or Path(str(args.page).strip("/")).name or "fixture"
        if fixture_name == "":
            fixture_name = "fixture"
        payload = fixture_template_payload(
            name=fixture_name,
            page=args.page,
            fixture_type=args.type,
            description=None,
        )
        text = render_fixture_template(payload)
        output_path = Path(args.output).expanduser()
        if args.output:
            output_path = output_path if output_path.is_absolute() else (workspace_root / output_path)
        else:
            output_path = workspace_root / "fixtures" / f"{fixture_name}.yaml"
        output_path = output_path.resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
        if args.format == "json":
            print(json.dumps({"path": str(output_path), "payload": payload}, ensure_ascii=False, indent=2))
        else:
            print(str(output_path))
        return 0
    if args.command == "capabilities":
        print(json.dumps(to_jsonable(build_capability_manifest()), ensure_ascii=False, indent=2))
        return 0
    if args.command == "atomic-capabilities":
        print(json.dumps(to_jsonable(build_atomic_capability_manifest()), ensure_ascii=False, indent=2))
        return 0
    if args.command == "doctor":
        manifest = build_capability_manifest()
        missing = [capability for capability in manifest.capabilities if not capability.available]
        blocking = missing if args.strict else [capability for capability in missing if capability.required]
        recommendations = doctor_recommendations(missing, strict=args.strict)
        vlm_summary = vlm_doctor_summary()
        playwright_status = playwright_runtime_status()
        perception = _build_perception_status(manifest, vlm_summary, playwright_status)
        payload = {
            "ok": not blocking,
            "available_count": manifest.available_count,
            "missing_count": manifest.missing_count,
            "blocking_missing_count": len(blocking),
            "perception": perception,
            "playwright": playwright_status,
            "missing": missing,
            "recommendations": recommendations,
            "vlm": {
                "local": {
                    "qwen2-vl": detect_vlm_backend("qwen2-vl"),
                    "moondream": detect_vlm_backend("moondream"),
                },
                "cloud": public_engine_status(detect_cloud_vision_backend()),
                "doctor_summary": vlm_summary,
            },
        }
        print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0 if not blocking else 1
    if args.command in QUALITY_COMMANDS:
        from .cli_quality import handle_quality_command

        return handle_quality_command(args, release_trial_runner=run_release_trial)
    if args.command == "install-ci-templates":
        result = install_ci_templates(
            args.root,
            workspace_root=args.workspace_root,
            overwrite=args.overwrite,
        )
        print(json.dumps(ci_template_install_to_dict(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "generate-ci":
        workflow = ci_workflow_template(
            workspace_root=args.workspace_root,
            python_version=args.python_version,
            node_version=args.node_version,
        )
        if args.output:
            output_path = Path(args.output).expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(workflow, encoding="utf-8")
        if args.format == "json":
            print(json.dumps({"schema_version": 1, "workflow": workflow}, ensure_ascii=False, indent=2))
        else:
            print(workflow.rstrip())
        return 0
    if args.command == "generate-integrations":
        try:
            result = install_integration_snippets(
                args.root,
                workspace_root=args.workspace_root,
                overwrite=args.overwrite,
            )
        except FileExistsError as exc:
            print(json.dumps({"schema_version": 1, "status": "blocked", "message": str(exc)}, ensure_ascii=False, indent=2))
            return 1
        if args.format == "markdown":
            print(
                "\n".join(
                    [
                        f"Cursor rules: {result.cursor_rules}",
                        f"Copilot instructions: {result.copilot_instructions}",
                        f"Windsurf rules: {result.windsurf_rules}",
                        f"JetBrains spec: {result.jetbrains_spec}",
                    ]
                )
            )
        else:
            print(json.dumps(integration_snippets_to_dict(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "export-to-playwright":
        result = export_workflow_to_playwright(
            args.workflow,
            output_path=args.output,
            spec_name=args.spec_name,
        )
        if args.format == "json":
            print(json.dumps(playwright_export_to_dict(result), ensure_ascii=False, indent=2))
        else:
            print(result.spec.rstrip())
        return 0
    if args.command == "github-pr-comment":
        event_path = args.event_path or os.environ.get("GITHUB_EVENT_PATH")
        try:
            repository = github_repository_from_env(args.repository)
        except ValueError as exc:
            print(json.dumps({"schema_version": 1, "status": "blocked", "message": str(exc)}, ensure_ascii=False, indent=2))
            return 1
        run_url = github_run_url_from_env(args.run_url)
        token = str(os.environ.get(args.token_env) or "").strip()
        if not token and not args.dry_run:
            print(json.dumps({"schema_version": 1, "status": "blocked", "message": f"Missing token env: {args.token_env}"}, ensure_ascii=False, indent=2))
            return 1
        number = github_event_pr_number(event_path)
        if number is None:
            print(json.dumps({"schema_version": 1, "status": "blocked", "message": "Unable to determine pull request number."}, ensure_ascii=False, indent=2))
            return 1
        payload = pr_failure_comment_result(
            report_root=args.report_root,
            quality_gate_root=args.quality_root,
            artifact_url=args.artifact_url,
            run_url=run_url,
            max_screenshots=args.max_screenshots,
        )
        payload["repository"] = repository
        payload["pull_request_number"] = number
        if args.dry_run:
            if args.format == "json":
                print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
            else:
                print(payload["body"].rstrip())
            return 0
        result = post_pr_comment(
            repository=repository,
            number=number,
            token=token,
            body=payload["body"],
        )
        payload["post_result"] = result
        if args.format == "json":
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        else:
            print(payload["body"].rstrip())
        return 0 if result.get("status") == "success" else 1
    if args.command in EXTERNAL_SAMPLE_COMMANDS:
        from .cli_external_samples import handle_external_sample_command

        return handle_external_sample_command(args)
    if args.command in {"init-workspace", "init"}:
        framework_hint = detect_framework_from_dir(Path(args.repo_root).resolve()) if args.auto_detect else None
        workspace = init_workspace(args.root, with_demo=not args.no_demo, overwrite=args.overwrite, framework_hint=framework_hint)
        payload = workspace_status(workspace)
        payload["next_steps"] = init_next_steps(workspace)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "generate-completions":
        paths = write_completion_scripts(args.output_dir)
        print(json.dumps({"schema_version": 1, "status": "success", "scripts": paths}, ensure_ascii=False, indent=2))
        return 0
    if args.command in WORKSPACE_READ_COMMANDS:
        from .cli_workspace import handle_workspace_read_command
        return handle_workspace_read_command(args)
    if args.command == "workspace-risk-policy-template":
        print(json.dumps(build_workspace_risk_policy_template(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-risk-policy-check":
        result = validate_workspace_risk_policy(open_workspace(args.root))
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    if args.command == "workspace-risk-policy-plan":
        result = build_workspace_risk_policy_apply_plan(
            open_workspace(args.root),
            overwrite=args.overwrite,
            apply=args.apply,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["validation_after"]["error_count"] == 0 else 1
    if args.command == "workspace-gui":
        return open_workspace_window(open_workspace(args.root), selected_run_id=args.run_id, limit=args.limit)
    if args.command == "workspace-gui-actions":
        report = build_gui_action_history_report(
            open_workspace(args.root),
            limit=args.limit,
            action=args.action,
            status=args.status,
        )
        if args.format == "markdown":
            print(gui_action_history_report_to_markdown(report))
        else:
            print(json.dumps(to_jsonable(report), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-gui-action-index":
        workspace = open_workspace(args.root)
        if args.risk:
            summary = build_gui_action_history_risk_summary(
                workspace,
                limit=args.limit,
                failed_action_limit=args.recent_error_limit,
                config=load_workspace_gui_action_history_risk_config(workspace),
            )
            if args.format == "markdown":
                print(gui_action_history_risk_to_markdown(summary))
            else:
                print(json.dumps(to_jsonable(summary), ensure_ascii=False, indent=2))
            return 0
        index = build_gui_action_history_index(workspace, limit=args.limit, recent_error_limit=args.recent_error_limit)
        if args.format == "markdown":
            print(gui_action_history_index_to_markdown(index))
        else:
            print(json.dumps(to_jsonable(index), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workbench-audit":
        return handle_workbench_audit_command(args)
    if args.command == "portfolio-dashboard":
        from .portfolio_dashboard import serve_portfolio_dashboard

        serve_portfolio_dashboard(
            project_roots=list(args.project or []),
            host=args.host,
            port=args.port,
            open_browser=not args.no_open,
        )
        return 0
    if args.command == "portfolio-worker":
        from .portfolio_worker import portfolio_mission_worker_to_markdown, run_portfolio_mission_worker

        payload = run_portfolio_mission_worker(
            project_roots=list(args.project or []),
            workspace_name=args.workspace_name,
            max_workers=args.max_workers,
            max_items_per_project=args.max_items_per_project,
            poll_seconds=args.poll_seconds,
            watch=args.watch,
            max_seconds=args.max_seconds,
        )
        if args.format == "json":
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        else:
            print(portfolio_mission_worker_to_markdown(payload))
        return 0 if str(payload.get("status") or "") in {"completed", "idle", "max_items_reached", "max_seconds_reached"} else 1
    if args.command in CLOUD_COMMANDS:
        from .cli_cloud import handle_cloud_command

        return handle_cloud_command(args)
    if args.command in REPAIR_COMMANDS:
        from .cli_repair import handle_repair_command

        return handle_repair_command(args)
    if args.command == "benchmarks":
        from .benchmarks import list_public_benchmarks

        payload = list_public_benchmarks(category=args.category)
        if args.format == "markdown":
            lines = ["# Checkpoint Public Benchmarks", ""]
            for item in payload["benchmarks"]:
                lines.append(f"- `{item['id']}`: {item['source']}")
            print("\n".join(lines))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0
    if args.command == "benchmark-plan":
        from .benchmarks import benchmark_plan_to_markdown, build_benchmark_plan

        payload = build_benchmark_plan(category=args.category, benchmark_id=args.benchmark_id)
        if args.format == "markdown":
            print(benchmark_plan_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0 if payload.get("status") == "ready" else 1
    if args.command == "benchmark-draft":
        from .benchmarks import benchmark_draft_to_markdown, build_benchmark_workflow_draft

        workspace_root = Path(args.workspace_root).resolve()
        output_path = (workspace_root / args.output).resolve() if args.output else None
        payload = build_benchmark_workflow_draft(
            scenario_id=args.scenario_id,
            workspace_root=workspace_root,
            output_path=output_path,
            dry_run=not args.save,
            overwrite=args.overwrite,
        )
        if args.format == "yaml" and payload.get("yaml"):
            print(payload["yaml"])
        elif args.format == "markdown":
            print(benchmark_draft_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0 if payload.get("status") == "success" else 1
    if args.command == "model-select":
        quota_snapshot = _load_optional_json(args.quota_file)
        selection = select_model_for_task(
            objective=args.goal,
            changed_files=args.changed_file,
            acceptance_criteria=args.acceptance,
            repeated_failure=args.repeated_failure,
            quota_snapshot=quota_snapshot,
            workspace_root=args.workspace_root,
            config_path=args.model_pool,
            credential_source=args.credential_source,
        )
        if args.format == "json":
            print(json.dumps(selection_to_dict(selection), ensure_ascii=False, indent=2))
        else:
            print(selection_to_markdown(selection))
        return 0 if selection.status == "selected" else 1
    if args.command == "permission-plan":
        from .security import permission_plan, permission_plan_to_markdown

        payload = permission_plan(list(args.assess_command or []), repo_root=args.repo_root)
        if args.format == "json":
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        else:
            print(permission_plan_to_markdown(payload))
        return 1 if payload.get("decision") == "deny" else 0
    if args.command == "hourly-plan":
        from .hourly_budget import build_hourly_plan, hourly_plan_to_markdown

        raw = _load_required_json(args.tasks_file)
        tasks = raw.get("tasks") if isinstance(raw, dict) else raw
        if not isinstance(tasks, list):
            print(json.dumps({"status": "blocked", "reason": "tasks_file_must_contain_list"}, ensure_ascii=False, indent=2))
            return 1
        payload = build_hourly_plan(
            tasks=tasks,
            quota_snapshot=_load_optional_json(args.quota_file),
            hours=args.hours,
            reserve_minutes=args.reserve_minutes,
        )
        if args.format == "json":
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        else:
            print(hourly_plan_to_markdown(payload))
        return 0
    if args.command == "notify":
        if args.template:
            print(json.dumps(notification_config_template(), ensure_ascii=False, indent=2))
            return 0
        payload = {}
        if args.payload_file:
            payload = _load_required_json(args.payload_file)
        elif args.payload:
            payload = json.loads(args.payload)
        notification = build_event_notification(args.event, payload if isinstance(payload, dict) else {"message": str(payload)})
        result = send_email_notification(notification, config_path=args.config, dry_run=not args.send)
        if args.format == "markdown":
            print(f"## Notification\n\nStatus: `{result.get('status')}`\n\nSubject: {result.get('subject') or notification.get('subject')}\n")
            if result.get("body"):
                print("```text")
                print(str(result["body"]).rstrip())
                print("```")
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("status") in {"planned", "sent", "skipped"} else 1
    if args.command == "slash":
        target = slash_command_to_argv(args.slash_command, list(args.slash_args or []))
        if not target:
            print(json.dumps({"status": "error", "reason": "unknown_slash_command", "command": args.slash_command}, ensure_ascii=False, indent=2))
            return 2
        return main(target)
    if args.command == "quickstart":
        print(build_welcome_message(welcome_cli))
        return 0
    if args.command in CHIEF_COMMANDS:
        from .cli_chief import handle_chief_command
        return handle_chief_command(args)
    if args.command in VERIFICATION_COMMANDS:
        from .cli_verification import handle_verification_command

        return handle_verification_command(args, codex_runner=run_codex_check)
    if args.command in WORKFLOW_COMMANDS:
        from .cli_workflow import handle_workflow_command

        return handle_workflow_command(
            args,
            format_error=format_cli_error,
            cli_error_suggestion=cli_error_suggestion,
        )
    if args.command in WORKSPACE_RUN_COMMANDS:
        from .cli_workspace import handle_workspace_run_command
        return handle_workspace_run_command(args)
    if args.command in WORKSPACE_MANAGE_COMMANDS:
        from .cli_workspace import handle_workspace_manage_command
        return handle_workspace_manage_command(args)
    if args.command in WORKSPACE_QUEUE_COMMANDS:
        from .cli_workspace import handle_workspace_queue_command
        return handle_workspace_queue_command(args)
    if args.command in WORKSPACE_RECORD_COMMANDS:
        from .cli_workspace import handle_workspace_record_command
        return handle_workspace_record_command(args, recorder=record_browser_session)
    return 2


def init_next_steps(workspace: Any) -> list[str]:
    root = quote_cli_arg(str(workspace.root))
    return [
        f"visual-agent show-status --workspace-root {root}",
        f"visual-agent verify-impl --workspace-root {root} --task-description \"Verify the current change\" --run-profile dry-run",
        f"visual-agent workspace-status --root {root}",
    ]


def quote_cli_arg(value: str) -> str:
    text = str(value)
    if not text or any(char.isspace() for char in text):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def slash_command_to_argv(name: str, args: list[str]) -> list[str]:
    normalized = str(name or "").strip().lstrip("/").lower()
    mapping = {
        "plan": "chief-plan",
        "memory": "chief-memory",
        "test": "test-plan",
        "risk": "permission-plan",
        "permission": "permission-plan",
        "verify": "verify-now",
        "status": "agent-status",
    }
    target = mapping.get(normalized)
    return [target, *args] if target else []


def load_inputs(raw_inputs: str | None, inputs_file: str | None) -> dict:
    if raw_inputs and inputs_file:
        raise ValueError("Use either --inputs or --inputs-file, not both.")
    if inputs_file:
        return json.loads(Path(inputs_file).read_text(encoding="utf-8-sig"))
    if raw_inputs:
        return json.loads(raw_inputs)
    return {}


def _load_required_json(path: str | Path) -> Any:
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8-sig"))


def _load_optional_json(path: str | Path | None) -> Any:
    if not path:
        return None
    return _load_required_json(path)


def parse_csv_set(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def parse_optional_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.lower() == "true"


def format_cli_error(exc: Exception | str, *, command: str = "") -> str:
    message = str(exc).strip()
    suggestion = cli_error_suggestion(message, command=command)
    if suggestion:
        return f"Error: {message}\nTry: {suggestion}"
    return f"Error: {message}"


def cli_error_suggestion(message: str, *, command: str = "") -> str:
    text = message.lower()
    if "workspace does not exist" in text:
        return "visual-agent init --root .agent-workspace"
    if "workflow not found" in text:
        return "visual-agent workspace-status --root .agent-workspace, then pass one listed workflow name."
    if "no such file or directory" in text or "filenotfounderror" in text or "not found" in text:
        if "workspace" in text:
            return "Run visual-agent init --root .agent-workspace, then retry with --workspace-root .agent-workspace."
        if "workflow" in text:
            return "Run visual-agent workspace-status --root .agent-workspace and choose an existing workflow."
        if "inputs" in text:
            return "Check the --inputs-file path, or place the file under .agent-workspace/inputs."
        return "Check the path from the project root, or rerun with an absolute path."
    if "could not infer --base-url" in text:
        return "Pass --base-url http://127.0.0.1:5173 for a running app, or --base-url fixtures/login_demo.html for a local fixture."
    if "generate-workflow requires --description" in text:
        return "visual-agent generate-workflow --description \"Verify login redirects to dashboard.\""
    if "workflow-lint requires a workflow path" in text:
        return "visual-agent workflow-lint --file workflows/login_flow.yaml"
    if "use either --inputs or --inputs-file" in text:
        return "Pass only one input source, then rerun the command."
    if "json" in text and "inputs-file" in text:
        return f"Check {command} --inputs-file for valid JSON, then retry."
    if "run-workflow requires --workflow" in text:
        return "visual-agent run-workflow --workflow workflows/login_flow.yaml --inputs-file inputs/demo.json"
    if command == "run-workflow":
        return "visual-agent run-workflow --workflow workflows/login_flow.yaml --inputs-file inputs/demo.json"
    if command == "generate-workflow":
        return "visual-agent generate-workflow --description \"Describe the workflow you want.\""
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
