from __future__ import annotations

from typing import Any


def quality_gate_payload(quality: Any) -> dict[str, Any]:
    return {
        "score": quality.total_score,
        "covers_success_path": quality.covers_success_path,
        "covers_error_path": quality.covers_error_path,
        "business_assertions": quality.business_assertion_count,
        "structural_assertions": quality.structural_assertion_count,
        "data_display_assertions": quality.data_display_assertion_count,
        "forbidden_error_assertions": quality.forbidden_error_assertion_count,
        "text_from_input_references": quality.text_from_input_reference_count,
        "invalid_text_from_references": list(quality.invalid_text_from_references),
        "gaps": list(quality.gaps[:3]),
        "recommendation": quality.recommendation,
    }


def semantic_summary_payload(generation: Any) -> dict[str, Any]:
    from .context_ingestion import summarize_data_displays

    model = generation.semantic_model
    display_summary = summarize_data_displays(model)
    return {
        "framework": model.framework,
        "confidence": model.confidence,
        "generation_method": generation.generation_method,
        "field_count": len(model.form_fields),
        "required_field_count": sum(1 for field in model.form_fields if field.required),
        "sensitive_field_count": sum(1 for field in model.form_fields if field.is_sensitive),
        "validation_rule_count": sum(len(field.validation_rules) for field in model.form_fields),
        "submit_action_count": len(model.submit_actions),
        "success_state_count": len(model.success_states),
        "error_state_count": len(model.error_states),
        "data_display_count": len(model.data_displays),
        "negative_input_case_count": len(generation.negative_input_cases),
        "fields": [field.name for field in model.form_fields[:8]],
        "success_states": [state.value for state in model.success_states[:5]],
        "data_displays": list(model.data_displays[:8]),
        "matched_data_displays": list(display_summary.matched[:8]),
        "unmatched_data_displays": list(display_summary.unmatched[:8]),
        "warnings": list(generation.warnings[:5]),
    }
