from __future__ import annotations

from visual_agent.agent_capabilities import (
    agents_doctor,
    canonical_agent_name,
    list_agent_profiles,
    load_agent_profile,
    recommend_worker_config,
)


def test_canonical_agent_name_normalizes_aliases() -> None:
    assert canonical_agent_name("Codex") == "codex"
    assert canonical_agent_name("claude") == "claude-code"
    assert canonical_agent_name("claude_code") == "claude-code"
    assert canonical_agent_name("gemini-cli") == "gemini"


def test_load_agent_profiles_present() -> None:
    names = {profile["agent"] for profile in list_agent_profiles()}
    assert {"codex", "claude-code", "gemini"} <= names
    assert load_agent_profile("unknown-agent") is None


def test_recommend_worker_config_codex_picks_model_and_writable_sandbox() -> None:
    profile = load_agent_profile("codex")
    config = recommend_worker_config(profile, task_kind="implementation")
    assert config["model"] == ""
    assert "--sandbox" in config["sandbox"]["flag"]
    assert config["approval"] == {}
    # Codex uses the user's Codex CLI default model for subscription compatibility.
    fast = recommend_worker_config(profile, task_kind="fast")
    assert fast["model"] == ""


def test_recommend_worker_config_inspection_prefers_no_write() -> None:
    profile = load_agent_profile("claude-code")
    config = recommend_worker_config(profile, task_kind="inspection")
    # Plan mode is the risk:none posture for Claude Code.
    assert config["sandbox"]["name"] == "plan"


def test_recommend_worker_config_gemini_is_inspection_first() -> None:
    profile = load_agent_profile("gemini")
    config = recommend_worker_config(profile, task_kind="implementation")
    assert config["sandbox"]["name"] == "read-only"
    assert "gemini" in config["model"]


def test_agents_doctor_reports_install_status_and_capabilities() -> None:
    report = agents_doctor(agents=("codex",))
    assert len(report) == 1
    item = report[0]
    assert item["agent"] == "codex"
    assert "installed" in item
    assert item["capabilities_often_missed"]
