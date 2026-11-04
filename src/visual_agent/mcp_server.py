"""Embedded MCP Server for Pacer (`python -m visual_agent.mcp_server`).

Allows Claude Code, Codex CLI (`pacer code`), Cursor, and VS Code to use Pacer
directly from inside the user's favorite coding interface without context bloat.

Implements the standard MCP JSON-RPC 2.0 stdio transport (supporting both
newline-delimited JSON-RPC used by Claude Code / modern MCP clients and
`Content-Length` framed stdio) with zero external third-party dependencies.

Exposed MCP Tools:
1. `pacer_auto`: Delegate a coding/bugfix task to Pacer's Stateless Micro-Agent
   pipeline (0ms local AST index -> <=1,500-token stateless prompt -> Gemini Flash
   SEARCH/REPLACE diff patch -> automated pytest verification -> Codex escalation).
2. `pacer_index`: Sub-20ms local Python AST symbol & snippet extractor so the host
   agent never needs to read entire large files into its main context window.
3. `pacer_canvas`: Inspect or update the `.pacer/canvas.json` state canvas.
4. `begin_pacer_task` / `get_pacer_memory` / `complete_pacer_task`: Native lifecycle
   hooks required by `codex_launcher.py` (`pacer code`).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import traceback
from typing import Any

try:
    from .canvas_manager import CanvasManager
    from .command_verification import (
        changed_acceptance_chain_files,
        changed_test_files,
        run_command_verification,
    )
    from .local_indexer import index_file_symbols
    from .subagent_orchestrator import SubagentOrchestrator, generate_subagent_plan
    from .verification_profiles import (
        choose_verification_command,
        validate_safe_verification_command,
    )
    from .workbuddy_bridge import WorkBuddyGatewayClient
except ImportError:
    from visual_agent.canvas_manager import CanvasManager
    from visual_agent.command_verification import (
        changed_acceptance_chain_files,
        changed_test_files,
        run_command_verification,
    )
    from visual_agent.local_indexer import index_file_symbols
    from visual_agent.subagent_orchestrator import SubagentOrchestrator, generate_subagent_plan
    from visual_agent.verification_profiles import (
        choose_verification_command,
        validate_safe_verification_command,
    )
    from visual_agent.workbuddy_bridge import WorkBuddyGatewayClient


MCP_PROTOCOL_VERSION = "2024-11-05"

MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "pacer_auto",
        "description": (
            "Execute a coding, feature, or bugfix task via Pacer's fast Stateless Canvas "
            "micro-agent pipeline (0ms local AST index + Gemini Flash SEARCH/REPLACE patch "
            "+ automated test verification). Use this tool instead of reading/editing many "
            "files in the main chat window to save time and quota."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "Concrete coding task description (include target file path if known).",
                },
                "verify_cmd": {
                    "type": "string",
                    "description": "Optional verification command (e.g. 'pytest tests/test_foo.py -q').",
                },
                "workspace_root": {
                    "type": "string",
                    "description": "Optional workspace root directory (defaults to current working directory).",
                },
            },
            "required": ["goal"],
        },
    },
    {
        "name": "pacer_index",
        "description": (
            "Sub-20ms local AST symbol & code-window extractor. Extracts classes, function "
            "signatures, line ranges, imports, and a focused snippet around a target line "
            "without loading the entire file into the conversation context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative or absolute file path to index.",
                },
                "target_line": {
                    "type": "integer",
                    "description": "Optional 1-indexed line number (e.g. from a traceback) to extract enclosing function.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional function/class name keyword to focus on.",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "pacer_canvas",
        "description": "Read the current Pacer Stateless Canvas (`.pacer/canvas.json`) summary.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_root": {
                    "type": "string",
                    "description": "Optional workspace root directory.",
                },
            },
        },
    },
    {
        "name": "begin_pacer_task",
        "description": "Initialize a Pacer task contract and baseline for `pacer code`.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_root": {"type": "string"},
                "repo_root": {"type": "string"},
                "goal": {"type": "string"},
            },
            "required": ["goal"],
        },
    },
    {
        "name": "get_pacer_memory",
        "description": "Fetch compact Pacer state memory for the current workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "detail": {"type": "string", "default": "compact"},
            },
        },
    },
    {
        "name": "complete_pacer_task",
        "description": "Verify and complete a Pacer task with automated test verification.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_root": {"type": "string"},
                "repo_root": {"type": "string"},
                "goal": {"type": "string"},
                "summary": {"type": "string"},
                "completion_evidence": {"type": "object"},
                "steps": {"type": "array"},
            },
            "required": ["goal"],
        },
    },
]


def _handle_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    cwd = Path(str(arguments.get("workspace_root") or arguments.get("repo_root") or Path.cwd())).resolve()

    if name == "pacer_index":
        file_path = str(arguments.get("file_path") or "").strip()
        target_line = arguments.get("target_line")
        line_no = int(target_line) if isinstance(target_line, int) or (isinstance(target_line, str) and target_line.isdigit()) else None
        query = str(arguments.get("query") or "")

        candidate = Path(file_path)
        resolved_file = candidate.resolve() if candidate.is_absolute() else (cwd / candidate).resolve()
        try:
            resolved_file.relative_to(cwd)
        except ValueError:
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"Path traversal rejected: '{file_path}' is outside workspace root '{cwd}'"}],
            }

        res = index_file_symbols(
            file_path,
            workspace_root=cwd,
            target_line=line_no,
            query=query,
        )
        if res.error:
            return {"isError": True, "content": [{"type": "text", "text": res.error}]}
        return {"isError": False, "content": [{"type": "text", "text": res.to_summary_text(include_snippet=True)}]}

    if name == "pacer_canvas":
        cm = CanvasManager(cwd)
        state = cm.load()
        return {
            "isError": False,
            "content": [{"type": "text", "text": json.dumps(state.to_dict(), ensure_ascii=False, indent=2)}],
        }

    if name == "pacer_auto":
        goal = str(arguments.get("goal") or "").strip()
        verify_cmd = str(arguments.get("verify_cmd") or "").strip()
        if not goal:
            return {"isError": True, "content": [{"type": "text", "text": "Error: 'goal' is required."}]}

        from .model_workflow import plan_with_gemini4
        try:
            plan = plan_with_gemini4(goal, cwd, allow_gemini38_fallback=True)
        except RuntimeError as exc:
            return {"isError": True, "content": [{"type": "text", "text": f"规划未完成：{exc}"}]}
        planner_notice = (
            "Planner: Gemini 3.8 fallback (planning only; no coding)"
            if getattr(plan, "planner_backend", None) == "gemini38"
            else "Planner: Gemini 4"
        )
        if verify_cmd:
            is_safe, safety_err = validate_safe_verification_command(verify_cmd, repo_root=cwd)
            if not is_safe:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Error: verify_cmd rejected by safety policy: {safety_err}"}],
                }
            for t in plan.tasks:
                if t.role in ("coder", "verifier"):
                    t.verification_command = verify_cmd

        logs: list[str] = []
        orchestrator = SubagentOrchestrator(
            plan,
            workspace_root=cwd,
            session_source="mcp",
            on_progress=lambda task, msg: logs.append(f"[{task.id}|{task.role}|{task.backend}] {msg}"),
        )
        summary = orchestrator.run()
        patched = []
        for t in plan.tasks:
            for pf in t.patched_files:
                if pf not in patched:
                    patched.append(pf)
        acceptance_tests = list(dict.fromkeys(summary.get("acceptance_test_files") or []))

        report_lines = [
            f"Pacer Auto Status: {'SUCCESS' if summary['success'] else 'FAILED'} ({summary['elapsed_seconds']:.1f}s)",
            f"Goal: {goal}",
            planner_notice,
            f"Completed Tasks: {summary['completed_tasks']}/{summary['total_tasks']}",
            f"Patched Files: {', '.join(patched) if patched else '(none)'}",
            f"Locked Acceptance Tests: {', '.join(acceptance_tests) if acceptance_tests else '(none)'}",
            "Execution Log:",
            *[f"  {line}" for line in logs[-10:]],
        ]
        return {
            "isError": not summary["success"],
            "content": [{"type": "text", "text": "\n".join(report_lines)}],
        }

    if name == "begin_pacer_task":
        goal = str(arguments.get("goal") or "").strip()
        cm = CanvasManager(cwd)
        cm.update_goal(goal, status="in_progress", reset_scratchpad=True)

        for ws in [arguments.get("workspace_root"), Path(cwd) / ".agent-workspace", cwd]:
            if not ws:
                continue
            ws_path = Path(ws).expanduser().resolve()
            try:
                from datetime import datetime, timezone
                from .pacer_launch_context import read_active_launch, update_active_launch, _task_goal_digest
                active = read_active_launch(ws_path)
                if active:
                    update_active_launch(
                        ws_path,
                        task_trigger={
                            "schema_version": 1,
                            "required": True,
                            "status": "triggered",
                            "expected_event": "begin_pacer_task",
                            "observed_event": "begin_pacer_task",
                            "triggered_at": datetime.now(timezone.utc).isoformat(),
                        },
                        task_lifecycle={
                            "schema_version": 1,
                            "status": "running",
                            "goal_digest": _task_goal_digest(goal),
                            "started_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    try:
                        from .pacer_events import append_pacer_event
                        append_pacer_event(
                            ws_path,
                            "task_trigger_observed",
                            {"event": "begin_pacer_task", "goal": goal},
                        )
                    except Exception:
                        pass
                    break
            except Exception:
                pass

        payload = {
            "status": "ready",
            "goal": goal,
            "task_contract": {
                "goal": goal,
                "requirements": [{"id": "R1", "text": goal}],
            },
        }
        return {"isError": False, "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}

    if name == "get_pacer_memory":
        cm = CanvasManager(cwd)
        state = cm.load()
        return {
            "isError": False,
            "content": [{"type": "text", "text": json.dumps(state.to_dict(), ensure_ascii=False)}],
        }

    if name == "complete_pacer_task":
        goal = str(arguments.get("goal") or "").strip()
        steps = arguments.get("steps") if isinstance(arguments.get("steps"), list) else []
        repo_cmd = choose_verification_command(cwd)
        cmd = repo_cmd or ""
        if not cmd and steps and isinstance(steps[0], dict):
            argv = steps[0].get("argv")
            if isinstance(argv, list) and argv:
                candidate = " ".join(str(x) for x in argv).strip()
                if re.search(r"\b(pytest|python\s+-m\s+pytest|npm\s+test|cargo\s+test|go\s+test)\b", candidate):
                    cmd = candidate
        if not cmd:
            argv = [sys.executable, "-m", "pytest", "-q"]
            cmd = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)

        tampered_tests = changed_test_files(repo_root=cwd)
        tampered_chain = changed_acceptance_chain_files(repo_root=cwd, command=cmd)

        verif = run_command_verification(command=cmd, repo_root=cwd)
        passed = (verif.get("verdict") == "pass") and not tampered_tests and not tampered_chain
        cm = CanvasManager(cwd)

        can_trust = False
        audit_report: dict[str, Any] | None = None
        lifecycle_workspace: Path | None = None

        for ws in [arguments.get("workspace_root"), Path(cwd) / ".agent-workspace", cwd]:
            if not ws:
                continue
            ws_path = Path(ws).expanduser().resolve()
            try:
                from .pacer_launch_context import read_active_launch
                active = read_active_launch(ws_path)
                if active:
                    lifecycle_workspace = ws_path
                    try:
                        from .task_review import audit_task_completion
                        audit_res = audit_task_completion(
                            launch_goal=str(active.get("launch_goal") or goal),
                            submitted_goal=goal,
                            summary=str(arguments.get("summary") or ("Verification passed" if passed else "Failed")),
                            completion_evidence=arguments.get("completion_evidence") or {},
                            requested_steps=steps,
                            repo_root=cwd,
                            task_contract=active.get("task_contract"),
                            source_baseline=active.get("source_baseline"),
                            verification=verif,
                        )
                        audit_report = audit_res
                        trust_verdict = str(audit_res.get("trust") or audit_res.get("user_report", {}).get("can_trust") or "")
                        if passed and trust_verdict in ("yes", "with_limits"):
                            can_trust = True
                    except Exception:
                        pass
                    break
            except Exception:
                pass

        if not passed:
            can_trust = False

        trusted_completion = passed and can_trust
        cm.update_goal(goal, acceptance_cmd=cmd, status="completed" if trusted_completion else "failed")

        if lifecycle_workspace is not None:
            try:
                from datetime import datetime, timezone
                from .pacer_launch_context import update_active_launch
                update_active_launch(
                    lifecycle_workspace,
                    task_lifecycle={
                        "schema_version": 1,
                        "status": "completed" if trusted_completion else "failed",
                        "ended_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                try:
                    from .pacer_events import append_pacer_event
                    append_pacer_event(
                        lifecycle_workspace,
                        "task_completed" if trusted_completion else "task_failed",
                        {
                            "event": "complete_pacer_task",
                            "goal": goal,
                            "passed": trusted_completion,
                            "tests_passed": passed,
                            "can_trust": can_trust,
                        },
                    )
                except Exception:
                    pass
            except Exception:
                pass

        result_payload = {
            "status": "completed" if trusted_completion else "failed",
            "can_trust": can_trust,
            "goal": goal,
            "verification": verif,
            "user_report_markdown": (
                f"### Pacer Verification: {'PASSED' if (passed and can_trust) else 'FAILED'}\n"
                f"- **Goal**: {goal}\n"
                f"- **Command**: `{cmd}`\n"
                f"- **Can Trust**: {'Yes' if can_trust else 'No'}\n"
            ),
        }
        if audit_report:
            result_payload["audit_report"] = audit_report

        return {"isError": not (passed and can_trust), "content": [{"type": "text", "text": json.dumps(result_payload, ensure_ascii=False)}]}

    return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}


def handle_jsonrpc_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Process a single JSON-RPC 2.0 request/notification and return the response dict (or None for notifications)."""
    method = str(msg.get("method") or "")
    msg_id = msg.get("id")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

    # Notifications have no 'id'
    if msg_id is None:
        return None

    try:
        if method == "initialize":
            client_proto = str(params.get("protocolVersion") or MCP_PROTOCOL_VERSION)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": client_proto,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "pacer-mcp", "version": "2.0.0"},
                },
            }

        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": MCP_TOOLS}}

        if method == "tools/call":
            tool_name = str(params.get("name") or "")
            tool_args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            try:
                tool_result = _handle_tool_call(tool_name, tool_args)
            except Exception as exc:
                tool_result = {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Tool '{tool_name}' failed: {str(exc)[:4000]}"}],
                }
            return {"jsonrpc": "2.0", "id": msg_id, "result": tool_result}

        if method in ("resources/list", "prompts/list"):
            key = "resources" if "resources" in method else "prompts"
            return {"jsonrpc": "2.0", "id": msg_id, "result": {key: []}}

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32603, "message": f"Internal error: {exc}"},
        }


def serve_stdio() -> int:
    """Run the MCP stdio loop supporting both line-delimited JSON and Content-Length framing."""
    stdin = sys.stdin.buffer
    real_stdout = sys.stdout.buffer
    # Redirect high-level sys.stdout to sys.stderr so any accidental print() doesn't corrupt JSON-RPC transport
    sys.stdout = sys.stderr

    while True:
        first_line = stdin.readline()
        if not first_line:
            break

        stripped = first_line.strip()
        if not stripped:
            continue

        use_content_length = False
        if stripped.lower().startswith(b"content-length:"):
            use_content_length = True
            try:
                length = int(stripped.split(b":", 1)[1].strip())
            except ValueError:
                continue
            if length < 0 or length > 16 * 1024 * 1024:
                continue
            # Consume remaining headers until blank line
            while True:
                hdr = stdin.readline()
                if not hdr or hdr.strip() == b"":
                    break
            body = stdin.read(length)
            if not body:
                break
            raw_json = body.decode("utf-8", errors="replace")
        else:
            raw_json = stripped.decode("utf-8", errors="replace")

        try:
            msg = json.loads(raw_json)
        except Exception:
            continue

        if not isinstance(msg, dict):
            continue

        resp = handle_jsonrpc_message(msg)
        if resp is not None:
            payload_bytes = json.dumps(resp, ensure_ascii=False).encode("utf-8")
            if use_content_length:
                header = f"Content-Length: {len(payload_bytes)}\r\n\r\n".encode("ascii")
                real_stdout.write(header + payload_bytes)
            else:
                real_stdout.write(payload_bytes + b"\n")
            real_stdout.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(serve_stdio())
