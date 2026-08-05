from __future__ import annotations

import subprocess
from pathlib import Path

from visual_agent.chief_dispatch import chief_dispatch_to_markdown
from visual_agent.diff_review import (
    build_review_prompt,
    parse_review_findings,
    review_markdown_lines,
    run_diff_review,
    subtract_baseline,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


def _repo_with_change(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    target = repo / "worker.py"
    target.write_text("def run():\n    try:\n        work()\n    except Exception:\n        pass\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    target.write_text(
        "def run():\n    try:\n        work()\n    except Exception:\n        pass\n\n"
        "def cancel():\n    flag.set()\n",
        encoding="utf-8",
    )
    return repo


SWALLOWED = (
    '[{"file": "worker.py", "line": 5, "category": "swallowed_error",'
    ' "summary": "exception dropped", "failure_scenario": "work() raises -> run() returns as if ok"}]'
)
CANCEL = (
    '[{"file": "worker.py", "line": 7, "category": "cancellation",'
    ' "summary": "cancel sets a flag nobody reads", "failure_scenario": "caller cancels -> loop keeps running"}]'
)


def test_findings_without_a_failure_scenario_are_dropped() -> None:
    reply = '[{"file": "a.py", "line": 1, "category": "concurrency", "summary": "this looks fragile"}]'

    # No demonstrable failure means it is taste, and taste is what makes people
    # stop reading review reports.
    assert parse_review_findings(reply) == []


def test_findings_parse_out_of_a_fenced_reply_with_prose_around_it() -> None:
    reply = "Here is what I found:\n```json\n" + SWALLOWED + "\n```\nHope that helps."

    findings = parse_review_findings(reply)

    assert len(findings) == 1
    assert findings[0]["category"] == "swallowed_error"
    assert findings[0]["line"] == 5


def test_an_unknown_category_is_normalized_rather_than_dropped() -> None:
    reply = (
        '[{"file": "a.py", "line": 1, "category": "Design Smell",'
        ' "summary": "s", "failure_scenario": "x -> y"}]'
    )

    assert parse_review_findings(reply)[0]["category"] == "other"


def test_baseline_subtraction_separates_history_from_this_change() -> None:
    split = subtract_baseline(
        change_findings=parse_review_findings(SWALLOWED) + parse_review_findings(CANCEL),
        base_findings=parse_review_findings(SWALLOWED),
    )

    assert [item["category"] for item in split["findings"]] == ["cancellation"]
    assert [item["category"] for item in split["pre_existing"]] == ["swallowed_error"]


def test_a_defect_that_base_also_has_is_not_blamed_on_the_change(tmp_path) -> None:
    repo = _repo_with_change(tmp_path)
    calls: list[str] = []

    def completion(_system: str, prompt: str) -> str:
        calls.append("base" if "BEFORE a change" in prompt else "change")
        # The swallowed except block predates the change; the reviewer sees it
        # both times, exactly as a real model would.
        return SWALLOWED

    review = run_diff_review(objective="add cancel", worktree=repo, completion=completion)

    assert calls == ["change", "base"]
    assert review["status"] == "clean"
    assert review["findings"] == []
    assert review["pre_existing"][0]["category"] == "swallowed_error"


def test_a_defect_only_the_change_introduces_is_reported(tmp_path) -> None:
    repo = _repo_with_change(tmp_path)

    def completion(_system: str, prompt: str) -> str:
        return SWALLOWED if "BEFORE a change" in prompt else CANCEL

    review = run_diff_review(objective="add cancel", worktree=repo, completion=completion)

    assert review["status"] == "findings"
    assert review["findings"][0]["category"] == "cancellation"
    assert review["findings"][0]["verdict"] == "introduced_or_left"


def test_a_clean_change_pass_skips_the_baseline_call_entirely(tmp_path) -> None:
    repo = _repo_with_change(tmp_path)
    calls: list[str] = []

    def completion(_system: str, prompt: str) -> str:
        calls.append(prompt)
        return "[]"

    review = run_diff_review(objective="add cancel", worktree=repo, completion=completion)

    assert review["status"] == "clean"
    assert len(calls) == 1


def test_a_reviewer_outage_is_reported_as_unknown_not_as_clean(tmp_path) -> None:
    repo = _repo_with_change(tmp_path)

    def completion(_system: str, _prompt: str) -> str:
        raise RuntimeError("503 from provider")

    review = run_diff_review(objective="add cancel", worktree=repo, completion=completion)

    assert review["status"] == "review_unavailable"
    markdown = "\n".join(review_markdown_lines(review))
    assert "别把「没报问题」当成「没问题」" in markdown


def test_review_never_reports_a_verdict_that_could_fail_a_dispatch(tmp_path) -> None:
    repo = _repo_with_change(tmp_path)

    review = run_diff_review(objective="x", worktree=repo, completion=lambda _s, _p: CANCEL)

    # The reviewer routes to a human; "fail" is not in its vocabulary, because a
    # reviewer that can fail a dispatch recreates false red.
    assert review["status"] in {"findings", "clean"}
    assert "verdict" not in review


def test_review_prompt_asks_for_a_failure_scenario_and_forbids_padding() -> None:
    prompt = build_review_prompt(objective="add retry", code_text="diff", mode="change")

    assert "failure_scenario" in prompt
    assert "An empty array is a valid and expected answer" in prompt
    assert "add retry" in prompt


def test_findings_reach_the_dispatch_markdown_with_location_and_scenario() -> None:
    review = {
        "status": "findings",
        "baseline": "compared",
        "findings": parse_review_findings(CANCEL),
        "pre_existing": [],
    }

    markdown = chief_dispatch_to_markdown({"status": "verified", "diff_review": review})

    assert "差异评审" in markdown
    assert "worker.py:7" in markdown
    assert "loop keeps running" in markdown


def test_no_review_configured_renders_nothing() -> None:
    assert review_markdown_lines(None) == []
    assert chief_dispatch_to_markdown({"status": "verified"}).count("差异评审") == 0


def test_dispatch_skips_the_review_when_no_review_model_is_configured(tmp_path) -> None:
    from visual_agent.chief_dispatch import _run_dispatch_diff_review

    # An absent reviewer must be absent from the report, not a clean review.
    assert _run_dispatch_diff_review(
        objective="x", worktree=tmp_path, base_ref="HEAD", model_policy={"implementation": "cheap"}
    ) is None


def test_dispatch_uses_the_review_tier_model_not_the_implementation_one(tmp_path, monkeypatch) -> None:
    from visual_agent import chief_dispatch

    repo = _repo_with_change(tmp_path)
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        chief_dispatch,
        "resolve_llm_backend",
        lambda model_id: seen.setdefault("model", model_id) or "backend",
    )
    monkeypatch.setattr(chief_dispatch, "run_llm_completion", lambda **kwargs: "[]")

    review = chief_dispatch._run_dispatch_diff_review(
        objective="x",
        worktree=repo,
        base_ref="HEAD",
        model_policy={"implementation": "cheap", "review": "claude-opus-5"},
    )

    assert seen["model"] == "claude-opus-5"
    assert review["status"] == "clean"
