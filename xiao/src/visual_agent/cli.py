from __future__ import annotations

import argparse
import json
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
        argv = argv_with_autocompact(argv, autocompact_setting())
    if rotation_enabled() and is_interactive_launch(argv):
        return run_claude_with_rotation(executable, argv)
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
    executable = shutil.which("gemini") or shutil.which("gemini.cmd") or shutil.which("gemini.exe")
    if not executable:
        print("Pacer requires Gemini CLI on PATH. Install it with: npm install -g @google/gemini-cli")
        return 127
    from .subprocess_window import prepare_subprocess_command

    completed = subprocess.run(prepare_subprocess_command([executable, *argv]))
    return int(completed.returncode)


def run_pacer_embed(argv: list[str] | None = None) -> int:
    """Embed Pacer into Claude Code, Codex CLI, and VS Code in the target workspace."""
    parser = argparse.ArgumentParser(
        prog="pacer embed",
        description="Embed Pacer into Claude Code (/pacer slash command + MCP server) and VS Code tasks.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Target workspace directory to embed Pacer into (default: current directory).",
    )
    parser.add_argument(
        "--target",
        dest="target_opt",
        default=None,
        help="Target workspace directory (override positional).",
    )
    args = parser.parse_args(argv or [])

    target_arg = args.target_opt or args.target or "."
    target_root = Path(target_arg).expanduser().resolve()
    pacer_repo_src = Path(__file__).resolve().parents[1]

    # Use portable command & paths when configuring inside the Pacer repo
    is_same_repo = False
    try:
        pacer_repo_root = pacer_repo_src.parent
        if target_root == pacer_repo_root or target_root.samefile(pacer_repo_root):
            is_same_repo = True
    except Exception:
        pass

    if is_same_repo:
        portable_py = "python"
        mcp_pythonpath = "src"
        vscode_pythonpath = "${workspaceFolder}/src"
        slash_cmd_run = 'PYTHONPATH="src" python -m visual_agent.cli auto "$ARGUMENTS"'
    else:
        try:
            rel_src = pacer_repo_src.relative_to(target_root).as_posix()
            mcp_pythonpath = rel_src
            vscode_pythonpath = f"${{workspaceFolder}}/{rel_src}"
            slash_cmd_run = f'PYTHONPATH="{rel_src}" python -m visual_agent.cli auto "$ARGUMENTS"'
        except ValueError:
            src_posix = pacer_repo_src.as_posix()
            py_exe = Path(sys.executable).resolve().as_posix()
            mcp_pythonpath = str(pacer_repo_src)
            vscode_pythonpath = str(pacer_repo_src)
            slash_cmd_run = f'PYTHONPATH="{src_posix}" "{py_exe}" -m visual_agent.cli auto "$ARGUMENTS"'
        portable_py = "python"

    written_files: list[str] = []

    # 1. Claude Code Slash Command: `.claude/commands/pacer.md` (project-scoped)
    # and `~/.claude/commands/pacer.md` (user-wide global)
    def _render_slash_content(run_cmd: str) -> str:
        return (
            "---\n"
            "description: 使用 Pacer 极速无状态微代理（0ms AST 索引 + 免费 Flash 差量补丁 + 自动跑测试）执行编码任务，不消耗主窗口上下文\n"
            "argument-hint: <要实现或修复的具体任务描述>\n"
            "---\n\n"
            "请勿在当前对话窗口中逐个读取大文件或手动编写长代码（以防撑爆上下文或耗时过长）。\n"
            "请直接调用内置的 `mcp__pacer__pacer_auto` 工具（如果已挂载 MCP），或者立刻用 Bash 执行以下单行命令将任务交给 Pacer 极速闭环：\n\n"
            "```bash\n"
            f"{run_cmd}\n"
            "```\n\n"
            "执行完毕后，只需向我简明汇报修改的文件路径和验证结果即可。\n"
        )

    # Project command can use relative PYTHONPATH when inside the workspace
    project_cmd_dir = target_root / ".claude" / "commands"
    try:
        project_cmd_dir.mkdir(parents=True, exist_ok=True)
        project_cmd_file = project_cmd_dir / "pacer.md"
        project_cmd_file.write_text(_render_slash_content(slash_cmd_run), encoding="utf-8")
        written_files.append(str(project_cmd_file))
    except OSError:
        pass

    # Global user command must ALWAYS use a stable absolute path so it runs from any workspace
    global_cmd_dir = Path.home() / ".claude" / "commands"
    try:
        global_cmd_dir.mkdir(parents=True, exist_ok=True)
        global_cmd_file = global_cmd_dir / "pacer.md"
        # Preserve existing non-Pacer user slash command by creating a backup
        if global_cmd_file.is_file():
            existing_content = global_cmd_file.read_text(encoding="utf-8", errors="ignore")
            if "pacer" not in existing_content.lower():
                backup_path = global_cmd_file.with_suffix(".md.bak")
                backup_path.write_text(existing_content, encoding="utf-8")
        stable_src_posix = pacer_repo_src.as_posix()
        stable_py_exe = Path(sys.executable).resolve().as_posix()
        global_slash_run = f'PYTHONPATH="{stable_src_posix}" "{stable_py_exe}" -m visual_agent.cli auto "$ARGUMENTS"'
        global_cmd_file.write_text(_render_slash_content(global_slash_run), encoding="utf-8")
        written_files.append(str(global_cmd_file))
    except OSError:
        pass

    # 2. Project `.mcp.json` for Claude Code / Cursor / VS Code MCP discovery
    mcp_json_path = target_root / ".mcp.json"
    mcp_config: dict = {"mcpServers": {}}
    if mcp_json_path.is_file():
        try:
            loaded = json.loads(mcp_json_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                mcp_config = loaded
                mcp_config.setdefault("mcpServers", {})
        except Exception:
            pass

    mcp_config["mcpServers"]["pacer"] = {
        "command": portable_py,
        "args": ["-m", "visual_agent.mcp_server"],
        "env": {
            "PYTHONPATH": mcp_pythonpath,
        },
    }
    mcp_json_path.write_text(json.dumps(mcp_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    written_files.append(str(mcp_json_path))

    # 3. VS Code `.vscode/tasks.json` shortcut so pressing Ctrl+Shift+B / Run Task triggers Pacer Auto
    vscode_tasks_path = target_root / ".vscode" / "tasks.json"
    existing_tasks_doc: dict[str, Any] = {
        "version": "2.0.0",
        "tasks": [],
        "inputs": [],
    }
    if vscode_tasks_path.is_file():
        try:
            loaded_tasks = json.loads(vscode_tasks_path.read_text(encoding="utf-8"))
            if isinstance(loaded_tasks, dict):
                existing_tasks_doc = loaded_tasks
                existing_tasks_doc.setdefault("version", "2.0.0")
                if not isinstance(existing_tasks_doc.get("tasks"), list):
                    existing_tasks_doc["tasks"] = []
                if not isinstance(existing_tasks_doc.get("inputs"), list):
                    existing_tasks_doc["inputs"] = []
        except Exception:
            pass

    pacer_tasks = [
        {
            "label": "Pacer Auto: 极速无状态编码",
            "type": "shell",
            "command": portable_py,
            "args": ["-m", "visual_agent.cli", "auto", "${input:pacerGoal}"],
            "options": {"env": {"PYTHONPATH": vscode_pythonpath}},
            "presentation": {"reveal": "always", "panel": "dedicated"},
            "problemMatcher": [],
        },
        {
            "label": "Pacer Verify: 工程防伪验收",
            "type": "shell",
            "command": portable_py,
            "args": ["-m", "visual_agent.cli", "verify"],
            "options": {"env": {"PYTHONPATH": vscode_pythonpath}},
            "presentation": {"reveal": "always", "panel": "shared"},
            "problemMatcher": [],
        },
    ]

    pacer_labels = {t["label"] for t in pacer_tasks}
    merged_tasks = [
        t for t in existing_tasks_doc["tasks"]
        if isinstance(t, dict) and t.get("label") not in pacer_labels
    ]
    merged_tasks.extend(pacer_tasks)
    existing_tasks_doc["tasks"] = merged_tasks

    pacer_input = {
        "id": "pacerGoal",
        "type": "promptString",
        "description": "输入要交给 Pacer 极速完成的任务描述",
    }
    existing_input_ids = {
        inp.get("id")
        for inp in existing_tasks_doc["inputs"]
        if isinstance(inp, dict)
    }
    if "pacerGoal" not in existing_input_ids:
        existing_tasks_doc["inputs"].append(pacer_input)

    try:
        vscode_tasks_path.parent.mkdir(parents=True, exist_ok=True)
        vscode_tasks_path.write_text(
            json.dumps(existing_tasks_doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written_files.append(str(vscode_tasks_path))
    except OSError:
        pass

    print("═══════════════════════════════════════════════════════════════")
    print(" [✓] Pacer 已成功嵌入你的 Code 环境！")
    print("═══════════════════════════════════════════════════════════════")
    for wf in written_files:
        print(f"  + 已生成配置: {wf}")
    print("\n现在你可以直接用最习惯的 3 种方式在 Code 里调用 Pacer：")
    print("  1. 在 Claude Code 聊天框直接输入斜杠命令：")
    print("     /pacer 给 examples/game_2048/core.py 增加 get_max_tile 方法")
    print("  2. 在 Claude Code / Codex 中自然对话（已自动挂载 pacer_auto 与 pacer_index MCP 工具）：")
    print("     直接说：“用 pacer_auto 帮我实现 xxx”")
    print("  3. 在 VS Code 编辑器中按 Ctrl+Shift+P -> Run Task -> 选择 [Pacer Auto: 极速无状态编码]")
    print("═══════════════════════════════════════════════════════════════")
    return 0


def run_pacer_backends(argv: list[str] | None = None) -> int:
    from .gemini_bridge import find_gemini_executable, is_gemini_available
    from .model_credentials import inspect_model_credentials
    from .workbuddy_bridge import WorkBuddyGatewayClient

    print("═══════════════════════════════════════════════════════════════")
    print(" Pacer 本机 AI 引擎聚合与连接状态")
    print("═══════════════════════════════════════════════════════════════")

    gm_ok = is_gemini_available()
    gm_bin = find_gemini_executable() or "未检测到"
    if gm_ok:
        print(f"  [✓] Gemini Flash CLI     : 就绪 ({gm_bin})")
        print("      用途: 免费高并发、单轮无状态 SEARCH/REPLACE 极速补丁生成")
    else:
        print("  [✗] Gemini CLI           : 未就绪 (可在终端执行 npm install -g @google/gemini-cli)")

    wb_client = WorkBuddyGatewayClient()
    health = wb_client.check_health()
    if health.get("available"):
        port = health.get("port")
        pid = wb_client.info.pid if wb_client.info else "?"
        sessions = wb_client.list_sessions()
        print(f"  [✓] WorkBuddy 桌面网关    : 就绪 (http://127.0.0.1:{port}, PID {pid}, 活跃会话 {len(sessions)})")
        print("      用途: 调度国内主力模型 (DeepSeek V3, Kimi K2.5, GLM 5.1) 进行常规代码编写")
    else:
        print("  [✗] WorkBuddy 桌面网关    : 未连接 (请启动 WorkBuddy 桌面客户端)")

    codex_bin = shutil.which("codex") or shutil.which("codex.cmd") or shutil.which("codex.exe")
    if codex_bin:
        print(f"  [✓] Codex Plus CLI       : 就绪 ({codex_bin})")
        print("      用途: 核心算法与高难架构 (严格受控调用，保护 5 小时限额)")
    else:
        print("  [✗] Codex CLI            : 未检测到")

    try:
        from .grok_bridge import get_grok_api_base, is_grok_available
        if is_grok_available():
            print(f"  [✓] xAI Grok (Sub2API)   : 就绪 ({get_grok_api_base()})")
            print("      用途: 极速大模型推理与补丁生成 (5-6s 流式返回)")
        else:
            print("  [-] xAI Grok (Sub2API)   : 未配置 PACER_GROK_API_KEY")
    except Exception:
        pass

    try:
        cred = inspect_model_credentials()
        providers = [p["provider"] for p in cred.get("providers", [])]
        if providers:
            print(f"  [✓] 本地 API 模型配置     : 已配置 ({', '.join(providers)})")
        else:
            print("  [-] 本地 API 模型配置     : 未配置特定 API Key (可选)")
    except Exception:
        pass

    print("═══════════════════════════════════════════════════════════════")
    return 0


def _display_subagent_plan(plan: Any) -> None:
    print("───────────────────────────────────────────────────────────────")
    print(f"目标: {plan.goal}")
    print(f"生成子任务数: {len(plan.tasks)}")
    print("───────────────────────────────────────────────────────────────")
    for i, t in enumerate(plan.tasks, 1):
        deps = f"依赖: {t.dependencies}" if t.dependencies else "无依赖（可并行）"
        files = f"涉及文件: {', '.join(t.target_files)}" if t.target_files else ""
        verif = f"验证命令: {t.verification_command}" if t.verification_command else ""
        print(f"  [{i}] {t.id} | {t.role.upper()} | 后端: [{t.backend.upper()}] — {t.title}")
        print(f"      说明: {t.instructions[:100]}{'...' if len(t.instructions) > 100 else ''}")
        if deps:
            print(f"      {deps}")
        if files:
            print(f"      {files}")
        if verif:
            print(f"      {verif}")
        print()
    print("───────────────────────────────────────────────────────────────")


def run_pacer_plan(argv: list[str] | None = None) -> int:
    from .subagent_orchestrator import generate_subagent_plan
    from .workbuddy_bridge import WorkBuddyGatewayClient

    goal = " ".join(argv or []).strip()
    if not goal:
        try:
            goal = input("请输入要拆解的目标或任务描述> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
    if not goal:
        print("目标为空，取消规划。")
        return 1

    print(f"\n[Pacer Plan] 正在快速生成子代理 DAG: {goal}\n")
    wb_ok = WorkBuddyGatewayClient().is_available
    plan = generate_subagent_plan(goal, workbuddy_available=wb_ok, fast_local_plan=True)

    _display_subagent_plan(plan)
    print('提示: 执行请运行 `pacer auto "<目标>"`')
    return 0


def run_pacer_auto(argv: list[str] | None = None) -> int:
    from .subagent_orchestrator import SubagentOrchestrator, generate_subagent_plan
    from .workbuddy_bridge import WorkBuddyGatewayClient

    argv_list = list(argv or [])
    dry_run = "--dry-run" in argv_list
    if dry_run:
        argv_list.remove("--dry-run")

    use_llm_plan = "--llm-plan" in argv_list
    if use_llm_plan:
        argv_list.remove("--llm-plan")

    verify_cmd = ""
    workspace_root = ""
    cleaned_argv: list[str] = []
    i = 0
    while i < len(argv_list):
        arg = argv_list[i]
        if arg == "--verify-cmd":
            if i + 1 < len(argv_list):
                verify_cmd = argv_list[i + 1]
                i += 2
                continue
        elif arg.startswith("--verify-cmd="):
            verify_cmd = arg[len("--verify-cmd=") :]
            i += 1
            continue
        elif arg in ("--workspace-root", "-w"):
            if i + 1 < len(argv_list):
                workspace_root = argv_list[i + 1]
                i += 2
                continue
        elif arg.startswith("--workspace-root="):
            workspace_root = arg[len("--workspace-root=") :]
            i += 1
            continue
        cleaned_argv.append(arg)
        i += 1
    argv_list = cleaned_argv

    goal = " ".join(argv_list).strip()
    if not goal:
        try:
            goal = input("请输入自主执行目标> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
    if not goal:
        print("目标为空，取消执行。")
        return 1

    ws_path = Path(workspace_root).expanduser().resolve() if workspace_root else Path.cwd()
    print(f"\n[Pacer Auto] 目标工作区: {ws_path}")
    print(f"[Pacer Auto] 正在自动规划子代理 DAG: {goal}")
    wb_ok = WorkBuddyGatewayClient().is_available
    plan = generate_subagent_plan(
        goal,
        repo_root=ws_path,
        workbuddy_available=wb_ok,
        fast_local_plan=(not use_llm_plan),
    )

    if verify_cmd:
        for t in plan.tasks:
            if t.role in ("coder", "verifier"):
                t.verification_command = verify_cmd

    if dry_run:
        print("[Pacer Auto] --dry-run 模式：仅展示拆解规划")
        _display_subagent_plan(plan)
        print('提示: 执行请运行 `pacer auto "<目标>"` (去掉 --dry-run)')
        return 0

    print(f"[Pacer Auto] 成功生成 {len(plan.tasks)} 个子任务，开始多模型调度与自愈闭环执行...\n")
    orchestrator = SubagentOrchestrator(plan, workspace_root=ws_path)
    summary = orchestrator.run()

    print("\n═══════════════════════════════════════════════════════════════")
    if summary["success"]:
        print(f" [✓] 自动化编排全部成功完成！耗时: {summary['elapsed_seconds']:.1f}s")
    else:
        print(f" [✗] 部分任务未完成或失败。耗时: {summary['elapsed_seconds']:.1f}s")
    print(f" 完成任务: {summary['completed_tasks']}/{summary['total_tasks']}")
    print("═══════════════════════════════════════════════════════════════")
    return 0 if summary["success"] else 1


def run_pacer_verify(argv: list[str] | None = None) -> int:
    """Wire up the verification suite: tamper guard, base-diff discrimination, delivery check, and evidence pack."""
    from .acceptance_discrimination import classify_acceptance, probe_base_command
    from .command_verification import (
        changed_acceptance_chain_files,
        changed_test_files,
        run_command_verification,
    )
    from .delivery_check import delivery_markdown_lines, run_delivery_check
    from .diff_summary import build_diff_summary, format_diff_summary
    from .evidence_pack import build_evidence_pack, evidence_markdown_lines
    from .pacer_voice import user_markdown_section, user_story
    from .verification_profiles import choose_verification_command

    parser = argparse.ArgumentParser(prog="pacer verify", description="Run Pacer's full delivery & acceptance gate.")
    parser.add_argument("--cmd", default="", help="Verification command (auto-detected if omitted).")
    parser.add_argument("--base", default="HEAD", help="Git base ref for discrimination & tamper checks.")
    parser.add_argument("--branch", default="", help="Git branch to run clean-checkout delivery check on.")
    parser.add_argument("--allow-test-edits", action="store_true", help="Allow test file modifications.")
    parser.add_argument("--skip-base-probe", action="store_true", help="Skip base-commit discrimination probe.")
    parser.add_argument("--goal", default="", help="Task objective description.")
    args = parser.parse_args(argv or [])

    repo_root = Path.cwd().resolve()
    cmd = args.cmd.strip() or choose_verification_command(repo_root)
    if not cmd:
        print("错误: 未指定 --cmd 且未能自动探测到项目的测试命令。")
        return 1

    if not args.allow_test_edits:
        tampered = changed_test_files(repo_root=repo_root, base_ref=args.base)
        if tampered:
            story = user_story(stop_reason="evidence_rejected", goal=args.goal, verification_command=cmd)
            print("\n".join(user_markdown_section(story)))
            print(f"\n被修改的测试文件: {', '.join(tampered)}")
            return 1
        chain_tampered = changed_acceptance_chain_files(repo_root=repo_root, command=cmd, base_ref=args.base)
        if chain_tampered:
            print(f"错误: 验收配置链文件被修改 ({', '.join(chain_tampered)})，拒绝执行验收。")
            return 1

    cmd_res = run_command_verification(command=cmd, repo_root=repo_root)
    verdict_ok = cmd_res.get("verdict") == "pass"

    base_probe = None
    if not args.skip_base_probe:
        base_probe = probe_base_command(command=cmd, repo_root=repo_root, base_ref=args.base)
    disc = classify_acceptance(command_result=cmd_res, base_probe=base_probe)

    delivery_res = None
    if args.branch.strip():
        delivery_res = run_delivery_check(repo_root=repo_root, branch=args.branch.strip(), command=cmd)

    diff_sum = build_diff_summary(repo_root=repo_root, base_ref=args.base)
    pack = build_evidence_pack(
        objective=args.goal,
        command=cmd,
        output=str(cmd_res.get("raw_output_tail") or cmd_res.get("output_tail") or ""),
        exit_code=cmd_res.get("exit_code"),
        delivery_status=str((delivery_res or {}).get("status") or ""),
    )

    stop_reason = "verified" if verdict_ok else (str(cmd_res.get("failure_kind") or "") or "verification_failed")
    story = user_story(
        stop_reason=stop_reason,
        goal=args.goal,
        product_change_count=diff_sum.get("file_count"),
        verification_command=cmd,
        acceptance_tier=str(disc.get("tier") or ""),
        delivery_status=str((delivery_res or {}).get("status") or ""),
    )

    lines: list[str] = []
    lines.extend(user_markdown_section(story))
    lines.append("")
    lines.append(f"**区分度评级**: `{disc.get('tier')}` — {disc.get('message')}")
    lines.extend(evidence_markdown_lines(pack))
    if delivery_res:
        lines.extend(delivery_markdown_lines(delivery_res))
    if diff_sum.get("file_count"):
        lines.extend(["", format_diff_summary(diff_sum)])

    print("\n".join(lines))
    if not verdict_ok:
        return 1
    if delivery_res and delivery_res.get("status") == "not_delivered":
        return 1
    return 0


def run_pacer_wb(argv: list[str] | None = None) -> int:
    from .workbuddy_bridge import WorkBuddyGatewayClient, invoke_workbuddy

    client = WorkBuddyGatewayClient()
    if not client.is_available:
        print("错误: 未检测到 WorkBuddy 桌面端。请启动 WorkBuddy 客户端后再试。")
        return 1

    prompt = " ".join(argv or []).strip()
    if not prompt:
        try:
            prompt = input("workbuddy> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
    if not prompt:
        return 0

    print("[WorkBuddy] 正在向本地网关提交任务...")
    res = invoke_workbuddy(prompt, cwd=Path.cwd())
    if res.success:
        print("\n" + res.response + "\n")
        return 0
    else:
        print(f"[WorkBuddy] 执行失败: {res.error}")
        return 1


def run_pacer_grok(argv: list[str] | None = None) -> int:
    from .grok_bridge import get_grok_api_base, invoke_grok, is_grok_available

    if not is_grok_available():
        print("错误: 未配置 xAI Grok API Key (可通过环境变量 PACER_GROK_API_KEY 配置)。")
        return 1

    prompt = " ".join(argv or []).strip()
    if not prompt:
        try:
            prompt = input("grok> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
    if not prompt:
        return 0

    print(f"[xAI Grok ({get_grok_api_base()})] 正在流式推理...")
    res = invoke_grok(prompt)
    if res.success:
        print("\n" + res.response + "\n")
        print(f"[耗时: {res.elapsed_seconds:.2f}s]")
        return 0
    else:
        print(f"[xAI Grok] 请求失败: {res.error}")
        return 1


def run_pacer_trace(argv: list[str] | None = None) -> int:
    """Inspect persistent execution traces across windows, sessions, and Claude Code conversations."""
    from .pacer_trace import read_pacer_traces

    parser = argparse.ArgumentParser(
        prog="pacer trace",
        description="Inspect persistent cross-window execution audit logs for Pacer.",
    )
    parser.add_argument("--limit", "-n", type=int, default=10, help="Number of traces to show (default: 10).")
    parser.add_argument("--global", dest="global_only", action="store_true", help="Only show user-global traces.")
    parser.add_argument("--json", action="store_true", help="Output raw JSON array.")
    args = parser.parse_args(argv or [])

    traces = read_pacer_traces(limit=args.limit, global_only=args.global_only)
    if args.json:
        print(json.dumps(traces, ensure_ascii=False, indent=2))
        return 0

    if not traces:
        print("未检测到任何 Pacer 留痕记录。")
        return 0

    print("═══════════════════════════════════════════════════════════════")
    print(f" Pacer 执行审计留痕 (最近 {len(traces)} 条)")
    print("═══════════════════════════════════════════════════════════════")
    for t in traces:
        status_icon = "✓" if t.get("status") == "success" else "✗"
        print(f"[{status_icon}] {t.get('trace_id')} | {t.get('timestamp')} | 来源: {t.get('session_source')}")
        print(f"    目标: {t.get('goal')}")
        print(f"    模型: 首选 [{t.get('primary_backend')}] -> 实际 [{t.get('effective_backend')}] | 耗时: {t.get('elapsed_seconds')}s")
        if t.get("failover_path"):
            print(f"    容错轮转: {' -> '.join(t['failover_path'])}")
        if t.get("patched_files"):
            print(f"    改动文件: {', '.join(t['patched_files'])}")
        if t.get("verification_command"):
            print(f"    验收命令: {t['verification_command']} [{t.get('verification_verdict')}]")
        if t.get("error"):
            print(f"    错误原因: {t['error']}")
        print()
    print("═══════════════════════════════════════════════════════════════")
    return 0


def run_pacer_dashboard(argv: list[str] | None = None) -> int:
    """Launch the Pacer modern web dashboard & control center."""
    from .dashboard import launch_dashboard
    parser = argparse.ArgumentParser(
        prog="pacer dashboard",
        description="Launch modern Web Dashboard for Pacer (Token & time metrics, auto-sniffer, 5 core pillars).",
    )
    parser.add_argument("--port", "-p", type=int, default=8765, help="Port to listen on (default: 8765).")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open browser.")
    args = parser.parse_args(argv or [])

    launch_dashboard(port=args.port, open_browser=not args.no_browser)
    return 0


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


def build_welcome_message(cli_name: str = "pacer") -> str:
    """Pacer — Multi-Model AI Aggregator & Automated Subagent Orchestration Engine."""
    return "\n".join(
        [
            "Pacer (DevPacer) — 多模型聚合与自动生成子代理编排系统",
            "",
            "自动编排与工程验收：",
            "  pacer dashboard        # 启动现代化中英双语控制面板（额度时间节省看板 + AI嗅探 + 五大核心）",
            "  pacer embed [目录]     # 一键将 Pacer 嵌入 Claude Code (/pacer指令 + MCP) 与 VS Code",
            "  pacer auto <目标>      # 0ms 本地拆解 + 无状态画布差量补丁 + 自愈测试回路",
            "  pacer plan <目标>      # 预览任务的子代理规划及模型分流（不执行）",
            "  pacer verify [选项]    # 执行完整工程验收（防篡改、Base区分度探针、净室交付检查）",
            "  pacer trace [选项]     # 跨窗口、跨会话查看 Pacer 持久化审计留痕记录",
            "  pacer backends         # 查看本机所有 AI 引擎连接状态（Gemini、WorkBuddy、Codex 等）",
            "",
            "模型单独调用 / 原生窗口：",
            "  pacer grok [内容]      # xAI Grok (Sub2API 极速流式推理，代码补丁/问题排查)",
            "  pacer gemini [内容]    # 免费超快 Gemini Flash（百万上下文，分析/索引/快速推断）",
            "  pacer wb [内容]        # 桌面端 WorkBuddy (DeepSeek V3 / Kimi / GLM 本地网关调用)",
            "  pacer codex            # 将终端交给 Codex Plus（5h 额度保护，专攻高难度架构/算法）",
            "  pacer claude           # Claude Code 原生窗口",
            "  pacer yolo             # Claude Code 全权限模式",
            "  pacer task             # 任务交接窗口轮转",
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

    embed_p = subparsers.add_parser("embed", help="Embed Pacer into Claude Code (/pacer slash command + MCP) and VS Code.")
    embed_p.add_argument("target", nargs="?", default=".", help="Target workspace directory")
    embed_p.add_argument("--target", dest="target_opt", default=None, help="Target workspace directory (flag form)")

    auto_p = subparsers.add_parser("auto", help="Automated subagent decomposition, multi-model routing and execution.")
    auto_p.add_argument("goal", nargs="*", help="Goal or task description")
    auto_p.add_argument("--verify-cmd", default="", help="Command to run for verification")
    auto_p.add_argument("--dry-run", action="store_true", help="Generate and display the plan without executing")
    auto_p.add_argument("--llm-plan", action="store_true", help="Use LLM planner instead of 0ms local rule planner")
    auto_p.add_argument("--workspace-root", "-w", default="", help="Workspace root directory for execution")

    plan_p = subparsers.add_parser("plan", help="Preview subagent DAG breakdown without execution.")
    plan_p.add_argument("goal", nargs="*", help="Goal or task description")

    verify_p = subparsers.add_parser("verify", help="Run Base-Diff acceptance discrimination, tamper guard, and delivery checks.")
    verify_p.add_argument("--cmd", default="", help="Verification command")
    verify_p.add_argument("--base", default="HEAD", help="Git base ref")
    verify_p.add_argument("--branch", default="", help="Git branch for delivery check")
    verify_p.add_argument("--allow-test-edits", action="store_true", help="Allow test edits")
    verify_p.add_argument("--skip-base-probe", action="store_true", help="Skip base probe")
    verify_p.add_argument("--goal", default="", help="Goal description")

    trace_p = subparsers.add_parser("trace", help="Inspect persistent cross-window execution audit logs.")
    trace_p.add_argument("--limit", "-n", type=int, default=10, help="Number of traces to show (default: 10).")
    trace_p.add_argument("--global", dest="global_only", action="store_true", help="Only show user-global traces.")
    trace_p.add_argument("--json", action="store_true", help="Output raw JSON array.")

    dashboard_p = subparsers.add_parser("dashboard", help="Launch modern Web Dashboard & Control Center (Tokens/Time saved, Sniffer, Pillars).")
    dashboard_p.add_argument("--port", "-p", type=int, default=8765, help="Port to listen on (default: 8765).")
    dashboard_p.add_argument("--no-browser", action="store_true", help="Do not auto-open browser.")

    subparsers.add_parser("mcp", help="Start the Pacer JSON-RPC stdio MCP server.")
    subparsers.add_parser("backends", help="Check status of all local and remote AI model engines.")

    wb_p = subparsers.add_parser("wb", help="Interact with WorkBuddy desktop local gateway.")
    wb_p.add_argument("prompt", nargs="*", help="Prompt to send to WorkBuddy")

    gemini_p = subparsers.add_parser("gemini", help="Invoke Gemini Flash.")
    gemini_p.add_argument("prompt", nargs="*", help="Prompt to send to Gemini")

    grok_p = subparsers.add_parser("grok", help="Invoke xAI Grok via Sub2API fast streaming gateway.")
    grok_p.add_argument("prompt", nargs="*", help="Prompt to send to Grok")

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
        if argv[0] in {"mcp", "--mcp"}:
            from .mcp_server import serve_stdio
            return serve_stdio()
        if argv[0] in {"embed", "--embed"}:
            return run_pacer_embed(argv[1:])
        if argv[0] in {"auto", "--auto"}:
            return run_pacer_auto(argv[1:])
        if argv[0] in {"plan", "--plan"}:
            return run_pacer_plan(argv[1:])
        if argv[0] in {"verify", "--verify"}:
            return run_pacer_verify(argv[1:])
        if argv[0] in {"trace", "--trace", "traces", "--traces"}:
            return run_pacer_trace(argv[1:])
        if argv[0] in {"backends", "models", "--backends", "--models"}:
            return run_pacer_backends(argv[1:])
        if argv[0] in {"wb", "workbuddy", "--wb"}:
            return run_pacer_wb(argv[1:])
        if argv[0] in {"codex", "code", "--codex"}:
            from .codex_launcher import launch_codex

            return launch_codex(argv[1:])
        if argv[0] in {"claude", "cc", "--claude"}:
            return launch_claude_code(argv[1:])
        if argv[0] in {"task", "--task", "chat", "--chat"}:
            from .pacer_task import run_pacer_task

            return run_pacer_task(argv[1:])
        if argv[0] in {"yolo", "claude-yolo", "cc-yolo", "--claude-yolo"}:
            return launch_claude_code_yolo(argv[1:])
        if argv[0] in {"gemini", "gm", "--gemini"}:
            return launch_gemini(argv[1:])
        if argv[0] in {"grok", "gork", "--grok"}:
            return run_pacer_grok(argv[1:])
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
    if args.command == "mcp":
        from .mcp_server import serve_stdio
        return serve_stdio()
    if args.command == "embed":
        target_val = getattr(args, "target_opt", None) or getattr(args, "target", ".")
        return run_pacer_embed(["--target", target_val])
    if args.command == "auto":
        goal_parts = getattr(args, "goal", [])
        return run_pacer_auto(
            [
                *goal_parts,
                *(["--verify-cmd", args.verify_cmd] if getattr(args, "verify_cmd", "") else []),
                *(["--dry-run"] if getattr(args, "dry_run", False) else []),
                *(["--llm-plan"] if getattr(args, "llm_plan", False) else []),
                *(["--workspace-root", args.workspace_root] if getattr(args, "workspace_root", "") else []),
            ]
        )
    if args.command == "plan":
        return run_pacer_plan(getattr(args, "goal", []))
    if args.command == "verify":
        verify_argv: list[str] = []
        if getattr(args, "cmd", ""):
            verify_argv += ["--cmd", args.cmd]
        if getattr(args, "base", ""):
            verify_argv += ["--base", args.base]
        if getattr(args, "branch", ""):
            verify_argv += ["--branch", args.branch]
        if getattr(args, "allow_test_edits", False):
            verify_argv.append("--allow-test-edits")
        if getattr(args, "skip_base_probe", False):
            verify_argv.append("--skip-base-probe")
        if getattr(args, "goal", ""):
            verify_argv += ["--goal", args.goal]
        return run_pacer_verify(verify_argv)
    if args.command == "trace":
        trace_argv: list[str] = []
        if getattr(args, "limit", 10):
            trace_argv += ["--limit", str(args.limit)]
        if getattr(args, "global_only", False):
            trace_argv.append("--global")
        if getattr(args, "json", False):
            trace_argv.append("--json")
        return run_pacer_trace(trace_argv)
    if args.command == "dashboard":
        dash_argv: list[str] = []
        if getattr(args, "port", None):
            dash_argv += ["--port", str(args.port)]
        if getattr(args, "no_browser", False):
            dash_argv.append("--no-browser")
        return run_pacer_dashboard(dash_argv)
    if args.command == "backends":
        return run_pacer_backends()
    if args.command == "wb":
        return run_pacer_wb(getattr(args, "prompt", []))
    if args.command == "gemini":
        return launch_gemini(getattr(args, "prompt", []))
    if args.command in {"grok", "gork"}:
        return run_pacer_grok(getattr(args, "prompt", []))
    if args.command == "completion":
        written = write_completion_scripts(args.output_dir)
        for shell, path in written.items():
            print(f"{shell}: {path}")
        return 0
    print(build_welcome_message(welcome_cli))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())