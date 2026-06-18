from __future__ import annotations

import json

from visual_agent.models import ActionStatus, Observation, ProviderKind
from visual_agent.providers import ProviderRegistry
from visual_agent.visual_rules import VISUAL_GUARD_OPT_OUT_TAG, VISUAL_GUARD_STEP_ID
from visual_agent.workflow import WorkflowRuntime, workflow_from_dict

from test_visual_rules import make_snapshot, text_item


class FakeAuditPage:
    url = "https://example.test/app"
    viewport_size = {"width": 1280, "height": 720}

    def __init__(self, snapshot):
        self._snapshot = snapshot

    def evaluate(self, _script, *args):
        return self._snapshot


def audit_provider(snapshot):
    def observe_browser(_params, provider_context):
        provider_context.resources["playwright_page"] = FakeAuditPage(snapshot)
        provider_context.resources["network_events"] = []
        provider_context.resources["console_events"] = []
        provider_context.resources["page_errors"] = []
        return Observation(
            provider=ProviderKind.DOM,
            source="https://example.test/app",
            elements=(),
            metadata={"title": "Demo", "url": "https://example.test/app", "visible_text": "Demo Ready", "visible_text_length": 10, "interactive_count": 1},
        )

    return observe_browser


def run_workflow(tmp_path, snapshot, steps=None, tags=()):
    providers = ProviderRegistry()
    providers.register("observe_browser", audit_provider(snapshot))
    workflow = workflow_from_dict(
        {
            "name": "visual-demo",
            "tags": list(tags),
            "steps": steps
            or [
                {"id": "observe", "action": "observe_browser", "url": "https://example.test/app"},
                {"id": "assert", "action": "assert_text", "text": "Demo Ready"},
            ],
        }
    )
    return WorkflowRuntime(output_dir=tmp_path, providers=providers).run(workflow, run_profile="supervised")


def test_explicit_visual_assertion_fails_on_blocking_findings(tmp_path) -> None:
    result = run_workflow(
        tmp_path,
        make_snapshot(texts=[text_item(7.0)]),
        steps=[
            {"id": "observe", "action": "observe_browser", "url": "https://example.test/app"},
            {"id": "visual", "action": "assert_visual_quality"},
        ],
    )

    visual = next(step for step in result.steps if step.id == "visual")
    assert visual.status == ActionStatus.FAILED
    assert "font_too_small" in visual.message


def test_explicit_visual_assertion_passes_and_writes_artifact(tmp_path) -> None:
    result = run_workflow(
        tmp_path,
        make_snapshot(texts=[text_item(16.0)]),
        steps=[
            {"id": "observe", "action": "observe_browser", "url": "https://example.test/app"},
            {"id": "visual", "action": "assert_visual_quality"},
        ],
    )

    visual = next(step for step in result.steps if step.id == "visual")
    assert visual.status == ActionStatus.SUCCESS
    artifact = json.loads((result.run_dir / "visual" / "visual.json").read_text(encoding="utf-8"))
    assert artifact["passed"] is True
    assert artifact["metrics"]["text_count"] == 1


def test_explicit_visual_assertion_accepts_threshold_params(tmp_path) -> None:
    result = run_workflow(
        tmp_path,
        make_snapshot(texts=[text_item(10.0)]),
        steps=[
            {"id": "observe", "action": "observe_browser", "url": "https://example.test/app"},
            # raise the blocking minimum so 10px text becomes a hard failure
            {"id": "visual", "action": "assert_visual_quality", "min_font_px_blocking": 11},
        ],
    )

    visual = next(step for step in result.steps if step.id == "visual")
    assert visual.status == ActionStatus.FAILED


def test_auto_guard_blocks_run_with_blocking_findings(tmp_path) -> None:
    result = run_workflow(tmp_path, make_snapshot(texts=[text_item(7.0)]))

    # all authored steps passed, the guard must still fail the run
    assert [step.status for step in result.steps[:2]] == [ActionStatus.SUCCESS, ActionStatus.SUCCESS]
    guard = result.steps[-1]
    assert guard.id == VISUAL_GUARD_STEP_ID
    assert guard.status == ActionStatus.FAILED
    assert guard.metadata["visual_audit"]["error_count"] == 1
    assert (result.run_dir / "visual" / f"{VISUAL_GUARD_STEP_ID}.json").exists()


def test_auto_guard_records_warnings_without_failing(tmp_path) -> None:
    result = run_workflow(tmp_path, make_snapshot(texts=[text_item(10.0)]))

    guard = result.steps[-1]
    assert guard.id == VISUAL_GUARD_STEP_ID
    assert guard.status == ActionStatus.SUCCESS
    assert guard.metadata["visual_audit"]["warning_count"] == 1


def test_auto_guard_appends_nothing_on_clean_page(tmp_path) -> None:
    result = run_workflow(tmp_path, make_snapshot(texts=[text_item(16.0)]))

    assert all(step.id != VISUAL_GUARD_STEP_ID for step in result.steps)


def test_auto_guard_opt_out_tag(tmp_path) -> None:
    result = run_workflow(
        tmp_path,
        make_snapshot(texts=[text_item(7.0)]),
        tags=(VISUAL_GUARD_OPT_OUT_TAG,),
    )

    assert all(step.id != VISUAL_GUARD_STEP_ID for step in result.steps)


def test_auto_guard_skips_non_browser_runs(tmp_path) -> None:
    providers = ProviderRegistry()
    providers.register(
        "observe_fixture",
        lambda _params, _context: Observation(
            provider=ProviderKind.MOCK,
            source="fixture",
            elements=(),
            metadata={"visible_text": "Demo Ready", "visible_text_length": 10},
        ),
    )
    workflow = workflow_from_dict(
        {
            "name": "fixture-demo",
            "steps": [
                {"id": "observe", "action": "observe_fixture", "path": "unused"},
                {"id": "assert", "action": "assert_text", "text": "Demo Ready"},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=providers).run(workflow, run_profile="supervised")

    assert all(step.id != VISUAL_GUARD_STEP_ID for step in result.steps)
    assert all(step.status == ActionStatus.SUCCESS for step in result.steps)


class FakeInteractiveLocator:
    def __init__(self, page):
        self.page = page

    def click(self):
        self.page.text = "Saved OK"


class FakeInteractivePage:
    url = "https://example.test/app"
    viewport_size = {"width": 1280, "height": 720}

    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.text = "Demo Ready"
        self._elements = (
            {"role": "button", "text": "Submit", "selector": "#submit", "bounds": {"left": 10, "top": 10, "width": 100, "height": 32}},
        )

    def evaluate(self, script, arg=None):
        if arg is not None:
            return list(self._elements)
        if "schema_version" in str(script):
            return self._snapshot
        return self.text

    def title(self):
        return "Demo"

    def screenshot(self, *, path, full_page=True):
        from pathlib import Path

        Path(path).write_bytes(b"fake-png")

    def locator(self, _selector):
        return FakeInteractiveLocator(self)

    def wait_for_timeout(self, _ms):
        return None


def interactive_provider(snapshot):
    def observe_browser(_params, provider_context):
        page = FakeInteractivePage(snapshot)
        provider_context.resources["playwright_page"] = page
        provider_context.resources["network_events"] = []
        provider_context.resources["console_events"] = []
        provider_context.resources["page_errors"] = []
        return Observation(
            provider=ProviderKind.DOM,
            source=page.url,
            elements=tuple(page.evaluate("collect", "selector")),
            metadata={"title": "Demo", "url": page.url, "visible_text": page.text, "visible_text_length": len(page.text), "interactive_count": 1},
        )

    return observe_browser


def test_full_loop_with_clean_visuals_reaches_l4(tmp_path) -> None:
    providers = ProviderRegistry()
    providers.register("observe_browser", interactive_provider(make_snapshot(texts=[text_item(16.0)])))
    workflow = workflow_from_dict(
        {
            "name": "full-loop",
            "steps": [
                {"id": "observe", "action": "observe_browser", "url": "https://example.test/app"},
                {"id": "check", "action": "assert_text", "text": "Demo Ready"},
                {"id": "submit", "action": "click", "target": {"text": "Submit", "role": "button"}},
                {"id": "verify", "action": "assert_text", "text": "Saved OK"},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=providers).run(workflow, run_profile="supervised")

    assert all(step.status == ActionStatus.SUCCESS for step in result.steps)
    assert result.acceptance["label"] == "L4"
    assert result.acceptance["is_product_acceptance"] is True
    assert result.run_checks["visual_guard"]["status"] == "passed"
    assert result.run_checks["product_guard"]["status"] == "passed"


def test_inspection_only_run_grades_l1(tmp_path) -> None:
    result = run_workflow(tmp_path, make_snapshot(texts=[text_item(16.0)]))

    assert result.acceptance["label"] == "L1"
    assert result.acceptance["is_product_acceptance"] is False
    assert "interaction" in result.acceptance["missing_for_next_level"]


def test_blocking_visual_findings_keep_grade_at_l3(tmp_path) -> None:
    providers = ProviderRegistry()
    providers.register("observe_browser", interactive_provider(make_snapshot(texts=[text_item(7.0)])))
    workflow = workflow_from_dict(
        {
            "name": "full-loop-bad-visuals",
            "steps": [
                {"id": "observe", "action": "observe_browser", "url": "https://example.test/app"},
                {"id": "check", "action": "assert_text", "text": "Demo Ready"},
                {"id": "submit", "action": "click", "target": {"text": "Submit", "role": "button"}},
                {"id": "verify", "action": "assert_text", "text": "Saved OK"},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=providers).run(workflow, run_profile="supervised")

    assert result.acceptance["label"] == "L3"
    assert result.run_checks["visual_guard"]["status"] == "failed"
    assert result.steps[-1].id == VISUAL_GUARD_STEP_ID


def test_auto_guard_not_appended_after_hard_failure(tmp_path) -> None:
    result = run_workflow(
        tmp_path,
        make_snapshot(texts=[text_item(7.0)]),
        steps=[
            {"id": "observe", "action": "observe_browser", "url": "https://example.test/app"},
            {"id": "assert", "action": "assert_text", "text": "Missing Text"},
        ],
    )

    assert result.steps[-1].id == "assert"
    assert result.steps[-1].status == ActionStatus.FAILED
    assert all(step.id != VISUAL_GUARD_STEP_ID for step in result.steps)
