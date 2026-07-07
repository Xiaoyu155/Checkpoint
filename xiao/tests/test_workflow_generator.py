from __future__ import annotations

import json
from pathlib import Path

from visual_agent.cli import main
from visual_agent.workflow import parse_workflow_file
from visual_agent.workflow_generator import build_workflow_system_prompt, generate_workflow_yaml, select_example_category
from visual_agent.workspace import Workspace, discover_workflows


def test_generate_workflow_dry_run_template_is_valid(tmp_path: Path) -> None:
    result = generate_workflow_yaml(
        description="Verify user login shows dashboard",
        workspace_root=tmp_path / ".agent-workspace",
        dry_run=True,
    )

    assert result["status"] == "success"
    assert result["source"] in {"template_fallback", "anthropic"}
    assert "observe_browser" in result["yaml"]
    assert "assert_browser_ready" in result["yaml"]
    assert "visibility: private" in result["yaml"]
    assert result["quality_score"] >= 0
    assert "quality" in result


def test_generate_workflow_selects_category_examples() -> None:
    assert select_example_category("Verify login and password reset") == "auth"
    assert select_example_category("Verify checkout and add to cart") == "ecommerce"
    assert select_example_category("Verify mobile h5 login") == "mobile_h5"

    prompt = build_workflow_system_prompt("Verify login flow")

    assert "login_basic" in prompt
    assert "login_redirect" in prompt
    assert "logout_flow" in prompt
    assert "visibility: public" in prompt

    ecommerce_prompt = build_workflow_system_prompt("Verify product page", page_type="ecommerce")
    assert "product_list" in ecommerce_prompt
    assert "order_confirm" in ecommerce_prompt


def test_public_example_workflows_parse() -> None:
    root = Path(__file__).resolve().parents[1] / "workflows" / "examples"
    paths = sorted(root.rglob("*.yaml"))

    assert len(paths) == 36
    for path in paths:
        workflow = parse_workflow_file(path)
        assert workflow.visibility == "public"
        assert workflow.author == "visual-agent-team"
        assert "example" in workflow.tags


def test_generate_workflow_saves_to_project_workflows(tmp_path: Path) -> None:
    workspace_root = tmp_path / ".agent-workspace"
    workspace_root.mkdir()

    result = generate_workflow_yaml(
        description="Verify checkout success message",
        workspace_root=workspace_root,
    )

    assert result["status"] == "success"
    saved_to = Path(str(result["saved_to"]))
    assert saved_to.exists()
    assert saved_to.parent == tmp_path / "workflows"
    workflow = parse_workflow_file(saved_to)
    assert workflow.visibility == "private"
    assert "verification" in workflow.tags


def test_generate_workflow_reports_similar_existing_workflow(tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    existing = workflows / "login_flow.yaml"
    existing.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: login_flow\n"
        "version: 1\n"
        "description: Verify user login shows dashboard\n"
        "tags: [verification]\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_ocr\n"
        "    mock_text: Dashboard\n",
        encoding="utf-8",
    )

    result = generate_workflow_yaml(
        description="Verify user login shows dashboard",
        workspace_root=tmp_path / ".agent-workspace",
        dry_run=True,
    )

    assert result["similar_workflows"]
    assert result["similar_workflows"][0]["name"] == "login_flow"
    assert "Existing similar workflow found" in result["message"]


def test_generate_workflow_page_type_hint_does_not_pollute_description(tmp_path: Path) -> None:
    result = generate_workflow_yaml(
        description="Verify product page loads",
        workspace_root=tmp_path / ".agent-workspace",
        dry_run=True,
        page_type="ecommerce",
    )

    assert result["status"] == "success"
    assert "ecommerce" in result["yaml"]
    assert "[page_type:" not in result["yaml"]


def test_generate_workflow_url_sets_initial_observe_url(tmp_path: Path) -> None:
    result = generate_workflow_yaml(
        description="Verify login",
        workspace_root=tmp_path / ".agent-workspace",
        dry_run=True,
        page_type="auth",
        url="http://localhost:5173/login",
    )

    assert result["status"] == "success"
    assert result["page_url"] == "http://localhost:5173/login"
    assert "http://localhost:5173/login" in result["yaml"]


def test_generate_workflow_retries_invalid_llm_yaml(tmp_path: Path, monkeypatch) -> None:
    calls = {"count": 0}

    def fake_generate(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return "not: [valid"
        return """
schema_version: 1
min_runtime_version: "0.1.0"
name: retry_success
version: 1
description: Retry success
tags: [verification, fast]
visibility: private
author: ""
license: ""
steps:
  - id: observe
    action: observe_browser
    url: "http://localhost:3000"
  - id: ready
    action: assert_browser_ready
    min_text_length: 1
  - id: assert_success
    action: assert_text
    text: Success
"""

    monkeypatch.setattr("visual_agent.workflow_generator._generate_with_llm_backend", fake_generate)

    result = generate_workflow_yaml(
        description="Verify retry success",
        workspace_root=tmp_path / ".agent-workspace",
        dry_run=True,
    )

    assert result["status"] == "success"
    assert result["workflow_name"] == "retry_success"
    assert len(result["generation_attempts"]) == 2


def test_generate_workflow_from_existing_mobile_variant(tmp_path: Path, capsys) -> None:
    workflow_dir = tmp_path / "workflows"
    workflow_dir.mkdir()
    source = workflow_dir / "login_flow.yaml"
    source.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: login_flow\n"
        "version: 1\n"
        "tags: [verification, auth]\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_browser\n"
        "    url: http://localhost:3000/login\n"
        "  - id: ready\n"
        "    action: assert_browser_ready\n"
        "    min_text_length: 1\n",
        encoding="utf-8",
    )

    code = main(
        [
            "generate-workflow",
            "--workspace-root",
            str(tmp_path / ".agent-workspace"),
            "--from-existing",
            "login_flow",
            "--variant",
            "mobile",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "success"
    assert payload["workflow_name"] == "login_flow_mobile"
    saved = Path(payload["saved_to"])
    assert saved.exists()
    assert "width: 375" in saved.read_text(encoding="utf-8")


def test_generate_workflow_from_sitemap(tmp_path: Path, capsys) -> None:
    sitemap = tmp_path / "sitemap.xml"
    sitemap.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.test/</loc></url>
  <url><loc>https://example.test/products</loc></url>
</urlset>
""",
        encoding="utf-8",
    )

    code = main(
        [
            "generate-workflow",
            "--workspace-root",
            str(tmp_path / ".agent-workspace"),
            "--from-sitemap",
            str(sitemap),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "success"
    assert payload["workflow_count"] == 2
    assert all(Path(item["saved_to"]).exists() for item in payload["generated"])


def test_cli_generate_workflow_dry_run_outputs_json(tmp_path: Path, capsys) -> None:
    code = main(
        [
            "generate-workflow",
            "--workspace-root",
            str(tmp_path / ".agent-workspace"),
            "--description",
            "Verify settings page loads",
            "--dry-run",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "success"
    assert payload["saved_to"] is None
    assert "assert_browser_ready" in payload["yaml"]
    assert "assert_text" in payload["yaml"]


def test_cli_generate_workflow_url_outputs_anchored_yaml(tmp_path: Path, capsys) -> None:
    code = main(
        [
            "generate-workflow",
            "--workspace-root",
            str(tmp_path / ".agent-workspace"),
            "--description",
            "Verify login",
            "--url",
            "http://localhost:5173/login",
            "--dry-run",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["page_url"] == "http://localhost:5173/login"
    assert "http://localhost:5173/login" in payload["yaml"]


def test_workflow_ref_reads_visibility_author_description_and_license(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / ".agent-workspace")
    workspace.workflows_dir.mkdir(parents=True)
    workflow_path = workspace.workflows_dir / "public_demo.yaml"
    workflow_path.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: public_demo\n"
        "version: 1\n"
        "description: Public demo workflow\n"
        "tags: [verification]\n"
        "visibility: public\n"
        "author: demo\n"
        "license: cc-by-4.0\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_ocr\n"
        "    mock_text: ready\n",
        encoding="utf-8",
    )

    refs = discover_workflows(workspace)

    assert len(refs) == 1
    assert refs[0].visibility == "public"
    assert refs[0].author == "demo"
    assert refs[0].description == "Public demo workflow"
    assert refs[0].license == "cc-by-4.0"
