"""Pacer desktop workbench — a real window, not a web page.

A dependency-free Tkinter app (Python ships Tk, so this packages into an .exe)
for non-technical users: pick a project folder, pick a coding agent, type a
goal in plain words, give the acceptance command, and hit Preview or Run. It
drives the exact same mission machinery as the CLI and the web dashboard
(``start_workbench_mission`` → ``run_chief_mission``), so behavior — clarity
gate, isolated worktree, test-command verification — is identical; only the
surface differs.

The Tk wiring is intentionally thin; the decisions live in small pure helpers
below so they can be tested without a display.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .agent_capabilities import agents_doctor
from .subprocess_window import hidden_subprocess_kwargs


def workbench_agents() -> list[str]:
    """Installed coding agents to offer, best-effort, with a sane default order."""
    try:
        doctor = agents_doctor()
    except Exception:  # noqa: BLE001 - the app must open even if detection fails
        doctor = []
    installed = [str(a.get("agent")) for a in doctor if isinstance(a, dict) and a.get("installed") and a.get("agent")]
    # Only agents with an executable coding adapter can actually run work.
    order = ["codex", "claude-code"]
    coding = [agent for agent in order if agent in installed]
    try:
        from .agent_backends import resolve_backend_by_name

        # Low-cost backend configured => cheapest backend goes first and becomes the
        # default, so subscription quota is only burned when the user opts in.
        if resolve_backend_by_name("bugteam"):
            coding.insert(0, "bugteam")
        elif resolve_backend_by_name("mimo"):
            coding.insert(0, "mimo")
    except Exception:  # noqa: BLE001 - backend detection must not block launch
        pass
    return coding or order


def start_dashboard_process(workspace_root: str | Path) -> int:
    """Start the web kanban as an independent process and return its PID."""
    import sys
    import threading

    # 如果是 exe 打包环境，直接在后台线程启动 dashboard
    if getattr(sys, 'frozen', False):
        def _run_dashboard():
            try:
                from .dashboard import serve_dashboard
                serve_dashboard(
                    workspace_root=workspace_root,
                    open_browser=True,
                )
            except Exception as e:
                print(f"Dashboard error: {e}")

        thread = threading.Thread(target=_run_dashboard, daemon=True)
        thread.start()
        # 返回线程 ID 作为标识
        return thread.ident or 0

    # 开发环境：使用子进程
    import subprocess
    cmd = [
        sys.executable,
        "-m",
        "visual_agent.cli",
        "dashboard",
        "--workspace-root",
        str(workspace_root),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(Path(workspace_root).expanduser().resolve().parent),
        **hidden_subprocess_kwargs(detached=True),
    )
    return int(proc.pid)


def start_portfolio_dashboard_process(project_roots: list[str | Path]) -> int:
    """Start Pacer's multi-project portfolio dashboard as an independent process."""
    import sys
    import threading

    roots = [str(Path(item).expanduser().resolve()) for item in project_roots if str(item).strip()]
    if not roots:
        raise ValueError("至少需要一个项目路径")

    # 如果是 exe 打包环境，直接在后台线程启动
    if getattr(sys, 'frozen', False):
        def _run_portfolio():
            try:
                from .portfolio_dashboard import serve_portfolio_dashboard
                serve_portfolio_dashboard(project_roots=roots, open_browser=True)
            except Exception as e:
                print(f"Portfolio dashboard error: {e}")

        thread = threading.Thread(target=_run_portfolio, daemon=True)
        thread.start()
        return thread.ident or 0

    # 开发环境：使用子进程
    import subprocess
    cmd = [sys.executable, "-m", "visual_agent.cli", "portfolio-dashboard"]
    for root in roots:
        cmd.extend(["--project", root])
    proc = subprocess.Popen(cmd, cwd=str(Path(roots[0]).parent), **hidden_subprocess_kwargs(detached=True))
    return int(proc.pid)


def resolve_workspace(project_dir: str | Path) -> Path:
    """The workspace lives under the chosen project. Created on demand so the
    user only has to point at a folder."""
    from .workspace import init_workspace

    project = Path(project_dir).expanduser().resolve()
    workspace = init_workspace(project / ".agent-workspace", with_demo=False)
    return workspace.root


def validate_launch(*, project_dir: str, goal: str, test_command: str) -> dict[str, Any]:
    """Pre-flight the form before touching the mission machinery."""
    if not str(project_dir or "").strip():
        return {"ok": False, "error": "请先选择项目文件夹。"}
    if not Path(project_dir).expanduser().is_dir():
        return {"ok": False, "error": "项目文件夹不存在。"}
    if not str(goal or "").strip():
        return {"ok": False, "error": "请填写目标（说清楚要做什么）。"}
    if not str(test_command or "").strip():
        return {
            "ok": True,
            "warning": "没有填验收命令：将只能做检查性验收，建议填上项目的测试命令（如 pytest -q）以获得确定性验收。",
        }
    return {"ok": True}


def _grounding_section(final_report: str) -> str:
    """Extract the plan-review section from a mission report, if present."""
    lines = str(final_report or "").splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("### 计划审查")), None)
    if start is None:
        return ""
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("### ")), len(lines))
    return "\n".join(lines[start + 1 : end]).strip()


def launch_desktop_app(project_dir: str | Path | None = None) -> int:
    """Open the Tk workbench window. Returns a process-style exit code."""
    import queue
    import threading
    import tkinter as tk
    from tkinter import filedialog, scrolledtext, ttk

    from .dashboard import build_mission_detail, start_workbench_mission
    from .goal_intake import intake_dialogue_lines, refine_goal
    from .workbench_model_config import (
        WorkbenchModelConfig,
        load_workbench_model_config,
        probe_workbench_model_config,
        redacted_config_summary,
        save_workbench_model_config,
    )

    THEME = {
        "bg": "#0b1117",
        "panel": "#111720",
        "card": "#161d27",
        "card2": "#1b2430",
        "line": "#283341",
        "fg": "#e6edf3",
        "mut": "#90a0b3",
        "acc": "#60a5fa",
        "ok": "#4ade80",
        "warn": "#f2c94c",
        "fail": "#fb7185",
    }

    root = tk.Tk()
    root.title("Pacer 工作台")
    root.geometry("1180x820")
    root.minsize(980, 700)
    root.configure(background=THEME["bg"])

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TFrame", background=THEME["bg"])
    style.configure("Shell.TFrame", background=THEME["bg"])
    style.configure("Panel.TFrame", background=THEME["card"], relief="flat")
    style.configure("Card.TFrame", background=THEME["card"], relief="flat")
    style.configure("Header.TFrame", background=THEME["panel"])
    style.configure("TLabel", background=THEME["bg"], foreground=THEME["fg"])
    style.configure("Header.TLabel", background=THEME["panel"], foreground=THEME["fg"], font=("Segoe UI", 11, "bold"))
    style.configure("Title.TLabel", background=THEME["bg"], foreground=THEME["fg"], font=("Segoe UI", 18, "bold"))
    style.configure("Sub.TLabel", background=THEME["bg"], foreground=THEME["mut"], font=("Segoe UI", 10))
    style.configure("CardTitle.TLabel", background=THEME["card"], foreground=THEME["mut"], font=("Segoe UI", 9, "bold"))
    style.configure("CardValue.TLabel", background=THEME["card"], foreground=THEME["fg"], font=("Segoe UI", 17, "bold"))
    style.configure("CardNote.TLabel", background=THEME["card"], foreground=THEME["mut"], font=("Segoe UI", 9))
    style.configure("Section.TLabelframe", background=THEME["card"], bordercolor=THEME["line"])
    style.configure("Section.TLabelframe.Label", background=THEME["card"], foreground=THEME["mut"], font=("Segoe UI", 9, "bold"))
    style.configure("TButton", padding=(12, 7), relief="flat", background=THEME["card2"], foreground=THEME["fg"])
    style.map("TButton", background=[("active", THEME["line"]), ("disabled", THEME["card2"])])
    style.configure("Primary.TButton", padding=(12, 7), relief="flat", background=THEME["acc"], foreground="#08111d")
    style.map("Primary.TButton", background=[("active", "#6d93ff"), ("disabled", THEME["acc"])])
    style.configure("Danger.TButton", padding=(12, 7), relief="flat", background=THEME["fail"], foreground="#1c1014")
    style.configure("TEntry", fieldbackground=THEME["card2"], foreground=THEME["fg"], insertcolor=THEME["fg"])
    style.configure("TCombobox", fieldbackground=THEME["card2"], foreground=THEME["fg"])
    style.configure("TCheckbutton", background=THEME["bg"], foreground=THEME["fg"])
    style.configure("Status.TLabel", background=THEME["bg"], foreground=THEME["ok"], font=("Segoe UI", 9, "bold"))

    state: dict[str, Any] = {
        "launch_id": None,
        "mission_id": None,
        "polling": False,
        "intake_goal": "",
        "intake_answers": [],
        "intake_payload": None,
    }
    events: "queue.Queue[tuple[str, Any]]" = queue.Queue()

    pad = {"padx": 12, "pady": 8}
    shell = ttk.Frame(root, style="Shell.TFrame")
    shell.pack(fill="both", expand=True)

    header = ttk.Frame(shell, style="Header.TFrame")
    header.pack(fill="x")
    header.grid_columnconfigure(1, weight=1)
    ttk.Label(header, text="Pacer 工作台", style="Header.TLabel").grid(row=0, column=0, sticky="w", padx=16, pady=(10, 2))
    ttk.Label(header, text="专注于目标理解、任务派发、执行和验收", style="Sub.TLabel").grid(row=1, column=0, sticky="w", padx=16, pady=(0, 10))
    top_meta = ttk.Frame(header, style="Header.TFrame")
    top_meta.grid(row=0, column=1, rowspan=2, sticky="e", padx=16, pady=10)
    project_chip = ttk.Label(top_meta, text="项目：未选择", style="Header.TLabel")
    project_chip.pack(side="top", anchor="e")
    worker_chip = ttk.Label(top_meta, text="Worker：未知", style="Header.TLabel")
    worker_chip.pack(side="top", anchor="e")

    frm = ttk.Frame(shell, style="Shell.TFrame")
    frm.pack(fill="both", expand=True, padx=16, pady=16)
    frm.grid_columnconfigure(0, weight=1)
    frm.grid_columnconfigure(1, weight=1)
    frm.grid_rowconfigure(2, weight=1)

    # --- Project / agent / execution setup ---
    setup = ttk.Frame(frm, style="Card.TFrame")
    setup.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
    setup.grid_columnconfigure(1, weight=1)
    ttk.Label(setup, text="项目", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", padx=14, pady=(12, 4))
    project_var = tk.StringVar(value=str(project_dir or ""))
    project_entry = ttk.Entry(setup, textvariable=project_var)
    project_entry.grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=(12, 4))

    def pick_folder() -> None:
        chosen = filedialog.askdirectory(title="选择要开发的项目文件夹")
        if chosen:
            project_var.set(chosen)

    ttk.Button(setup, text="浏览…", command=pick_folder).grid(row=0, column=2, sticky="e", padx=(0, 14), pady=(12, 4))
    ttk.Label(setup, text="编码 agent", style="CardTitle.TLabel").grid(row=1, column=0, sticky="w", padx=14, pady=(0, 4))
    agent_var = tk.StringVar()
    agents = workbench_agents()
    agent_box = ttk.Combobox(setup, textvariable=agent_var, values=agents, state="readonly", width=18)
    agent_box.current(0)
    agent_box.grid(row=1, column=1, sticky="w", padx=(0, 10), pady=(0, 4))
    ttk.Label(setup, text="按目标先理解，再派活；不要求用户会写指令。", style="Sub.TLabel").grid(row=1, column=2, sticky="e", padx=(0, 14), pady=(0, 4))

    model_config = load_workbench_model_config()
    ttk.Label(setup, text="总工作台模型接口", style="CardTitle.TLabel").grid(row=2, column=0, sticky="w", padx=14, pady=(8, 4))
    model_grid = ttk.Frame(setup, style="Card.TFrame")
    model_grid.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(0, 14), pady=(8, 4))
    model_grid.grid_columnconfigure(1, weight=2)
    model_grid.grid_columnconfigure(3, weight=1)
    api_base_var = tk.StringVar(value=model_config.base_url)
    api_key_var = tk.StringVar(value=model_config.api_key)
    api_model_var = tk.StringVar(value=model_config.model)
    api_effort_var = tk.StringVar(value=model_config.reasoning_effort)
    model_status_var = tk.StringVar(value=redacted_config_summary(model_config))
    ttk.Label(model_grid, text="Base URL", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 6))
    ttk.Entry(model_grid, textvariable=api_base_var).grid(row=0, column=1, sticky="ew", padx=(0, 10))
    ttk.Label(model_grid, text="Model", style="CardTitle.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 6))
    ttk.Entry(model_grid, textvariable=api_model_var, width=18).grid(row=0, column=3, sticky="ew", padx=(0, 10))
    ttk.Label(model_grid, text="API Key", style="CardTitle.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
    ttk.Entry(model_grid, textvariable=api_key_var, show="*").grid(row=1, column=1, columnspan=3, sticky="ew", padx=(0, 10), pady=(6, 0))
    ttk.Label(model_grid, text="Reasoning", style="CardTitle.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
    ttk.Combobox(
        model_grid,
        textvariable=api_effort_var,
        values=("", "low", "medium", "high", "xhigh"),
        state="readonly",
        width=18,
    ).grid(row=2, column=1, sticky="w", pady=(6, 0))
    model_button_row = ttk.Frame(model_grid, style="Card.TFrame")
    model_button_row.grid(row=0, column=4, rowspan=2, sticky="nsew")
    save_model_btn = ttk.Button(model_button_row, text="保存接口")
    test_model_btn = ttk.Button(model_button_row, text="测试")
    save_model_btn.pack(fill="x")
    test_model_btn.pack(fill="x", pady=(6, 0))
    ttk.Label(setup, textvariable=model_status_var, style="Sub.TLabel").grid(row=3, column=1, columnspan=2, sticky="w", padx=(0, 14), pady=(0, 10))

    # --- Left / right panels ---
    left = ttk.Frame(frm, style="Card.TFrame")
    left.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
    left.grid_columnconfigure(0, weight=1)
    left.grid_rowconfigure(2, weight=1)

    right = ttk.Frame(frm, style="Card.TFrame")
    right.grid(row=1, column=1, sticky="nsew", padx=(6, 0))
    right.grid_columnconfigure(0, weight=1)
    right.grid_rowconfigure(1, weight=1)
    # 运行日志 must stretch with the window — it is where the user actually
    # reads mission progress, so give it the larger share of extra height.
    right.grid_rowconfigure(2, weight=2)

    # --- Goal ---
    goal_frame = ttk.LabelFrame(left, text="目标接待", style="Section.TLabelframe")
    goal_frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 8))
    goal_frame.grid_columnconfigure(0, weight=1)
    goal_header = ttk.Frame(goal_frame, style="Card.TFrame")
    goal_header.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
    goal_header.grid_columnconfigure(0, weight=1)
    ttk.Label(goal_header, text="说人话，把要做的事情讲出来", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
    refine_btn = ttk.Button(goal_header, text="帮我理清目标", style="Primary.TButton")
    refine_btn.grid(row=0, column=1, sticky="e")
    goal_text = tk.Text(goal_frame, height=5, wrap="word", bg=THEME["card2"], fg=THEME["fg"], insertbackground=THEME["fg"],
                        relief="flat", highlightthickness=1, highlightbackground=THEME["line"], highlightcolor=THEME["acc"],
                        padx=10, pady=8)
    goal_text.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))

    # --- Intake conversation ---
    intake_frame = ttk.LabelFrame(left, text="目标对话", style="Section.TLabelframe")
    intake_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))
    intake_frame.grid_columnconfigure(0, weight=1)
    intake_frame.grid_rowconfigure(0, weight=1)
    intake_log = scrolledtext.ScrolledText(intake_frame, height=10, wrap="word", state="disabled",
                                           bg=THEME["card2"], fg=THEME["fg"], insertbackground=THEME["fg"],
                                           relief="flat", highlightthickness=1, highlightbackground=THEME["line"],
                                           highlightcolor=THEME["acc"])
    intake_log.grid(row=0, column=0, sticky="nsew", padx=12, pady=(12, 8))

    answer_row = ttk.Frame(intake_frame, style="Card.TFrame")
    answer_row.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
    answer_row.grid_columnconfigure(1, weight=1)
    ttk.Label(answer_row, text="补充", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
    answer_var = tk.StringVar()
    answer_entry = ttk.Entry(answer_row, textvariable=answer_var)
    answer_entry.grid(row=0, column=1, sticky="ew")
    answer_btn = ttk.Button(answer_row, text="继续对话")
    answer_btn.grid(row=0, column=2, sticky="e", padx=(8, 0))

    # --- Right side controls ---
    control_frame = ttk.LabelFrame(right, text="执行控制", style="Section.TLabelframe")
    control_frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 8))
    control_frame.grid_columnconfigure(0, weight=1)

    ttk.Label(control_frame, text="验收命令", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))
    test_var = tk.StringVar()
    ttk.Entry(control_frame, textvariable=test_var).grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
    ttk.Label(control_frame, text="可以留空（自动探测项目的测试命令）；填了更准，如 pytest -q / npm test", style="Sub.TLabel").grid(row=2, column=0, sticky="w", padx=12)

    auto_merge_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(control_frame, text="验收通过后自动合并", variable=auto_merge_var).grid(row=3, column=0, sticky="w", padx=12, pady=(8, 0))
    button_row = ttk.Frame(control_frame, style="Card.TFrame")
    button_row.grid(row=4, column=0, sticky="ew", padx=12, pady=12)
    button_row.grid_columnconfigure(0, weight=1)
    preview_btn = ttk.Button(button_row, text="预览（不花钱）")
    run_btn = ttk.Button(button_row, text="开始执行", style="Primary.TButton")
    board_btn = ttk.Button(button_row, text="打开看板工作台")
    portfolio_btn = ttk.Button(button_row, text="多项目观察台")
    preview_btn.grid(row=0, column=0, sticky="ew")
    run_btn.grid(row=0, column=1, sticky="ew", padx=8)
    board_btn.grid(row=0, column=2, sticky="ew")
    portfolio_btn.grid(row=0, column=3, sticky="ew", padx=(8, 0))

    summary_frame = ttk.LabelFrame(right, text="状态摘要", style="Section.TLabelframe")
    summary_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))
    summary_frame.grid_columnconfigure(0, weight=1)
    summary_frame.grid_rowconfigure(1, weight=0)
    summary_frame.grid_rowconfigure(2, weight=0)
    summary_frame.grid_rowconfigure(3, weight=1)
    summary_row = ttk.Frame(summary_frame, style="Card.TFrame")
    summary_row.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
    summary_row.grid_columnconfigure(1, weight=1)
    status_var = tk.StringVar(value="填好上面的内容，点预览或执行。")
    status_label = ttk.Label(summary_row, textvariable=status_var, style="Status.TLabel")
    status_label.grid(row=0, column=0, sticky="w")
    meta_row = ttk.Frame(summary_frame, style="Card.TFrame")
    meta_row.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
    meta_row.grid_columnconfigure(0, weight=1)
    meta_row.grid_columnconfigure(1, weight=1)
    workspace_var = tk.StringVar(value="Workspace：未选择")
    agents_var = tk.StringVar(value="Agents：-")
    worker_var = tk.StringVar(value="Worker：未知")
    ttk.Label(meta_row, textvariable=workspace_var, style="Sub.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Label(meta_row, textvariable=agents_var, style="Sub.TLabel").grid(row=0, column=1, sticky="e")
    ttk.Label(summary_frame, textvariable=worker_var, style="Sub.TLabel").grid(row=2, column=0, sticky="w", padx=12, pady=(0, 8))
    summary_kpis = ttk.Frame(summary_frame, style="Card.TFrame")
    summary_kpis.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 8))
    summary_kpis.grid_columnconfigure(0, weight=1)
    summary_kpis.grid_columnconfigure(1, weight=1)
    summary_kpis.grid_columnconfigure(2, weight=1)
    kpi_verified = ttk.Label(summary_kpis, text="0", style="CardValue.TLabel")
    kpi_running = ttk.Label(summary_kpis, text="0", style="CardValue.TLabel")
    kpi_queue = ttk.Label(summary_kpis, text="0", style="CardValue.TLabel")
    kpi_saved_usd = ttk.Label(summary_kpis, text="$0.00", style="CardValue.TLabel")
    ttk.Label(summary_kpis, text="已验收", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
    kpi_verified.grid(row=1, column=0, sticky="w")
    ttk.Label(summary_kpis, text="执行中", style="CardTitle.TLabel").grid(row=0, column=1, sticky="w")
    kpi_running.grid(row=1, column=1, sticky="w")
    ttk.Label(summary_kpis, text="队列", style="CardTitle.TLabel").grid(row=0, column=2, sticky="w")
    kpi_queue.grid(row=1, column=2, sticky="w")
    ttk.Label(summary_kpis, text="MiMo 节省", style="CardTitle.TLabel").grid(row=0, column=3, sticky="w")
    kpi_saved_usd.grid(row=1, column=3, sticky="w")

    detail_frame = ttk.LabelFrame(summary_frame, text="工作台概览", style="Section.TLabelframe")
    detail_frame.grid(row=4, column=0, sticky="nsew", padx=12, pady=(0, 12))
    detail_frame.grid_columnconfigure(0, weight=1)
    detail_frame.grid_columnconfigure(1, weight=1)
    detail_frame.grid_rowconfigure(1, weight=1)
    detail_frame.grid_rowconfigure(3, weight=1)
    saved_min_var = tk.StringVar(value="0.0 分钟")
    efficiency_var = tk.StringVar(value="0%")
    plans_var = tk.StringVar(value="0")
    ttk.Label(detail_frame, text="时间节省", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Label(detail_frame, textvariable=saved_min_var, style="CardValue.TLabel").grid(row=1, column=0, sticky="w")
    ttk.Label(detail_frame, text="综合效率", style="CardTitle.TLabel").grid(row=0, column=1, sticky="w")
    ttk.Label(detail_frame, textvariable=efficiency_var, style="CardValue.TLabel").grid(row=1, column=1, sticky="w")
    recent_frame = ttk.LabelFrame(detail_frame, text="最近任务", style="Section.TLabelframe")
    recent_frame.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(10, 8))
    recent_frame.grid_columnconfigure(0, weight=1)
    recent_frame.grid_rowconfigure(0, weight=1)
    recent_box = scrolledtext.ScrolledText(recent_frame, height=7, wrap="word", state="disabled",
                                           bg=THEME["card2"], fg=THEME["fg"], insertbackground=THEME["fg"],
                                           relief="flat", highlightthickness=1, highlightbackground=THEME["line"],
                                           highlightcolor=THEME["acc"])
    recent_box.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
    queue_frame = ttk.LabelFrame(detail_frame, text="待办队列", style="Section.TLabelframe")
    queue_frame.grid(row=3, column=0, columnspan=2, sticky="nsew")
    queue_frame.grid_columnconfigure(0, weight=1)
    queue_frame.grid_rowconfigure(0, weight=1)
    queue_box = scrolledtext.ScrolledText(queue_frame, height=7, wrap="word", state="disabled",
                                          bg=THEME["card2"], fg=THEME["fg"], insertbackground=THEME["fg"],
                                          relief="flat", highlightthickness=1, highlightbackground=THEME["line"],
                                          highlightcolor=THEME["acc"])
    queue_box.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

    # --- Output log ---
    log_frame = ttk.LabelFrame(right, text="运行日志", style="Section.TLabelframe")
    log_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
    log_frame.grid_columnconfigure(0, weight=1)
    log_frame.grid_rowconfigure(0, weight=1)
    log = scrolledtext.ScrolledText(log_frame, height=14, wrap="word", state="disabled",
                                    bg=THEME["card2"], fg=THEME["fg"], insertbackground=THEME["fg"],
                                    relief="flat", highlightthickness=1, highlightbackground=THEME["line"],
                                    highlightcolor=THEME["acc"])
    log.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

    def append_log(line: str) -> None:
        log.configure(state="normal")
        log.insert("end", line + "\n")
        log.see("end")
        log.configure(state="disabled")

    def refresh_agents() -> None:
        refreshed = workbench_agents()
        agent_box.configure(values=refreshed)
        current = agent_var.get()
        if current not in refreshed and refreshed:
            agent_var.set(refreshed[0])
        elif refreshed and not current:
            agent_var.set(refreshed[0])
        agents_var.set("Agents：" + ", ".join(refreshed))
        worker_chip.configure(text=f"Worker：{agent_var.get() or '未知'}")

    def current_model_config() -> WorkbenchModelConfig:
        return WorkbenchModelConfig(
            base_url=api_base_var.get().strip(),
            api_key=api_key_var.get().strip(),
            model=api_model_var.get().strip(),
            reasoning_effort=api_effort_var.get().strip(),
        )

    def save_model_config_from_ui() -> None:
        config = current_model_config()
        try:
            path = save_workbench_model_config(config)
        except Exception as exc:  # noqa: BLE001
            model_status_var.set(f"保存失败：{exc}")
            status_var.set("模型接口保存失败")
            return
        model_status_var.set(redacted_config_summary(config))
        append_log(f"✓ 总工作台模型接口已保存到 {path}")
        status_var.set("模型接口已保存")
        refresh_agents()

    def test_model_config_from_ui() -> None:
        config = current_model_config()
        model_status_var.set("正在测试 /v1/models …")
        test_model_btn.configure(state="disabled")

        def worker() -> None:
            result = probe_workbench_model_config(config)
            events.put(("model_config_tested", result))

        threading.Thread(target=worker, daemon=True).start()

    save_model_btn.configure(command=save_model_config_from_ui)
    test_model_btn.configure(command=test_model_config_from_ui)

    def clear_intake_log() -> None:
        intake_log.configure(state="normal")
        intake_log.delete("1.0", "end")
        intake_log.configure(state="disabled")

    def append_intake(line: str) -> None:
        intake_log.configure(state="normal")
        intake_log.insert("end", line + "\n")
        intake_log.see("end")
        intake_log.configure(state="disabled")

    def render_textbox(widget: Any, lines: list[str]) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", "\n".join(lines).rstrip())
        widget.configure(state="disabled")

    def render_intake(payload: dict[str, Any], *, goal: str, answers: list[str]) -> None:
        state["intake_payload"] = payload
        clear_intake_log()
        append_intake("工作台：我先把你的目标整理一下。")
        for line in intake_dialogue_lines(payload, answers=answers):
            append_intake(line)
        questions = [str(q).strip() for q in (payload.get("clarifying_questions") or []) if str(q).strip()]
        if questions:
            append_intake("")
            append_intake("请直接补一句，不用整理成正式需求。")
            answer_entry.focus_set()
        else:
            append_intake("")
            append_intake("如果你认可这个改写，就可以直接点「开始执行」或继续补充。")
        suggested = str(payload.get("suggested_goal") or "").strip()
        if suggested and suggested != goal and payload.get("already_clear"):
            goal_text.delete("1.0", "end")
            goal_text.insert("1.0", suggested)
        status_var.set("目标对话已更新")

    def start_intake_round(*, answer: str | None = None) -> None:
        goal = goal_text.get("1.0", "end").strip()
        if not goal:
            status_var.set("先写一句大概的目标，我来帮你理清。")
            return
        if state.get("intake_goal") != goal:
            state["intake_goal"] = goal
            state["intake_answers"] = []
            state["intake_payload"] = None
            clear_intake_log()
        clean_answer = str(answer or "").strip()
        if clean_answer:
            state["intake_answers"].append(clean_answer)
            append_intake(f"你：{clean_answer}")
            answer_var.set("")
        refine_btn.configure(state="disabled")
        answer_btn.configure(state="disabled")
        status_var.set("正在理解你的目标并准备下一轮追问…")

        def worker() -> None:
            try:
                payload = refine_goal(goal, answers=list(state["intake_answers"]))
                events.put(("intake_refined", {"payload": payload, "goal": goal, "answers": list(state["intake_answers"]) }))
            except Exception as exc:  # noqa: BLE001
                events.put(
                    (
                        "intake_refined",
                        {
                            "payload": {"source": "error", "model_error": str(exc)},
                            "goal": goal,
                            "answers": list(state["intake_answers"]),
                        },
                    )
                )

        threading.Thread(target=worker, daemon=True).start()

    def set_busy(busy: bool) -> None:
        preview_btn.configure(state="disabled" if busy else "normal")
        run_btn.configure(state="disabled" if busy else "normal")

    def start(execute: bool) -> None:
        project = project_var.get().strip()
        goal = goal_text.get("1.0", "end").strip()
        test_command = test_var.get().strip()
        check = validate_launch(project_dir=project, goal=goal, test_command=test_command)
        if not check["ok"]:
            status_var.set(check["error"])
            return
        if check.get("warning"):
            append_log("⚠ " + check["warning"])
        set_busy(True)
        status_var.set("执行中…" if execute else "生成预览…")
        append_log(("▶ 开始执行：" if execute else "▶ 预览：") + goal)

        def worker() -> None:
            try:
                workspace_root = resolve_workspace(project)
                result = start_workbench_mission(
                    workspace_root=workspace_root,
                    repo_root=project,
                    goal=goal,
                    test_command=test_command,
                    agent=agent_var.get(),
                    execute=execute,
                    merge_policy="auto" if auto_merge_var.get() else "manual",
                    intake=state.get("intake_payload") if isinstance(state.get("intake_payload"), dict) else None,
                    answers=list(state.get("intake_answers") or []),
                )
                events.put(("launched", {"result": result, "workspace_root": str(workspace_root)}))
            except Exception as exc:  # noqa: BLE001 - surface to the window
                events.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    preview_btn.configure(command=lambda: start(False))
    run_btn.configure(command=lambda: start(True))

    def open_board() -> None:
        # The kanban workbench (web) is the full product surface: board columns,
        # diff review, one-click merge. This window stays a thin launcher.
        project = project_var.get().strip()
        if not project or not Path(project).expanduser().is_dir():
            status_var.set("请先选择项目文件夹，再打开看板。")
            return
        if state.get("board_started"):
            status_var.set("看板已在运行，看浏览器窗口（或重新打开刚才的地址）。")
            return

        def worker() -> None:
            try:
                workspace_root = resolve_workspace(project)
                pid = start_dashboard_process(workspace_root)
                events.put(("board_started", {"pid": pid}))
            except Exception as exc:  # noqa: BLE001 - surface to the window
                state["board_started"] = False
                events.put(("error", f"看板启动失败：{exc}"))

        state["board_started"] = True
        threading.Thread(target=worker, daemon=True).start()
        append_log("▶ 看板工作台正在独立启动，浏览器会自动打开。")
        status_var.set("看板正在启动。")

    board_btn.configure(command=open_board)

    def open_portfolio_board() -> None:
        project = project_var.get().strip()
        roots = [project] if project else []
        if not roots or not Path(roots[0]).expanduser().is_dir():
            status_var.set("请先选择至少一个项目文件夹。")
            return
        if state.get("portfolio_started"):
            status_var.set("多项目观察台已在运行。")
            return

        def worker() -> None:
            try:
                pid = start_portfolio_dashboard_process(roots)
                events.put(("portfolio_started", {"pid": pid}))
            except Exception as exc:  # noqa: BLE001
                state["portfolio_started"] = False
                events.put(("error", f"多项目观察台启动失败：{exc}"))

        state["portfolio_started"] = True
        threading.Thread(target=worker, daemon=True).start()
        append_log("▶ Pacer 多项目观察台正在独立启动。")
        status_var.set("多项目观察台正在启动。")

    portfolio_btn.configure(command=open_portfolio_board)

    def refine() -> None:
        start_intake_round()

    def send_answer() -> None:
        start_intake_round(answer=answer_var.get())

    refine_btn.configure(command=refine)
    answer_btn.configure(command=send_answer)
    answer_entry.bind("<Return>", lambda _event: send_answer())

    def poll_mission(workspace_root: str) -> None:
        mission_id = state.get("mission_id")
        if not mission_id:
            return
        try:
            detail = build_mission_detail(workspace_root, mission_id)
        except Exception:  # noqa: BLE001
            return
        mission = detail.get("mission") or {}
        status = str(mission.get("status") or "")
        stop = str(mission.get("stop_reason") or "")
        status_var.set(f"状态：{status}  {('· ' + stop) if stop else ''}")
        live = detail.get("live_logs") if isinstance(detail.get("live_logs"), dict) else {}
        live_key = str(live.get("latest_key") or "")
        live_tail = str(live.get("latest_tail") or "").strip()
        if live_key and live_tail and state.get("last_live_log_key") != live_key:
            state["last_live_log_key"] = live_key
            append_log("---- 当前执行日志 ----")
            append_log(live_tail[-1800:])
        if status and status not in {"running", "created", "preview_running", "background_running"}:
            if stop == "needs_clarification":
                # The mission already reviewed the repo's plan documents before
                # stopping; surface that conversation instead of a bare error.
                review = _grounding_section(str(detail.get("final_report") or ""))
                from .goal_intake import intake_to_markdown, refine_goal

                objective = str(mission.get("objective") or "").strip()
                intake_payload = refine_goal(objective, enable_model=True)
                if review:
                    append_log("⚠ 目标还不够具体。我先审查了项目里的计划文档：")
                    append_log(review)
                append_log("⚠ 目标还不够具体。我先按当前目标重新接待了一次：")
                append_log(intake_to_markdown(intake_payload))
                if review:
                    append_log("按上面的建议确认下一步（或直接把待办写进目标），再点「预览」或「执行」。")
                else:
                    append_log("把上面的补充发到目标对话里，工作台会继续追问。")
                append_log("⚠ 目标太模糊，工作台无法派发。")
                status_var.set("⚠ 目标需要更具体，请看下方审查建议")
            else:
                append_log(f"■ 结束：{status} {('（' + stop + '）') if stop else ''}")
                report = str(detail.get("final_report") or "").strip()
                if report:
                    append_log("---- 最终报告 ----")
                    append_log(report[:4000])
            set_busy(False)
            state["mission_id"] = None
            state["polling"] = False
            return
        root.after(1500, lambda: poll_mission(workspace_root))

    def drain_events() -> None:
        try:
            while True:
                kind, payload = events.get_nowait()
                if kind == "error":
                    append_log("✖ 启动失败：" + str(payload))
                    status_var.set("启动失败")
                    set_busy(False)
                elif kind == "board_started":
                    pid = payload.get("pid")
                    append_log(f"✓ 看板工作台已独立启动（PID {pid}）。")
                    status_var.set("看板已独立启动；关闭本窗口不会停止看板。")
                elif kind == "portfolio_started":
                    pid = payload.get("pid")
                    append_log(f"✓ Pacer 多项目观察台已独立启动（PID {pid}）。")
                    status_var.set("多项目观察台已独立启动。")
                elif kind == "refined":
                    refine_btn.configure(state="normal")
                    src = payload.get("source")
                    if src == "model":
                        status_var.set("已用便宜模型理清目标。")
                    elif payload.get("model_error"):
                        status_var.set("没配便宜模型，先用本地规则给建议。")
                    else:
                        status_var.set("已用本地规则给建议。")
                    qs = payload.get("clarifying_questions") or []
                    if qs:
                        append_log("需要先说清楚：")
                        for q in qs:
                            append_log("  • " + str(q))
                    suggested = str(payload.get("suggested_goal") or "").strip()
                    if suggested and suggested != goal_text.get("1.0", "end").strip():
                        append_log("建议改写为：" + suggested)
                        goal_text.delete("1.0", "end")
                        goal_text.insert("1.0", suggested)
                    hint = str(payload.get("acceptance_hint") or "").strip()
                    if hint:
                        append_log("建议验收：" + hint)
                    elif payload.get("already_clear"):
                        append_log("目标已经足够清晰，可以直接开始。")
                elif kind == "intake_refined":
                    refine_btn.configure(state="normal")
                    answer_btn.configure(state="normal")
                    event_payload = payload.get("payload") or {}
                    goal = str(event_payload.get("input_goal") or event_payload.get("goal") or "").strip()
                    answers = [str(item).strip() for item in event_payload.get("answers", []) if str(item).strip()]
                    render_intake(event_payload, goal=goal, answers=answers)
                    if event_payload.get("source") == "model":
                        status_var.set("目标对话已更新：模型给了下一轮建议。")
                    elif event_payload.get("model_error"):
                        status_var.set("目标对话已更新：模型不可用，先用本地规则继续。")
                    else:
                        status_var.set("目标对话已更新：先用本地规则继续。")
                    if event_payload.get("clarifying_questions"):
                        append_log("需要继续补充的点已同步到目标对话。")
                elif kind == "model_config_tested":
                    test_model_btn.configure(state="normal")
                    if payload.get("ok"):
                        model_status_var.set(f"接口可用：HTTP {payload.get('status')}")
                        append_log(f"✓ 总工作台模型接口测试通过：{payload.get('url')}")
                    else:
                        detail = payload.get("error") or payload.get("status") or "unknown"
                        model_status_var.set(f"接口测试失败：{detail}")
                        append_log(f"✖ 总工作台模型接口测试失败：{detail}")
                elif kind == "launched":
                    result = payload["result"]
                    if not result.get("ok"):
                        append_log("✖ " + str(result.get("error") or "无法开始"))
                        status_var.set(str(result.get("error") or "无法开始"))
                        set_busy(False)
                    else:
                        append_log("✓ 已派发，等待 mission 落盘…")
                        state["workspace_root"] = payload["workspace_root"]
                        _await_mission_id(payload["workspace_root"], result.get("launch_id"))
        except queue.Empty:
            pass
        root.after(300, drain_events)

    def _await_mission_id(workspace_root: str, launch_id: str | None) -> None:
        # start_workbench_mission runs run_chief_mission in a thread; the mission
        # id appears in the launch registry once the mission file is written.
        from .dashboard import _launch_snapshot

        match = next((item for item in _launch_snapshot() if item.get("launch_id") == launch_id), None)
        if match and match.get("mission_id"):
            state["mission_id"] = match["mission_id"]
            append_log(f"mission: {match['mission_id']}")
            poll_mission(workspace_root)
            return
        if match and match.get("state") == "error":
            append_log("✖ 启动失败：" + str(match.get("error") or ""))
            set_busy(False)
            return
        root.after(600, lambda: _await_mission_id(workspace_root, launch_id))

    def sync_summary(project: str | None = None) -> None:
        project_text = str(project or "").strip()
        project_chip.configure(text=f"项目：{Path(project_text).name if project_text else '未选择'}")
        try:
            from .dashboard import build_dashboard_data

            data = build_dashboard_data(resolve_workspace(project_text)) if project_text and Path(project_text).is_dir() else {}
        except Exception:
            data = {}
        worker = data.get("worker") or {}
        worker_chip.configure(text=f"Worker：{'运行中' if worker.get('running') else '空闲'}")
        worker_var.set(f"Worker：{'运行中' if worker.get('running') else '空闲'}")
        value = data.get("value") or {}
        missions = data.get("missions") or []
        queue_items = data.get("queue") or []
        installed = data.get("installed_agents") or []
        plans = data.get("plans") or []
        kpi_verified.configure(text=str(value.get("verified") or 0))
        kpi_running.configure(text=str(len([m for m in missions if str(m.get("status") or '') in {'running', 'preview_running'}])))
        kpi_queue.configure(text=str(len(queue_items)))
        kpi_saved_usd.configure(text=f"${float((value.get('mimo_efficiency') or {}).get('saved_usd') or 0.0):.2f}")
        workspace_var.set(f"Workspace：{data.get('workspace_root') or '未选择'}")
        agents_var.set(f"Agents：{', '.join(installed) if installed else '未检测到'}")
        saved_min_var.set(f"{float((value.get('mimo_efficiency') or {}).get('saved_minutes') or 0.0):.1f} 分钟")
        efficiency_var.set(f"{int((value.get('mimo_efficiency') or {}).get('efficiency_gain_percent') or 0)}%")
        plans_var.set(str(len(plans)))
        recent_lines: list[str] = []
        for mission in missions[:5]:
            status = str(mission.get("status") or "unknown")
            board = str(mission.get("board_column") or status)
            objective = str(mission.get("objective") or mission.get("goal") or "").strip()
            recent_lines.append(f"- {board} · {objective[:72]}")
            recent_lines.append(f"  {str(mission.get('mission_id') or '')}")
        if not recent_lines:
            recent_lines = ["暂无任务。"]
        render_textbox(recent_box, recent_lines)
        queue_lines: list[str] = []
        for item in queue_items[:5]:
            objective = str(item.get("objective") or item.get("goal") or "").strip()
            agent = str(item.get("agent") or "")
            test = str(item.get("test_command") or "")
            queue_lines.append(f"- {objective[:60]}")
            queue_lines.append(f"  {agent} · {test or '无验收命令'}")
        if not queue_lines:
            queue_lines = ["暂无排队任务。"]
        render_textbox(queue_box, queue_lines)

    def on_project_change(*_: Any) -> None:
        sync_summary(project_var.get())

    project_var.trace_add("write", on_project_change)
    sync_summary(project_var.get())

    root.after(300, drain_events)

    # Startup guard: warn if no AI coding tool is installed.
    if not workbench_agents():
        from tkinter import messagebox
        messagebox.showwarning(
            "Pacer — 未检测到 AI 工具",
            "未在系统 PATH 中找到任何 AI 编码工具（codex / claude / gemini）。\n\n"
            "建议安装至少一种：\n"
            "  • npm install -g @openai/codex\n"
            "  • npm install -g @anthropic-ai/claude-code\n"
            "  • npm install -g @google/gemini-cli\n\n"
            "安装后重启 Pacer 即可。\n"
            "（你仍可打开看板工作台查看历史任务。）"
        )

    root.mainloop()
    return 0
