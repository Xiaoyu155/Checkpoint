from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .capabilities import Capability, build_capability_manifest
from .validation import ValidationResult, validate_workflow
from .workflow import Workflow


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    workflow_name: str
    strict: bool
    validation: ValidationResult
    missing_required_capabilities: tuple[Capability, ...]
    unavailable_used_capabilities: tuple[Capability, ...]
    warnings: tuple[str, ...]
    environment: dict[str, Any] | None = None
    project_type: str | None = None


def run_preflight(
    workflow: Workflow,
    *,
    strict: bool = False,
    allow_high_risk: bool = False,
    require_optional_capabilities: bool = False,
    workspace_root: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int | None = None,
    dist_dir: str | Path | None = None,
    max_age_minutes: int = 10,
) -> PreflightResult:
    validation = validate_workflow(workflow, strict=strict, allow_high_risk=allow_high_risk)
    manifest = build_capability_manifest()
    missing = tuple(capability for capability in manifest.capabilities if not capability.available)
    missing_required = tuple(
        capability
        for capability in missing
        if capability.required or (require_optional_capabilities and capability.kind != "dependency")
    )
    unavailable_used = unavailable_capabilities_used_by_workflow(workflow, missing)
    warnings = tuple(issue.message for issue in validation.issues if issue.level == "warning")
    ok = validation.valid and not missing_required and not unavailable_used
    environment = inspect_environment(
        Path(workspace_root).resolve() if workspace_root is not None else None,
        host=host,
        port=port,
        dist_dir=dist_dir,
        max_age_minutes=max_age_minutes,
    ) if workspace_root is not None else None
    return PreflightResult(
        ok=ok,
        workflow_name=workflow.name,
        strict=strict,
        validation=validation,
        missing_required_capabilities=missing_required,
        unavailable_used_capabilities=unavailable_used,
        warnings=warnings,
        environment=environment,
        project_type=(environment or {}).get("project_type") if environment else None,
    )


def unavailable_capabilities_used_by_workflow(
    workflow: Workflow,
    missing_capabilities: tuple[Capability, ...],
) -> tuple[Capability, ...]:
    missing_by_name = {capability.name: capability for capability in missing_capabilities}
    used_actions = {step.action for step in workflow.steps}
    unavailable = []
    for action in sorted(used_actions):
        capability = missing_by_name.get(action)
        if capability is not None:
            unavailable.append(capability)
    return tuple(unavailable)


def check_port_alive(host: str, port: int, timeout: float = 3.0) -> dict[str, Any]:
    start = datetime.now(timezone.utc)
    try:
        with socket.create_connection((host, int(port)), timeout=float(timeout)):
            return {
                "schema_version": 1,
                "host": host,
                "port": int(port),
                "ok": True,
                "status": "ok",
                "message": f"Port {host}:{port} is responding.",
                "suggestion": "",
                "checked_at": start.isoformat(timespec="seconds"),
            }
    except Exception as exc:
        return {
            "schema_version": 1,
            "host": host,
            "port": int(port),
            "ok": False,
            "status": "error",
            "message": f"Port {host}:{port} is not responding: {exc}",
            "suggestion": f"Start the dev server and try {host}:{port} again.",
            "checked_at": start.isoformat(timespec="seconds"),
        }


def check_build_freshness(dist_dir: str | Path, max_age_minutes: int = 10) -> dict[str, Any]:
    path = Path(dist_dir)
    now = datetime.now(timezone.utc)
    if not path.exists():
        return {
            "schema_version": 1,
            "path": str(path),
            "ok": False,
            "status": "missing",
            "message": f"Build output not found: {path}",
            "suggestion": f"Run the project build command so {path} is created.",
            "age_minutes": None,
        }
    latest_mtime = _latest_mtime(path)
    if latest_mtime is None:
        return {
            "schema_version": 1,
            "path": str(path),
            "ok": False,
            "status": "empty",
            "message": f"Build output directory is empty: {path}",
            "suggestion": f"Run the build command so {path} contains artifacts.",
            "age_minutes": None,
        }
    age_minutes = max(0.0, (now.timestamp() - latest_mtime.timestamp()) / 60.0)
    fresh = age_minutes <= float(max_age_minutes)
    return {
        "schema_version": 1,
        "path": str(path),
        "ok": fresh,
        "status": "ok" if fresh else "stale",
        "message": (
            f"Build output is fresh ({age_minutes:.1f} minutes old)."
            if fresh
            else f"Build output is stale ({age_minutes:.1f} minutes old, limit {max_age_minutes})."
        ),
        "suggestion": "" if fresh else "Rebuild the project before running workflow checks.",
        "age_minutes": round(age_minutes, 2),
    }


def detect_project_type(root: str | Path) -> str | None:
    project_root = Path(root)
    python_type = detect_python_project_type(project_root)
    if _has_path(project_root, "src", "manifest.json") or _has_path(project_root, "manifest.json"):
        return "uni-app"
    if _has_any_path(project_root, "dist", "dev", "h5") or _has_any_path(project_root, "dist", "build", "h5"):
        return "uni-app"
    if _has_any_path(project_root, "nuxt.config.ts") or _has_any_path(project_root, "nuxt.config.js") or _has_any_path(project_root, ".nuxt") or _has_any_path(project_root, ".output"):
        return "nuxt3"
    if _has_any_path(project_root, "next.config.js") or _has_any_path(project_root, "next.config.mjs") or _has_any_path(project_root, "next.config.ts"):
        return "nextjs"
    if _has_any_path(project_root, "vite.config.ts") or _has_any_path(project_root, "vite.config.js") or _has_any_path(project_root, "vite.config.mts"):
        return "vite"
    package_json = project_root / "package.json"
    if package_json.exists():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            package = {}
        deps: dict[str, Any] = {}
        if isinstance(package, dict):
            for key in ("dependencies", "devDependencies", "peerDependencies"):
                value = package.get(key)
                if isinstance(value, dict):
                    deps.update(value)
        dep_names = set(deps)
        if "next" in dep_names:
            return "nextjs"
        if "nuxt" in dep_names or "nuxt3" in dep_names:
            return "nuxt3"
        if "vite" in dep_names:
            return "vite"
        if python_type:
            return python_type
        if "vue" in dep_names and has_frontend_source(project_root, ".vue"):
            return "vue"
        if ("react" in dep_names or "react-dom" in dep_names) and (
            has_frontend_source(project_root, ".tsx") or has_frontend_source(project_root, ".jsx")
        ):
            return "react"
    if python_type:
        return python_type
    if any((project_root / name).exists() for name in ("pyproject.toml", "setup.py", "setup.cfg", "tox.ini")):
        return "python"
    if _has_path(project_root, "manage.py"):
        return "django"
    requirements = project_root / "requirements.txt"
    if requirements.exists():
        text = requirements.read_text(encoding="utf-8", errors="ignore").lower()
        if "django" in text:
            return "django"
        if "fastapi" in text:
            return "fastapi"
        if "flask" in text:
            return "flask"
    if _has_any_extension(project_root, ".vue"):
        return "vue"
    if _has_any_extension(project_root, ".tsx") or _has_any_extension(project_root, ".jsx"):
        return "react"
    if _has_any_extension(project_root, ".html"):
        return "html"
    return None


def detect_python_project_type(project_root: Path) -> str | None:
    if _has_path(project_root, "manage.py"):
        return "django"
    requirements = project_root / "requirements.txt"
    if requirements.exists():
        text = requirements.read_text(encoding="utf-8", errors="ignore").lower()
        if "django" in text:
            return "django"
        if "fastapi" in text:
            return "fastapi"
        if "flask" in text:
            return "flask"
        return "python"
    if any((project_root / name).exists() for name in ("pyproject.toml", "setup.py", "setup.cfg")):
        return "python"
    return None


def has_frontend_source(project_root: Path, extension: str) -> bool:
    ignored = {".venv", "venv", "node_modules", ".agent-workspace", "dist", "build", ".next", ".nuxt", ".output"}
    for path in project_root.rglob(f"*{extension}"):
        if any(part in ignored for part in path.parts):
            continue
        return True
    return False


def inspect_environment(
    root: Path | None,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    dist_dir: str | Path | None = None,
    max_age_minutes: int = 10,
) -> dict[str, Any]:
    project_root = root.resolve() if root is not None else Path.cwd().resolve()
    project_type = detect_project_type(project_root)
    recommended_port = port or recommended_project_port(project_type)
    port_check = check_port_alive(host, recommended_port) if recommended_port is not None else None
    build_candidates = candidate_build_dirs(project_root, project_type, dist_dir=dist_dir)
    build_checks = [check_build_freshness(candidate, max_age_minutes=max_age_minutes) for candidate in build_candidates]
    any_fresh_build = any(check.get("ok") is True for check in build_checks)
    port_ok = True if port_check is None else bool(port_check.get("ok"))
    build_ok = True if not build_checks else any_fresh_build
    ok = port_ok and build_ok
    status = "OK" if ok else "WARN"
    warnings = []
    if not port_ok and port_check is not None:
        warnings.append(port_check.get("message"))
    if build_checks and not any_fresh_build:
        warnings.append("No fresh build artifacts found.")
    return {
        "schema_version": 1,
        "project_root": str(project_root),
        "project_type": project_type or "unknown",
        "status": status,
        "ok": ok,
        "host": host,
        "port": recommended_port,
        "port_check": port_check,
        "build_checks": build_checks,
        "warnings": [warning for warning in warnings if warning],
        "recommendations": environment_recommendations(project_type, port_check, build_checks),
    }


def dependency_preflight(repo_root: str | Path, test_command: str | None) -> dict[str, Any]:
    root = Path(repo_root).expanduser().resolve()
    command = str(test_command or "")
    if _command_mentions_pytest(command) or (_is_python_project(root) and not (root / "package.json").exists()):
        return _python_dependency_preflight(root, command)
    if (root / "package.json").exists():
        return _node_dependency_preflight(root)
    if (root / "Cargo.toml").exists():
        return _basic_dependency_preflight(package_manager="cargo", lockfile="Cargo.lock" if (root / "Cargo.lock").exists() else "")
    if (root / "go.mod").exists():
        return _basic_dependency_preflight(package_manager="go", lockfile="go.sum" if (root / "go.sum").exists() else "")
    return _basic_dependency_preflight(package_manager="none", lockfile="")


def _node_dependency_preflight(root: Path) -> dict[str, Any]:
    package = _read_package_json(root)
    package_manager, lockfile = _node_package_manager_and_lockfile(root)
    installed_lock = root / "node_modules" / ".package-lock.json"
    deps_installed = bool(lockfile and installed_lock.exists())
    native_install_risk = _node_native_install_risk(package)
    warnings: list[str] = []
    if not lockfile:
        warnings.append("node_lockfile_missing")
    if not deps_installed:
        warnings.append("node_dependencies_not_installed")
    if native_install_risk:
        warnings.append("native_dependency_risk")
    return {
        "package_manager": package_manager,
        "lockfile": lockfile,
        "deps_installed": deps_installed,
        "cache_available": _node_cache_available(root),
        "native_install_risk": native_install_risk,
        "estimated_install_minutes": 0 if deps_installed else (12 if native_install_risk else 9),
        "warnings": warnings,
    }


def _python_dependency_preflight(root: Path, command: str) -> dict[str, Any]:
    deps_installed = _python_pytest_importable(command)
    warnings = [] if deps_installed else ["pytest_not_importable"]
    return {
        "package_manager": "pip",
        "lockfile": "requirements.txt" if (root / "requirements.txt").exists() else "",
        "deps_installed": deps_installed,
        "cache_available": _python_cache_available(root),
        "native_install_risk": False,
        "estimated_install_minutes": 0 if deps_installed else 5,
        "warnings": warnings,
    }


def _basic_dependency_preflight(*, package_manager: str, lockfile: str) -> dict[str, Any]:
    return {
        "package_manager": package_manager,
        "lockfile": lockfile,
        "deps_installed": True,
        "cache_available": False,
        "native_install_risk": False,
        "estimated_install_minutes": 0,
        "warnings": [],
    }


def _read_package_json(root: Path) -> dict[str, Any]:
    try:
        payload = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _node_package_manager_and_lockfile(root: Path) -> tuple[str, str]:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm", "pnpm-lock.yaml"
    if (root / "package-lock.json").exists():
        return "npm", "package-lock.json"
    if (root / "yarn.lock").exists():
        return "npm", "yarn.lock"
    return "npm", ""


def _node_native_install_risk(package: dict[str, Any]) -> bool:
    risky = {"node-gyp", "sharp", "canvas", "sqlite3", "bcrypt"}
    deps = package.get("dependencies") if isinstance(package.get("dependencies"), dict) else {}
    return any(str(name) in risky for name in deps)


def _node_cache_available(root: Path) -> bool:
    return any((root / path).exists() for path in (".npm-cache", "node_modules/.cache"))


def _python_cache_available(root: Path) -> bool:
    return any((root / path).exists() for path in (".pip-cache", ".cache/pip"))


def _is_python_project(root: Path) -> bool:
    markers = ("pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "requirements.txt")
    return any((root / name).exists() for name in markers) or any((root / name).is_dir() for name in ("tests", "test"))


def _command_mentions_pytest(command: str) -> bool:
    return "pytest" in str(command or "").lower()


def _python_pytest_importable(command: str) -> bool:
    argv = _python_interpreter_argv(command)
    try:
        completed = subprocess.run(
            [*argv, "-c", "import pytest"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
            **_python_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _python_interpreter_argv(command: str) -> list[str]:
    try:
        tokens = shlex.split(str(command or ""), posix=os.name != "nt")
    except ValueError:
        tokens = []
    if not tokens:
        return [sys.executable]
    tokens = [_strip_outer_quotes(token) for token in tokens]
    executable = Path(tokens[0]).name.lower()
    if executable == "py" or executable.startswith("py."):
        argv = [tokens[0]]
        for token in tokens[1:]:
            if token.startswith("-") and len(token) > 1 and token[1].isdigit():
                argv.append(token)
                continue
            break
        return argv
    if executable.startswith("python"):
        return [tokens[0]]
    return [sys.executable]


def _strip_outer_quotes(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _python_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": startupinfo, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def candidate_build_dirs(project_root: Path, project_type: str | None, *, dist_dir: str | Path | None = None) -> list[Path]:
    if dist_dir is not None:
        return [Path(dist_dir) if Path(dist_dir).is_absolute() else project_root / Path(dist_dir)]
    if project_type == "nextjs":
        return [project_root / ".next"]
    if project_type == "nuxt3":
        return [project_root / ".output", project_root / ".nuxt"]
    if project_type == "vite":
        return [project_root / "dist", project_root / "build"]
    if project_type == "uni-app":
        return [project_root / "dist" / "dev" / "h5", project_root / "dist" / "build" / "h5"]
    return [project_root / "dist", project_root / "build"]


def recommended_project_port(project_type: str | None) -> int | None:
    return {
        "nextjs": 3000,
        "nuxt3": 3000,
        "vite": 5173,
        "vue": 5173,
        "react": 3000,
        "uni-app": 8080,
    }.get(str(project_type or ""))


def environment_recommendations(
    project_type: str | None,
    port_check: dict[str, Any] | None,
    build_checks: list[dict[str, Any]],
) -> list[str]:
    recommendations = []
    if port_check is not None and not port_check.get("ok"):
        recommendations.append(str(port_check.get("suggestion") or "Start the local dev server."))
    if build_checks and not any(check.get("ok") for check in build_checks):
        recommendations.append("Rebuild the project output before retrying.")
    if project_type == "uni-app":
        recommendations.append("Check dist/dev/h5 or dist/build/h5 for fresh H5 artifacts.")
    return recommendations


def _latest_mtime(path: Path) -> datetime | None:
    latest: datetime | None = None
    if path.is_file():
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        current = datetime.fromtimestamp(item.stat().st_mtime, tz=timezone.utc)
        if latest is None or current > latest:
            latest = current
    if latest is None and path.exists():
        stat = path.stat()
        return datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    return latest


def _has_path(root: Path, *parts: str) -> bool:
    return (root.joinpath(*parts)).exists()


def _has_any_path(root: Path, *parts: str) -> bool:
    return (root.joinpath(*parts)).exists()


def _has_any_extension(root: Path, extension: str) -> bool:
    return any(root.rglob(f"*{extension}"))
