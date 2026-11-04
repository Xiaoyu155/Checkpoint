"""Pacer as a local Anthropic-compatible model for Claude Code.

The model endpoint owns execution. It never asks the frontend model to call
Pacer or returns tool calls that let that frontend take over implementation.
Other model IDs are relayed to the user's existing provider unchanged.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

MODEL_ID = "pacer"
DEFAULT_PORT = 8091
MAX_BODY = 32 * 1024 * 1024
MODEL_PROGRESS_HEARTBEAT_INTERVAL_SECONDS = 10.0
DEFAULT_RUN_TIMEOUT_SECONDS = 8 * 60 * 60
MAX_RUN_TIMEOUT_SECONDS = 8 * 60 * 60
SSE_POLL_INTERVAL_SECONDS = 5.0
RESULT_PUBLISH_TIMEOUT_SECONDS = 1.0
HOP_HEADERS = {"host", "connection", "content-length", "transfer-encoding", "keep-alive", "proxy-connection"}


def config_path() -> Path:
    return Path.home() / ".pacer" / "model-gateway" / "config.json"


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(block.get("text", "")) for block in content
                         if isinstance(block, dict) and block.get("type") in {"text", "input_text", "output_text"})
    return ""


def latest_prompt(body: dict[str, Any]) -> str:
    """Use the latest user utterance. Do not concatenate earlier turns."""
    from .context_profile import extract_latest_utterance

    source = body.get("messages")
    messages = [m for m in source if isinstance(m, dict)] if isinstance(source, list) else []
    users = [m for m in messages if m.get("role") == "user"]
    if not users:
        raise ValueError("Pacer requires a final user message.")
    content = users[-1].get("content", "")
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        raise ValueError("Start a new user turn after switching to Pacer; it does not continue another model's tool call.")
    prompt = re.sub(r"<system-reminder>.*?</system-reminder>", "", extract_latest_utterance(messages), flags=re.S).strip()
    if not prompt:
        raise ValueError("A text task is required; image-only requests are not supported by this adapter.")
    return prompt


def resolve_workspace(body: dict[str, Any], config: dict[str, Any], header: str = "") -> Path:
    """Use the Code environment's existing project directory as this request's root."""
    default = Path(config["workspace_root"]).resolve()
    system = text_content(body.get("system", "")) + "\n" + "\n".join(
        text_content(m.get("content")) for m in body.get("messages", [])
        if isinstance(m, dict) and m.get("role") == "system")
    found = re.search(r"(?:Primary working directory|Working directory|Current working directory):\s*([^\r\n<]+)", system, re.I)
    tagged = re.search(r"<cwd>\s*([^<]+)\s*</cwd>", system, re.I)
    raw = header or (found.group(1).strip().strip('`') if found else "") or (tagged.group(1).strip() if tagged else "")
    root = Path(raw).expanduser().resolve() if raw else default
    if not root.is_dir():
        raise ValueError(f"Workspace does not exist: {root}")
    if config.get("allow_all_workspaces", True) is not True:
        allowed = [Path(p).resolve() for p in config.get("allowed_roots", [str(default)])]
        if not any(root == base or root.is_relative_to(base) for base in allowed):
            raise ValueError(f"Pacer rejected workspace outside its configured allowed_roots: {root}")
    return root


def claims_execution(model_name: str) -> bool:
    """Pacer owns its own id and the Gemini 3.8 picker entry that used to grab tasks."""
    name = str(model_name or "").strip().lower().replace("_", "-")
    if name in {MODEL_ID, "checkpoint", "visual-agent"}:
        return True
    if "antigravity" in name or "反重力" in name:
        return True
    return "gemini" in name and ("3.8" in name or "3-8" in name or "flash-low" in name or "flash-tiered" in name)


def is_status_request(prompt: str) -> bool:
    return prompt.strip().lower().rstrip(" .!") in {"/models", "/status", "\u6a21\u578b\u72b6\u6001", "\u67e5\u770b\u6a21\u578b\u72b6\u6001"}


def is_task(prompt: str) -> bool:
    if re.search(r"只回复|仅回复|reply only|say exactly", prompt, re.I):
        return False
    if re.match(r"\s*(?:解释一下|什么是|请解释|what is|explain the concept)", prompt, re.I):
        return False
    if re.search(r"继续|接着做|接着把|许愿|托管|狗粮", prompt):
        return True
    chinese = (
        r"实现|修复|优化|修改|改动|改成|改为|换成|设为|排查|调试|开发|重构|跑测试|运行测试|部署|接入|集成|安装|添加|增加|写代码|编写"
        r"|加|加上|补充|更新|删|删除|移除|调整|校验|规则|函数|模块|接口|单测|测试|跑通|刚才|上一个"
        r"|分析|调查|诊断|检查|定位|看一下|看看|查一下|研究|找出|点不动|不工作|不能用|打不开"
        r"|报错|崩溃|故障|亏损|亏钱|出了问题|有问题|问题很大"
    )
    english = (
        r"\b(?:add|create|extend|enhance|improve|fix|implement|debug|refactor|build|write|"
        r"change|modify|update|test|deploy|install|remove|delete|migrate|upgrade|optimize|"
        r"integrate|support|document|analyze|investigate|inspect|check|review)\b"
    )
    return bool(re.search(chinese + "|" + english, prompt, re.I))


def legacy_execution_running(root: Path) -> bool:
    import psutil
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        if process.pid == os.getpid():
            continue
        args = process.info.get("cmdline") or []
        if "visual_agent.pacer_model" in args and "serve" in args:
            try:
                config_file = Path(args[args.index("--config") + 1]) if "--config" in args else config_path()
                other_config = json.loads(config_file.read_text(encoding="utf-8"))
                if Path(other_config["workspace_root"]).resolve() == root.resolve():
                    return True
            except (OSError, ValueError, KeyError, IndexError):
                continue
        if "visual_agent.cli" not in args or "auto" not in args:
            continue
        for flag in ("--workspace-root", "-w"):
            if flag in args:
                index = args.index(flag) + 1
                if index < len(args):
                    other = Path(args[index]).resolve()
                    if root == other or root.is_relative_to(other) or other.is_relative_to(root):
                        return True
    return False


def run_engine(prompt: str, root: Path, progress: Callable[[str], None]) -> tuple[str, dict[str, Any]]:
    """Bridge to the existing engine; internal collaboration is unchanged."""
    from visual_agent.subagent_orchestrator import SubagentOrchestrator
    from .pacer_trace import record_pacer_trace
    from .wish_intake import append_live_log, format_progress_frame, resolve_wish

    if is_status_request(prompt):
        from .provider_runtime import format_status_table
        return format_status_table(), {"success": True, "status_response": True, "total_tasks": 0}
    decision = resolve_wish(prompt, root)
    if not decision.proceed:
        append_live_log(root, decision.question)
        progress(decision.question + "\n")
        return decision.question + "\n", {"success": True, "awaiting_user": True, "completed_tasks": 0, "total_tasks": 0}
    prompt = decision.goal
    from .session_projection import SessionProjection

    SessionProjection(root).append_event("user_input", content=prompt)
    from .model_workflow import configure_plan, plan_with_gemini4, review_implementation
    try:
        from .dag_scheduler import classify_task_complexity

        explicitly_requests_gemini4 = bool(re.search(
            r"(?:使用|调用|交给|让|用|\buse\b|\busing\b|\bvia\b)\s*(?:网页\s*)?gemini\s*-?\s*4",
            prompt,
            re.I,
        ))
        force_remote_planner = classify_task_complexity(prompt).value == "L3" or explicitly_requests_gemini4
        plan = plan_with_gemini4(
            prompt,
            root,
            allow_gemini38_fallback=True,
            force_remote=force_remote_planner,
        )
    except RuntimeError as exc:
        progress(f"规划未完成：{exc}\n")
        return f"规划未完成：{exc}\n", {"success": False, "planner_status": "blocked", "completed_tasks": 0, "total_tasks": 0}
    routing = configure_plan(plan, root, progress, planner_already_ran=True)
    progress(f"候选源码：{', '.join(decision.files) or '续跑计划'}。预估 {getattr(plan, 'eta_seconds', 0):.0f}s。\n")

    last_forwarded_heartbeat = 0.0

    def report(task: Any, message: str) -> None:
        nonlocal last_forwarded_heartbeat
        if "心跳 #" in message:
            now = time.monotonic()
            if now - last_forwarded_heartbeat < MODEL_PROGRESS_HEARTBEAT_INTERVAL_SECONDS:
                return
            last_forwarded_heartbeat = now
        remaining = max(0, int(getattr(task, "max_retries", 1)) - int(getattr(task, "retries", 0)))
        progress(format_progress_frame(task.id, task.role, task.backend, message, remaining) + "\n")

    repair_instructions = {task.id: task.instructions for task in plan.tasks if getattr(task, "role", "") == "coder"}
    engine = None
    result: dict[str, Any] = {}
    max_adversarial_repairs = 2
    for review_attempt in range(max_adversarial_repairs + 1):
        engine = SubagentOrchestrator(
            plan,
            workspace_root=root,
            on_progress=report,
            heartbeat_interval=1.5,
            session_source="pacer-model",
            show_timeline=True,
            resume=decision.resume if review_attempt == 0 else False,
            allow_gemini4_diagnosis=True,
            defer_trace_recording=True,
            require_red_baseline=False,
            green_incremental=True,
        )
        result = engine.run()
        coder_tasks = [t for t in plan.tasks if getattr(t, "role", "") == "coder"]
        if result.get("success") and coder_tasks and not any(
            r.get("verdict") == "pass" and r.get("exit_code") == 0
            for r in result.get("verification_receipts", [])
        ):
            result["success"] = False
            result["collaboration_error"] = "No successful physical verification receipt; implementation is not accepted."
        if coder_tasks:
            result = review_implementation(plan, result, routing, root, progress)
        else:
            # A local verification-only plan made no code changes to review.
            # Treat the physical verifier receipt as its acceptance result and
            # avoid requiring an unrelated model reviewer for a read-only run.
            result["review"] = {
                "approved": True,
                "status": "not_required_verification_only",
                "issues": [],
                "repair_required": False,
                "sandbox_executed": False,
            }
        review = result.get("review") if isinstance(result.get("review"), dict) else {}
        if not review.get("repair_required") or review_attempt >= max_adversarial_repairs:
            break

        counterexample = review.get("counterexample_result") if isinstance(review.get("counterexample_result"), dict) else {}
        failure_detail = str(
            counterexample.get("output")
            or counterexample.get("error")
            or result.get("collaboration_error")
            or review.get("issues")
            or "Adversarial review rejected the implementation."
        )[:5000]
        expressions = review.get("counter_examples") if isinstance(review.get("counter_examples"), list) else []
        repair_note = (
            "[Adversarial Counter-Example Repair]\n"
            "A different model family produced a boundary assertion that failed against this implementation. "
            "Fix the implementation while preserving the accepted interfaces and existing tests.\n"
            "Counter-example assertions:\n"
            + ("\n".join(f"- {expression}" for expression in expressions) or "- No executable assertion was accepted")
            + "\nFailure evidence / traceback:\n"
            + failure_detail
        )
        progress(f"[review] counter-example failed; returning evidence to coders (repair {review_attempt + 1}/{max_adversarial_repairs}).\n")
        for task in plan.tasks:
            if getattr(task, "role", "") != "coder":
                continue
            task.instructions = f"{repair_instructions.get(task.id, task.instructions)}\n\n{repair_note}"
            task.status = "pending"
            task.output = ""
            task.error = None
            task.retries = 0
            task.patched_files = []

    assert engine is not None
    trace_payload = getattr(engine, "deferred_trace_payload", None)
    if trace_payload is not None:
        trace_payload["status"] = "success" if result.get("success") else "failed"
        review = result.get("review")
        metadata = dict(trace_payload.get("metadata") or {})
        if isinstance(review, dict):
            metadata["review"] = {
                "approved": review.get("approved"),
                "status": review.get("status"),
                "model": review.get("model"),
                "error": review.get("error"),
                "issues": review.get("issues"),
            }
        trace_payload["metadata"] = metadata
        # The reviewer is a real model invocation — count it alongside the
        # coder/planner dispatches so the trace reflects actual model calls.
        if isinstance(review, dict) and review.get("model"):
            trace_payload["model_calls"] = int(trace_payload.get("model_calls") or 0) + 1
            trace_payload["usage_measurement"] = "measured"
        if not result.get("success"):
            trace_payload["error"] = (
                result.get("collaboration_error")
                or trace_payload.get("error")
                or "Final Pacer acceptance failed"
            )
        record_pacer_trace(**trace_payload)
    files = list(dict.fromkeys(
        [f for task in plan.tasks for f in task.patched_files]
        + list(result.get("acceptance_test_files") or [])
    ))
    patched_files = list(dict.fromkeys(
        str(path).replace("\\", "/")
        for task in plan.tasks
        for path in getattr(task, "patched_files", [])
        if str(path).strip()
    ))
    if patched_files:
        from .canvas_manager import CanvasManager
        from .local_indexer import index_file_symbols

        canvas = CanvasManager(root)
        for rel_path in patched_files:
            indexed = index_file_symbols(rel_path, workspace_root=root)
            if not indexed.error:
                canvas.upsert_focus_file(
                    indexed.file_path,
                    symbols=indexed.compact_symbols(),
                    imports=indexed.imports,
                    target_snippet=indexed.focused_snippet,
                )
        canvas.set_recent_focus_files(patched_files)
    else:
        # Preserve this turn's requested targets so a filename-free incremental follow-up
        # stays attached to the intended files, including when a run needs retry.
        from .canvas_manager import CanvasManager

        CanvasManager(root).set_recent_focus_files(decision.files)
    if result.get("success"):
        text = "Pacer 已完成执行。\n"
    else:
        text = "Pacer 执行未通过验收，任务尚未完成。\n"
    text += f"完成子任务：{result.get('completed_tasks', 0)}/{result.get('total_tasks', len(plan.tasks))}\n"
    if files:
        text += "涉及文件：" + "、".join(files) + "\n"
    errors = [f"{t.id}: {t.error}" for t in plan.tasks if t.error]
    if errors:
        text += "失败原因：\n" + "\n".join(errors)[:4000] + "\n"
    if result.get("collaboration_error"):
        text += result["collaboration_error"] + "\n"
    dispatches = list(getattr(engine, "_prompt_dispatches", []))
    trail = [f"planner:{getattr(plan, 'planner_backend', 'local')}"]
    trail.extend(f"{d.get('task_id')}:{d.get('backend')}(retry={d.get('retry', 0)})" for d in dispatches)
    attempts = result.get("api_node_attempts", [])
    result["collaboration_trail"] = trail
    result["api_request_count"] = len(attempts)
    text += "\n协作轨迹: " + " -> ".join(trail) + "\n"
    text += f"写码 API 请求次数: {len(attempts)}；物理验收记录: {len(result.get('verification_receipts', []))}\n"
    text += "详细结果已记录；执行仍由 Pacer 负责，不回退给入口模型继续修改。"
    return text, result


def chat(prompt: str, body: dict[str, Any]) -> str:
    if re.search(r"你是谁|你是什么模型|who are you|what model are you", prompt, re.I):
        return "我是 Pacer，是接入 Claude Code 的任务编排模型入口。开发请求由 Pacer 引擎执行，内部协作模型按引擎配置调用。"
    if re.fullmatch(r"\s*(?:你好|您好|嗨|哈喽|hello|hi|hey)[\s!,.。！~～]*", prompt, re.I) or re.search(
        r"(?:你现在是否|你现在能|你能不能|你可以).*?(?:干活|帮我|处理任务|工作)", prompt
    ):
        return "你好，我是 Pacer，已就绪。你可以直接描述要处理的任务。"
    from visual_agent.grok_bridge import invoke_grok
    conversation = [m for m in body.get("messages", []) if m.get("role") in ("user", "assistant")]
    history = "\n".join(f"{m.get('role')}: {text_content(m.get('content'))}"
                        for m in conversation[-4:])[-6000:]
    system_prompt = (
        "You are Pacer's conversation interface. Be concise. Do not claim to have changed files or run tools. "
        "Coding execution belongs to the Pacer engine, not this conversation."
    )
    grok_error = ""
    try:
        response = invoke_grok(
            history or prompt,
            timeout=60,
            retries=0,
            system_prompt=system_prompt,
        )
        grok_response = str(response.response or "")
        if response.success and grok_response.strip():
            return grok_response
        grok_error = response.error or "Grok returned an empty response"
    except Exception as exc:
        grok_error = str(exc) or exc.__class__.__name__

    from visual_agent.gemini_bridge import invoke_gemini

    fallback_model = os.environ.get("PACER_GEMINI38_FALLBACK_MODEL", "gemini-3.8-flash-low").strip()
    if not fallback_model.lower().startswith(("gemini-3.8", "gemini-3-8")):
        raise RuntimeError(
            f"Grok conversation backend failed: {grok_error}; "
            "Gemini 3.8 fallback model must use the gemini-3.8 family"
        )
    try:
        fallback = invoke_gemini(
            history or prompt,
            model=fallback_model,
            yolo=False,
            output_format="text",
            timeout_seconds=45,
            system_prompt=system_prompt,
        )
    except Exception as exc:
        fallback_error = str(exc) or exc.__class__.__name__
    else:
        fallback_response = str(fallback.response or "")
        if fallback.success and fallback_response.strip():
            return fallback_response
        fallback_error = fallback.error or "Gemini 3.8 returned an empty response"
    raise RuntimeError(
        f"Grok conversation backend failed: {grok_error}; Gemini 3.8 fallback failed: {fallback_error}"
    )


def _openai_to_anthropic_body(body: dict[str, Any]) -> dict[str, Any]:
    """Convert OpenAI /v1/chat/completions request to Anthropic /v1/messages shape."""
    messages: list[dict[str, Any]] = []
    system_text = ""
    for m in body.get("messages", []):
        if isinstance(m, dict):
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                system_text = text_content(content)
            else:
                messages.append({"role": role, "content": content})
    result: dict[str, Any] = {"model": MODEL_ID, "messages": messages}
    if system_text:
        result["system"] = system_text
    if body.get("stream"):
        result["stream"] = True
    return result


def _openai_responses_to_anthropic_body(body: dict[str, Any]) -> dict[str, Any]:
    """Convert OpenAI /v1/responses request to Anthropic /v1/messages shape."""
    # Responses API uses 'input' instead of 'messages'
    inputs = body.get("input", [])
    messages: list[dict[str, Any]] = []
    system_text = ""
    if isinstance(inputs, list):
        for item in inputs:
            if isinstance(item, dict):
                role = item.get("role", "user")
                content = item.get("content", "")
                if isinstance(content, list):
                    content = [
                        {"type": "text", "text": part.get("text", "")}
                        if isinstance(part, dict) and part.get("type") == "input_text"
                        else part
                        for part in content
                    ]
                if role == "system":
                    system_text = text_content(content)
                else:
                    messages.append({"role": role, "content": content})
    elif isinstance(inputs, str):
        messages.append({"role": "user", "content": inputs})
    # Also check top-level instructions
    instructions = body.get("instructions") or body.get("system")
    if instructions:
        system_text = text_content(instructions)
    result: dict[str, Any] = {"model": MODEL_ID, "messages": messages}
    if system_text:
        result["system"] = system_text
    if body.get("stream"):
        result["stream"] = True
    return result


class PacerModelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], config: dict[str, Any], *, engine: Callable = run_engine, chat_backend: Callable = chat):
        import portalocker
        root = Path(config["workspace_root"]).resolve()
        lock_dirs = {Path(config["_config_path"]).resolve().parent} if config.get("_config_path") else {config_path().parent / ("workspace-" + __import__("hashlib").sha256(str(root).encode("utf-8")).hexdigest()[:16])}
        self._serve_locks = []
        self._pid_paths = []
        try:
            for directory in sorted(lock_dirs):
                directory.mkdir(parents=True, exist_ok=True)
                stream = (directory / "serve.lock").open("a+")
                try:
                    portalocker.lock(stream, portalocker.LOCK_EX | portalocker.LOCK_NB)
                except portalocker.LockException as exc:
                    stream.close()
                    raise RuntimeError("Pacer model serve is already running for this configuration or workspace") from exc
                self._serve_locks.append(stream)
                pid_path = directory / "serve.pid"
                pid_path.write_text(str(os.getpid()), encoding="ascii")
                self._pid_paths.append(pid_path)
            super().__init__(address, PacerModelHandler)
        except BaseException:
            self._release_serve_locks()
            raise
        self.config = config
        self.engine = engine
        self.chat_backend = chat_backend
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pacer-model")
        try:
            run_timeout = float(config.get("run_timeout_seconds", DEFAULT_RUN_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            run_timeout = DEFAULT_RUN_TIMEOUT_SECONDS
        if not math.isfinite(run_timeout) or run_timeout <= 0:
            run_timeout = DEFAULT_RUN_TIMEOUT_SECONDS
        self.run_timeout_seconds = min(run_timeout, MAX_RUN_TIMEOUT_SECONDS)
        self.run_lock = threading.Lock()
        self.amendment_lock = threading.Lock()
        self.amendment_queue: list[str] = []
        self.active_workspace_root: Path | None = None
        self.amendment_count = 0
        self.amendment_limit = 8

    def server_close(self) -> None:
        try:
            super().server_close()
            # Keep the instance lock until active engine jobs have stopped writing.
            self.executor.shutdown(wait=True, cancel_futures=True)
        finally:
            self._release_serve_locks()

    def _release_serve_locks(self) -> None:
        import portalocker
        for path in self._pid_paths:
            path.unlink(missing_ok=True)
        self._pid_paths.clear()
        for stream in self._serve_locks:
            portalocker.unlock(stream)
            stream.close()
        self._serve_locks.clear()


class PacerModelHandler(BaseHTTPRequestHandler):
    server: PacerModelServer
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        # Do not log user prompts, authentication headers, or query strings.
        print(f"[pacer-model] {self.command} {urllib.parse.urlsplit(self.path).path}", flush=True)

    def _json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = str(self.server.config.get("auth_token", ""))
        supplied = self.headers.get("x-api-key", "") or self.headers.get("Authorization", "").removeprefix("Bearer ")
        if expected and hmac.compare_digest(supplied, expected):
            return True
        if urllib.parse.urlsplit(self.path).path in {"/api/model-config", "/api/models/probe", "/api/extension"}:
            from .model_admin import management_token

            try:
                return hmac.compare_digest(supplied, management_token(self.server.config))
            except (KeyError, TypeError):
                return False
        return False

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"type": "error", "error": {"type": "invalid_request_error" if status < 500 else "api_error", "message": message}})

    def do_GET(self) -> None:
        from .model_admin import handle
        if handle(self, "GET"):
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health":
            self._json(200, {"service": "pacer-model", "status": "ready", "model": MODEL_ID,
                             "pid": os.getpid(), "busy": self.server.run_lock.locked()})
            return
        if not self._authorized():
            self._error(401, "Authentication required")
            return
        if path == "/v1/models":
            item = {"id": MODEL_ID, "type": "model", "display_name": "Pacer · 多模型任务引擎", "created_at": "2026-10-30T00:00:00Z"}
            self._json(200, {"data": [item], "has_more": False, "first_id": MODEL_ID, "last_id": MODEL_ID})
        elif path == "/v1/models/pacer":
            self._json(200, {"id": MODEL_ID, "type": "model", "display_name": "Pacer"})
        else:
            self._relay(b"")

    def do_POST(self) -> None:
        from .model_admin import handle
        if not self._authorized():
            self._error(401, "Authentication required")
            return
        if handle(self, "POST"):
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                self._error(413, "Invalid request body length")
                return
            raw = self.rfile.read(length)
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("Request body must be an object")
        except (ValueError, UnicodeError) as exc:
            self._error(400, str(exc))
            return
        if not claims_execution(str(body.get("model") or "")):
            self._relay(raw)
            return
        body["model"] = MODEL_ID
        path = urllib.parse.urlsplit(self.path).path
        original_path = path
        protocol = {
            "/v1/chat/completions": "openai-chat",
            "/v1/responses": "openai-responses",
        }.get(original_path, "anthropic")
        if path == "/v1/messages/count_tokens":
            self._json(200, {"input_tokens": max(1, len(raw) // 3)})
            return
        # Normalize OpenAI protocol paths to the shared Anthropic-style handler
        if path == "/v1/chat/completions":
            # Convert OpenAI chat format to Anthropic messages format
            body = _openai_to_anthropic_body(body)
            path = "/v1/messages"
        elif path == "/v1/responses":
            # Convert OpenAI responses format to Anthropic messages format
            body = _openai_responses_to_anthropic_body(body)
            path = "/v1/messages"
        if path != "/v1/messages":
            self._error(404, "Pacer supports /v1/messages (Anthropic), /v1/chat/completions and /v1/responses (OpenAI)")
            return
        try:
            prompt = latest_prompt(body)
            root = resolve_workspace(body, self.server.config, self.headers.get("x-pacer-workspace", ""))
        except (ValueError, OSError) as exc:
            self._error(400, str(exc))
            return
        if not is_task(prompt) or is_status_request(prompt):
            try:
                if is_status_request(prompt):
                    from .provider_runtime import format_status_table
                    answer = format_status_table()
                else:
                    answer = str(self.server.chat_backend(prompt, body))
            except Exception as exc:
                self._error(502, f"Pacer conversation backend failed: {exc}")
                return
            message_id = "pm-chat-" + uuid4().hex
            if body.get("stream"):
                events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=2)
                events.put_nowait(("done", answer))
                self._stream(events, message_id, protocol=protocol)
            else:
                self._json(200, self._protocol_response(message_id, answer, protocol))
            return
        if not self.server.run_lock.acquire(blocking=False):
            conflict = False
            queue_full = False
            with self.server.amendment_lock:
                active_root = self.server.active_workspace_root
                if active_root is None or active_root != root.resolve():
                    conflict = True
                elif self.server.amendment_count >= self.server.amendment_limit:
                    queue_full = True
                else:
                    self.server.amendment_queue.append(prompt)
                    self.server.amendment_count += 1
            if conflict:
                self._error(409, "A Pacer run is active or finishing; only follow-ups for its workspace can be queued.")
            elif queue_full:
                self._error(429, "The active Pacer run has reached its follow-up request limit; retry later.")
            else:
                self._json(200, self._protocol_response("pm-amend", f"已进入当前 Pacer 工作流，这一步结束后处理：{prompt}\n", protocol))
            return
        try:
            if legacy_execution_running(root):
                self.server.run_lock.release()
                self._error(409, "An existing Pacer Auto process is writing this workspace. Let it finish first.")
                return
        except Exception:
            self.server.run_lock.release()
            self._error(503, "Unable to check the workspace execution owner")
            return
        with self.server.amendment_lock:
            self.server.active_workspace_root = root.resolve()
            self.server.amendment_count = 0
        events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=256)
        client_disconnected = threading.Event()
        request_deadline = time.monotonic() + self.server.run_timeout_seconds
        run_id = "pm-" + uuid4().hex
        record = Path(self.server.config.get("runs_dir") or root / ".pacer/model-gateway/runs") / f"{run_id}.json"

        def progress(message: str) -> None:
            if client_disconnected.is_set():
                return
            try:
                events.put_nowait(("text", message))
            except queue.Full:
                # Streaming backpressure may discard progress, never the result.
                return

        def execute() -> None:
            result: dict[str, Any] = {}
            active_prompt = prompt
            try:
                write_json(record, {"id": run_id, "status": "running", "kind": "wish", "workspace_root": str(root), "started_at": datetime.now(timezone.utc).isoformat()})
                progress(f"Pacer 已接管任务（{run_id}）。\n")
                answer, result = self.server.engine(prompt, root, progress)
                answers = [answer]
                while True:
                    with self.server.amendment_lock:
                        if not self.server.amendment_queue:
                            if self.server.active_workspace_root == root.resolve():
                                self.server.active_workspace_root = None
                            break
                        nxt = self.server.amendment_queue.pop(0)
                    active_prompt = nxt
                    progress(f"同一窗口的下一句进入工作流：{nxt}\n")
                    answer, result = self.server.engine(nxt, root, progress)
                    answers.append(answer)
                answer = "\n".join(answers)
                if result.get("awaiting_user"):
                    status = "awaiting_input"
                elif result.get("success"):
                    status = "completed"
                else:
                    status = "failed"
                write_json(record, {"id": run_id, "status": status, "kind": "wish", "workspace_root": str(root), "finished_at": datetime.now(timezone.utc).isoformat(), "answer": answer, "result": result, "followed_utterances": max(0, len(answers) - 1)})
                self._publish_result(events, answer, client_disconnected, request_deadline)
            except Exception as exc:
                with self.server.amendment_lock:
                    queued_amendments = list(self.server.amendment_queue)
                    self.server.amendment_queue.clear()
                    if self.server.active_workspace_root == root.resolve():
                        self.server.active_workspace_root = None
                answer = f"Pacer 执行失败，任务尚未完成：{exc}"
                failed_amendments = []
                if active_prompt != prompt:
                    failed_amendments.append({"prompt": active_prompt, "status": "failed", "error": str(exc)})
                failed_amendments.extend(
                    {"prompt": queued, "status": "failed", "error": "主运行异常终止，未执行"}
                    for queued in queued_amendments
                )
                if failed_amendments:
                    answer += "\n以下 follow-up amendment 明确失败，请重新提交："
                    answer += "".join(f"\n- {item['prompt']}：{item['error']}" for item in failed_amendments)
                try:
                    write_json(record, {
                        "id": run_id,
                        "status": "failed",
                        "error": str(exc),
                        "failed_amendments": failed_amendments,
                        "dropped_amendments": len(failed_amendments),
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                    })
                finally:
                    self._publish_result(events, answer, client_disconnected, request_deadline)
            finally:
                with self.server.amendment_lock:
                    if self.server.active_workspace_root == root.resolve():
                        self.server.active_workspace_root = None
                    # Never carry a queued prompt into the next workspace run.
                    self.server.amendment_queue.clear()
                    self.server.amendment_count = 0
                self.server.run_lock.release()

        try:
            self.server.executor.submit(execute)
        except Exception as exc:
            with self.server.amendment_lock:
                queued_amendments = list(self.server.amendment_queue)
                self.server.amendment_queue.clear()
                self.server.amendment_count = 0
                if self.server.active_workspace_root == root.resolve():
                    self.server.active_workspace_root = None
            try:
                write_json(record, {
                    "id": run_id,
                    "status": "failed",
                    "error": f"Pacer executor rejected the run: {exc}",
                    "failed_amendments": [
                        {"prompt": item, "status": "failed", "error": "主任务未能启动"}
                        for item in queued_amendments
                    ],
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                })
            except OSError:
                pass
            finally:
                self.server.run_lock.release()
            self._error(503, "Pacer executor is unavailable; the run was not started")
            return
        if body.get("stream"):
            self._stream(events, run_id, protocol=protocol, disconnected=client_disconnected, deadline=request_deadline)
        else:
            chunks: list[str] = []
            while True:
                remaining = request_deadline - time.monotonic()
                if remaining <= 0:
                    client_disconnected.set()
                    self._error(504, "Pacer run exceeded its wall-clock deadline")
                    return
                try:
                    kind, data = events.get(timeout=remaining)
                except queue.Empty:
                    client_disconnected.set()
                    self._error(504, "Pacer run exceeded its wall-clock deadline")
                    return
                if kind == "done":
                    chunks.append(data)
                    break
                chunks.append(data)
            self._json(200, self._protocol_response(run_id, "".join(chunks), protocol))

    @staticmethod
    def _publish_result(
        events: queue.Queue,
        answer: str,
        disconnected: threading.Event,
        deadline: float,
    ) -> bool:
        # A stalled consumer must not hold the workspace lock for the entire run budget.
        deadline = min(deadline, time.monotonic() + RESULT_PUBLISH_TIMEOUT_SECONDS)
        while not disconnected.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                events.put(("done", answer), timeout=min(0.25, remaining))
                return True
            except queue.Full:
                continue
        return False

    def _message(self, run_id: str, text: str) -> dict[str, Any]:
        # These are adapter estimates, not bills or usage claims for child models.
        return {"id": run_id, "type": "message", "role": "assistant", "model": MODEL_ID,
                "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": max(1, len(text) // 3)}}

    def _protocol_response(self, run_id: str, text: str, protocol: str) -> dict[str, Any]:
        if protocol == "openai-chat":
            return {
                "id": "chatcmpl-" + run_id.replace("-", "_"),
                "object": "chat.completion",
                "created": int(time.time()),
                "model": MODEL_ID,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }],
                # Pacer does not aggregate upstream provider usage across a run.
                "usage": None,
            }
        if protocol == "openai-responses":
            response_id = "resp_" + run_id.replace("-", "_")
            item = {
                "id": "msg_" + run_id.replace("-", "_"),
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": [], "logprobs": []}],
            }
            return {
                "id": response_id,
                "object": "response",
                "created_at": int(time.time()),
                "status": "completed",
                "completed_at": int(time.time()),
                "error": None,
                "incomplete_details": None,
                "model": MODEL_ID,
                "output": [item],
                "output_text": text,
                # Provider usage is not available at this adapter boundary.
                "usage": None,
            }
        return self._message(run_id, text)

    def _stream(
        self,
        events: queue.Queue,
        run_id: str,
        *,
        protocol: str = "anthropic",
        disconnected: threading.Event | None = None,
        deadline: float | None = None,
    ) -> None:
        """Stream events in Anthropic or OpenAI SSE format."""
        is_openai_chat = protocol == "openai-chat"
        is_openai_responses = protocol == "openai-responses"

        def bound_write_timeout() -> None:
            connection = getattr(self, "connection", None)
            if connection is not None:
                connection.settimeout(max(0.001, min(SSE_POLL_INTERVAL_SECONDS, deadline - time.monotonic())))

        def send_sse(data: dict[str, Any]) -> None:
            if is_openai_chat:
                payload = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            elif is_openai_responses:
                payload = f"event: {data.get('type', 'message')}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            else:
                name = data.get("type", "message")
                payload = f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            bound_write_timeout()
            self.wfile.write(payload.encode("utf-8"))
            self.wfile.flush()

        if deadline is None:
            timeout = getattr(getattr(self, "server", None), "run_timeout_seconds", DEFAULT_RUN_TIMEOUT_SECONDS)
            deadline = time.monotonic() + timeout
        timed_out = False
        try:
            bound_write_timeout()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Pacer-Run-ID", run_id)
            self.send_header("X-Pacer-Usage", "adapter-estimate")
            self.end_headers()

            output_text: list[str] = []
            response_id = "resp_" + run_id.replace("-", "_")
            item_id = "msg_" + run_id.replace("-", "_")
            if is_openai_chat:
                # OpenAI chat.completions SSE format
                send_sse({"id": "chatcmpl-" + run_id.replace("-", "_"), "object": "chat.completion.chunk",
                          "created": int(time.time()), "model": MODEL_ID,
                          "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
            elif is_openai_responses:
                created = int(time.time())
                send_sse({"type": "response.created", "sequence_number": 0, "response": {
                    "id": response_id, "object": "response", "created_at": created,
                    "status": "in_progress", "completed_at": None, "error": None,
                    "incomplete_details": None, "model": MODEL_ID, "output": [], "usage": None,
                }})
                send_sse({"type": "response.output_item.added", "sequence_number": 1, "output_index": 0,
                          "item": {"id": item_id, "type": "message", "status": "in_progress",
                                   "role": "assistant", "content": []}})
                send_sse({"type": "response.content_part.added", "sequence_number": 2, "item_id": item_id,
                          "output_index": 0, "content_index": 0,
                          "part": {"type": "output_text", "text": "", "annotations": [], "logprobs": []}})
            else:
                message = self._message(run_id, "")
                message.update(content=[], stop_reason=None)
                send_sse({"type": "message_start", "message": message})
                send_sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            output_chars = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    if disconnected is not None:
                        disconnected.set()
                    break
                try:
                    kind, text = events.get(timeout=min(SSE_POLL_INTERVAL_SECONDS, remaining))
                except queue.Empty:
                    if time.monotonic() >= deadline:
                        timed_out = True
                        if disconnected is not None:
                            disconnected.set()
                        break
                    bound_write_timeout()
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    if not is_openai_chat and not is_openai_responses:
                        send_sse({"type": "ping"})
                    continue
                output_chars += len(text)
                output_text.append(text)
                if is_openai_chat:
                    send_sse({"id": "chatcmpl-" + run_id.replace("-", "_"), "object": "chat.completion.chunk",
                              "created": int(time.time()), "model": MODEL_ID,
                              "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})
                elif is_openai_responses:
                    send_sse({"type": "response.output_text.delta", "sequence_number": 2 + len(output_text),
                              "item_id": item_id, "output_index": 0, "content_index": 0, "delta": text})
                else:
                    send_sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})
                if kind == "done":
                    break
            if timed_out:
                timeout_error = {"type": "timeout_error", "message": "Pacer run exceeded its wall-clock deadline"}
                if is_openai_chat:
                    send_sse({"error": timeout_error, "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]})
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                else:
                    send_sse({"type": "error", "error": timeout_error})
                return
            final_text = "".join(output_text)
            if is_openai_chat:
                send_sse({"id": "chatcmpl-" + run_id.replace("-", "_"), "object": "chat.completion.chunk",
                          "created": int(time.time()), "model": MODEL_ID,
                          "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            elif is_openai_responses:
                sequence = 3 + len(output_text)
                part = {"type": "output_text", "text": final_text, "annotations": [], "logprobs": []}
                item = {"id": item_id, "type": "message", "status": "completed", "role": "assistant", "content": [part]}
                send_sse({"type": "response.output_text.done", "sequence_number": sequence,
                          "item_id": item_id, "output_index": 0, "content_index": 0, "text": final_text,
                          "logprobs": []})
                send_sse({"type": "response.content_part.done", "sequence_number": sequence + 1,
                          "item_id": item_id, "output_index": 0, "content_index": 0, "part": part})
                send_sse({"type": "response.output_item.done", "sequence_number": sequence + 2,
                          "output_index": 0, "item": item})
                response = {
                    "id": response_id, "object": "response", "created_at": int(time.time()),
                    "status": "completed", "completed_at": int(time.time()), "error": None,
                    "incomplete_details": None, "model": MODEL_ID, "output": [item], "usage": None,
                }
                send_sse({"type": "response.completed", "sequence_number": sequence + 3, "response": response})
            else:
                send_sse({"type": "content_block_stop", "index": 0})
                send_sse({"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                          "usage": {"output_tokens": max(1, output_chars // 3)}})
                send_sse({"type": "message_stop"})
        except (ConnectionError, OSError):
            if disconnected is not None:
                disconnected.set()

    def _relay(self, raw: bytes) -> None:
        upstream = str(self.server.config.get("upstream_url", "")).rstrip("/")
        if not upstream or urllib.parse.urlsplit(upstream).port == self.server.server_port:
            self._error(502, "The previous model provider is not configured")
            return
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
        req = urllib.request.Request(upstream + self.path, data=raw if self.command == "POST" else None, headers=headers, method=self.command)
        # Preserve the existing local router; do not send loopback requests through a VPN proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        sent = False
        try:
            try:
                response = opener.open(req, timeout=300)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                self.send_response(response.status)
                for key, value in response.headers.items():
                    if key.lower() not in HOP_HEADERS:
                        self.send_header(key, value)
                self.end_headers()
                sent = True
                reader = getattr(response, "read1", response.read)
                while chunk := reader(8192):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        except (OSError, urllib.error.URLError):
            if not sent:
                self._error(502, "The previous model provider could not be reached")
            else:
                self.close_connection = True


def health(config: dict[str, Any]) -> dict[str, Any] | None:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{config['port']}/health", timeout=2) as response:
            data = json.load(response)
        return data if data.get("service") == "pacer-model" else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _ensure_loopback_proxy_bypass(env: dict[str, Any]) -> None:
    """Keep this gateway's loopback URL out of inherited HTTP proxy routes."""
    entries: list[str] = []
    seen: set[str] = set()
    existing_values = (env.get("NO_PROXY"), env.get("no_proxy"))
    for raw in existing_values:
        for entry in str(raw or "").split(","):
            value = entry.strip()
            if value and value.casefold() not in seen:
                entries.append(value)
                seen.add(value.casefold())
    for host in ("localhost", "127.0.0.1", "::1"):
        if host.casefold() not in seen:
            entries.append(host)
            seen.add(host.casefold())
    # Windows environment names are case-insensitive, and JSON consumers such as
    # PowerShell reject NO_PROXY/no_proxy as duplicate property names.
    env.pop("no_proxy", None)
    env["NO_PROXY"] = ",".join(entries)


def start(config_file: Path) -> dict[str, Any]:
    from .provider_runtime import ensure_template
    ensure_template(config_file.parent)
    config = json.loads(config_file.read_text(encoding="utf-8"))
    if ready := health(config):
        return ready
    from visual_agent.subprocess_window import hidden_subprocess_kwargs
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    env["PACER_GATEWAY_CONFIG"] = str(config_file.resolve())
    log_path = config_file.with_name("server.log")
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, "-m", "visual_agent.pacer_model", "serve", "--config", str(config_file)],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                   cwd=config["workspace_root"], close_fds=True, **hidden_subprocess_kwargs(detached=True))
    for _ in range(40):
        if ready := health(config):
            return ready
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise RuntimeError(f"Pacer model gateway did not start; inspect {log_path}")


def install(workspace: Path, settings_path: Path, config_file: Path, port: int, *, autostart: bool = True) -> dict[str, Any]:
    from .provider_runtime import ensure_template
    ensure_template(config_file.parent)
    settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    previous = json.loads(config_file.read_text(encoding="utf-8")) if config_file.exists() else {}
    env = settings.setdefault("env", {})
    if not isinstance(env, dict):
        raise ValueError("Claude settings 'env' must be a JSON object")
    _ensure_loopback_proxy_bypass(env)
    old_url = str(env.get("ANTHROPIC_BASE_URL") or "http://127.0.0.1:8090")
    our_url = f"http://127.0.0.1:{port}"
    if old_url.rstrip("/") == our_url:
        old_url = previous.get("upstream_url", "http://127.0.0.1:8090")
    else:
        previous_port = previous.get("port")
        previous_url = urllib.parse.urlsplit(old_url)
        if (
            isinstance(previous_port, int)
            and previous_url.hostname in {"127.0.0.1", "localhost", "::1"}
            and previous_url.port == previous_port
        ):
            old_url = str(previous.get("upstream_url") or old_url)
    if urllib.parse.urlsplit(old_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("This installation expects an existing loopback provider/router; configure remote providers explicitly.")
    token = str(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or previous.get("auth_token") or secrets.token_urlsafe(32))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = settings_path.with_name(f"settings.before-pacer-model-{stamp}.json")
    if settings_path.exists():
        shutil.copy2(settings_path, backup)
    allowed_roots = [str(workspace.resolve())]
    previous_roots = previous.get("allowed_roots", [])
    if not isinstance(previous_roots, list):
        previous_roots = []
    for previous_root in previous_roots:
        try:
            resolved_root = str(Path(previous_root).expanduser().resolve())
        except (OSError, RuntimeError, TypeError):
            continue
        if resolved_root not in allowed_roots:
            allowed_roots.append(resolved_root)
    config = {"port": port, "workspace_root": str(workspace.resolve()), "allow_all_workspaces": True,
              "allowed_roots": allowed_roots,
              "upstream_url": old_url, "auth_token": token, "runs_dir": str(config_file.parent / "runs"), "settings_backup": str(backup)}
    write_json(config_file, config)
    env.update(ANTHROPIC_BASE_URL=our_url, ANTHROPIC_AUTH_TOKEN=token, ANTHROPIC_MODEL=MODEL_ID,
               ANTHROPIC_CUSTOM_MODEL_OPTION=MODEL_ID, ANTHROPIC_CUSTOM_MODEL_OPTION_NAME="Pacer · 多模型任务引擎",
               ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION="任务直接交由 Pacer 编排执行")
    settings["model"] = MODEL_ID
    picker = settings.setdefault("modelPicker", {})
    picker["options"] = [{"model": MODEL_ID, "label": "Pacer · 多模型任务引擎", "description": "入口直接接管任务，内部模型由 Pacer 调度"}] + [row for row in picker.get("options", []) if row.get("model") != MODEL_ID]
    write_json(settings_path, settings)
    if autostart and os.name == "nt":
        bootstrap = config_file.with_name("start_gateway.py")
        bootstrap.write_text("import sys\nsys.path.insert(0, " + repr(str(Path(__file__).resolve().parents[1])) + ")\nfrom visual_agent.pacer_model import start\nfrom pathlib import Path\nstart(Path(" + repr(str(config_file)) + "))\n", encoding="utf-8")
        startup = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/Pacer Model Gateway.vbs"
        command = subprocess.list2cmdline([sys.executable, str(bootstrap)]).replace('"', '""')
        startup.write_text('CreateObject("WScript.Shell").Run "' + command + '", 0, False\n', encoding="utf-8")
    return {"model": MODEL_ID, "base_url": our_url, "upstream_url": old_url, "settings_backup": str(backup)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Expose Pacer as a Claude Code model")
    parser.add_argument("command", choices=["serve", "start", "status", "install", "manage"])
    parser.add_argument("--config", type=Path, default=config_path())
    parser.add_argument("--workspace-root", type=Path, default=Path.cwd())
    parser.add_argument("--settings", type=Path, default=Path.home() / ".claude/settings.json")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-autostart", action="store_true")
    args = parser.parse_args(argv)
    os.environ["PACER_GATEWAY_CONFIG"] = str(args.config.resolve())
    if args.command == "install":
        result = install(args.workspace_root, args.settings, args.config, args.port, autostart=not args.no_autostart)
        result["health"] = start(args.config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "start":
        print(json.dumps(start(args.config), ensure_ascii=False))
        return 0
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if args.command == "manage":
        import webbrowser
        from .model_admin import management_token
        start(args.config)
        webbrowser.open(f"http://127.0.0.1:{config['port']}/settings#token={management_token(config)}")
        print(f"Pacer model management: http://127.0.0.1:{config['port']}/settings")
        return 0
    if args.command == "status":
        result = health(config)
        report = dict(result or {"status": "offline", "pid": None})
        report["port"] = config["port"]
        from .provider_runtime import status_rows
        try:
            report["providers"] = status_rows()
        except ValueError as exc:
            report["providers_error"] = str(exc)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if result else 1
    config["_config_path"] = str(args.config.resolve())
    server = PacerModelServer(("127.0.0.1", int(config["port"])), config)
    print(f"Pacer model listening on http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
