from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Callable


def _project_root(target_root: Path) -> Path:
    for candidate in (target_root, target_root / "xiao"):
        if (candidate / "src" / "visual_agent" / "cli.py").is_file():
            return candidate
    raise AssertionError(f"Could not locate the xiao project under {target_root}")


def _invoke(main: Callable[[list[str]], int], args: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            code = main(args)
        except SystemExit as exc:
            code = int(exc.code or 0)
    return int(code or 0), stdout.getvalue(), stderr.getvalue()


def _assert_non_doctor_flag_policy(main: Callable[[list[str]], int]) -> None:
    cases = (
        ["agents", "list", "--format", "json"],
        ["agents", "show", "codex", "--format", "json"],
    )
    notice_markers = ("ignored", "doctor-only", "doctor only", "only applies to agents doctor")
    for default_args in cases:
        code, default_output, default_error = _invoke(main, default_args)
        assert code == 0, f"default {' '.join(default_args[:2])} failed: {default_error or default_output}"

        flagged_args = [*default_args[:2], "--installed-only", *default_args[2:]]
        code, flagged_output, flagged_error = _invoke(main, flagged_args)
        if code != 0:
            continue
        assert flagged_output == default_output, (
            f"--installed-only changed {' '.join(default_args[:2])} output instead of being ignored"
        )
        notice = flagged_error.lower()
        assert any(marker in notice for marker in notice_markers), (
            f"--installed-only was silently ignored by {' '.join(default_args[:2])}; "
            "emit an ignored/doctor-only notice or reject the option"
        )


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: b2_agents_doctor_installed_only.py <target-root>", file=sys.stderr)
        return 2

    project = _project_root(Path(argv[0]).expanduser().resolve())
    sys.path.insert(0, str(project / "src"))

    from visual_agent import agent_capabilities as capabilities
    from visual_agent.cli import main as cli_main

    profiles = [
        {
            "agent": "codex",
            "display_name": "Codex Test Agent",
            "executable": "codex",
            "capabilities_often_missed": ["headless"],
        },
        {
            "agent": "claude-code",
            "display_name": "Unavailable Test Agent",
            "executable": "claude",
            "capabilities_often_missed": ["worktrees"],
        },
    ]
    by_name = {str(profile["agent"]): profile for profile in profiles}
    capabilities.list_agent_profiles = lambda: list(profiles)
    capabilities.load_agent_profile = lambda agent: by_name.get(capabilities.canonical_agent_name(agent))
    capabilities.probe_agent = lambda profile, **_kwargs: {
        "agent": str(profile["agent"]),
        "executable": str(profile["executable"]),
        "installed": profile["agent"] == "codex",
        "path": "C:/tools/codex.exe" if profile["agent"] == "codex" else "",
        "version": "test-version" if profile["agent"] == "codex" else "",
        "verified_on": "2026-07-10",
    }

    code, output, error = _invoke(cli_main, ["agents", "doctor", "--format", "json"])
    assert code == 0, f"default agents doctor failed: {error or output}"
    default_records = json.loads(output)
    assert [item["agent"] for item in default_records] == ["codex", "claude-code"]
    assert all("installed" in item for item in default_records)

    code, output, error = _invoke(
        cli_main,
        ["agents", "doctor", "--installed-only", "--format", "json"],
    )
    assert code == 0, f"--installed-only JSON was not accepted: {error or output}"
    installed_records = json.loads(output)
    assert [item["agent"] for item in installed_records] == ["codex"]
    assert all(item.get("installed") is True for item in installed_records)

    code, output, error = _invoke(
        cli_main,
        ["agents", "doctor", "--installed-only", "--format", "markdown"],
    )
    assert code == 0, f"--installed-only markdown failed: {error or output}"
    assert "Codex Test Agent" in output
    assert "Unavailable Test Agent" not in output

    code, output, error = _invoke(
        cli_main,
        ["agents", "doctor", "claude-code", "--installed-only", "--format", "json"],
    )
    assert code == 0, f"explicit unavailable agent failed: {error or output}"
    assert json.loads(output) == []

    _assert_non_doctor_flag_policy(cli_main)

    print("B2_PRIVATE_VERIFIER_OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except AssertionError as exc:
        print(f"B2_PRIVATE_VERIFIER_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
