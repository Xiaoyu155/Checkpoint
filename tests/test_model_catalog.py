from __future__ import annotations
import json
from unittest.mock import patch
from visual_agent.model_catalog import load_profiles, save_profiles, workflow
from visual_agent.model_workflow import configure_plan, review_implementation
from visual_agent.gemini4_vps_client import Gemini4Response
from visual_agent.subagent_orchestrator import SubagentPlan, SubagentTask


def test_secret_not_returned_and_blank_save_preserves_key():
    save_profiles({"providers":{"gemini4":{"api_key":"test-secret"}}})
    data = save_profiles({"workflow":"gemini4","providers":{"gemini4":{"api_key":""}}})
    assert "test-secret" not in json.dumps(data)
    assert load_profiles()["providers"]["gemini4"]["api_key"] == "test-secret"
    assert data["providers"]["gemini4"]["key_configured"] is True


def test_explicit_model_instruction_and_topic_are_distinct():
    explicit = workflow("fast", "L2", "Use Gemini4 to implement a parser")
    assert explicit["coder"] == "workbuddy"
    assert explicit["planner"] == "gemini4_vps"
    assert workflow("fast", "L2", "Fix the Gemini4 gateway")["coder"] == "workbuddy"
    assert workflow("balanced", "L3")["planner"] == "gemini4_vps"
    assert workflow("balanced", "L3")["coder"] == "workbuddy"
    assert workflow("balanced", "L1")["reviewer"] == "local"


def test_gemini4_workflow_assigns_real_worker(tmp_path):
    save_profiles({"workflow":"gemini4"})
    task = SubagentTask("coder","Implement","coder","grok","Implement",target_files=["a.py"])
    plan = SubagentPlan("Implement a.py",[task],str(tmp_path),complexity="L2")
    with patch("visual_agent.gemini4_vps_client.invoke_gemini4_vps", return_value=Gemini4Response(True, "plan")):
        route = configure_plan(plan,tmp_path,lambda text:None)
    assert task.backend == "workbuddy"
    assert route["planner"] == "gemini4_vps"
    assert route["fallback"] == "grok"


def test_configure_plan_preserves_mixed_parallel_coder_backends(tmp_path):
    save_profiles({"workflow": "gemini4"})
    tasks = [
        SubagentTask("coder-a", "Implement A", "coder", "gemini38", "Implement A", target_files=["a.py"]),
        SubagentTask("coder-b", "Implement B", "coder", "workbuddy", "Implement B", target_files=["b.py"]),
    ]
    plan = SubagentPlan("Implement independent modules", tasks, str(tmp_path), complexity="L2")

    route = configure_plan(plan, tmp_path, lambda _text: None, planner_already_ran=True)

    assert [task.backend for task in tasks] == ["gemini38", "workbuddy"]
    assert route["coder"] == "workbuddy"


def test_review_fails_closed_instead_of_treating_any_text_as_approval(tmp_path):
    plan = SubagentPlan("Implement a.py",[],str(tmp_path))
    candidate = {"backend": "gemini4_vps", "family": "google", "model": "test-reviewer"}
    with patch("visual_agent.model_workflow._adversarial_reviewer_candidates", return_value=[candidate]), \
         patch("visual_agent.model_workflow._invoke_adversarial_reviewer", return_value=("looks fine", None)):
        result = review_implementation(plan,{"success":True},workflow("balanced","L3"),tmp_path,lambda text:None)
    assert result["success"] is False
    assert result["review"]["approved"] is False


def test_review_requires_explicit_positive_verdict(tmp_path):
    plan = SubagentPlan("Implement a.py",[],str(tmp_path))
    candidate = {"backend": "gemini4_vps", "family": "google", "model": "test-reviewer"}
    with patch("visual_agent.model_workflow._adversarial_reviewer_candidates", return_value=[candidate]), \
         patch("visual_agent.model_workflow._invoke_adversarial_reviewer", return_value=(' {"approved":true,"issues":[]}', None)):
        result = review_implementation(plan,{"success":True},workflow("balanced","L3"),tmp_path,lambda text:None)
    assert result["success"] is True
