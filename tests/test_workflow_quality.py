from __future__ import annotations

from visual_agent.context_ingestion import FormField, UISemanticModel
from visual_agent.workflow_quality import score_workflow_quality


def test_score_workflow_quality_counts_forbidden_error_contract() -> None:
    yaml_text = """
    schema_version: 1
    min_runtime_version: "0.1.0"
    name: login_verification
    version: 1
    steps:
      - id: observe_initial
        action: observe_browser
        url: http://localhost:3000/login
      - id: assert_browser_ready
        action: assert_browser_ready
        min_text_length: 1
      - id: wait_success
        action: wait_for
        condition: text
        text: Welcome Dashboard
      - id: assert_known_errors_absent
        action: assert_text_contract
        forbidden_any:
          - Invalid password
    """

    score = score_workflow_quality(yaml_text)

    assert score.covers_success_path is True
    assert score.covers_error_path is True
    assert score.forbidden_error_assertion_count == 1
    assert "no error path covered" not in score.gaps


def test_score_workflow_quality_counts_text_from_as_data_display() -> None:
    yaml_text = """
    schema_version: 1
    min_runtime_version: "0.1.0"
    name: profile_verification
    version: 1
    steps:
      - id: observe_initial
        action: observe_browser
        url: http://localhost:3000/profile
      - id: assert_browser_ready
        action: assert_browser_ready
        min_text_length: 1
      - id: wait_success
        action: wait_for
        condition: text
        text: Profile saved successfully
      - id: assert_displayed_name
        action: assert_text
        text_from: input.displayName
    """
    model = UISemanticModel(
        entry_url="http://localhost:3000/profile",
        page_title=None,
        form_fields=(),
        submit_actions=(),
        success_states=(),
        error_states=(),
        data_displays=("displayName",),
        framework="react",
        confidence=0.8,
    )

    score = score_workflow_quality(yaml_text, model)

    assert score.covers_data_display is True
    assert score.data_display_assertion_count == 1
    assert score.text_from_input_reference_count == 1
    assert score.business_assertion_count == 2


def test_score_workflow_quality_flags_invalid_text_from_references() -> None:
    yaml_text = """
    schema_version: 1
    min_runtime_version: "0.1.0"
    name: profile_verification
    version: 1
    steps:
      - id: observe_initial
        action: observe_browser
        url: http://localhost:3000/profile
      - id: assert_browser_ready
        action: assert_browser_ready
        min_text_length: 1
      - id: wait_success
        action: wait_for
        condition: text
        text: Profile saved successfully
      - id: assert_displayed_missing
        action: assert_text
        text_from: input.timezone
    """
    model = UISemanticModel(
        entry_url="http://localhost:3000/profile",
        page_title=None,
        form_fields=(FormField(name="displayName", label="Display name"),),
        submit_actions=(),
        success_states=(),
        error_states=(),
        data_displays=("displayName", "timezone"),
        framework="react",
        confidence=0.8,
    )

    score = score_workflow_quality(yaml_text, model)

    assert score.covers_data_display is False
    assert score.data_display_assertion_count == 0
    assert score.text_from_input_reference_count == 0
    assert score.invalid_text_from_references == ("input.timezone",)
    assert any("text_from references missing input fields" in gap for gap in score.gaps)


def test_score_workflow_quality_gives_assert_no_error_partial_error_credit() -> None:
    yaml_text = """
    schema_version: 1
    min_runtime_version: "0.1.0"
    name: weak_error_verification
    version: 1
    steps:
      - id: observe_initial
        action: observe_browser
        url: http://localhost:3000/profile
      - id: assert_browser_ready
        action: assert_browser_ready
        min_text_length: 1
      - id: wait_success
        action: wait_for
        condition: text
        text: Saved successfully
      - id: assert_no_error
        action: assert_no_error
    """

    score = score_workflow_quality(yaml_text)

    assert score.covers_error_path is True
    assert score.forbidden_error_assertion_count == 0
    assert score.total_score < 1.0
