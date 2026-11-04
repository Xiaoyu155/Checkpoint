from __future__ import annotations

import json
from pathlib import Path

from visual_agent.dependency_bootstrap import bootstrap_worktree_dependencies, detect_bootstrap


def _node_project(root: Path, *, lockfile: str = "package-lock.json", installed: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(json.dumps({"name": "demo", "version": "1.0.0"}), encoding="utf-8")
    if lockfile:
        (root / lockfile).write_text("{}\n", encoding="utf-8")
    if installed:
        (root / "node_modules").mkdir(exist_ok=True)
        (root / "node_modules" / ".package-lock.json").write_text("{}\n", encoding="utf-8")
    return root


def test_node_project_without_dependencies_needs_a_bootstrap(tmp_path: Path) -> None:
    detection = detect_bootstrap(_node_project(tmp_path / "app"))

    assert detection["needed"] is True
    assert detection["manager"] == "npm"
    assert detection["lockfile"] == "package-lock.json"


def test_already_installed_project_is_left_alone(tmp_path: Path) -> None:
    detection = detect_bootstrap(_node_project(tmp_path / "app", installed=True))

    assert detection["needed"] is False
    assert detection["reason"] == "already_installed"


def test_lockfile_picks_the_matching_package_manager(tmp_path: Path) -> None:
    pnpm = detect_bootstrap(_node_project(tmp_path / "a", lockfile="pnpm-lock.yaml"))
    yarn = detect_bootstrap(_node_project(tmp_path / "b", lockfile="yarn.lock"))

    assert pnpm["manager"] == "pnpm"
    assert yarn["manager"] == "yarn"


def test_project_without_a_lockfile_is_not_bootstrapped(tmp_path: Path) -> None:
    detection = detect_bootstrap(_node_project(tmp_path / "app", lockfile=""))

    # A resolving install could pick different versions than the user runs;
    # only deterministic, lockfile-driven installs are safe to do behind them.
    assert detection["needed"] is False
    assert detection["reason"] == "no_lockfile"


def test_non_node_project_needs_nothing(tmp_path: Path) -> None:
    root = tmp_path / "rust"
    root.mkdir()
    (root / "Cargo.toml").write_text("[package]\n", encoding="utf-8")

    assert detect_bootstrap(root)["needed"] is False


def test_bootstrap_can_be_disabled(tmp_path: Path) -> None:
    result = bootstrap_worktree_dependencies(_node_project(tmp_path / "app"), enabled=False)

    assert result["status"] == "disabled"


def test_bootstrap_skips_when_nothing_is_needed(tmp_path: Path) -> None:
    result = bootstrap_worktree_dependencies(_node_project(tmp_path / "app", installed=True))

    assert result["status"] == "skipped"


def test_failed_bootstrap_reports_instead_of_raising(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    # package.json declares a dependency the lockfile does not carry, so
    # `npm ci` refuses offline and deterministically ("out of sync").
    (root / "package.json").write_text(
        json.dumps({"name": "demo", "version": "1.0.0", "dependencies": {"left-pad": "^1.3.0"}}),
        encoding="utf-8",
    )
    (root / "package-lock.json").write_text(
        json.dumps({"name": "demo", "version": "1.0.0", "lockfileVersion": 3, "packages": {}}),
        encoding="utf-8",
    )

    result = bootstrap_worktree_dependencies(root, timeout_seconds=180.0)

    # A failed bootstrap must degrade into evidence, never raise into the mission.
    assert result["status"] in {"failed", "unavailable", "timeout"}
    assert result["needed"] is True
