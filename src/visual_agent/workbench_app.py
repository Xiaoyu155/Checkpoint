"""DevPacer desktop workbench — a real window, not a web page.

A dependency-free Tkinter app (Python ships Tk, so this packages into an .exe)
for non-technical users: pick a project folder, pick a coding agent, type a
goal in plain words, give the acceptance command, and hit Preview or Run. It
drives the exact same mission machinery as the CLI and the web dashboard
(``start_workbench_mission`` → ``run_chief_mission``), so behavior — clarity
gate, isolated worktree, test-command verification, merge-on-verified — is
identical; only the surface differs.

The Tk wiring is intentionally thin; the decisions live in small pure helpers
below so they can be tested without a display.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .agent_capabilities import agents_doctor


def workbench_agents() -> list[str]:
    """Installed coding agents to offer, best-effort, with a sane default order."""
    try:
        doctor = agents_doctor()
    except Exception:  # noqa: BLE001 - the app must open even if detection fails
        doctor = []
    installed = [str(a.get("agent")) for a in doctor if isinstance(a, dict) and a.get("installed") and a.get("agent")]
    # Only agents with an executable coding adapter can actually run work.
    coding = [a for a in installed if a in {"claude-code", "codex"}]
    return coding or ["claude-code", "codex"]


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


def launch_desktop_app(project_dir: str | Path | None = None) -> int:
    """Open the Tk workbench window. Returns a process-style exit code."""
    import queue
    import threading
    import tkinter as tk
    from tkinter import filedialog, scrolledtext, ttk

    from .dashboard import build_mission_detail, start_workbench_mission

    root = tk.Tk()
    root.title("DevPacer 工作台")
    root.geometry("860x680")
    root.minsize(720, 560)

    state: dict[str, Any] = {"launch_id": None, "mission_id": None, "polling": False}
    events: "queue.Queue[tuple[str, Any]]" = queue.Queue()

    pad = {"padx": 10, "pady": 6}
    frm = ttk.Frame(root)
    frm.pack(fill="both", expand=True)

    # --- Project row ---
    row1 = ttk.Frame(frm)
    row1.pack(fill="x", **pad)
    ttk.Label(row1, text="项目文件夹").pack(side="left")
    project_var = tk.StringVar(value=str(project_dir or ""))
    project_entry = ttk.Entry(row1, textvariable=project_var)
    project_entry.pack(side="left", fill="x", expand=True, padx=8)

    def pick_folder() -> None:
        chosen = filedialog.askdirectory(title="选择要开发的项目文件夹")
        if chosen:
            project_var.set(chosen)

    ttk.Button(row1, text="浏览…", command=pick_folder).pack(side="left")

    # --- Agent row ---
    row2 = ttk.Frame(frm)
    row2.pack(fill="x", **pad)
    ttk.Label(row2, text="编码 agent").pack(side="left")
    agent_var = tk.StringVar()
    agents = workbench_agents()
    agent_box = ttk.Combobox(row2, textvariable=agent_var, values=agents, state="readonly", width=18)
    agent_box.current(0)
    agent_box.pack(side="left", padx=8)
    ttk.Label(row2, text="（账号 = 该 CLI 已登录的账号；先在终端登录 claude / codex）", foreground="#888").pack(side="left")

    # --- Goal ---
    goal_row = ttk.Frame(frm)
    goal_row.pack(fill="x", padx=10)
    ttk.Label(goal_row, text="目标（说人话，说清楚要做什么）").pack(side="left")
    refine_btn = ttk.Button(goal_row, text="帮我理清目标")
    refine_btn.pack(side="right")
    goal_text = tk.Text(frm, height=4, wrap="word")
    goal_text.pack(fill="x", padx=10, pady=(0, 6))

    # --- Test command ---
    row3 = ttk.Frame(frm)
    row3.pack(fill="x", **pad)
    ttk.Label(row3, text="验收命令").pack(side="left")
    test_var = tk.StringVar()
    ttk.Entry(row3, textvariable=test_var).pack(side="left", fill="x", expand=True, padx=8)
    ttk.Label(row3, text="如 pytest -q / npm test", foreground="#888").pack(side="left")

    # --- Buttons ---
    row4 = ttk.Frame(frm)
    row4.pack(fill="x", **pad)
    preview_btn = ttk.Button(row4, text="预览（不花钱）")
    run_btn = ttk.Button(row4, text="开始执行")
    preview_btn.pack(side="left")
    run_btn.pack(side="left", padx=8)
    status_var = tk.StringVar(value="填好上面的内容，点预览或执行。")
    ttk.Label(row4, textvariable=status_var, foreground="#4a9").pack(side="left", padx=10)

    # --- Output log ---
    ttk.Label(frm, text="进度").pack(anchor="w", padx=10)
    log = scrolledtext.ScrolledText(frm, height=14, wrap="word", state="disabled")
    log.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def append_log(line: str) -> None:
        log.configure(state="normal")
        log.insert("end", line + "\n")
        log.see("end")
        log.configure(state="disabled")

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
                )
                events.put(("launched", {"result": result, "workspace_root": str(workspace_root)}))
            except Exception as exc:  # noqa: BLE001 - surface to the window
                events.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    preview_btn.configure(command=lambda: start(False))
    run_btn.configure(command=lambda: start(True))

    def refine() -> None:
        goal = goal_text.get("1.0", "end").strip()
        if not goal:
            status_var.set("先写一句大概的目标，我来帮你理清。")
            return
        refine_btn.configure(state="disabled")
        status_var.set("正在用便宜模型帮你理清目标…")

        def worker() -> None:
            from .goal_intake import refine_goal

            try:
                events.put(("refined", refine_goal(goal)))
            except Exception as exc:  # noqa: BLE001
                events.put(("refined", {"source": "error", "model_error": str(exc)}))

        threading.Thread(target=worker, daemon=True).start()

    refine_btn.configure(command=refine)

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
        if status and status not in {"running", "created", "preview_running"}:
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

    root.after(300, drain_events)
    root.mainloop()
    return 0
