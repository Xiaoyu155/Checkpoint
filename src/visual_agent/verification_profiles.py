from __future__ import annotations

import json
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
    profiles.extend(_python_profiles(root))
    profiles.extend(_rust_profiles(root))
    profiles.extend(_go_profiles(root))
    return _dedupe_profiles(profiles)


def choose_verification_command(repo_root: str | Path) -> str:
    profiles = detect_verification_profiles(repo_root)
    return str(profiles[0]["command"]) if profiles else ""


def resolve_test_command(command: str | None, *, repo_root: str | Path) -> tuple[str | None, dict[str, Any] | None]:
    text = str(command or "").strip()
    if text and text.lower() != "auto":
        return text, None
    detected = choose_verification_command(repo_root)
    if not detected:
        return (None if not text else ""), {"source": "auto", "status": "not_found", "profiles": []}
    return detected, {"source": "auto", "status": "found", "command": detected, "profiles": detect_verification_profiles(repo_root)}


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


def _python_profiles(root: Path) -> list[dict[str, Any]]:
    markers = ("pytest.ini", "tox.ini", "pyproject.toml", "setup.cfg")
    has_tests = any((root / name).exists() for name in markers) or any((root / name).is_dir() for name in ("tests", "test"))
    if not has_tests:
        return []
    return [{"kind": "python", "name": "pytest", "command": "python -m pytest -q", "reason": "python test markers"}]


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
