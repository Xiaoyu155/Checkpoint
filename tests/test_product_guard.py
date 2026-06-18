from __future__ import annotations

from visual_agent.models import ActionStatus, Observation, ProviderKind
from visual_agent.product_guard import (
    PRODUCT_GUARD_OPT_OUT_TAG,
    PRODUCT_GUARD_STEP_ID,
    evaluate_product_guard,
)
from visual_agent.providers import ProviderRegistry
from visual_agent.workflow import WorkflowRuntime, workflow_from_dict


def test_guard_passes_with_no_resources() -> None:
    assert evaluate_product_guard(None).passed is True
    assert evaluate_product_guard({}).passed is True


def test_guard_blocks_console_errors_but_not_warnings() -> None:
    report = evaluate_product_guard(
        {
            "console_events": [
                {"type": "warning", "text": "deprecated API"},
                {"type": "error", "text": "Uncaught TypeError: x is undefined"},
            ]
        }
    )

    assert report.passed is False
    assert len(report.console_errors) == 1
    assert "console error" in report.summary()


def test_guard_blocks_page_errors() -> None:
    report = evaluate_product_guard({"page_errors": [{"type": "pageerror", "message": "ReferenceError"}]})

    assert report.passed is False
    assert len(report.page_errors) == 1


def test_guard_ignores_known_false_positives() -> None:
    report = evaluate_product_guard(
        {
            "console_events": [
                # browser echo of a 4xx resource load follows the 4xx policy: not blocking
                {"type": "error", "text": "Failed to load resource: the server responded with a status of 409 (Conflict)"},
            ],
            "network_events": [
                # navigation aborted because it became a download — routine, not a defect
                {"type": "request_failed", "url": "/exports/a.csv", "failure": "net::ERR_ABORTED"},
            ],
        }
    )

    assert report.passed is True


def test_guard_blocks_server_errors_and_failed_requests_but_not_4xx() -> None:
    report = evaluate_product_guard(
        {
            "network_events": [
                {"url": "/api/pay", "status": 500, "method": "POST"},
                {"url": "/api/list", "status": 404, "method": "GET"},
                {"type": "request_failed", "url": "/api/upload"},
                {"url": "/api/ok", "status": 200, "method": "GET"},
            ]
        }
    )

    assert report.passed is False
    assert len(report.server_errors) == 1
    assert len(report.failed_requests) == 1
    assert report.client_error_count == 1
    assert report.violation_count == 2


def seeded_browser_provider(*, console_events=(), page_errors=(), network_events=()):
    """Fake observe_browser that simulates a page with seeded defects."""

    def observe_browser(_params, provider_context):
        provider_context.resources["network_events"] = list(network_events)
        provider_context.resources["console_events"] = list(console_events)
        provider_context.resources["page_errors"] = list(page_errors)
        return Observation(
            provider=ProviderKind.DOM,
            source="https://example.test/app",
            elements=({"role": "heading", "text": "Demo Ready", "selector": "h1", "bounds": {"left": 0, "top": 0, "width": 100, "height": 20}},),
            metadata={"title": "Demo", "url": "https://example.test/app", "visible_text": "Demo Ready", "visible_text_length": 10, "interactive_count": 1},
        )

    return observe_browser


def seeded_workflow(extra_tags: tuple[str, ...] = (), failing: bool = False) -> dict:
    return {
        "name": "seeded-app",
        "tags": list(extra_tags),
        "steps": [
            {"id": "observe", "action": "observe_browser", "url": "https://example.test/app"},
            {"id": "assert", "action": "assert_text", "text": "Missing Text" if failing else "Demo Ready"},
        ],
    }


def test_run_with_console_error_fails_via_product_guard(tmp_path) -> None:
    providers = ProviderRegistry()
    providers.register(
        "observe_browser",
        seeded_browser_provider(console_events=[{"type": "error", "text": "Uncaught TypeError"}]),
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=providers).run(
        workflow_from_dict(seeded_workflow()), run_profile="supervised"
    )

    # every authored step passed, yet the run must fail: that is the guard's job
    assert [step.status for step in result.steps[:2]] == [ActionStatus.SUCCESS, ActionStatus.SUCCESS]
    guard_step = result.steps[-1]
    assert guard_step.id == PRODUCT_GUARD_STEP_ID
    assert guard_step.status == ActionStatus.FAILED
    assert "console error" in guard_step.message
    assert guard_step.metadata["product_guard"]["console_errors"]


def test_run_with_server_error_fails_via_product_guard(tmp_path) -> None:
    providers = ProviderRegistry()
    providers.register(
        "observe_browser",
        seeded_browser_provider(network_events=[{"url": "/api/estimate", "status": 500, "method": "POST"}]),
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=providers).run(
        workflow_from_dict(seeded_workflow()), run_profile="supervised"
    )

    guard_step = result.steps[-1]
    assert guard_step.id == PRODUCT_GUARD_STEP_ID
    assert guard_step.status == ActionStatus.FAILED
    assert "5xx" in guard_step.message


def test_clean_run_appends_no_guard_step(tmp_path) -> None:
    providers = ProviderRegistry()
    providers.register("observe_browser", seeded_browser_provider())

    result = WorkflowRuntime(output_dir=tmp_path, providers=providers).run(
        workflow_from_dict(seeded_workflow()), run_profile="supervised"
    )

    assert [step.status for step in result.steps] == [ActionStatus.SUCCESS, ActionStatus.SUCCESS]
    assert all(step.id != PRODUCT_GUARD_STEP_ID for step in result.steps)


def test_opt_out_tag_skips_product_guard(tmp_path) -> None:
    providers = ProviderRegistry()
    providers.register(
        "observe_browser",
        seeded_browser_provider(console_events=[{"type": "error", "text": "expected noise"}]),
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=providers).run(
        workflow_from_dict(seeded_workflow(extra_tags=(PRODUCT_GUARD_OPT_OUT_TAG,))),
        run_profile="supervised",
    )

    assert all(step.id != PRODUCT_GUARD_STEP_ID for step in result.steps)


def test_guard_not_appended_after_hard_step_failure(tmp_path) -> None:
    providers = ProviderRegistry()
    providers.register(
        "observe_browser",
        seeded_browser_provider(console_events=[{"type": "error", "text": "Uncaught TypeError"}]),
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=providers).run(
        workflow_from_dict(seeded_workflow(failing=True)), run_profile="supervised"
    )

    assert result.steps[-1].status == ActionStatus.FAILED
    assert result.steps[-1].id == "assert"
    assert all(step.id != PRODUCT_GUARD_STEP_ID for step in result.steps)
