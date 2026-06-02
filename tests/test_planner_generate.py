from __future__ import annotations

import json

from visual_agent.cli import main
from visual_agent.planner_generate import (
    build_planner_draft_prompt,
    generate_planner_draft,
    parse_planner_yaml,
    planner_draft_recovery_suggestions,
    preview_planner_draft_save,
    save_planner_draft_result,
    strip_markdown_fence,
)
from visual_agent.workflow import parse_workflow_file
from visual_agent.workspace import init_workspace


def test_build_planner_draft_prompt_uses_safe_workspace_context(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    prompt = build_planner_draft_prompt(workspace, "检查登录页")

    assert "Return only YAML" in prompt
    assert "检查登录页" in prompt
    assert "capabilities" in prompt
    assert "never include credentials" in prompt
    assert "demo_user" not in prompt


def test_parse_planner_yaml_accepts_markdown_fenced_yaml() -> None:
    text = """
```yaml
schema_version: 1
min_runtime_version: "0.1.0"
name: draft_login
version: 1
steps:
  - id: observe
    action: observe_html
    path: fixtures/login.html
  - id: assert
    action: assert_text
    text: 登录
```
"""

    parsed = parse_planner_yaml(text)

    assert strip_markdown_fence(text).startswith("schema_version")
    assert parsed["status"] == "success"
    assert parsed["workflow"].name == "draft_login"


def test_parse_planner_yaml_normalizes_common_model_params_shape() -> None:
    text = """
schema_version: 1
min_runtime_version: "0.1.0"
name: model_shape
version: 1
steps:
  - name: observe login
    action: observe_html
    params:
      path: fixtures/login.html
  - name: assert login
    action: assert_text
    params:
      text: 登录
"""

    parsed = parse_planner_yaml(text)
    workflow = parsed["workflow"]

    assert parsed["status"] == "success"
    assert workflow.steps[0].id == "observe_login"
    assert workflow.steps[0].params["path"] == "fixtures/login.html"
    assert workflow.steps[1].id == "assert_login"
    assert workflow.steps[1].params["text"] == "登录"


def test_parse_planner_yaml_normalizes_common_model_input_shape() -> None:
    text = """
schema_version: 1
min_runtime_version: "0.1.0"
name: model_input_shape
version: 1
steps:
  - step: observe_fixture
    name: observe login page
    action: observe_html
    input:
      path: fixtures/login_demo.html
  - step: assert_text
    name: assert login heading
    action: assert_text
    input:
      text: 登录
"""

    parsed = parse_planner_yaml(text)
    workflow = parsed["workflow"]

    assert parsed["status"] == "success"
    assert workflow.steps[0].id == "observe_login_page"
    assert workflow.steps[0].params["path"] == "fixtures/login_demo.html"
    assert workflow.steps[1].id == "assert_login_heading"
    assert workflow.steps[1].params["text"] == "登录"


def test_generate_planner_draft_plan_only_does_not_call_model(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    key_file = tmp_path / "keys.txt"
    key_file.write_text("xiaomimimo api key: sk-xiaomi-secret-value-abcdef\n", encoding="utf-8")

    result = generate_planner_draft(workspace, "检查登录页", source=key_file, execute=False)

    assert result["status"] == "planned"
    assert result["executed"] is False
    assert result["api_plan"]["ready"] is True
    assert result["api_plan"]["probe"]["sends_secret"] is False


def test_generate_planner_draft_checks_model_yaml(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    key_file = tmp_path / "keys.txt"
    key_file.write_text("xiaomimimo api key: sk-xiaomi-secret-value-abcdef\n", encoding="utf-8")

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "id": "chatcmpl-draft",
                    "object": "chat.completion",
                    "model": "mimo-v2.5",
                    "choices": [
                        {
                            "message": {
                                "content": """
schema_version: 1
min_runtime_version: "0.1.0"
name: generated_login_check
version: 1
steps:
  - id: observe
    action: observe_html
    path: fixtures/login.html
  - id: assert
    action: assert_text
    text: 登录
"""
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"total_tokens": 10},
                }
            ).encode("utf-8")

    monkeypatch.setattr("visual_agent.planner_generate.urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())

    result = generate_planner_draft(workspace, "检查登录页", source=key_file, execute=True)
    text = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "valid"
    assert result["parse_status"] == "success"
    assert result["check"]["valid"] is True
    assert result["workflow"]["name"] == "generated_login_check"
    assert "sk-xiaomi-secret-value-abcdef" not in text


def test_generate_planner_draft_gives_recovery_suggestions_for_unparseable_yaml(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    key_file = tmp_path / "keys.txt"
    key_file.write_text("xiaomimimo api key: sk-xiaomi-secret-value-abcdef\n", encoding="utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "I can help.\n- not: [valid"}}]}).encode("utf-8")

    monkeypatch.setattr("visual_agent.planner_generate.urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())

    result = generate_planner_draft(workspace, "检查登录页", source=key_file, execute=True)
    suggestions = result["recovery_suggestions"]
    text = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "generated"
    assert result["parse_status"] == "error"
    assert any("YAML-only" in item for item in suggestions)
    assert any("indentation" in item for item in suggestions)
    assert "sk-xiaomi-secret-value-abcdef" not in text


def test_planner_draft_recovery_suggestions_cover_unsafe_check_issues() -> None:
    result = {
        "status": "invalid",
        "parse_status": "success",
        "check": {
            "valid": False,
            "issues": [
                {"level": "error", "code": "high_risk_blocked", "step_id": "save_auth"},
                {"level": "error", "code": "capability_not_planner_visible", "step_id": "shell"},
                {"level": "error", "code": "path_outside_workspace", "step_id": "observe"},
            ],
        },
    }

    suggestions = planner_draft_recovery_suggestions(result)

    assert any("Remove high-risk actions" in item for item in suggestions)
    assert any("planner-visible atomic capabilities" in item for item in suggestions)
    assert any("workspace-relative paths" in item for item in suggestions)


def test_save_planner_draft_result_writes_only_valid_workflow_inside_workspace(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    result = {
        "status": "valid",
        "workflow": {
            "schema_version": 1,
            "min_runtime_version": "0.1.0",
            "name": "generated_login_check",
            "version": 1,
            "steps": [
                {"id": "observe", "action": "observe_html", "path": "fixtures/login.html"},
                {"id": "assert", "action": "assert_text", "text": "登录"},
            ],
        },
    }

    saved = save_planner_draft_result(workspace, result, "drafts/generated_login_check")
    blocked = save_planner_draft_result(workspace, result, "../outside")

    assert saved["save"]["status"] == "saved"
    assert saved["save"]["path"] == "workflows/drafts/generated_login_check.yaml"
    assert saved["preflight"]["ok"] is True
    assert saved["preflight"]["workflow_name"] == "generated_login_check"
    assert saved["preflight"]["missing_required_count"] == 0
    assert parse_workflow_file(workspace.root / saved["save"]["path"]).name == "generated_login_check"
    assert blocked["save"]["status"] == "blocked"
    assert blocked["save"]["reason"] == "path_outside_workflows"


def test_preview_planner_draft_save_returns_diff_without_writing(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    result = {
        "status": "valid",
        "workflow": {
            "schema_version": 1,
            "min_runtime_version": "0.1.0",
            "name": "preview_login_check",
            "version": 1,
            "steps": [
                {"id": "observe", "action": "observe_html", "path": "fixtures/login.html"},
                {"id": "assert", "action": "assert_text", "text": "登录"},
            ],
        },
    }

    preview = preview_planner_draft_save(workspace, result, "drafts/preview_login_check")
    target = workspace.workflows_dir / "drafts" / "preview_login_check.yaml"

    assert preview["save"]["status"] == "previewed"
    assert preview["save"]["path"] == "workflows/drafts/preview_login_check.yaml"
    assert "--- /dev/null" in preview["save"]["diff"]
    assert "+name: preview_login_check" in preview["save"]["diff"]
    assert "preflight" not in preview
    assert not target.exists()


def test_workspace_planner_draft_cli_saves_valid_model_draft(tmp_path, capsys, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    key_file = tmp_path / "keys.txt"
    key_file.write_text("xiaomimimo api key: sk-xiaomi-secret-value-abcdef\n", encoding="utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": """
schema_version: 1
min_runtime_version: "0.1.0"
name: saved_login_check
version: 1
steps:
  - name: observe login
    action: observe_html
    params:
      path: fixtures/login.html
  - name: assert login
    action: assert_text
    params:
      text: 登录
"""
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    monkeypatch.setattr("visual_agent.planner_generate.urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())

    code = main(
        [
            "workspace-planner-draft",
            "--root",
            str(workspace.root),
            "--instruction",
            "检查登录页",
            "--source",
            str(key_file),
            "--run",
            "--save-as",
            "saved_login_check",
            "--format",
            "markdown",
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert "Save status: `saved`" in output
    assert "Preflight OK: `True`" in output
    assert "## Preflight" in output
    assert parse_workflow_file(workspace.workflows_dir / "saved_login_check.yaml").name == "saved_login_check"
    assert "sk-xiaomi-secret-value-abcdef" not in output


def test_workspace_planner_draft_cli_preview_save_does_not_write(tmp_path, capsys, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    key_file = tmp_path / "keys.txt"
    key_file.write_text("xiaomimimo api key: sk-xiaomi-secret-value-abcdef\n", encoding="utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": """
schema_version: 1
min_runtime_version: "0.1.0"
name: preview_login_check
version: 1
steps:
  - id: observe
    action: observe_html
    path: fixtures/login.html
  - id: assert
    action: assert_text
    text: 登录
"""
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    monkeypatch.setattr("visual_agent.planner_generate.urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())

    code = main(
        [
            "workspace-planner-draft",
            "--root",
            str(workspace.root),
            "--instruction",
            "检查登录页",
            "--source",
            str(key_file),
            "--run",
            "--save-as",
            "preview_login_check",
            "--preview-save",
            "--format",
            "markdown",
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert "Save status: `previewed`" in output
    assert "## Save Diff" in output
    assert not (workspace.workflows_dir / "preview_login_check.yaml").exists()
    assert "sk-xiaomi-secret-value-abcdef" not in output


def test_workspace_planner_draft_cli_plan_only(tmp_path, capsys) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    key_file = tmp_path / "keys.txt"
    key_file.write_text("xiaomimimo api key: sk-xiaomi-secret-value-abcdef\n", encoding="utf-8")

    code = main(
        [
            "workspace-planner-draft",
            "--root",
            str(workspace.root),
            "--instruction",
            "检查登录页",
            "--source",
            str(key_file),
            "--format",
            "markdown",
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert "Status: `planned`" in output
    assert "sk-xiaomi-secret-value-abcdef" not in output
