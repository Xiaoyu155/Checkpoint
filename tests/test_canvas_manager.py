from __future__ import annotations

from visual_agent.canvas_manager import CanvasManager, estimate_tokens
from visual_agent.session_projection import SessionProjection


def test_stateless_prompt_keeps_static_prefix_before_dynamic_tail(tmp_path):
    target = tmp_path / "src" / "pricing.py"
    target.parent.mkdir(parents=True)
    target.write_text("def calc_short_position_size(balance, leverage, risk_ratio):\n    return 0\n", encoding="utf-8")
    canvas = CanvasManager(tmp_path)
    canvas.update_goal("Refine the position threshold")
    canvas.add_invariants("Price and position ratios use Decimal with four places, ROUND_HALF_UP.")
    canvas.upsert_focus_file(
        "src/pricing.py",
        symbols=["def calc_short_position_size(balance, leverage, risk_ratio)"],
        imports=["decimal.Decimal"],
    )
    prompt = canvas.build_stateless_prompt(
        "Change the threshold after the latest verification error.",
        context_profile="Stable project context profile.",
    )
    retry_prompt = canvas.build_stateless_prompt(
        "Retry the threshold update after AssertionError.",
        context_profile="Stable project context profile.",
    )

    contract = prompt.index("[1. FIXED SYSTEM CONTRACT]")
    invariants = prompt.index("[2. PROJECT INVARIANTS]")
    ast_signatures = prompt.index("[3. TARGET FILE AST SIGNATURES]")
    dynamic = prompt.index("[4. DYNAMIC TASK AND EXECUTION ERRORS]")
    assert contract < invariants < ast_signatures < dynamic
    assert "calc_short_position_size" in prompt
    assert "Stable project context profile." in prompt
    assert estimate_tokens(prompt) <= 1500
    assert prompt.split("[4. DYNAMIC TASK AND EXECUTION ERRORS]", 1)[0] == retry_prompt.split(
        "[4. DYNAMIC TASK AND EXECUTION ERRORS]", 1
    )[0]
    assert SessionProjection(tmp_path).snapshot()["latest_projected_prompt"]["prompt"] == retry_prompt


def test_recent_focus_files_survive_focus_reset_and_are_projected(tmp_path):
    target = tmp_path / "sandbox_exam" / "pricing.py"
    target.parent.mkdir(parents=True)
    target.write_text("def calc_short_position_size(balance, leverage, risk_ratio):\n    return 1\n", encoding="utf-8")
    canvas = CanvasManager(tmp_path)
    canvas.set_recent_focus_files(["sandbox_exam/pricing.py"])

    # A new task clears stale AST excerpts; the prior turn's focus path is retained and re-indexed.
    canvas.update_goal("Change the last threshold", reset_focus=True)
    state = canvas.load()
    prompt = canvas.build_stateless_prompt("Change the last threshold")

    assert state.recent_focus_files == ["sandbox_exam/pricing.py"]
    assert "sandbox_exam/pricing.py" in prompt
    assert "calc_short_position_size" in prompt


def test_set_recent_focus_files_deduplicates_paths(tmp_path):
    canvas = CanvasManager(tmp_path)
    canvas.set_recent_focus_files(["src/a.py", "src/a.py", "src/b.py"])

    reloaded = CanvasManager(tmp_path).load()

    assert reloaded.recent_focus_files == ["src/a.py", "src/b.py"]


def test_large_prompt_reserves_focus_and_keeps_complete_patch_format(tmp_path):
    canvas = CanvasManager(tmp_path, max_prompt_tokens=1500)
    canvas.update_goal("Repair the focus regression " + "goal " * 500)
    canvas.add_invariants(*[f"Invariant {index}: keep this rule intact. " * 30 for index in range(8)])
    canvas.upsert_focus_file(
        "src/critical.py",
        symbols=[f"def critical_operation_{index}(value)" for index in range(40)],
        target_snippet="\n".join(f"    value_{index} = source_{index} + 1" for index in range(500)),
    )
    canvas.update_scratchpad(latest_error="Traceback and error context " * 800)

    prompt = canvas.build_stateless_prompt(
        "Fix the critical operation " * 500,
        acceptance_exam=[{"id": f"exam_{index}", "question": "Check behavior " * 80} for index in range(20)],
        context_profile="Project profile " * 700,
    )

    focus = prompt.split("[3. TARGET FILE AST SIGNATURES]", 1)[1].split(
        "[4. DYNAMIC TASK AND EXECUTION ERRORS]", 1
    )[0]
    assert estimate_tokens(prompt) <= 1500
    assert estimate_tokens(focus) / estimate_tokens(prompt) >= 0.25
    assert "src/critical.py" in focus
    assert "<<<<<<< SEARCH" in prompt
    assert "<exact existing lines>" in prompt
    assert "=======" in prompt
    assert "<replacement lines>" in prompt
    assert ">>>>>>> REPLACE" in prompt


def test_session_projection_compacts_journal_without_losing_folded_state(tmp_path):
    projection = SessionProjection(tmp_path)
    for index in range(520):
        projection.append_event("user_input", content=f"request-{index}")

    snapshot = projection.snapshot()

    assert snapshot["event_count"] == 520
    assert snapshot["latest_user_input"] == "request-519"
    assert projection.snapshot_path.is_file()
    assert len(projection.read_events()) <= 2 * 64


def test_session_projection_snapshot_is_a_pure_fold_of_committed_events(tmp_path):
    projection = SessionProjection(tmp_path)
    projection.append_event("user_input", content="Change the last threshold")
    projection.append_event("recent_focus_files", paths=["src/pricing.py"])
    projection.append_model_request(
        backend="gemini38",
        system_prompt="system contract",
        user_prompt="static prefix\ndynamic tail",
        phase="coder",
    )
    log_before = projection.events_path.read_bytes()

    snapshot = projection.snapshot()

    assert snapshot["event_count"] == 3
    assert snapshot["latest_user_input"] == "Change the last threshold"
    assert snapshot["recent_focus_files"] == ["src/pricing.py"]
    assert snapshot["latest_model_request"]["system_prompt"] == "system contract"
    assert snapshot["latest_model_request"]["user_prompt"] == "static prefix\ndynamic tail"
    assert projection.events_path.read_bytes() == log_before
