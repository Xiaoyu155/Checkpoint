"""Agent capability profiles.

Codex and Claude Code gain features fast (new models, sandbox modes, parallel
execution). Instead of hard-coding that knowledge across the planner, Checkpoint
keeps a versioned YAML profile per agent in ``agent_profiles/``. To track a new
release, update the YAML — not the code. ``agents doctor`` probes what is
installed locally and compares it against the profile so a user can see the
capabilities they are not using yet.
"""

from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any


PROFILES_DIR = Path(__file__).resolve().parent / "agent_profiles"

# Normalize the many labels users type to a canonical profile name.
AGENT_ALIASES = {
    "codex": "codex",
    "openai-codex": "codex",
    "openai": "codex",
    "claude": "claude-code",
    "claude-code": "claude-code",
    "claudecode": "claude-code",
    "claude_code": "claude-code",
    # Low-cost hosted backends use the same unattended patch-worker adapter, but
    # they must not silently map to Claude Code.
    "bugteam": "mimo",
    "bug-team": "mimo",
    "gpt-bug-team": "mimo",
    "mimo": "mimo",
    "xiaomimimo": "mimo",
    "gemini": "gemini",
    "gemini-cli": "gemini",
    "google-gemini": "gemini",
}


def canonical_agent_name(agent: str) -> str:
    key = str(agent).strip().lower().replace(" ", "-")
    return AGENT_ALIASES.get(key, key)


@lru_cache(maxsize=None)
def _load_yaml(path_str: str) -> dict[str, Any]:
    import yaml

    text = Path(path_str).read_text(encoding="utf-8")
    payload = yaml.safe_load(text)
    return payload if isinstance(payload, dict) else {}


def load_agent_profile(agent: str) -> dict[str, Any] | None:
    """Return the capability profile for an agent, or None if unknown."""
    name = canonical_agent_name(agent)
    path = PROFILES_DIR / f"{name}.yaml"
    if not path.exists():
        return None
    return _load_yaml(str(path))


def list_agent_profiles() -> list[dict[str, Any]]:
    if not PROFILES_DIR.exists():
        return []
    profiles = []
    for path in sorted(PROFILES_DIR.glob("*.yaml")):
        payload = _load_yaml(str(path))
        if payload:
            profiles.append(payload)
    return profiles


def recommend_worker_config(profile: dict[str, Any], *, task_kind: str = "implementation") -> dict[str, Any]:
    """Pick a model + permission posture for a worker from the profile.

    ``task_kind`` is one of ``implementation`` (default), ``fast`` (small/
    mechanical), or ``inspection`` (read-only planning).
    """
    primary_role = str(profile.get("primary_role") or "").strip()
    if primary_role == "multimodal_inspection" and task_kind == "implementation":
        task_kind = "inspection"
    models = profile.get("models") if isinstance(profile.get("models"), list) else []
    model = _select_model(models, task_kind=task_kind)
    sandbox = _select_mode(profile.get("sandbox_modes"), task_kind=task_kind)
    approval = _select_mode(profile.get("approval_modes"), task_kind=task_kind, prefer_first=True)
    parallelism = profile.get("parallelism") if isinstance(profile.get("parallelism"), list) else []
    reasoning = profile.get("reasoning") if isinstance(profile.get("reasoning"), dict) else {}
    if not reasoning and isinstance(profile.get("reasoning_config"), dict):
        reasoning = profile["reasoning_config"]
    reasoning_effort = str(reasoning.get("default_strategy") or "inherit").strip() or "inherit"
    parallel_hint = ""
    if parallelism:
        first = parallelism[0]
        if isinstance(first, dict):
            parallel_hint = str(first.get("how") or "")
    return {
        "model": model,
        "model_flag": _fill_model_flag(profile.get("model_flag"), model),
        "sandbox": sandbox,
        "approval": approval,
        "reasoning_effort": reasoning_effort,
        "parallelism_hint": parallel_hint,
    }


def _select_model(models: list[Any], *, task_kind: str) -> str:
    wanted_role = {
        "fast": "fast",
        "balanced": "balanced",
        "inspection": "default",
        "implementation": "default",
    }.get(task_kind, "default")
    entries = [m for m in models if isinstance(m, dict) and m.get("id")]
    for entry in entries:
        if str(entry.get("role") or "") == wanted_role:
            return str(entry["id"])
    # No exact role (e.g. an agent without a "balanced" model): fall back to the
    # default model rather than failing.  If a profile only defines non-default
    # roles, keep implementation tasks on the user's CLI default instead of
    # accidentally downgrading strong work to a cheaper role.
    for entry in entries:
        if str(entry.get("role") or "") == "default":
            return str(entry["id"])
    return ""


def _select_mode(modes: Any, *, task_kind: str, prefer_first: bool = False) -> dict[str, str]:
    entries = [m for m in modes if isinstance(m, dict) and m.get("flag")] if isinstance(modes, list) else []
    if not entries:
        return {}
    if task_kind == "inspection":
        # Prefer a no-write / planning posture (risk none) when only inspecting.
        for entry in entries:
            if str(entry.get("risk") or "") == "none":
                return {"name": str(entry.get("name") or ""), "flag": str(entry["flag"])}
    if prefer_first:
        return {"name": str(entries[0].get("name") or ""), "flag": str(entries[0]["flag"])}
    # Default: the first low-risk writable posture, else the first entry.
    for entry in entries:
        if str(entry.get("risk") or "") in {"", "low"} and str(entry.get("name") or "") not in {"read-only", "plan"}:
            return {"name": str(entry.get("name") or ""), "flag": str(entry["flag"])}
    return {"name": str(entries[0].get("name") or ""), "flag": str(entries[0]["flag"])}


def _fill_model_flag(model_flag: Any, model: str) -> str:
    template = str(model_flag or "").strip()
    if not template or not model:
        return f"--model {model}".strip() if model else ""
    # Replace a "<...>" placeholder with the concrete model id.
    start = template.find("<")
    end = template.find(">")
    if 0 <= start < end:
        return template[:start] + model + template[end + 1 :]
    return f"{template} {model}"


def _probe_bypass_permissions(found: str, *, timeout: float = 6.0) -> bool:
    """Return True if the executable's --help output mentions bypassPermissions."""
    argv = [found, "--help"]
    if found.lower().endswith((".cmd", ".bat")):
        argv = ["cmd", "/c", found, "--help"]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = (completed.stdout or completed.stderr or "")
        return "bypassPermissions" in output
    except (OSError, subprocess.SubprocessError):
        return False


def probe_agent(profile: dict[str, Any], *, timeout: float = 6.0) -> dict[str, Any]:
    """Detect whether the agent's executable is installed and its version."""
    executable = str(profile.get("executable") or "").strip()
    result: dict[str, Any] = {
        "agent": str(profile.get("agent") or ""),
        "executable": executable,
        "installed": False,
        "path": "",
        "version": "",
        "verified_on": str(profile.get("verified_on") or ""),
    }
    if not executable:
        return result
    found = shutil.which(executable)
    if not found:
        return result
    result["installed"] = True
    result["path"] = found
    version_command = str(profile.get("version_command") or f"{executable} --version").strip()
    # Use the resolved path (Windows .CMD/.EXE shims are not found by bare name).
    version_args = version_command.split()[1:]
    argv = [found, *version_args]
    # CreateProcess cannot run .cmd/.bat shims directly; route them through cmd.
    if found.lower().endswith((".cmd", ".bat")):
        argv = ["cmd", "/c", found, *version_args]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = (completed.stdout or completed.stderr or "").strip()
        result["version"] = output.splitlines()[0].strip() if output else ""
    except (OSError, subprocess.SubprocessError):
        result["version"] = ""
    # Check bypassPermissions support if the profile lists it as a sandbox mode.
    sandbox_modes = profile.get("sandbox_modes") or []
    if isinstance(sandbox_modes, list) and any(
        "bypassPermissions" in str(m.get("name") or "") or "bypassPermissions" in str(m.get("flag") or "")
        for m in sandbox_modes
        if isinstance(m, dict)
    ):
        result["bypass_permissions_supported"] = _probe_bypass_permissions(found, timeout=timeout)
    return result


def agents_doctor(*, agents: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Report install status + often-missed capabilities per agent."""
    if agents:
        profiles = [p for p in (load_agent_profile(a) for a in agents) if p]
    else:
        profiles = list_agent_profiles()
    report = []
    for profile in profiles:
        probe = probe_agent(profile)
        missed = profile.get("capabilities_often_missed")
        report.append(
            {
                **probe,
                "display_name": str(profile.get("display_name") or profile.get("agent") or ""),
                "primary_role": str(profile.get("primary_role") or ""),
                "headless": (profile.get("headless") or {}).get("command", "") if isinstance(profile.get("headless"), dict) else "",
                "capabilities_often_missed": list(missed) if isinstance(missed, list) else [],
                "source": str(profile.get("source") or ""),
            }
        )
    return report


def agents_doctor_to_markdown(report: list[dict[str, Any]]) -> str:
    if not report:
        return "No agent capability profiles are available."
    lines = ["## Agent Capability Doctor", ""]
    for item in report:
        status = "installed" if item.get("installed") else "not found"
        version = f" ({item['version']})" if item.get("version") else ""
        lines.append(f"### {item.get('display_name')} — `{item.get('executable')}`: {status}{version}")
        if item.get("installed") and item.get("path"):
            lines.append(f"- Path: `{item['path']}`")
        if item.get("verified_on"):
            lines.append(f"- Profile verified on: {item['verified_on']}")
        if item.get("primary_role"):
            lines.append(f"- Role: `{item['primary_role']}`")
        if item.get("headless"):
            lines.append(f"- Headless: `{item['headless']}`")
        missed = item.get("capabilities_often_missed") or []
        if missed:
            lines.append("- Capabilities you may not be using:")
            lines.extend(f"  - {cap}" for cap in missed)
        if item.get("source"):
            lines.append(f"- Reference: {item['source']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def agent_profile_to_markdown(profile: dict[str, Any]) -> str:
    name = str(profile.get("display_name") or profile.get("agent") or "agent")
    lines = [f"## {name}", ""]
    if profile.get("summary"):
        lines.extend([str(profile["summary"]).strip(), ""])
    if profile.get("primary_role"):
        lines.append(f"Primary role: `{profile['primary_role']}`")
    headless = profile.get("headless")
    if isinstance(headless, dict) and headless.get("command"):
        lines.append(f"Headless: `{headless['command']}`")
    models = profile.get("models")
    if isinstance(models, list):
        lines.extend(["", "### Models", ""])
        for entry in models:
            if isinstance(entry, dict) and entry.get("id"):
                uses = ", ".join(entry.get("use_for", []) or [])
                lines.append(f"- `{entry['id']}` ({entry.get('role', '')}) — {uses}")
    for section, title in (("sandbox_modes", "Sandbox Modes"), ("approval_modes", "Approval Modes"), ("parallelism", "Parallelism")):
        entries = profile.get(section)
        if isinstance(entries, list) and entries:
            lines.extend(["", f"### {title}", ""])
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                label = entry.get("name") or entry.get("id") or ""
                detail = entry.get("flag") or entry.get("how") or ""
                risk = f" [risk: {entry['risk']}]" if entry.get("risk") else ""
                lines.append(f"- `{label}`: {detail}{risk}")
    return "\n".join(lines).rstrip()
