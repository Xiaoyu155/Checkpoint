"""Bounded model collaboration around the existing patch-and-test engine."""
from __future__ import annotations
import ast
import fnmatch
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Callable

from .model_catalog import load_profiles, workflow
from .command_verification import is_test_path
from .repo_map import build_repo_map, render_repo_map
from .verification_profiles import validate_safe_verification_command
from .live_call_guard import model_cli_launch_blocked


logger = logging.getLogger(__name__)


def configure_plan(
    plan: Any,
    root: Path,
    progress: Callable[[str], None],
    *,
    planner_already_ran: bool = False,
) -> dict[str, Any]:
    # A verifier runs the physical gate locally; model review belongs to the
    # explicit reviewer phase and must not make a green test depend on a chat reply.
    for task in plan.tasks:
        if getattr(task, "role", "") == "verifier":
            task.backend = "local"
    routing = workflow(load_profiles()["workflow"], plan.complexity, plan.goal)
    planner_used = getattr(plan, "planner_backend", None) or routing["planner"]
    routing["planner_used"] = planner_used
    explicit = os.environ.get("PACER_CODER_CASCADE")
    if explicit:
        routing["coder"] = "explicit_cascade"
        first = next((x.strip().lower() for x in explicit.split(",") if x.strip()), None)
        if first:
            for task in plan.tasks:
                if task.role == "coder":
                    task.backend = first
    else:
        coder_tasks = [task for task in plan.tasks if task.role == "coder"]
        same_wave_coders = [
            task for task in coder_tasks
            if not getattr(task, "dependencies", [])
            and any(not is_test_path(path) for path in (getattr(task, "target_files", []) or []))
        ]
        assigned_backends_before_split = {
            str(getattr(task, "backend", "") or "").strip().lower()
            for task in same_wave_coders
        }
        if len(same_wave_coders) == 2 and len(assigned_backends_before_split) <= 1:
            from .gemini_bridge import is_gemini38_available
            from .subprocess_window import resolve_codex_executable

            if is_gemini38_available() and resolve_codex_executable():
                # Keep first-wave independent modules on distinct model families,
                # even when a remote planner labels both with the same backend.
                for index, task in enumerate(same_wave_coders):
                    task.backend = "gemini38" if index % 2 == 0 else "codex"
        assigned_backends = {str(getattr(task, "backend", "") or "").strip().lower() for task in coder_tasks}
        # Preserve deliberate per-task provider assignments in a mixed
        # parallel plan. A single/default backend is still normalized to the
        # workflow's selected writer below.
        preserve_parallel_mix = len(coder_tasks) > 1 and len(assigned_backends - {""}) > 1
        if not preserve_parallel_mix:
            for task in coder_tasks:
                task.backend = routing["coder"]
    planner_label = "gemini38_fallback" if planner_used == "gemini38" else planner_used
    progress(f"[工作流] {routing['name']}：规划 {planner_label} → 实现 {routing['coder']} → 本地验收 → 审查 {routing['reviewer']}\n")
    if routing["planner"] == "gemini4_vps" and planner_already_ran:
        if planner_used == "gemini38":
            routing["planner_status"] = "fallback"
            routing["reviewer_fallback"] = "gemini38"
            progress("[gemini4_vps/planner] Gemini 4 不可用，已降级到 Gemini 3.8 生成计划；Gemini 3.8 只规划，不写代码。\n")
        elif planner_used == "local":
            routing["planner_status"] = "local"
            progress("[planner] 已采用本地规则生成并校验的任务计划。\n")
        else:
            routing["planner_status"] = "ready"
            progress("[gemini4_vps/planner] 已采用前置生成并校验的任务计划，跳过重复请求。\n")
    elif routing["planner"] == "gemini4_vps":
        from .gemini4_vps_client import invoke_gemini4_vps
        response = invoke_gemini4_vps(
            "Give a concise implementation plan, risks, and concrete acceptance checks. Do not claim any edits.\nTask:\n" + plan.goal[:5000],
            system_prompt="You are the architecture planner in Pacer. The implementation and verification will be performed by separate workers.", timeout_seconds=45)
        if response.success:
            for task in plan.tasks:
                if task.role == "coder":
                    task.instructions += "\n[Gemini 4 advisory plan; preserve the original task and locked tests]\n" + response.content[:3000]
            progress("[gemini4_vps/planner] 规划已返回，交给实现模型执行。\n")
        else:
            routing["planner_status"] = "blocked"
            progress("[gemini4_vps/planner] 辅助规划请求失败；没有通过规划门禁，必须停止执行：" + str(response.error)[:200] + "\n")
            raise RuntimeError(response.error or "Gemini 4 planner did not return a usable plan")
    return routing


_FAKE_PLAN_FILES = {"impl_core.py", "impl_router.py", "app_main.py"}


def _pacer_workspace_scope(goal: str, repo_root: str | Path) -> tuple[list[str], list[str]] | None:
    """Return the real Pacer source/test roots and file inventory for self-work."""
    from .wish_intake import _is_pacer_self_request

    if not _is_pacer_self_request(goal):
        return None
    root = Path(repo_root).expanduser().resolve()
    source_dir = Path(__file__).resolve().parent
    try:
        source_rel = source_dir.relative_to(root).as_posix()
    except ValueError:
        return None

    project_root = source_dir.parent.parent
    test_dir = project_root / "tests"
    allowed_roots = [source_rel]
    inventory = [
        path.relative_to(root).as_posix()
        for path in sorted(source_dir.glob("*.py"))
        if path.is_file()
    ]
    if test_dir.is_dir():
        test_rel = test_dir.relative_to(root).as_posix()
        allowed_roots.append(test_rel)
        inventory.extend(
            path.relative_to(root).as_posix()
            for path in sorted(test_dir.rglob("test_*.py"))
            if path.is_file() and not any(part.startswith(".") for part in path.relative_to(test_dir).parts)
        )
    return allowed_roots, inventory


def _validate_target_path(
    path_text: str,
    root: Path,
    allowed_roots: list[str] | None,
    *,
    allow_missing_test_parent: bool = False,
) -> str:
    normalized = str(path_text).replace("\\", "/").strip()
    relative = Path(normalized)
    if not normalized or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"planner returned an unsafe target path: {path_text!r}")
    try:
        resolved = (root / relative).resolve()
        relative_posix = resolved.relative_to(root).as_posix()
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError(f"planner target escapes the workspace: {path_text!r}") from exc
    if allowed_roots and not any(
        relative_posix == allowed or relative_posix.startswith(allowed.rstrip("/") + "/")
        for allowed in allowed_roots
    ):
        raise ValueError(f"planner target is outside the Pacer source/test roots: {relative_posix}")
    if resolved.exists() and not resolved.is_file():
        raise ValueError(f"planner target is not a file: {relative_posix}")
    if (
        not resolved.exists()
        and not resolved.parent.is_dir()
        and not (
            allow_missing_test_parent
            and is_test_path(relative_posix)
            and Path(relative_posix).suffix.lower() == ".py"
        )
    ):
        raise ValueError(
            f"planner invented a target under a directory that does not exist: {relative_posix}"
        )
    return relative_posix


def _explicit_existing_pytest_command(goal: str, target_files: list[str], root: Path) -> str:
    """Honor an explicit pytest command for an existing test included in the plan."""
    command_pattern = re.compile(
        r"(?P<command>(?:python(?:\.exe)?\s+-m\s+pytest|pytest)\s+"
        r"(?:\"[^\"`\r\n]+\"|'[^'`\r\n]+'|[^\s`]+)(?:\s+-q\b)?)",
        re.IGNORECASE,
    )
    target_set = {str(path).replace("\\", "/").strip() for path in target_files}
    for match in command_pattern.finditer(str(goal or "")):
        command = match.group("command").strip()
        path_match = re.search(
            r"(?:\"([^\"]+\.py)\"|'([^']+\.py)'|([^\s]+\.py))",
            command,
            re.IGNORECASE,
        )
        if not path_match:
            continue
        test_path = next(group for group in path_match.groups() if group is not None).replace("\\", "/")
        if (
            test_path in target_set
            and Path(test_path).name.lower().startswith("test_")
            and (root / test_path).is_file()
        ):
            return command
    return ""


def _validate_unittest_discovery(
    command: str,
    root: Path,
    allowed_roots: list[str] | None,
    *,
    planned_test_path: str = "",
) -> None:
    lowered = command.lower()
    if "unittest" not in lowered or "discover" not in lowered:
        return
    start_match = re.search(
        r"(?:^|\s)(?:-s|--start-directory)(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))",
        command,
    )
    if not start_match:
        raise ValueError("unittest discovery must name an explicit existing test directory")
    start_text = next(group for group in start_match.groups() if group is not None)
    start = Path(start_text)
    if not start.is_absolute():
        start = root / start
    try:
        start = start.resolve()
        start_rel = start.relative_to(root).as_posix()
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError("unittest discovery directory escapes the workspace") from exc
    if not start.is_dir():
        raise ValueError(f"unittest discovery directory does not exist: {start_rel}")
    if allowed_roots and not any(
        start_rel == allowed or start_rel.startswith(allowed.rstrip("/") + "/")
        for allowed in allowed_roots
    ):
        raise ValueError(f"unittest discovery is outside the Pacer test roots: {start_rel}")
    pattern_match = re.search(
        r"(?:^|\s)(?:-p|--pattern)(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))",
        command,
    )
    pattern = next((group for group in pattern_match.groups() if group is not None), "test*.py") if pattern_match else "test*.py"
    planned_test = Path(planned_test_path) if planned_test_path else None
    planned_absolute = (root / planned_test).resolve() if planned_test and not planned_test.is_absolute() else None
    planned_matches = bool(
        planned_absolute
        and planned_absolute.is_relative_to(start)
        and fnmatch.fnmatch(planned_absolute.name, pattern)
    )
    if not any(start.rglob(pattern)) and not planned_matches:
        raise ValueError(f"unittest discovery matches no tests in {start_rel}: {pattern}")


def build_plan_from_gemini4_text(
    goal: str,
    repo_root: str | Path,
    text: str | dict[str, Any],
    *,
    allowed_roots: list[str] | None = None,
) -> Any:
    """Turn a Gemini 4 JSON plan into tasks that each carry their own exam."""
    from .subagent_orchestrator import SubagentPlan, SubagentTask

    if isinstance(text, dict):
        data = text
    elif isinstance(text, str):
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("planner returned no JSON object")
        data = json.loads(text[start : end + 1])
    else:
        raise ValueError("planner returned no JSON object")
    raw_tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("planner JSON has no tasks")
    root = Path(repo_root).expanduser().resolve()
    tasks: list[Any] = []
    for index, item in enumerate(raw_tasks, 1):
        if not isinstance(item, dict):
            raise ValueError("planner task must be an object")
        files = [
            _validate_target_path(str(path), root, allowed_roots)
            for path in item.get("target_files") or []
            if str(path).strip()
        ]
        command = str(item.get("assertion_cmd") or "").strip()
        if not files or not command:
            raise ValueError("each task needs target_files and assertion_cmd")
        command = _normalize_planner_command(command)
        explicit_test_command = _explicit_existing_pytest_command(goal, files, root)
        if explicit_test_command:
            # An operator-named, existing focused test is authoritative. Do
            # not let a planner substitute a newly generated exam file for
            # the requested physical verification command.
            command = _normalize_planner_command(explicit_test_command)
            acceptance_test: dict[str, str] = {}
        else:
            acceptance_test = _validate_acceptance_test(item.get("acceptance_test"), root, allowed_roots, command)
            _validate_acceptance_test_call_signatures(acceptance_test, files, root)
        command = _normalize_unittest_command(command, root, acceptance_test)
        safe, safety_reason = validate_safe_verification_command(command, repo_root=root)
        if not safe:
            raise ValueError(f"planner returned an unsafe acceptance command: {safety_reason}")
        missing_test_targets = [
            path for path in files
            if is_test_path(path) and not (root / path).is_file()
        ]
        if missing_test_targets:
            if not acceptance_test:
                raise ValueError(
                    "planner targets a new test file without an acceptance_test source: "
                    + ", ".join(missing_test_targets)
                )
            if acceptance_test["path"] in files:
                raise ValueError(
                    "acceptance_test must be a separate locked file, not a coder target; remove "
                    + acceptance_test["path"] + " from target_files"
                )
        if any(Path(path).name in _FAKE_PLAN_FILES for path in files):
            raise ValueError("planner used a placeholder file name")
        if command in {"pytest -q", "python -m pytest -q", "pytest tests/"}:
            raise ValueError("planner used a whole-suite command")
        _validate_unittest_discovery(
            command,
            root,
            allowed_roots,
            planned_test_path=acceptance_test.get("path") or "",
        )
        role = str(item.get("role") or "coder")
        if explicit_test_command and re.search(
            r"只验证|仅验证|只运行测试|verification[- ]only|verify only|no code edits",
            goal,
            re.IGNORECASE,
        ):
            role = "verifier"
        tasks.append(
            SubagentTask(
                id=f"task_{index}",
                title=str(item.get("title") or f"Task {index}"),
                role=role,
                backend="workbuddy" if role == "coder" else "local",
                instructions=str(item.get("instructions") or goal),
                target_files=files,
                verification_command=command,
                acceptance_test=acceptance_test,
                acceptance_exam=[
                    {
                        "id": f"exam_{index}",
                        "question": str(item.get("question") or command),
                        "check_type": "execution",
                        "assertion_cmd": command,
                        "target_files": files,
                        "expected_exit_code": 0,
                    }
                ],
            )
        )
    return SubagentPlan(goal=goal, tasks=tasks, repo_root=str(repo_root))


def _invoke_gemini38_json(
    prompt: str,
    *,
    repo_root: Path,
    system_prompt: str,
    timeout_seconds: int = 45,
) -> Any:
    """Use Gemini 3.8 for bounded planning/review output only, never code edits."""
    from .gemini_bridge import invoke_gemini38 as invoke_gemini

    model = os.environ.get("PACER_GEMINI38_FALLBACK_MODEL", "gemini-3.8-flash-low").strip()
    if not model.lower().startswith(("gemini-3.8", "gemini-3-8")):
        raise RuntimeError("PACER_GEMINI38_FALLBACK_MODEL must name a Gemini 3.8 model")
    return invoke_gemini(
        prompt,
        model=model,
        yolo=False,
        output_format="json",
        expect_json=True,
        cwd=repo_root,
        timeout_seconds=timeout_seconds,
        system_prompt=system_prompt,
    )


def _local_rule_plan(goal: str, root: Path) -> Any:
    from .subagent_orchestrator import generate_subagent_plan

    plan = generate_subagent_plan(
        goal,
        repo_root=root,
        workbuddy_available=False,
        gemini_available=False,
        fast_local_plan=True,
    )
    plan.planner_backend = "local"
    return plan


def plan_with_gemini4(
    goal: str,
    repo_root: str | Path,
    *,
    allow_gemini38_fallback: bool = False,
    force_remote: bool = True,
) -> Any:
    """Prefer Gemini 4; optionally fail over to a planning-only Gemini 3.8 call."""
    root = Path(repo_root).expanduser().resolve()
    if not force_remote:
        return _local_rule_plan(goal, root)
    # The planner calls gemini4_vps_client directly. Check that same config
    # and pool here; provider_runtime's optional API slot may be disabled even
    # while the legacy VPS profile is enabled.
    try:
        from .gemini4_vps_client import (
            GATEWAY_API_KEY,
            gemini4_settings,
            is_gemini4_vps_available,
            is_gemini4_vps_pool_healthy,
        )
        from .provider_runtime import _is_vps_gateway

        settings = gemini4_settings()
        # Only apply the live pool gate when the user supplied a real key.
        # The built-in gateway token is a compatibility placeholder; probing
        # the host's real pool for isolated/default profiles would override
        # the planner's normal error and Gemini 3.8 fallback behavior.
        explicit_key = bool(settings.get("api_key")) and settings.get("api_key") != GATEWAY_API_KEY
        if (
            explicit_key
            and is_gemini4_vps_available()
            and _is_vps_gateway(settings.get("base_url", ""))
            and not is_gemini4_vps_pool_healthy()
        ):
            return _local_rule_plan(goal, root)
    except Exception as exc:
        # A failed preflight is inconclusive; let the configured invocation
        # report its own error and preserve the planner's explicit fallback.
        logger.warning(
            "reviewer preflight failed; retaining configured fallback",
            extra={"stage": "gemini4_preflight", "error_type": type(exc).__name__},
        )
    pacer_scope = _pacer_workspace_scope(goal, root)
    allowed_roots: list[str] | None = None
    inventory_text = ""
    if pacer_scope:
        allowed_roots, inventory = pacer_scope
        inventory_text = (
            "\nPacer workspace roots (only these may be changed):\n"
            + "\n".join(f"- {path}" for path in allowed_roots)
            + "\nExisting source and test files:\n"
            + "\n".join(f"- {path}" for path in inventory)
            + "\nChoose target_files only from this inventory. New files are allowed only directly under one of these existing roots.\n"
        )
    else:
        try:
            repo_data = build_repo_map(repo_root=root, cache_path=root / ".pacer" / "repo_map.json")
            repo_excerpt = render_repo_map(repo_data, goal=goal, max_lines=55)
        except (OSError, ValueError, RuntimeError):
            repo_excerpt = ""
        if repo_excerpt:
            inventory_text = (
                "\nCurrent project files and locally indexed symbols (use this to choose exact paths):\n"
                + repo_excerpt
                + "\nChoose existing production files and a focused test file under an existing test directory when available. "
                "The acceptance command must exercise the requested behavior and fail on this pre-task workspace; "
                "do not use an unrelated command that already passes.\n"
            )
    planner_prompt = (
        "Split this wish into JSON only. Schema: {\"tasks\":[{\"role\":\"coder\",\"title\":\"...\","
        "\"target_files\":[\"existing/file.py\"],\"assertion_cmd\":\"a command that fails before the change and passes after\","
        "\"question\":\"what the exam proves\",\"acceptance_test\":{\"path\":\"tests/test_feature.py\",\"source\":\"complete runnable Python test source\"}}]}. "
        "Do not use impl_core.py, impl_router.py, app_main.py, or pytest -q.\n"
        "Target files must be grounded in the project map; never ask the user for a filename when the map identifies the relevant module.\n"
        "Pacer may create the parent directory for its locked acceptance test when needed, but only inside this workspace. "
        "Acceptance commands must use a standard targeted test runner (pytest <test-path> -q, python -m pytest <test-path> -q, or python -m unittest <test-path>). "
        "Never use python -c, inline scripts, shell chaining, redirection, or commands that can modify the workspace.\n"
        "If no suitable existing test covers the requested behavior, provide acceptance_test with a new test path and complete source. "
        "Pacer will create and lock this test, run it against the current code, then dispatch the coder only if it fails on the requested behavior. "
        "Do not include acceptance_test.path in target_files. The test must use a substantive assertion, have no file/network/system side effects, and its path must be run by assertion_cmd. "
        "Match every call to a target function against its indexed AST signature: supply all required positional and keyword-only arguments, and never omit a required argument or assume it has a default. Use concrete argument values for every required parameter.\n"
        "If the wish explicitly names an existing focused pytest file and command, keep that exact command as verification_command; do not replace it with a newly generated acceptance_test.\n"
        + inventory_text
        + "Wish:\n"
        + goal[:18000]
    )
    system_prompt = "You are Pacer's task planner. Return a grounded JSON plan and acceptance exams; do not implement or edit code."
    primary_error = ""
    try:
        from .gemini4_vps_client import invoke_gemini4_vps
        from .session_projection import SessionProjection

        SessionProjection(root).append_model_request(
            backend="gemini4_vps",
            system_prompt=system_prompt,
            user_prompt=planner_prompt,
            phase="planner",
        )
        response = invoke_gemini4_vps(
            planner_prompt,
            system_prompt=system_prompt,
            timeout_seconds=60,
        )
        if response.success and (response.content or "").strip():
            try:
                plan = build_plan_from_gemini4_text(goal, root, response.content, allowed_roots=allowed_roots)
                plan.planner_backend = "gemini4_vps"
                return plan
            except (ValueError, json.JSONDecodeError) as exc:
                primary_error = f"Gemini 4 returned an unusable plan: {exc}"
        else:
            primary_error = response.error or "Gemini 4 returned an empty plan"
    except Exception as exc:
        primary_error = str(exc) or exc.__class__.__name__

    if not allow_gemini38_fallback:
        raise RuntimeError(f"Gemini 4 planner failed: {primary_error}")

    fallback_prompt = (
        planner_prompt
        + "\nGemini 4 planning failed validation: " + primary_error[:500]
        + "\nProduce the same grounded plan as a planning-only fallback. "
        "Do not write code, call tools, or claim implementation. The acceptance command must pass Pacer's safe test-runner policy."
    )
    fallback_errors: list[str] = []
    for attempt in range(2):
        try:
            from .session_projection import SessionProjection

            fallback_system_prompt = (
                "You are Pacer's Gemini 3.8 planning fallback. Your only role is to draft a grounded JSON task plan "
                "with safe, concrete acceptance exams. Never implement code or edit files."
            )
            SessionProjection(root).append_model_request(
                backend="gemini38",
                system_prompt=fallback_system_prompt,
                user_prompt=fallback_prompt,
                phase="planner_fallback",
                metadata={"attempt": attempt + 1},
            )
            fallback_response = _invoke_gemini38_json(
                fallback_prompt,
                repo_root=root,
                system_prompt=fallback_system_prompt,
                timeout_seconds=90,
            )
            if not fallback_response.success or not (fallback_response.response or "").strip():
                raise RuntimeError(fallback_response.error or "Gemini 3.8 returned an empty plan")
            parsed = getattr(fallback_response, "parsed_json", None)
            candidate = (
                parsed
                if isinstance(parsed, dict) and isinstance(parsed.get("tasks"), list)
                else fallback_response.response
            )
            plan = build_plan_from_gemini4_text(
                goal,
                root,
                candidate,
                allowed_roots=allowed_roots,
            )
            plan.planner_backend = "gemini38"
            return plan
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            fallback_errors.append(error[:500])
            retryable_transport = any(
                marker in error.lower()
                for marker in (
                    "timeout", "timed out", "429", "rate limit", "connection reset",
                    "connection aborted", "temporarily unavailable", "http 502", "http 503", "http 504",
                )
            )
            if attempt == 0 and (retryable_transport or isinstance(exc, ValueError)):
                fallback_prompt += (
                    "\nThe previous response failed Pacer validation: " + error[:500]
                    + "\nRetry with exactly one concise task and one complete JSON object. "
                    "Keep the plan under 1600 characters; encode test-source newlines as escaped \\n, escape quotes, "
                    "and use an existing test directory or a root-level test_*.py file. "
                    "Do not repeat the rejected path or command."
                )
                continue
            break
    fallback_error = "; ".join(fallback_errors) or "Gemini 3.8 returned an empty plan"
    raise RuntimeError(
        f"Gemini 4 planner failed ({primary_error}); Gemini 3.8 planning fallback failed ({fallback_error})"
    )


def _normalize_planner_command(command: str) -> str:
    """Map the common python3 alias to PATH's Python on Windows."""
    text = str(command or "").strip()
    if os.name == "nt" and re.match(r"^pytest(?=\s)", text, re.IGNORECASE):
        return "python -m " + text
    if os.name == "nt" and re.match(r"^python3(?=\s)", text, re.IGNORECASE):
        if not shutil.which("python3") and shutil.which("python"):
            return re.sub(r"^python3(?=\s)", "python", text, count=1, flags=re.IGNORECASE)
    return text


def _normalize_unittest_command(
    command: str,
    root: Path,
    acceptance_test: dict[str, str],
) -> str:
    """Use unittest discovery for nested test files without package initializers."""
    text = str(command or "").strip()
    lowered = text.lower()
    if "unittest" not in lowered or "discover" in lowered:
        return text
    test_path = str(acceptance_test.get("path") or "").replace("\\", "/")
    if not test_path:
        path_match = re.search(r"(?i)(?:[\w.-]+[\\/])*test_[\w.-]+\.py\b", text)
        if path_match:
            test_path = path_match.group(0).replace("\\", "/")
        else:
            module_match = re.search(
                r"(?i)(?:^|\s)-m\s+unittest\s+([A-Za-z_][A-Za-z0-9_.]*)\b",
                text,
            )
            if module_match:
                test_path = module_match.group(1).replace(".", "/") + ".py"
    if not test_path:
        return text
    path = Path(test_path)
    if path.suffix.lower() != ".py" or len(path.parts) == 1:
        return text
    init_file = root / path.parent / "__init__.py"
    target_exists = (root / path).is_file() or acceptance_test.get("path") == test_path
    if init_file.is_file() or not target_exists:
        return text
    start_dir = path.parent.as_posix()
    pattern = path.name
    return f'python -m unittest discover -s "{start_dir}" -p "{pattern}"'


def _validate_acceptance_test(
    raw: Any,
    root: Path,
    allowed_roots: list[str] | None,
    command: str,
) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("acceptance_test must contain path and source fields")
    path = _validate_target_path(
        str(raw.get("path") or ""),
        root,
        allowed_roots,
        allow_missing_test_parent=True,
    )
    if not is_test_path(path) or Path(path).suffix.lower() != ".py":
        raise ValueError("acceptance_test path must be a new Python test file")
    target = root / path
    if target.exists():
        raise ValueError(f"acceptance_test must not overwrite an existing test: {path}")
    source = str(raw.get("source") or "").strip()
    if not source or len(source.encode("utf-8")) > 32_000:
        raise ValueError("acceptance_test source must be non-empty and at most 32 KB")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"acceptance_test has invalid Python syntax: {exc}") from exc
    forbidden_imports = {"os", "subprocess", "socket", "shutil", "requests", "urllib", "ctypes"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {alias.name.split(".", 1)[0] for alias in node.names}
            if names & forbidden_imports:
                raise ValueError("acceptance_test imports a forbidden system or network module")
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".", 1)[0] in forbidden_imports:
            raise ValueError("acceptance_test imports a forbidden system or network module")
        elif isinstance(node, ast.Call):
            called = node.func.id if isinstance(node.func, ast.Name) else ""
            if called in {"open", "exec", "eval", "compile", "__import__"}:
                raise ValueError(f"acceptance_test uses a forbidden dynamic or file operation: {called}")
    from .witness_verifier import WitnessVerifier

    substantive, detail, _violations = WitnessVerifier(root).verify_assertion_substance(source)
    if not substantive:
        raise ValueError(f"acceptance_test failed Pacer's assertion-substance check: {detail}")
    if "unittest" in command.lower():
        has_unittest_case = any(
            isinstance(node, ast.ClassDef)
            and any(
                (isinstance(base, ast.Name) and base.id.endswith("TestCase"))
                or (isinstance(base, ast.Attribute) and base.attr == "TestCase")
                for base in node.bases
            )
            and any(
                isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and member.name.startswith("test_")
                for member in node.body
            )
            for node in tree.body
        )
        if not has_unittest_case:
            raise ValueError(
                "unittest acceptance_test must define test_* methods on a unittest.TestCase subclass; "
                "use pytest for function-style tests"
            )
    normalized_command = command.lower().replace("\\", "/")
    module = Path(path).with_suffix("").as_posix().replace("/", ".").lower()
    if path.lower() not in normalized_command and module not in normalized_command:
        raise ValueError("verification command does not run the declared acceptance_test")
    return {"path": path, "source": source}


def _validate_acceptance_test_call_signatures(
    acceptance_test: dict[str, str],
    target_files: list[str],
    root: Path,
) -> None:
    """Reject planner-authored exams that call a target function with the wrong arity."""
    source = str(acceptance_test.get("source") or "")
    if not source:
        return
    try:
        exam_tree = ast.parse(source)
    except SyntaxError:
        return  # _validate_acceptance_test reports the syntax error with its path.

    imported_names: dict[str, dict[str, tuple[list[str], bool]]] = {}
    for raw_path in target_files:
        rel_path = Path(str(raw_path).replace("\\", "/"))
        if rel_path.suffix.lower() != ".py":
            continue
        target = root / rel_path
        if not target.is_file():
            continue  # New modules have no pre-change signature to validate yet.
        try:
            module_tree = ast.parse(target.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError):
            continue
        module_name = ".".join(rel_path.with_suffix("").parts)
        signatures: dict[str, tuple[list[str], bool]] = {}
        for node in module_tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            positional = [*args.posonlyargs, *args.args]
            optional_count = len(args.defaults)
            required = [arg.arg for arg in positional[: max(0, len(positional) - optional_count)]]
            required.extend(
                arg.arg for arg, default in zip(args.kwonlyargs, args.kw_defaults) if default is None
            )
            signatures[node.name] = (required, args.vararg is not None)
        if signatures:
            imported_names[module_name] = signatures

    aliases: dict[str, tuple[list[str], bool]] = {}
    for node in exam_tree.body:
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        for module_name, signatures in imported_names.items():
            if node.module != module_name:
                continue
            for item in node.names:
                if item.name == "*":
                    aliases.update(signatures)
                elif item.name in signatures:
                    aliases[item.asname or item.name] = signatures[item.name]

    for node in ast.walk(exam_tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        signature = aliases.get(node.func.id)
        if not signature:
            continue
        required, accepts_varargs = signature
        if any(keyword.arg is None for keyword in node.keywords):
            continue  # A **mapping can supply required keyword arguments dynamically.
        if any(isinstance(arg, ast.Starred) for arg in node.args):
            continue  # A *sequence can supply positional arguments dynamically.
        supplied = {name for name in required[: len(node.args)]}
        supplied.update(keyword.arg for keyword in node.keywords if keyword.arg)
        missing = [name for name in required if name not in supplied]
        if missing:
            raise ValueError(
                f"acceptance_test call to {node.func.id} omits required argument(s): "
                + ", ".join(missing)
            )


def diagnose_failure(goal: str, error: str) -> str:
    """Ask Gemini 4 for one diagnosis. A failed call stays failed."""
    from .gemini4_vps_client import invoke_gemini4_vps
    prompt = (
        "Diagnose this failed coding exam. Do not claim any file was edited.\nGoal:\n"
        + goal[:1500] + "\nError:\n" + error[:1500]
    )
    system_prompt = "You are Pacer's diagnosis reviewer. Be concise."
    from .session_projection import SessionProjection

    SessionProjection(Path.cwd()).append_model_request(
        backend="gemini4_vps",
        system_prompt=system_prompt,
        user_prompt=prompt,
        phase="diagnosis",
    )
    response = invoke_gemini4_vps(
        prompt,
        system_prompt=system_prompt,
        timeout_seconds=45,
    )
    if not response.success or not (response.content or "").strip():
        raise RuntimeError(response.error or "empty_content")
    return response.content.strip()


def _effective_coder_family(result: dict[str, Any], plan: Any) -> tuple[str, str]:
    from .task_review import backend_model_family

    backend = str(result.get("effective_backend") or "").strip().lower()
    if backend in {"api", "domestic_api", "premium_api"}:
        attempts = [item for item in result.get("api_node_attempts", []) if isinstance(item, dict) and item.get("success")]
        if attempts:
            selected = attempts[-1]
            provider = str(selected.get("provider") or "")
            model = str(selected.get("model") or "")
            return backend_model_family(backend, provider=provider, model=model), f"{provider}:{model}"
    if not backend:
        backend = next((str(task.backend).lower() for task in plan.tasks if task.role == "coder"), "unknown")
    underlying_model = ""
    if backend == "workbuddy":
        underlying_model = str(result.get("effective_model") or os.environ.get("PACER_WORKBUDDY_MODEL_FAMILY") or os.environ.get("PACER_WORKBUDDY_MODEL") or "")
        if not underlying_model:
            try:
                from .provider_runtime import load_providers
                underlying_model = str(load_providers().get("tier1_free", {}).get("workbuddy", {}).get("model", ""))
            except (ValueError, OSError, TypeError):
                underlying_model = ""
    return backend_model_family(backend, model=underlying_model), backend


class ReviewerUnavailableError(RuntimeError):
    """Structured fail-closed result when independent review cannot run."""
    def __init__(self, skipped_reason: str, diagnostics: list[dict[str, str]] | None = None):
        super().__init__(skipped_reason)
        self.skipped_reason = skipped_reason
        self.diagnostics = list(diagnostics or [])

    def as_dict(self) -> dict[str, Any]:
        return {"type": type(self).__name__, "skipped_reason": self.skipped_reason,
                "diagnostics": self.diagnostics}


class ReviewerCandidates(list):
    def __init__(self):
        super().__init__()
        self.diagnostics: list[dict[str, str]] = []

    def skipped(self, backend: str, reason: str) -> None:
        record = {"backend": backend, "skipped_reason": reason}
        self.diagnostics.append(record)
        logger.warning("reviewer unavailable", extra=record)


def _adversarial_reviewer_candidates() -> ReviewerCandidates:
    """Discover configured reviewers without network probes or CLI invocation."""
    from .provider_runtime import api_nodes, antigravity_node, architect_node, load_providers
    from .subprocess_window import resolve_codex_executable
    from .task_review import backend_model_family
    candidates = ReviewerCandidates()
    try:
        data = load_providers()
    except Exception as exc:
        candidates.skipped("providers", "configuration_error:" + type(exc).__name__)
        return candidates
    try:
        if resolve_codex_executable():
            candidates.append({"backend": "codex", "family": "openai", "model": "Codex CLI"})
        else:
            candidates.skipped("codex", "executable_missing")
    except Exception as exc:
        candidates.skipped("codex", "discovery_error:" + type(exc).__name__)
    try:
        node = antigravity_node(data, probe=False)
        if node.enabled and not node.blockers and (node.api_key or node.cli_available):
            candidates.append({"backend": "gemini38", "family": "google", "model": node.model, "node": node})
        else:
            candidates.skipped("gemini38", "disabled_or_unconfigured")
    except Exception as exc:
        candidates.skipped("gemini38", "discovery_error:" + type(exc).__name__)
    try:
        from .workbuddy_bridge import get_gateway_info
        info = get_gateway_info()
        row = data.get("tier1_free", {}).get("workbuddy", {})
        model = os.environ.get("PACER_WORKBUDDY_MODEL_FAMILY") or os.environ.get("PACER_WORKBUDDY_MODEL") or row.get("model", "")
        family = backend_model_family("workbuddy", model=model)
        if info is not None and row.get("enabled", True) and family != "unknown":
            candidates.append({"backend": "workbuddy", "family": family, "model": model})
        else:
            candidates.skipped("workbuddy", "unknown_underlying_family" if family == "unknown" else "gateway_unconfigured")
    except Exception as exc:
        candidates.skipped("workbuddy", "discovery_error:" + type(exc).__name__)
    try:
        from .grok_bridge import is_grok_available
        if is_grok_available():
            candidates.append({"backend": "grok", "family": "xai", "model": "Grok"})
        else:
            candidates.skipped("grok", "unconfigured")
    except Exception as exc:
        candidates.skipped("grok", "discovery_error:" + type(exc).__name__)
    for slot in ("domestic_api", "premium_api"):
        try:
            nodes = api_nodes(slot, data)
            added = False
            for node in nodes:
                family = backend_model_family(slot, provider=node.provider, model=node.model)
                if node.is_available() and family != "unknown":
                    candidates.append({"backend": slot, "family": family,
                                       "model": f"{node.provider}:{node.model}", "node": node})
                    added = True
                elif node.is_available():
                    candidates.skipped(slot, "unknown_underlying_family")
            if not added:
                candidates.skipped(slot, "no_configured_known_family_node")
        except Exception as exc:
            candidates.skipped(slot, "discovery_error:" + type(exc).__name__)
    try:
        node = architect_node(data, probe=False)
        family = backend_model_family("gemini4_vps", provider=node.provider, model=node.model)
        if node.is_available() and family != "unknown":
            candidates.append({"backend": "gemini4_vps", "family": family, "model": node.model, "node": node})
        else:
            candidates.skipped("gemini4_vps", "disabled_unconfigured_or_unknown_family")
    except Exception as exc:
        candidates.skipped("gemini4_vps", "discovery_error:" + type(exc).__name__)
    if not any(item["backend"] == "gemini4_vps" for item in candidates):
        try:
            from .gemini4_vps_client import GATEWAY_API_KEY, gemini4_settings
            settings = gemini4_settings()
            key = settings.get("api_key")
            model = str(settings.get("model") or "")
            family = backend_model_family("gemini4_vps", provider="gemini", model=model)
            if settings.get("enabled") and settings.get("base_url") and key and key != GATEWAY_API_KEY and model:
                candidates.append({"backend": "gemini4_vps", "family": family, "model": model})
            else:
                candidates.skipped("gemini4_vps_explicit", "disabled_or_unconfigured")
        except Exception as exc:
            candidates.skipped("gemini4_vps_explicit", "discovery_error:" + type(exc).__name__)
    return candidates


def _invoke_adversarial_reviewer(candidate: dict[str, Any], prompt: str, root: Path) -> tuple[str, str | None]:
    backend = str(candidate.get("backend") or "")
    try:
        if backend == "codex":
            from .subprocess_window import hidden_subprocess_kwargs, prepare_subprocess_command, resolve_codex_executable
            import subprocess
            codex_executable = resolve_codex_executable()
            if not codex_executable:
                return "", "Codex executable not found on PATH"
            command = prepare_subprocess_command([codex_executable, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--color", "never", "-"])
            # This is the final spawn boundary: block real CLI launches during
            # pytest unless explicitly enabled, while allowing patched launchers.
            if model_cli_launch_blocked(command, subprocess.Popen):
                return "", "Live Codex CLI calls are disabled during pytest."
            # File-backed output avoids waiting forever on a descendant that inherits
            # the CLI's stdout pipe after the Codex parent exits or hits quota.
            with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(mode="w+b") as stderr_file:
                proc = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    cwd=str(root),
                    **hidden_subprocess_kwargs(),
                )
                try:
                    proc.communicate(input=prompt.encode("utf-8"), timeout=45)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.communicate(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
                    return "", "Codex reviewer timed out after 45 seconds"
                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout_text = stdout_file.read().decode("utf-8", errors="replace").strip()
                stderr_text = stderr_file.read().decode("utf-8", errors="replace").strip()
            return (stdout_text, None) if proc.returncode == 0 and stdout_text else ("", (stderr_text or "Codex reviewer returned empty content")[-500:])
        if backend == "gemini38":
            from .gemini_bridge import invoke_gemini38

            response = invoke_gemini38(
                prompt,
                cwd=root,
                system_prompt="You are an adversarial implementation reviewer. Return only the requested JSON.",
                timeout_seconds=75,
                expect_json=True,
            )
            return (response.response, None) if response.success else ("", str(response.error or "reviewer failed"))
        if backend == "gemini4_vps":
            node = candidate.get("node")
            if node is not None:
                from .llm_providers import resolve_llm_backend, run_llm_completion

                content = run_llm_completion(
                    backend=resolve_llm_backend(f"{node.provider}:{node.model}"),
                    system_prompt="You are an adversarial implementation reviewer. Return only the requested JSON.",
                    prompt=prompt,
                    max_tokens=3000,
                    api_key=node.api_key,
                    base_url=node.base_url,
                    endpoint=node.endpoint,
                    timeout_seconds=75,
                )
                return (content, None) if str(content or "").strip() else ("", "reviewer returned empty content")
            from .gemini4_vps_client import invoke_gemini4_vps

            response = invoke_gemini4_vps(
                prompt,
                system_prompt="You are an adversarial implementation reviewer. Return only the requested JSON.",
                timeout_seconds=75,
            )
            return (str(response.content), None) if response.success else ("", str(response.error or "reviewer failed"))
        if backend == "workbuddy":
            from .workbuddy_bridge import invoke_workbuddy

            response = invoke_workbuddy(prompt, cwd=root, apply_changes=False, timeout_seconds=75)
            return (response.response, None) if response.success else ("", str(response.error or "reviewer failed"))
        if backend == "grok":
            from .grok_bridge import invoke_grok

            response = invoke_grok(
                prompt,
                system_prompt="You are an adversarial implementation reviewer. Return only the requested JSON.",
                timeout=75,
            )
            return (response.response, None) if response.success else ("", str(response.error or "reviewer failed"))
        node = candidate.get("node")
        if node is not None:
            from .llm_providers import resolve_llm_backend, run_llm_completion

            content = run_llm_completion(
                backend=resolve_llm_backend(f"{node.provider}:{node.model}"),
                system_prompt="You are an adversarial implementation reviewer. Return only the requested JSON.",
                prompt=prompt,
                max_tokens=3000,
                api_key=node.api_key,
                base_url=node.base_url,
                endpoint=node.endpoint,
                timeout_seconds=75,
            )
            return (content, None) if str(content or "").strip() else ("", "reviewer returned empty content")
        return "", f"Unsupported adversarial reviewer backend: {backend}"
    except Exception as exc:
        from .security import redact_secret_text

        return "", redact_secret_text(str(exc))


def review_implementation(plan: Any, result: dict[str, Any], routing: dict[str, Any], root: Path,
                          progress: Callable[[str], None]) -> dict[str, Any]:
    result["collaboration"] = routing
    if not result.get("success"):
        return result
    from .task_review import (
        backend_model_family,
        counterexample_module_name,
        extract_counterexample_assertions,
        run_executable_counterexamples,
    )

    coders = [t for t in plan.tasks if getattr(t, "role", "") == "coder" and getattr(t, "patched_files", [])]
    if not coders:
        # Plans without a coder patch still honor the configured reviewer and
        # require an explicit positive verdict; arbitrary prose is not approval.
        if routing.get("reviewer") not in (None, "", "local"):
            candidates = _adversarial_reviewer_candidates()
            requested = routing["reviewer"]
            reviewer = next((item for item in candidates if item.get("backend") == requested), None)
            if reviewer is None:
                result["success"] = False
                result["collaboration_error"] = "Adversarial review unavailable: requested reviewer is not configured"
                result["review"] = {
                    "approved": False,
                    "status": "skipped_no_reviewer",
                    "error": ReviewerUnavailableError("no_configured_reviewer", getattr(candidates, "diagnostics", [])).as_dict(),
                    "issues": [],
                    "sandbox_executed": False,
                }
                return result
            prompt = (
                "Review this completed Pacer task result. Return JSON only with an explicit boolean approved and issues.\n"
                "Task:\n" + str(getattr(plan, "goal", ""))[:2500]
                + "\nResult:\n" + json.dumps(result, ensure_ascii=False, default=str)[:2500]
            )
            try:
                content, error = _invoke_adversarial_reviewer(reviewer, prompt, root)
                match = re.search(r"\{[\s\S]*\}", content)
                verdict = json.loads(match.group(0)) if match else None
                approved = bool(
                    not error
                    and isinstance(verdict, dict)
                    and isinstance(verdict.get("approved"), bool)
                    and verdict["approved"]
                )
                result["review"] = {
                    "approved": approved,
                    "status": "reviewed_no_coder_changes" if approved else "review_rejected_or_invalid",
                    "issues": verdict.get("issues", []) if isinstance(verdict, dict) else [],
                    "model": reviewer.get("model"),
                    "family": reviewer.get("family"),
                    "sandbox_executed": False,
                }
                if not approved:
                    result["success"] = False
                    result["collaboration_error"] = error or "Reviewer did not return an explicit positive verdict"
                return result
            except Exception as exc:
                result["success"] = False
                result["collaboration_error"] = f"Reviewer failed: {exc}"
                result["review"] = {"approved": False, "status": "review_error", "sandbox_executed": False}
                return result
        result["review"] = {
            "approved": True,
            "status": "not_applicable_no_coder_changes",
            "issues": [],
            "cross_family_attempted": False,
            "cross_family_errors": [],
            "blind_fallback": False,
            "sandbox_executed": False,
        }
        return result
    if len(coders) > 1:
        # Each sandbox imports one task's module. Keep that task's contract and
        # symbols isolated even when failover put all writers on one family.
        from types import SimpleNamespace
        reviews = []
        for task in coders:
            scoped_goal = getattr(task, "instructions", "") or getattr(task, "title", "") or plan.goal
            scoped_plan = SimpleNamespace(goal=scoped_goal, tasks=[task])
            scoped_result = dict(result, effective_backend=task.backend, success=True)
            reviewed = review_implementation(scoped_plan, scoped_result, dict(routing), root, progress)
            review = dict(reviewed.get("review") or {})
            review["task_id"] = task.id
            reviews.append(review)
            if not reviewed.get("success"):
                result["success"] = False
                result["collaboration_error"] = reviewed.get("collaboration_error", "Adversarial review failed")
                result["review"] = review
                result["reviews"] = reviews
                return result
        result["reviews"] = reviews
        result["review"] = {
            "status": "per_coder",
            "approved": all(r.get("approved") is True for r in reviews),
            "sandbox_executed": all(r.get("sandbox_executed") is True for r in reviews),
            "reviews": reviews,
        }
        return result

    coder_family, coder_label = _effective_coder_family(result, plan)
    coder_families = {coder_family}
    for task in plan.tasks:
        if getattr(task, "role", "") == "coder":
            task_backend = str(getattr(task, "backend", "") or "")
            if task_backend.lower() in {"api", "domestic_api", "premium_api"}:
                for attempt in result.get("api_node_attempts", []):
                    if isinstance(attempt, dict) and attempt.get("success"):
                        coder_families.add(backend_model_family(
                            task_backend,
                            provider=str(attempt.get("provider") or ""),
                            model=str(attempt.get("model") or ""),
                        ))
            else:
                coder_families.add(coder_family if task_backend == coder_label
                                   else backend_model_family(task_backend))
    available_reviewers = _adversarial_reviewer_candidates()
    diagnostics = list(getattr(available_reviewers, "diagnostics", []))
    known_families = {"google", "openai", "anthropic", "deepseek", "moonshot", "qwen", "zhipu", "meta", "mistral", "xai"}
    heterogeneous_candidates = [item for item in available_reviewers
                                if coder_families <= known_families
                                and item.get("family") in known_families
                                and item.get("family") not in coder_families]
    if not heterogeneous_candidates:
        reason = "unknown_coder_family" if not coder_families <= known_families else "no_independent_reviewer"
        error = ReviewerUnavailableError(reason, diagnostics)
        result["success"] = False
        result["collaboration_error"] = str(error)
        result["review"] = {"approved": False, "status": "skipped_no_heterogeneous_reviewer",
                            "sandbox_executed": False, "coder_family": coder_family,
                            "error": error.as_dict(), "skipped_reasons": diagnostics}
        progress("[review] independent reviewer unavailable; acceptance blocked.\n")
        return result
    requested_reviewer = routing.get("reviewer")
    if requested_reviewer and requested_reviewer != "local":
        heterogeneous_candidates.sort(key=lambda item: item["backend"] != requested_reviewer)
    snippets = []
    acceptance_tests = []
    verification_evidence = []
    changed_python_files: list[str] = []
    target_contracts: list[str] = []
    for rel in dict.fromkeys(f for t in plan.tasks for f in t.patched_files):
        path = (root / rel).resolve()
        if path.is_relative_to(root) and path.is_file() and not any(p.startswith('.') for p in Path(rel).parts):
            source = path.read_text(encoding="utf-8", errors="replace")
            snippets.append(rel + "\n" + source[:2500])
            if path.suffix.lower() == ".py" and not is_test_path(rel):
                normalized_rel = str(rel).replace("\\", "/")
                changed_python_files.append(normalized_rel)
                try:
                    module_name = counterexample_module_name(normalized_rel, root)
                    module_tree = ast.parse(source)
                    signatures = []
                    for node in module_tree.body:
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                            signatures.append(f"{node.name}({ast.unparse(node.args)})")
                    target_contracts.append(
                        f"{normalized_rel}: import module `{module_name}`; public functions: "
                        + (", ".join(signatures) or "(inspect implementation)")
                    )
                except (OSError, SyntaxError, ValueError):
                    target_contracts.append(f"{normalized_rel}: public module under review")
    for task in plan.tasks:
        command = str(getattr(task, "verification_command", "") or "").strip()
        if command:
            task_status = str(getattr(task, "status", "") or "").lower()
            gate_status = "PASS" if result.get("success") and task_status == "completed" else "NOT VERIFIED"
            verification_evidence.append(f"{command} => {gate_status}")

        spec = task.acceptance_test if isinstance(getattr(task, "acceptance_test", None), dict) else {}
        rel_test = str(spec.get("path") or "").replace("\\", "/").strip()
        if not rel_test:
            continue
        rel_path = Path(rel_test)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            acceptance_tests.append(f"{rel_test} [rejected unsafe planner path]")
            continue
        test_path = (root / rel_path).resolve()
        if not test_path.is_relative_to(root) or not test_path.is_file():
            acceptance_tests.append(f"{rel_test} [locked test file unavailable]")
            continue
        test_source = test_path.read_text(encoding="utf-8", errors="replace")
        acceptance_tests.append(f"{rel_test} [Pacer-authored and locked before coding]\n{test_source[:2500]}")

    verification_text = "\n".join(verification_evidence) or "No per-task verification command was recorded."
    acceptance_text = "\n\n".join(acceptance_tests) or "No planner-authored acceptance test was required."
    counterexample_target = changed_python_files[0] if changed_python_files else ""
    contract_text = "\n".join(target_contracts) or (counterexample_target or "No Python target")
    review_prompt = (
        "You are the adversarial reviewer. The coder model family was " + coder_family + " (" + coder_label + "). "
        "Review the delivered implementation and try to break it with concrete boundary cases. "
        "Return ONLY JSON with this shape: "
        "{\"approved\":true,\"issues\":[],\"counter_examples\":[\"assert raises(ValueError, public_function, ...)\"]}. "
        "You MUST provide 1 to 3 executable, single-line Python assert probes for contract boundaries. "
        "Even when you find no defect, provide passing boundary probes that exercise the stated contract. "
        "Probe exceptions with the preloaded `raises(ValueError, function, ...)` helper. "
        "The sandbox pre-imports public symbols from each listed module and provides `target`, `Decimal`, `decimal`, `math`, and `raises`. "
        "Use the named public function in assertions; when a module has exactly one public function, `target(...)` is also accepted. "
        "You may write `assert public_function(...) == ...` or `assert raises(ValueError, public_function, ...)`. "
        "The module import paths and public function signatures are:\n"
        + contract_text
        + "\nUse one assert per counter_examples item. Imports and simple assignments immediately before an assert are also accepted. "
        "Avoid comprehensions, private attributes, filesystem access, and unrelated assertions. "
        "Challenge boundaries such as None, empty collections, zero, negatives, long input, or precision limits. "
        "Set approved=false when you identify a defect. Do not invent tests that merely restate the code. "
        "Pacer authored and locked the listed acceptance tests before coding, then ran the listed verification commands. "
        "The worker was not expected to create or re-provide those test files. Do not report a test as missing when it is listed below. "
        "Do not edit or propose file changes.\nTask:\n"
        + plan.goal[:2500]
        + "\nPacer local verification evidence:\n"
        + verification_text[:2500]
        + "\nPacer-locked acceptance tests:\n"
        + acceptance_text[:6000]
        + "\nImplementation:\n"
        + "\n".join(snippets)[:9000]
    )
    from .session_projection import SessionProjection
    review_candidates = []
    if heterogeneous_candidates:
        cross_family_attempted = True
        cross_family_errors: list[dict[str, str]] = []
        selected = None
        response_text = ""
        parsed_verdict: dict[str, Any] | None = None
        parse_error = ""
        for reviewer in heterogeneous_candidates:
            system_prompt = "" if reviewer["backend"] == "workbuddy" else "You are an adversarial implementation reviewer. Return only the requested JSON."
            SessionProjection(root).append_model_request(
                backend=str(reviewer["backend"]), system_prompt=system_prompt, user_prompt=review_prompt,
                phase="adversarial_review",
                metadata={"model": reviewer.get("model"), "family": reviewer.get("family"), "cross_family": True},
            )
            text, error = _invoke_adversarial_reviewer(reviewer, review_prompt, root)
            if error:
                cross_family_errors.append({"backend": str(reviewer["backend"]), "error": str(error)[:600]})
                continue
            extracted = extract_counterexample_assertions(text)
            candidate_verdict = None
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
            if not cleaned.startswith("{"):
                match = re.search(r"\{[\s\S]*\}", cleaned)
                if match:
                    cleaned = match.group(0)
            try:
                decoded = json.loads(cleaned)
                if isinstance(decoded, dict) and isinstance(decoded.get("approved"), bool):
                    candidate_verdict = decoded
                    extracted = extract_counterexample_assertions(
                        decoded.get("counter_examples", decoded.get("counterexamples", extracted))
                    )
            except ValueError:
                pass
            if candidate_verdict is None and extracted:
                candidate_verdict = {"approved": True, "issues": [], "counter_examples": extracted}
            if candidate_verdict is not None and not extracted:
                cross_family_errors.append({
                    "backend": str(reviewer["backend"]),
                    "error": "reviewer returned no executable assertions",
                })
                continue
            if candidate_verdict is None:
                cross_family_errors.append({"backend": str(reviewer["backend"]), "error": "reviewer returned no executable assertions"})
                continue
            selected, response_text, parsed_verdict = reviewer, text, candidate_verdict
            break
        review_candidates = cross_family_errors
    else:
        cross_family_attempted = False
        cross_family_errors = []
        selected = None
        response_text = ""
        parsed_verdict = None
        parse_error = "no available heterogeneous reviewer"

    blind_fallback = False

    if selected is None:
        result["success"] = False
        error = ReviewerUnavailableError("no_usable_independent_reviewer_response", diagnostics + cross_family_errors)
        result["collaboration_error"] = str(error)
        result["review"] = {
            "error": error.as_dict(),
            "approved": False,
            "status": "skipped_unconfigured_or_quota" if cross_family_attempted else "skipped_no_heterogeneous_reviewer",
            "model": "",
            "coder_family": coder_family,
            "coder_families": sorted(coder_families),
            "issues": [],
            "cross_family_attempted": cross_family_attempted,
            "cross_family_errors": cross_family_errors,
            "blind_fallback": blind_fallback,
            "sandbox_executed": False,
        }
        progress("[review] no usable reviewer response; acceptance blocked, physical verification retained.\n")
        return result

    candidate = selected
    routing["reviewer"] = candidate["backend"]
    routing["reviewer_model"] = candidate["model"]
    routing["reviewer_family"] = candidate["family"]
    reviewer_name = str(candidate.get("model") or candidate["backend"])
    verdict = parsed_verdict or {"approved": True, "issues": [], "counter_examples": extract_counterexample_assertions(response_text)}
    issues = verdict.get("issues", []) if isinstance(verdict.get("issues", []), list) else [str(verdict.get("issues"))]
    assertions = extract_counterexample_assertions(verdict.get("counter_examples", verdict.get("counterexamples", response_text)))
    if not counterexample_target:
        counterexample_result = {"passed": True, "status": "not_applicable_no_python_source"}
    elif not assertions:
        counterexample_result = {"passed": False, "status": "invalid_counterexample_ignored", "error": "Reviewer must provide one to three executable assert expressions."}
    else:
        counterexample_result = run_executable_counterexamples(root, counterexample_target, assertions)
    counterexample_status = counterexample_result.get("status")
    if counterexample_status in {"invalid_counterexample_ignored", "counterexample_execution_error"}:
        result["success"] = False
        result["collaboration_error"] = (
            "Adversarial review did not execute a valid sandbox assertion: "
            + str(counterexample_result.get("error") or counterexample_status)
        )
        result["review"] = {
            "approved": False,
            "status": counterexample_status,
            "issues": issues,
            "model": reviewer_name,
            "family": candidate["family"],
            "coder_family": coder_family,
            "coder_families": sorted(coder_families),
            "counter_examples": assertions if isinstance(assertions, list) else [],
            "counterexample_result": counterexample_result,
            "repair_required": False,
            "cross_family_attempted": cross_family_attempted,
            "cross_family_errors": cross_family_errors,
            "blind_fallback": blind_fallback,
            "sandbox_executed": False,
        }
        progress(f"[review] {counterexample_status}; review blocked, physical verification retained.\n")
        return result
    approved = bool(verdict.get("approved")) and bool(counterexample_result.get("passed"))
    sandbox_executed = counterexample_status == "passed" or counterexample_status == "counterexample_failed"
    result["review"] = {
        "approved": approved,
        "status": ("reviewed_blind_fallback" if blind_fallback and counterexample_result.get("passed") else
                   "reviewed" if counterexample_result.get("passed") else "counterexample_failed"),
        "issues": issues,
        "model": reviewer_name,
        "family": candidate["family"],
        "coder_family": coder_family,
        "coder_families": sorted(coder_families),
        "counter_examples": assertions if isinstance(assertions, list) else [],
        "counterexample_result": counterexample_result,
        "repair_required": counterexample_status == "counterexample_failed",
        "cross_family_attempted": cross_family_attempted,
        "cross_family_errors": cross_family_errors,
        "blind_fallback": blind_fallback,
        "sandbox_executed": sandbox_executed,
    }
    if verdict.get("approved") is False:
        result["success"] = False
        result["collaboration_error"] = f"Adversarial reviewer rejected the implementation: {issues}"
    elif not counterexample_result.get("passed"):
        result["success"] = False
        result["collaboration_error"] = (
            "Adversarial executable counter-example failed: "
            + str(counterexample_result.get("error") or counterexample_result.get("output") or "unknown failure")
        )
    progress(f"[review] approved={approved}; reviewer_family={candidate['family']}\n")
    return result
