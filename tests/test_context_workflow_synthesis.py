from __future__ import annotations

import json
from pathlib import Path

import pytest

from visual_agent.context_ingestion import CodeChange, GenerationContext, ingest_context, summarize_data_displays
from visual_agent.workflow import parse_workflow_file
from visual_agent.workflow_synthesis import generate_workflow_from_context


HTML_LOGIN = """
<!doctype html>
<html>
<head><title>Login</title></head>
<body>
  <form action="/dashboard">
    <label for="email">Email</label>
    <input id="email" name="email" type="email" required>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" required>
    <button type="submit">Sign in</button>
  </form>
  <p>Welcome to Dashboard</p>
  <p class="error">Invalid password</p>
</body>
</html>
"""


def test_ingest_html_form_semantics() -> None:
    ctx = GenerationContext(
        task_description="Verify login",
        code_changes=(CodeChange(file_path="login.html", before=None, after=HTML_LOGIN, change_type="added"),),
        base_url="http://localhost:3000/login",
        project_root=".",
    )

    model = ingest_context(ctx)

    assert model.framework == "html"
    assert model.confidence >= 0.8
    assert [field.name for field in model.form_fields] == ["email", "password"]
    assert model.form_fields[0].validation_rules == ("required", "email_format")
    assert model.form_fields[1].validation_rules == ("required",)
    assert model.form_fields[1].is_sensitive is True
    assert model.submit_actions[0].text == "Sign in"
    assert any(state.value == "/dashboard" for state in model.success_states)
    assert any(state.text == "Invalid password" for state in model.error_states)


def test_generate_workflow_from_context_dry_run_scores_success_path() -> None:
    ctx = GenerationContext(
        task_description="Verify login redirects to dashboard",
        code_changes=(CodeChange(file_path="login.html", before=None, after=HTML_LOGIN, change_type="added"),),
        base_url="http://localhost:3000/login",
        project_root=".",
    )

    result = generate_workflow_from_context(ctx=ctx, dry_run=True)

    assert result.status == "success"
    assert result.workflow_path is None
    assert result.quality_score.total_score >= 0.6
    assert result.quality_score.covers_error_path is True
    assert "value_from: input.email" in result.workflow_yaml
    assert "url_contains: /dashboard" in result.workflow_yaml
    assert "assert_known_errors_absent" in result.workflow_yaml
    assert "forbidden_any:" in result.workflow_yaml
    assert "Invalid password" in result.workflow_yaml
    assert len(result.generation_trace) <= 10
    assert "field email -> paste input.email" in result.generation_trace
    assert "field password -> paste input.password sensitive" in result.generation_trace
    assert any("success url /dashboard -> wait_for url" == item for item in result.generation_trace)
    assert "known error texts -> forbidden_any" in result.generation_trace


def test_generate_workflow_from_context_saves_workflow_and_inputs(tmp_path: Path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    ctx = GenerationContext(
        task_description="Verify login redirects to dashboard",
        code_changes=(CodeChange(file_path="login.html", before=None, after=HTML_LOGIN, change_type="added"),),
        base_url="http://localhost:3000/login",
        project_root=str(workspace),
    )

    result = generate_workflow_from_context(ctx=ctx)

    assert result.workflow_path is not None
    assert result.inputs_path is not None
    assert Path(result.workflow_path).exists()
    assert Path(result.inputs_path).exists()
    workflow = parse_workflow_file(result.workflow_path)
    assert workflow.name == result.workflow_name
    assert "verification" in workflow.tags
    inputs = json.loads(Path(result.inputs_path).read_text(encoding="utf-8"))
    assert inputs["email"] == "demo@example.com"
    assert inputs["password"] == ""


def test_generated_inputs_template_uses_safe_examples(tmp_path: Path) -> None:
    html = """
    <form action="/saved">
      <input name="username" placeholder="Username">
      <input name="phone" type="tel" placeholder="Phone">
      <input name="quantity" type="number">
      <input name="api_key" placeholder="API key">
      <button type="submit">Save</button>
      <p>Saved successfully</p>
    </form>
    """
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    ctx = GenerationContext(
        task_description="Verify profile saves",
        code_changes=(CodeChange(file_path="profile.html", before=None, after=html, change_type="added"),),
        base_url="http://localhost:3000/profile",
        project_root=str(workspace),
    )

    result = generate_workflow_from_context(ctx=ctx)
    inputs = json.loads(Path(str(result.inputs_path)).read_text(encoding="utf-8"))

    assert inputs["username"] == "demo_user"
    assert inputs["phone"] == "15500000000"
    assert inputs["quantity"] == "1"
    assert inputs["api_key"] == ""


def test_generated_inputs_template_respects_validation_rules(tmp_path: Path) -> None:
    html = """
    <form action="/saved">
      <input name="username" minlength="8" maxlength="12">
      <input name="pin" pattern="\\d{4}">
      <input name="quantity" type="number" min="3" max="5">
      <input name="password" type="password" minlength="12">
      <button type="submit">Save</button>
      <p>Saved successfully</p>
    </form>
    """
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    ctx = GenerationContext(
        task_description="Verify constrained profile saves",
        code_changes=(CodeChange(file_path="profile.html", before=None, after=html, change_type="added"),),
        base_url="http://localhost:3000/profile",
        project_root=str(workspace),
    )

    result = generate_workflow_from_context(ctx=ctx)
    inputs = json.loads(Path(str(result.inputs_path)).read_text(encoding="utf-8"))

    assert inputs["username"] == "demo_user"
    assert inputs["pin"] == "1111"
    assert inputs["quantity"] == "3"
    assert inputs["password"] == ""


def test_negative_input_cases_are_draft_only_and_respect_validation_rules() -> None:
    html = """
    <form action="/saved">
      <input name="email" type="email" required>
      <input name="username" minlength="8" maxlength="12">
      <input name="pin" pattern="\\d{4}">
      <input name="quantity" type="number" min="3" max="5">
      <button type="submit">Save</button>
      <p>Saved successfully</p>
      <p>Invalid input</p>
    </form>
    """
    result = generate_workflow_from_context(
        ctx=GenerationContext(
            task_description="Verify constrained form saves",
            code_changes=(CodeChange(file_path="profile.html", before=None, after=html, change_type="added"),),
            base_url="http://localhost:3000/profile",
            project_root=".",
        ),
        dry_run=True,
    )
    cases = {case["id"]: case for case in result.negative_input_cases}

    assert cases["invalid_email_required"]["inputs"]["email"] == ""
    assert cases["invalid_email_email_format"]["inputs"]["email"] == "not-an-email"
    assert cases["invalid_username_min_length"]["inputs"]["username"] == "a" * 7
    assert cases["invalid_username_max_length"]["inputs"]["username"] == "a" * 13
    assert cases["invalid_pin_pattern"]["inputs"]["pin"] == "invalid"
    assert cases["invalid_quantity_min"]["inputs"]["quantity"] == "2"
    assert cases["invalid_quantity_max"]["inputs"]["quantity"] == "6"
    assert all(case["mode"] == "draft_only" for case in result.negative_input_cases)
    assert any("draft negative_input_cases" in item for item in result.generation_trace)
    assert "invalid_email_required" not in result.workflow_yaml
    assert result.negative_workflow_path is None
    assert result.negative_workflow_yaml is not None
    assert result.negative_workflow_ready is True
    assert result.negative_workflow_reason == "ready"
    assert result.negative_workflow_reset_strategy == "fresh_observe_per_case"
    assert result.negative_oracles == ({"text": "Invalid input", "source": "html:text"},)
    assert "negative_draft" in result.negative_workflow_yaml
    assert "invalid_email_required" in result.negative_workflow_yaml
    assert "tags:" in result.negative_workflow_yaml
    assert "negative" in result.negative_workflow_yaml
    assert "affects:" in result.negative_workflow_yaml
    assert "metadata:" in result.negative_workflow_yaml
    assert "reset_strategy: fresh_observe_per_case" in result.negative_workflow_yaml


def test_negative_workflow_without_error_oracle_is_not_ready() -> None:
    html = """
    <form action="/saved">
      <input name="email" type="email" required>
      <button type="submit">Save</button>
      <p>Saved successfully</p>
    </form>
    """
    result = generate_workflow_from_context(
        ctx=GenerationContext(
            task_description="Verify constrained form saves",
            code_changes=(CodeChange(file_path="profile.html", before=None, after=html, change_type="added"),),
            base_url="http://localhost:3000/profile",
            project_root=".",
        ),
        dry_run=True,
    )

    assert result.negative_input_cases
    assert result.negative_workflow_yaml is not None
    assert result.negative_workflow_ready is False
    assert result.negative_workflow_reason == "no_negative_oracle"
    assert result.negative_workflow_reset_strategy == "fresh_observe_per_case"
    assert result.negative_oracles == ()


def test_error_oracle_ignores_success_text_with_error_keyword() -> None:
    html = """
    <form action="/saved">
      <input name="email" type="email" required>
      <button type="submit">Save</button>
      <p>Saved successfully without invalid input</p>
    </form>
    """
    model = ingest_context(
        GenerationContext(
            task_description="Verify constrained form saves",
            code_changes=(CodeChange(file_path="profile.html", before=None, after=html, change_type="added"),),
            base_url="http://localhost:3000/profile",
            project_root=".",
        )
    )
    result = generate_workflow_from_context(
        ctx=GenerationContext(
            task_description="Verify constrained form saves",
            code_changes=(CodeChange(file_path="profile.html", before=None, after=html, change_type="added"),),
            base_url="http://localhost:3000/profile",
            project_root=".",
        ),
        dry_run=True,
    )

    assert model.error_states == ()
    assert result.negative_workflow_ready is False
    assert result.negative_workflow_reason == "no_negative_oracle"


def test_negative_oracles_redact_secret_text() -> None:
    html = """
    <form action="/saved">
      <input name="email" type="email" required>
      <button type="submit">Save</button>
      <p>Invalid api_key=sk-secret-value</p>
    </form>
    """
    result = generate_workflow_from_context(
        ctx=GenerationContext(
            task_description="Verify constrained form saves",
            code_changes=(CodeChange(file_path="profile.html", before=None, after=html, change_type="added"),),
            base_url="http://localhost:3000/profile",
            project_root=".",
        ),
        dry_run=True,
    )

    raw = str(result.negative_oracles)
    assert result.negative_oracles[0]["text"] == "Invalid api_key=[REDACTED]"
    assert "sk-secret-value" not in raw


def test_negative_workflow_draft_is_saved_separately_and_parseable(tmp_path: Path) -> None:
    html = """
    <form action="/saved">
      <input name="email" type="email" required>
      <input name="password" type="password" required minlength="8">
      <button type="submit">Save</button>
      <p>Saved successfully</p>
      <p>Invalid input</p>
    </form>
    """
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    result = generate_workflow_from_context(
        ctx=GenerationContext(
            task_description="Verify constrained form saves",
            code_changes=(CodeChange(file_path="profile.html", before=None, after=html, change_type="added"),),
            base_url="http://localhost:3000/profile",
            project_root=str(workspace),
        )
    )

    assert result.workflow_path is not None
    assert result.negative_workflow_path is not None
    assert Path(result.negative_workflow_path).exists()
    assert Path(result.negative_workflow_path).name.endswith("_negative_draft.yaml")
    workflow = parse_workflow_file(result.negative_workflow_path)
    assert workflow.name.endswith("_negative_draft")
    assert "negative" in workflow.tags
    assert "draft" in workflow.tags
    text = Path(result.negative_workflow_path).read_text(encoding="utf-8")
    assert "invalid_email_required" in text
    assert "not-an-email" in text
    assert "Invalid input" in text
    assert "aaaaaaaa" not in text


def test_ingest_react_form_semantics() -> None:
    jsx = """
    export function Signup() {
      const navigate = useNavigate();
      async function submit() {
        navigate("/welcome");
      }
      return (
        <form onSubmit={submit}>
          <Input name="email" label="Email" type="email" required />
          <input name="password" placeholder="Password" type="password" required minLength="8" />
          <button type="submit">Create account</button>
          {success && <p>Account created successfully</p>}
          {error && <p>Invalid email address</p>}
        </form>
      );
    }
    """
    ctx = GenerationContext(
        task_description="Verify signup creates account",
        code_changes=(CodeChange(file_path="Signup.tsx", before=None, after=jsx, change_type="added"),),
        base_url="http://localhost:3000/signup",
        project_root=".",
    )

    model = ingest_context(ctx)

    assert model.framework == "react"
    assert [field.name for field in model.form_fields] == ["email", "password"]
    assert model.form_fields[0].validation_rules == ("required", "email_format")
    assert model.form_fields[1].validation_rules == ("required", "min_length:8")
    assert model.form_fields[1].is_sensitive is True
    assert model.submit_actions[0].text == "Create account"
    assert any(state.value == "/welcome" for state in model.success_states)
    assert any(state.value == "Account created successfully" for state in model.success_states)
    assert any(state.text == "Invalid email address" for state in model.error_states)


def test_generate_react_workflow_uses_redirect_and_success_text() -> None:
    jsx = """
    function Profile() {
      return (
        <form>
          <input name="displayName" placeholder="Display name" />
          <button type="submit">Save profile</button>
          <p>Profile saved successfully</p>
          <p>{displayName}</p>
        </form>
      );
    }
    """
    ctx = GenerationContext(
        task_description="Verify profile saves",
        code_changes=(CodeChange(file_path="Profile.jsx", before=None, after=jsx, change_type="added"),),
        base_url="http://localhost:3000/profile",
        project_root=".",
    )

    result = generate_workflow_from_context(ctx=ctx, dry_run=True)

    assert result.quality_score.total_score >= 0.6
    assert result.quality_score.covers_data_display is True
    assert "fill_displayname" in result.workflow_yaml.lower()
    assert "assert_displayed_displayname" in result.workflow_yaml.lower()
    assert "text_from: input.displayName" in result.workflow_yaml
    assert "Profile saved successfully" in result.workflow_yaml


def test_data_display_summary_weakly_matches_nested_non_sensitive_fields() -> None:
    jsx = """
    function Profile() {
      return (
        <form>
          <input name="displayName" placeholder="Display name" />
          <input name="email" placeholder="Email" />
          <button type="submit">Save profile</button>
          <p>Profile saved successfully</p>
          <p>{profile.displayName}</p>
          <p>{user.email}</p>
          <p>{profile.timezone}</p>
        </form>
      );
    }
    """
    result = generate_workflow_from_context(
        ctx=GenerationContext(
            task_description="Verify profile saves",
            code_changes=(CodeChange(file_path="Profile.jsx", before=None, after=jsx, change_type="added"),),
            base_url="http://localhost:3000/profile",
            project_root=".",
        ),
        dry_run=True,
    )
    summary = summarize_data_displays(result.semantic_model)

    assert summary.matched == ("profile.displayName", "user.email")
    assert summary.unmatched == ("profile.timezone",)
    assert "text_from: input.displayName" in result.workflow_yaml
    assert "text_from: input.email" in result.workflow_yaml
    assert "input.timezone" not in result.workflow_yaml
    assert "display displayName -> assert_text text_from input.displayName" in result.generation_trace
    assert "display email -> assert_text text_from input.email" in result.generation_trace
    assert "display profile.timezone -> semantic_summary only" in result.generation_trace


def test_generate_react_workflow_does_not_assert_sensitive_display() -> None:
    jsx = """
    function Account() {
      return (
        <form>
          <input name="password" type="password" required />
          <button type="submit">Save password</button>
          <p>Password updated successfully</p>
          <p>{password}</p>
        </form>
      );
    }
    """
    ctx = GenerationContext(
        task_description="Verify password saves",
        code_changes=(CodeChange(file_path="Account.jsx", before=None, after=jsx, change_type="added"),),
        base_url="http://localhost:3000/account",
        project_root=".",
    )

    result = generate_workflow_from_context(ctx=ctx, dry_run=True)

    assert result.semantic_model.data_displays == ("password",)
    assert summarize_data_displays(result.semantic_model).matched == ()
    assert summarize_data_displays(result.semantic_model).unmatched == ()
    assert "text_from: input.password" not in result.workflow_yaml
    assert "assert_displayed_password" not in result.workflow_yaml


def test_react_common_field_components_are_parsed_as_inputs() -> None:
    jsx = """
    function SettingsForm() {
      return (
        <form>
          <TextField name="displayName" label="Display name" required minLength="3" />
          <Form.Field name="timezone" label="Timezone" />
          <button type="submit">Save settings</button>
          <p>Settings saved successfully</p>
          <p>{settings.displayName}</p>
          <p>{settings.timezone}</p>
        </form>
      );
    }
    """
    result = generate_workflow_from_context(
        ctx=GenerationContext(
            task_description="Verify settings save",
            code_changes=(CodeChange(file_path="SettingsForm.jsx", before=None, after=jsx, change_type="added"),),
            base_url="http://localhost:3000/settings",
            project_root=".",
        ),
        dry_run=True,
    )

    assert [field.name for field in result.semantic_model.form_fields] == ["displayName", "timezone"]
    assert result.semantic_model.form_fields[0].validation_rules == ("required", "min_length:3")
    assert summarize_data_displays(result.semantic_model).matched == ("settings.displayName", "settings.timezone")
    assert "text_from: input.displayName" in result.workflow_yaml
    assert "text_from: input.timezone" in result.workflow_yaml


@pytest.mark.parametrize(
    ("component", "expected_name", "expected_type"),
    [
        ('<Select name="status" label="Status" />', "status", "select"),
        ('<DatePicker name="birthdate" label="Birth date" />', "birthdate", "date"),
        ('<InputNumber name="quantity" label="Quantity" min="1" max="99" />', "quantity", "number"),
        ('<Switch checked={enabled} label="Enabled" />', "enabled", "boolean"),
        ('<Upload name="avatar" label="Avatar" />', "avatar", "file"),
    ],
)
def test_react_component_library_fields_are_parsed(component: str, expected_name: str, expected_type: str) -> None:
    jsx = f"""
    function ProductForm() {{
      return (
        <form>
          {component}
          <button type="submit">Save product</button>
          <p>Product saved successfully</p>
        </form>
      );
    }}
    """
    model = ingest_context(
        GenerationContext(
            task_description="Verify product save",
            code_changes=(CodeChange(file_path="ProductForm.tsx", before=None, after=jsx, change_type="added"),),
            base_url="http://localhost:3000/products/new",
            project_root=".",
        )
    )

    assert [(field.name, field.field_type) for field in model.form_fields] == [(expected_name, expected_type)]


def test_react_antd_modal_ok_text_is_parsed_as_confirm_action() -> None:
    jsx = """
    function UsersTable() {
      return (
        <section>
          <button type="button">Delete Ada</button>
          <Modal open={confirmOpen} okText="Confirm Delete" title="Delete user">
            Delete Ada?
          </Modal>
          <p>User deleted successfully</p>
        </section>
      );
    }
    """
    result = generate_workflow_from_context(
        ctx=GenerationContext(
            task_description="Verify user deletion",
            code_changes=(CodeChange(file_path="UsersTable.tsx", before=None, after=jsx, change_type="added"),),
            base_url="http://localhost:3000/users",
            project_root=".",
        ),
        dry_run=True,
    )

    assert [action.text for action in result.semantic_model.submit_actions] == ["Delete Ada", "Confirm Delete"]
    assert "click_confirm_2" in result.workflow_yaml


def test_react_list_row_delete_action_is_parsed_as_verification_flow() -> None:
    jsx = """
    function UsersTable() {
      return (
        <section>
          <table>
            <tbody>
              <tr>
                <td>Ada Lovelace</td>
                <td><button type="button">Delete Ada</button></td>
              </tr>
            </tbody>
          </table>
          <p>User deleted successfully</p>
        </section>
      );
    }
    """
    result = generate_workflow_from_context(
        ctx=GenerationContext(
            task_description="Verify user row deletion",
            code_changes=(CodeChange(file_path="UsersTable.jsx", before=None, after=jsx, change_type="added"),),
            base_url="http://localhost:3000/users",
            project_root=".",
        ),
        dry_run=True,
    )

    assert result.semantic_model.form_fields == ()
    assert result.semantic_model.submit_actions[0].text == "Delete Ada"
    assert any(state.value == "User deleted successfully" for state in result.semantic_model.success_states)
    assert "text: Delete Ada" in result.workflow_yaml
    assert "User deleted successfully" in result.workflow_yaml


def test_react_delete_confirmation_clicks_destructive_then_confirm() -> None:
    jsx = """
    function DeleteUserDialog() {
      return (
        <section>
          <button type="button">Delete Ada</button>
          <div role="dialog">
            <button type="button">Cancel</button>
            <button type="button">Confirm Delete</button>
          </div>
          <p>User deleted successfully</p>
        </section>
      );
    }
    """
    result = generate_workflow_from_context(
        ctx=GenerationContext(
            task_description="Verify delete confirmation",
            code_changes=(CodeChange(file_path="DeleteUserDialog.jsx", before=None, after=jsx, change_type="added"),),
            base_url="http://localhost:3000/users",
            project_root=".",
        ),
        dry_run=True,
    )

    assert [action.text for action in result.semantic_model.submit_actions] == ["Delete Ada", "Confirm Delete"]
    assert "id: click_submit" in result.workflow_yaml
    assert "text: Delete Ada" in result.workflow_yaml
    assert "id: click_confirm_2" in result.workflow_yaml
    assert "text: Confirm Delete" in result.workflow_yaml
    assert "submit Delete Ada -> click" in result.generation_trace
    assert "submit Confirm Delete -> click" in result.generation_trace


def test_ingest_vue_form_semantics() -> None:
    vue = """
    <template>
      <form>
        <label for="email">Email</label>
        <input id="email" name="email" required>
        <button type="submit">Save</button>
        <p>Profile saved successfully</p>
        <p>Invalid email</p>
      </form>
    </template>
    <script setup>
    router.push('/profile')
    </script>
    """
    ctx = GenerationContext(
        task_description="Verify profile saves",
        code_changes=(CodeChange(file_path="Profile.vue", before=None, after=vue, change_type="added"),),
        base_url="http://localhost:3000/profile",
        project_root=".",
    )

    model = ingest_context(ctx)

    assert model.framework == "vue"
    assert [field.name for field in model.form_fields] == ["email"]
    assert model.submit_actions[0].text == "Save"
    assert any(state.value == "/profile" for state in model.success_states)
    assert any(state.value == "Profile saved successfully" for state in model.success_states)
    assert any(state.text == "Invalid email" for state in model.error_states)


def test_ingest_django_backend_semantics() -> None:
    django_view = """
    from django.shortcuts import redirect
    from django.contrib import messages

    def login(request):
        messages.success(request, "Login successful")
        return redirect("/dashboard")
    """
    ctx = GenerationContext(
        task_description="Verify login backend redirects",
        code_changes=(CodeChange(file_path="views.py", before=None, after=django_view, change_type="added"),),
        base_url="http://localhost:8000/login",
        project_root=".",
    )

    model = ingest_context(ctx)

    assert model.framework == "django"
    assert model.confidence >= 0.6
    assert any(state.value == "/dashboard" for state in model.success_states)
    assert any(state.value == "Login successful" for state in model.success_states)


def test_ingest_fastapi_and_flask_backend_redirects() -> None:
    fastapi = """
    from fastapi import FastAPI
    from fastapi.responses import RedirectResponse
    app = FastAPI()
    @app.post("/login")
    def login():
        return RedirectResponse(url="/dashboard")
    """
    flask = """
    from flask import Flask, redirect
    app = Flask(__name__)
    @app.route("/submit", methods=["POST"])
    def submit():
        return redirect("/done")
    """

    fastapi_model = ingest_context(
        GenerationContext(
            task_description="Verify login",
            code_changes=(CodeChange(file_path="main.py", before=None, after=fastapi, change_type="added"),),
            base_url="http://localhost:8000/login",
            project_root=".",
        )
    )
    flask_model = ingest_context(
        GenerationContext(
            task_description="Verify submit",
            code_changes=(CodeChange(file_path="app.py", before=None, after=flask, change_type="added"),),
            base_url="http://localhost:5000/submit",
            project_root=".",
        )
    )

    assert fastapi_model.framework == "fastapi"
    assert any(state.value == "/dashboard" for state in fastapi_model.success_states)
    assert flask_model.framework == "flask"
    assert any(state.value == "/done" for state in flask_model.success_states)


def test_ingest_merges_frontend_and_backend_semantics() -> None:
    jsx = """
    function Login() {
      return (
        <form>
          <input name="email" placeholder="Email" />
          <button type="submit">Sign in</button>
        </form>
      );
    }
    """
    django_view = """
    from django.shortcuts import redirect
    def login(request):
        return redirect("/dashboard")
    """
    ctx = GenerationContext(
        task_description="Verify login redirects",
        code_changes=(
            CodeChange(file_path="Login.tsx", before=None, after=jsx, change_type="added"),
            CodeChange(file_path="views.py", before=None, after=django_view, change_type="added"),
        ),
        base_url="http://localhost:3000/login",
        project_root=".",
    )

    model = ingest_context(ctx)

    assert model.framework == "react"
    assert [field.name for field in model.form_fields] == ["email"]
    assert any(state.value == "/dashboard" for state in model.success_states)


def test_ingest_nextjs_app_router_server_action_semantics() -> None:
    page = """
    import { redirect } from "next/navigation";

    async function saveProfile(formData) {
      "use server";
      redirect("/profile");
    }

    export default function ProfilePage() {
      return (
        <form action={saveProfile}>
          <input name="displayName" placeholder="Display name" required minLength="3" />
          <input name="email" type="email" required />
          <button type="submit">Save profile</button>
          <p>Profile saved successfully</p>
          <p>{profile.displayName}</p>
          <p>Invalid email</p>
        </form>
      );
    }
    """
    result = generate_workflow_from_context(
        ctx=GenerationContext(
            task_description="Verify Next.js profile saves",
            code_changes=(CodeChange(file_path="app/profile/page.tsx", before=None, after=page, change_type="added"),),
            base_url="http://localhost:3000/profile",
            project_root=".",
        ),
        dry_run=True,
    )

    model = result.semantic_model

    assert model.framework == "nextjs"
    assert [field.name for field in model.form_fields] == ["displayName", "email"]
    assert model.form_fields[0].validation_rules == ("required", "min_length:3")
    assert model.form_fields[1].validation_rules == ("required", "email_format")
    assert model.submit_actions[0].text == "Save profile"
    assert any(state.value == "/profile" for state in model.success_states)
    assert any(state.value == "Profile saved successfully" for state in model.success_states)
    assert any(state.text == "Invalid email" for state in model.error_states)
    assert "url_contains: /profile" in result.workflow_yaml
    assert "text_from: input.displayName" in result.workflow_yaml
    assert result.semantic_model.parse_warnings == ()


def test_ingest_nextjs_use_router_push_semantics() -> None:
    component = """
    "use client";
    import { useRouter } from "next/navigation";

    export default function LoginPage() {
      const router = useRouter();
      return (
        <form>
          <input name="email" type="email" required />
          <button type="submit" onClick={() => router.push("/dashboard")}>Sign in</button>
          <p>Welcome Dashboard</p>
        </form>
      );
    }
    """
    model = ingest_context(
        GenerationContext(
            task_description="Verify Next.js login redirects",
            code_changes=(CodeChange(file_path="app/login/page.tsx", before=None, after=component, change_type="added"),),
            base_url="http://localhost:3000/login",
            project_root=".",
        )
    )

    assert model.framework == "nextjs"
    assert [field.name for field in model.form_fields] == ["email"]
    assert any(state.value == "/dashboard" for state in model.success_states)
    assert any(state.value == "Welcome Dashboard" for state in model.success_states)


def test_ingest_remix_action_form_semantics() -> None:
    route = """
    import { Form, redirect } from "@remix-run/react";

    export async function action({ request }) {
      return redirect("/orders");
    }

    export default function OrderRoute() {
      return (
        <Form method="post">
          <input name="orderId" placeholder="Order ID" required pattern="\\d{6}" />
          <input name="email" type="email" required />
          <button type="submit">Create order</button>
          <p>Order created successfully</p>
          <p>{order.orderId}</p>
          <p>Invalid order ID</p>
        </Form>
      );
    }
    """
    result = generate_workflow_from_context(
        ctx=GenerationContext(
            task_description="Verify Remix order creation",
            code_changes=(CodeChange(file_path="app/routes/orders._index.tsx", before=None, after=route, change_type="added"),),
            base_url="http://localhost:3000/orders/new",
            project_root=".",
        ),
        dry_run=True,
    )

    model = result.semantic_model

    assert model.framework == "remix"
    assert [field.name for field in model.form_fields] == ["orderId", "email"]
    assert model.form_fields[0].validation_rules == ("required", "pattern:\\d{6}")
    assert model.form_fields[1].validation_rules == ("required", "email_format")
    assert model.submit_actions[0].text == "Create order"
    assert any(state.value == "/orders" for state in model.success_states)
    assert any(state.value == "Order created successfully" for state in model.success_states)
    assert any(state.text == "Invalid order ID" for state in model.error_states)
    assert "url_contains: /orders" in result.workflow_yaml
    assert "text_from: input.orderId" in result.workflow_yaml
    assert result.negative_input_cases


def test_ingest_sveltekit_form_and_server_action_semantics() -> None:
    page = """
    <script>
      import { goto } from '$app/navigation';
    </script>

    <form method="POST">
      <label for="displayName">Display name</label>
      <input id="displayName" name="displayName" required minlength="3">
      <label for="email">Email</label>
      <input id="email" name="email" type="email" required>
      <button type="submit">Save profile</button>
      <p>Profile saved successfully</p>
      <p>{profile.displayName}</p>
      <p>Invalid email</p>
    </form>
    """
    server = """
    import { redirect, fail } from '@sveltejs/kit';

    export const actions = {
      default: async ({ request }) => {
        return redirect(303, '/profile');
      }
    };
    """
    result = generate_workflow_from_context(
        ctx=GenerationContext(
            task_description="Verify SvelteKit profile saves",
            code_changes=(
                CodeChange(file_path="src/routes/profile/+page.svelte", before=None, after=page, change_type="added"),
                CodeChange(file_path="src/routes/profile/+page.server.ts", before=None, after=server, change_type="added"),
            ),
            base_url="http://localhost:5173/profile",
            project_root=".",
        ),
        dry_run=True,
    )

    model = result.semantic_model

    assert model.framework == "sveltekit"
    assert [field.name for field in model.form_fields] == ["displayName", "email"]
    assert model.form_fields[0].validation_rules == ("required", "min_length:3")
    assert model.form_fields[1].validation_rules == ("required", "email_format")
    assert model.submit_actions[0].text == "Save profile"
    assert any(state.value == "/profile" for state in model.success_states)
    assert any(state.value == "Profile saved successfully" for state in model.success_states)
    assert any(state.text == "Invalid email" for state in model.error_states)
    assert "url_contains: /profile" in result.workflow_yaml
    assert "text_from: input.displayName" in result.workflow_yaml


def test_ingest_sveltekit_goto_semantics() -> None:
    page = """
    <script>
      import { goto } from '$app/navigation';
      function submit() {
        goto('/dashboard');
      }
    </script>

    <form on:submit={submit}>
      <input name="email" type="email" required>
      <button type="submit">Sign in</button>
      <p>Welcome Dashboard</p>
    </form>
    """
    model = ingest_context(
        GenerationContext(
            task_description="Verify SvelteKit login redirects",
            code_changes=(CodeChange(file_path="src/routes/login/+page.svelte", before=None, after=page, change_type="added"),),
            base_url="http://localhost:5173/login",
            project_root=".",
        )
    )

    assert model.framework == "sveltekit"
    assert [field.name for field in model.form_fields] == ["email"]
    assert any(state.value == "/dashboard" for state in model.success_states)
    assert any(state.value == "Welcome Dashboard" for state in model.success_states)


def test_parse_warnings_report_missing_success_state() -> None:
    html = """
    <form>
      <input name="email">
      <button type="submit">Save</button>
    </form>
    """
    model = ingest_context(
        GenerationContext(
            task_description="Verify save",
            code_changes=(CodeChange(file_path="profile.html", before=None, after=html, change_type="added"),),
            base_url="http://localhost:3000/profile",
            project_root=".",
        )
    )

    assert "submit action found but no success state" in model.parse_warnings


def test_parse_warnings_report_missing_submit_action() -> None:
    html = """
    <form>
      <input name="email">
    </form>
    """
    model = ingest_context(
        GenerationContext(
            task_description="Verify save",
            code_changes=(CodeChange(file_path="profile.html", before=None, after=html, change_type="added"),),
            base_url="http://localhost:3000/profile",
            project_root=".",
        )
    )

    assert "form fields found but no submit action" in model.parse_warnings


def test_parse_warnings_report_react_form_without_fields_and_unmatched_display() -> None:
    jsx = """
    function Profile() {
      return (
        <form>
          <CustomField value={profile.displayName} />
          <button type="submit">Save</button>
        </form>
      );
    }
    """
    result = generate_workflow_from_context(
        ctx=GenerationContext(
            task_description="Verify profile save",
            code_changes=(CodeChange(file_path="Profile.jsx", before=None, after=jsx, change_type="added"),),
            base_url="http://localhost:3000/profile",
            project_root=".",
        ),
        dry_run=True,
    )

    assert "no form fields extracted" in result.warnings
    assert any("unmatched data displays: profile.displayName" == warning for warning in result.warnings)


def test_generate_low_confidence_context_uses_llm_fallback(monkeypatch) -> None:
    def fake_generate(*_args, **_kwargs) -> str:
        return """
        ```yaml
        schema_version: 1
        min_runtime_version: "0.1.0"
        name: verify_canvas_widget_verification
        version: 1
        description: Verify canvas widget
        tags: [verification, fast]
        visibility: private
        author: ""
        license: ""
        steps:
          - id: observe_initial
            action: observe_browser
            url: http://localhost:3000/widget
          - id: assert_browser_ready
            action: assert_browser_ready
            min_text_length: 1
          - id: assert_ready
            action: assert_text
            text: Widget ready
        ```
        """

    monkeypatch.setattr("visual_agent.workflow_synthesis._generate_with_anthropic", fake_generate)
    ctx = GenerationContext(
        task_description="Verify canvas widget",
        code_changes=(CodeChange(file_path="widget.txt", before=None, after="custom widget renders Widget ready", change_type="added"),),
        base_url="http://localhost:3000/widget",
        project_root=".",
    )

    result = generate_workflow_from_context(ctx=ctx, dry_run=True)

    assert result.generation_method == "llm"
    assert result.quality_score.total_score >= 0.6
    assert "Widget ready" in result.workflow_yaml


def test_generate_low_confidence_context_falls_back_when_llm_unavailable(monkeypatch) -> None:
    def fake_generate(*_args, **_kwargs) -> str:
        raise ImportError("anthropic")

    monkeypatch.setattr("visual_agent.workflow_synthesis._generate_with_anthropic", fake_generate)
    ctx = GenerationContext(
        task_description="Verify unknown widget",
        code_changes=(CodeChange(file_path="widget.txt", before=None, after="opaque widget code", change_type="added"),),
        base_url="http://localhost:3000/widget",
        project_root=".",
    )

    result = generate_workflow_from_context(ctx=ctx, dry_run=True)

    assert result.generation_method == "static_fallback"
    assert any("llm fallback unavailable" in warning for warning in result.warnings)
    assert "observe_browser" in result.workflow_yaml
