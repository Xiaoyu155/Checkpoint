from __future__ import annotations

from visual_agent.workflow import workflow_from_dict


def test_workflow_from_dict_parses_tags() -> None:
    workflow = workflow_from_dict(
        {
            "name": "checkout_verification",
            "version": 1,
            "tags": ["verification", "checkout"],
            "steps": [{"id": "observe", "action": "observe_screen"}],
        }
    )

    assert workflow.tags == ("verification", "checkout")
