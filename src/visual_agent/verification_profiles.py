from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any


SCRIPT_PREFERENCE = (
    "test",
    "check",
    "verify",
    "lint",
    "build",
)


def detect_verification_profiles(repo_root: str | Path) -> list[dict[str, Any]]:
    """Best-effort test/build command discovery for ordinary projects."""
    root = Path(repo_root).expanduser().resolve()
    profiles: list[dict[str, Any]] = []
    profiles.extend(_node_profiles(root))
    profiles.extend(_python_profiles(root, base="HEAD"))
    profiles.extend(_rust_profiles(root))
    profiles.extend(_go_profiles(root))
    return _dedupe_profiles(profiles)


def choose_verification_command(repo_root: str | Path) -> str:
    profiles = detect_verification_profiles(repo_root)
    return str(profiles[0]["command"]) if profiles else ""


def build_test_plan(repo_root: str | Path, *, base: str = "HEAD") -> dict[str, Any]:
    root = Path(repo_root).expanduser().resolve()
    profiles: list[dict[str, Any]] = []
    profiles.extend(_node_profiles(root))
    profiles.extend(_python_profiles(root, base=base))
    profiles.extend(_rust_profiles(root))
    profiles.extend(_go_profiles(root))
    profiles = _dedupe_profiles(profiles)
    command = str(profiles[0]["command"]) if profiles else ""
    return {
        "status": "found" if command else "not_found",
        "command": command,
        "base": base,
        "profiles": profiles,
    }


def resolve_test_command(command: str | None, *, repo_root: str | Path) -> tuple[str | None, dict[str, Any] | None]:
    text = str(command or "").strip()
    if not text:
        return None, None
    if text and text.lower() != "auto":
        return text, None
    plan = build_test_plan(repo_root)
    detected = str(plan.get("command") or "")
    if not detected:
        return (None if not text else ""), {"source": "auto", "status": "not_found", "profiles": []}
    return detected, {"source": "auto", **plan}


def estimate_verification_timeout(repo_root: str | Path, command: str, base_timeout: float) -> float:
    timeout = float(base_timeout)
    if verification_timeout_reason(repo_root, command):
        return timeout + 1200.0
    return timeout


def verification_timeout_reason(repo_root: str | Path, command: str) -> str:
    root = Path(repo_root).expanduser().resolve()
    text = str(command or "").lower()
    if _is_node_project(root):
        marker = _conditional_node_dependency_marker(text)
        if marker and not (root / marker).exists():
            return "missing_node_dependency_marker"
        if not (root / "node_modules").exists():
            return "missing_node_modules"
        if not marker and _contains_node_install_command(text):
            return "node_dependency_bootstrap"
    if "pytest" in text and _is_python_project(root) and not (root / ".venv").exists():
        return "missing_python_venv"
    return ""


def conditional_test_command_short_circuit(repo_root: str | Path, command: str) -> dict[str, Any]:
    root = Path(repo_root).expanduser().resolve()
    text = _normalize_windows_command_operators(str(command or ""))
    marker = _conditional_node_dependency_marker(text)
    if marker is None or not (root / marker).exists():
        return {}
    if not re.search(
        r"\bif\s+not\s+exist\s+"
        + re.escape(marker.as_posix()).replace("/", r"[\\/]")
        + r"\s+npm\s+(ci|install)\b(?:(?!&&).)*&&\s*\S+",
        text,
        re.IGNORECASE | re.DOTALL,
    ):
        return {}
    return {
        "status": "blocked",
        "reason": "conditional_test_command_short_circuit",
        "marker": marker.as_posix(),
        "message": (
            "The verification command uses `if not exist <node_modules marker> npm ci ... && <tests>`. "
            "Because the marker already exists, Windows cmd skips the test command and can return exit 0 "
            "without running acceptance. Use a command that always runs the tests after the optional install."
        ),
    }


def _normalize_windows_command_operators(command: str) -> str:
    return str(command or "").replace("^&^&", "&&").replace("^&", "&")


def _conditional_node_dependency_marker(command: str) -> Path | None:
    match = re.search(
        r"if\s+not\s+exist\s+(?P<path>node_modules[\\/][^\s&|]+[\\/]package\.json)\s+npm\s+(ci|install)\b",
        command,
        re.IGNORECASE,
    )
    if not match:
        return None
    return Path(match.group("path").replace("\\", "/"))


def _contains_node_install_command(command: str) -> bool:
    return bool(re.search(r"\b(npm|pnpm|yarn)\s+(ci|install)\b", command, re.IGNORECASE))


def _node_profiles(root: Path) -> list[dict[str, Any]]:
    package_json = root / "package.json"
    if not package_json.exists():
        return []
    try:
        package = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    scripts = package.get("scripts") if isinstance(package, dict) else {}
    if not isinstance(scripts, dict):
        return []
    manager = _node_package_manager(root)
    profiles = []
    if "test" in scripts:
        profiles.append(
            {
                "kind": "node",
                "name": "test",
                "command": f"{manager} test" if manager != "npm" else "npm test",
                "reason": "package.json test script",
            },
        )
    for name in SCRIPT_PREFERENCE:
        if name == "test":
            continue
        if name in scripts:
            profiles.append(
                {
                    "kind": "node",
                    "name": name,
                    "command": f"{manager} run {name}" if manager != "npm" else f"npm run {name}",
                    "reason": f"package.json script '{name}'",
                }
            )
    return profiles


def _python_profiles(root: Path, *, base: str) -> list[dict[str, Any]]:
    markers = ("pytest.ini", "tox.ini", "pyproject.toml", "setup.cfg")
    has_tests = any((root / name).exists() for name in markers) or any((root / name).is_dir() for name in ("tests", "test"))
    if not has_tests:
        return []
    focused = _focused_pytest_profile(root, base=base)
    if focused is not None:
        return [focused, {"kind": "python", "name": "pytest", "command": "python -m pytest -q", "reason": "python test markers"}]
    return [{"kind": "python", "name": "pytest", "command": "python -m pytest -q", "reason": "python test markers"}]


def _focused_pytest_profile(root: Path, *, base: str) -> dict[str, Any] | None:
    changed = _git_changed_paths(root, base=base)
    if not changed:
        return None
    selected: list[str] = []
    fallback_to_full = False
    for rel in changed:
        path = rel.replace("\\", "/").strip("/")
        if not path or path.startswith((".agent-workspace/", ".runs/", "runs/")):
            continue
        if path.startswith(("tests/", "test/")) and path.endswith(".py"):
            selected.append(path)
            continue
        if path.endswith(".py"):
            mapped = _candidate_tests_for_python_path(root, path)
            if mapped:
                selected.extend(mapped)
            else:
                fallback_to_full = True
        elif path in {"pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg"}:
            fallback_to_full = True
    selected = sorted(dict.fromkeys(selected))
    if not selected or fallback_to_full:
        return None
    return {
        "kind": "python",
        "name": "pytest-focused",
        "command": "python -m pytest -q " + " ".join(_quote_arg(item) for item in selected),
        "reason": "git changed files mapped to pytest targets",
        "changed_paths": changed,
        "targets": selected,
    }


def _candidate_tests_for_python_path(root: Path, rel_path: str) -> list[str]:
    path = Path(rel_path)
    stem = path.stem
    if stem == "__init__":
        return []
    candidates = [
        Path("tests") / f"test_{stem}.py",
        Path("test") / f"test_{stem}.py",
    ]
    parts = path.parts
    if len(parts) >= 3 and parts[0] == "src":
        package_parts = parts[2:-1]
        candidates.append(Path("tests").joinpath(*package_parts, f"test_{stem}.py"))
    return [candidate.as_posix() for candidate in candidates if (root / candidate).exists()]


def _git_changed_paths(root: Path, *, base: str) -> list[str]:
    commands = (
        ("git", "-C", str(root), "diff", "--name-only", base, "--"),
        ("git", "-C", str(root), "diff", "--name-only", "--cached", "--"),
        ("git", "-C", str(root), "ls-files", "--others", "--exclude-standard"),
    )
    changed: list[str] = []
    for command in commands:
        try:
            result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue
        changed.extend(line.strip() for line in result.stdout.splitlines() if line.strip())
    return sorted(dict.fromkeys(changed))


def _quote_arg(value: str) -> str:
    if not value or any(char.isspace() for char in value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _rust_profiles(root: Path) -> list[dict[str, Any]]:
    if (root / "Cargo.toml").exists():
        return [{"kind": "rust", "name": "cargo-test", "command": "cargo test", "reason": "Cargo.toml"}]
    return []


def _go_profiles(root: Path) -> list[dict[str, Any]]:
    if (root / "go.mod").exists():
        return [{"kind": "go", "name": "go-test", "command": "go test ./...", "reason": "go.mod"}]
    return []


def _node_package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    return "npm"


def _is_node_project(root: Path) -> bool:
    return (root / "package.json").exists()


def _is_python_project(root: Path) -> bool:
    markers = ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg", "setup.py", "requirements.txt")
    return any((root / name).exists() for name in markers) or any((root / name).is_dir() for name in ("tests", "test"))


def _dedupe_profiles(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for profile in profiles:
        command = str(profile.get("command") or "").strip()
        if not command or command in seen:
            continue
        seen.add(command)
        kept.append(profile)
    return kept
