from __future__ import annotations

import pytest

from visual_agent.visual_rules import (
    RULE_BROKEN_IMAGE,
    RULE_FONT_BELOW_RECOMMENDED,
    RULE_FONT_TOO_SMALL,
    RULE_HORIZONTAL_OVERFLOW,
    RULE_LOW_CONTENT_COVERAGE,
    RULE_OCCLUDED_CONTROL,
    RULE_PENDING_IMAGE,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    VisualRuleConfig,
    collect_visual_snapshot,
    evaluate_visual_snapshot,
    run_visual_audit,
)


def make_snapshot(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "viewport": {"width": 1280, "height": 720},
        "document": {"scroll_width": 1280, "client_width": 1280, "scroll_height": 2000, "client_height": 720},
        "texts": [],
        "images": [],
        "controls": [],
        "overflow_culprits": [],
        "content_coverage": 0.4,
        "truncated": {"texts": False, "images": False, "controls": False},
    }
    base.update(overrides)
    return base


def text_item(font_size: float, snippet: str = "示例文本") -> dict:
    return {"selector": "div.demo", "tag": "div", "snippet": snippet, "font_size": font_size, "rect": {"left": 0, "top": 0, "width": 100, "height": 20}}


def test_clean_snapshot_passes() -> None:
    report = evaluate_visual_snapshot(make_snapshot())

    assert report.passed is True
    assert report.findings == ()
    assert "passed" in report.summary()


def test_unreadable_font_is_blocking() -> None:
    report = evaluate_visual_snapshot(make_snapshot(texts=[text_item(7.0)]))

    assert report.passed is False
    assert report.findings[0].rule == RULE_FONT_TOO_SMALL
    assert report.findings[0].severity == SEVERITY_ERROR
    assert "7px" in report.findings[0].message


def test_small_but_legible_font_is_warning() -> None:
    report = evaluate_visual_snapshot(make_snapshot(texts=[text_item(10.0)]))

    assert report.passed is True
    assert report.findings[0].rule == RULE_FONT_BELOW_RECOMMENDED
    assert report.findings[0].severity == SEVERITY_WARNING


def test_recommended_font_passes_and_zero_size_skipped() -> None:
    report = evaluate_visual_snapshot(make_snapshot(texts=[text_item(12.0), text_item(0.0), text_item(16.0)]))

    assert report.findings == ()


def test_font_findings_capped_per_rule() -> None:
    report = evaluate_visual_snapshot(
        make_snapshot(texts=[text_item(7.0, f"text-{index}") for index in range(30)]),
        VisualRuleConfig(max_findings_per_rule=5),
    )

    assert report.error_count == 5


def test_horizontal_overflow_blocks_with_culprit() -> None:
    report = evaluate_visual_snapshot(
        make_snapshot(
            document={"scroll_width": 1800, "client_width": 1280, "scroll_height": 2000, "client_height": 720},
            overflow_culprits=[{"selector": "div.wide", "tag": "div", "rect": {"left": 0, "top": 0, "width": 1800, "height": 50}}],
        )
    )

    assert report.passed is False
    finding = report.findings[0]
    assert finding.rule == RULE_HORIZONTAL_OVERFLOW
    assert finding.severity == SEVERITY_ERROR
    assert "div.wide" in finding.message
    assert finding.evidence["culprits"]


def test_overflow_within_tolerance_passes() -> None:
    report = evaluate_visual_snapshot(
        make_snapshot(document={"scroll_width": 1281, "client_width": 1280, "scroll_height": 2000, "client_height": 720})
    )

    assert report.passed is True
    assert report.findings == ()


def test_broken_image_is_blocking() -> None:
    report = evaluate_visual_snapshot(
        make_snapshot(
            images=[
                {"selector": "img.hero", "src": "https://cdn.test/hero.png", "has_source": True, "complete": True, "natural_width": 0, "loading": "auto", "visible": True, "rect": {"left": 0, "top": 0, "width": 300, "height": 200}},
            ]
        )
    )

    assert report.passed is False
    assert report.findings[0].rule == RULE_BROKEN_IMAGE
    assert "hero.png" in report.findings[0].message


def test_lazy_unloaded_image_is_ignored_and_pending_eager_is_warning() -> None:
    report = evaluate_visual_snapshot(
        make_snapshot(
            images=[
                {"selector": "img.lazy", "src": "a.png", "has_source": True, "complete": False, "natural_width": 0, "loading": "lazy", "visible": False, "rect": {}},
                {"selector": "img.eager", "src": "b.png", "has_source": True, "complete": False, "natural_width": 0, "loading": "eager", "visible": True, "rect": {}},
                {"selector": "img.placeholder", "src": "", "has_source": False, "complete": True, "natural_width": 0, "loading": "auto", "visible": True, "rect": {}},
            ]
        )
    )

    assert report.passed is True
    assert [item.rule for item in report.findings] == [RULE_PENDING_IMAGE]
    assert report.findings[0].evidence["src"] == "b.png"


def test_occluded_control_is_warning() -> None:
    report = evaluate_visual_snapshot(
        make_snapshot(
            controls=[
                {"selector": "button.pay", "tag": "button", "text": "提交订单", "rect": {}, "occluded": True, "occluded_by": "div.overlay"},
                {"selector": "a.home", "tag": "a", "text": "首页", "rect": {}, "occluded": False, "occluded_by": ""},
            ]
        )
    )

    assert report.passed is True
    assert [item.rule for item in report.findings] == [RULE_OCCLUDED_CONTROL]
    assert "div.overlay" in report.findings[0].message


def test_low_content_coverage_is_warning() -> None:
    report = evaluate_visual_snapshot(make_snapshot(content_coverage=0.01))

    assert report.passed is True
    assert report.findings[0].rule == RULE_LOW_CONTENT_COVERAGE
    assert "1.0%" in report.findings[0].message


def test_strict_mode_promotes_warnings_to_blocking() -> None:
    report = evaluate_visual_snapshot(
        make_snapshot(texts=[text_item(10.0)]),
        VisualRuleConfig(strict=True),
    )

    assert report.passed is False
    assert report.findings[0].severity == SEVERITY_ERROR


def test_skip_rules_filters_findings() -> None:
    report = evaluate_visual_snapshot(
        make_snapshot(texts=[text_item(7.0)], content_coverage=0.01),
        VisualRuleConfig(skip_rules=frozenset({RULE_FONT_TOO_SMALL})),
    )

    assert report.error_count == 0
    assert [item.rule for item in report.findings] == [RULE_LOW_CONTENT_COVERAGE]


def test_config_from_params_rejects_unknown_rule() -> None:
    with pytest.raises(ValueError, match="Unknown visual rules"):
        VisualRuleConfig.from_params({"skip_rules": ["nonexistent_rule"]})


def test_config_from_params_reads_thresholds() -> None:
    config = VisualRuleConfig.from_params({"min_font_px": 14, "strict": True, "skip_rules": [RULE_OCCLUDED_CONTROL]})

    assert config.min_font_px == 14.0
    assert config.strict is True
    assert RULE_OCCLUDED_CONTROL in config.skip_rules


def test_snapshot_error_fails_audit_with_reason() -> None:
    report = evaluate_visual_snapshot({"schema_version": 1, "error": "ReferenceError: boom"})

    assert report.passed is False
    assert "boom" in report.summary()
    assert report.findings == ()


def test_metrics_summarize_counts() -> None:
    report = evaluate_visual_snapshot(make_snapshot(texts=[text_item(7.0), text_item(16.0)]))

    assert report.metrics["text_count"] == 2
    assert report.metrics["findings_by_rule"] == {RULE_FONT_TOO_SMALL: 1}


class FakeSnapshotPage:
    def __init__(self, result):
        self._result = result

    def evaluate(self, _script, *args):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def test_collect_snapshot_rejects_non_browser_doubles() -> None:
    assert collect_visual_snapshot(FakeSnapshotPage("plain text")) is None
    assert collect_visual_snapshot(FakeSnapshotPage(["list"])) is None
    assert collect_visual_snapshot(None) is None


def test_collect_snapshot_wraps_evaluate_failures() -> None:
    snapshot = collect_visual_snapshot(FakeSnapshotPage(RuntimeError("page closed")))

    assert snapshot is not None
    assert "page closed" in snapshot["error"]


def test_run_visual_audit_without_page_returns_none() -> None:
    assert run_visual_audit({}) is None
    assert run_visual_audit(None) is None


def test_run_visual_audit_with_page_evaluates() -> None:
    report = run_visual_audit({"playwright_page": FakeSnapshotPage(make_snapshot(texts=[text_item(7.0)]))})

    assert report is not None
    assert report.passed is False
