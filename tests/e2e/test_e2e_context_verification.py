from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from .helpers import json_output, run_cli


def test_e2e_git_diff_verify_impl_dry_run_writes_status_and_artifacts(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is required for this test")

    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = repo / ".agent-workspace"

    init = run_cli("init-workspace", "--root", str(workspace), "--overwrite", cwd=repo)
    assert init.returncode == 0, init.stdout + init.stderr

    fixture = workspace / "fixtures" / "profile.html"
    fixture.write_text(
        """
        <form action="/profile">
          <label for="displayName">Display name</label>
          <input id="displayName" name="displayName">
          <button type="submit">Save profile</button>
        </form>
        <p>Profile saved successfully</p>
        <p>Demo User</p>
        """,
        encoding="utf-8",
    )

    page = repo / "app" / "profile" / "page.tsx"
    page.parent.mkdir(parents=True)
    page.write_text(
        """
        export default function ProfilePage() {
          return <form><input name="displayName" /></form>;
        }
        """,
        encoding="utf-8",
    )
    git(repo, "init")
    git(repo, "add", "app/profile/page.tsx")
    git(repo, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")

    page.write_text(
        """
        import { redirect } from "next/navigation";

        async function saveProfile(formData: FormData) {
          "use server";
          redirect("/profile");
        }

        export default function ProfilePage() {
          return (
            <form action={saveProfile}>
              <label htmlFor="displayName">Display name</label>
              <input id="displayName" name="displayName" required minLength="3" />
              <button type="submit">Save profile</button>
              <p>Profile saved successfully</p>
              <p>Display name is required</p>
              <p>{profile.displayName}</p>
            </form>
          );
        }
        """,
        encoding="utf-8",
    )

    generated = run_cli(
        "generate-from-diff",
        "--workspace-root",
        str(workspace),
        "--repo-root",
        str(repo),
        "--task-description",
        "Verify profile form saves and displays the updated profile name",
        "--base-url",
        "fixtures/profile.html",
        "--dry-run",
        "--no-untracked",
        cwd=repo,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    generation_payload = json_output(generated)

    assert generation_payload["status"] == "success"
    assert generation_payload["changed_files"] == ["app/profile/page.tsx"]
    assert generation_payload["quality"]["score"] >= 0.6
    assert generation_payload["quality"]["data_display_assertions"] == 1
    assert generation_payload["quality"]["text_from_input_references"] == 1
    assert generation_payload["semantic_summary"]["framework"] == "nextjs"
    assert generation_payload["semantic_summary"]["field_count"] == 1
    assert generation_payload["semantic_summary"]["required_field_count"] == 1
    assert generation_payload["semantic_summary"]["negative_input_case_count"] >= 2
    assert generation_payload["semantic_summary"]["matched_data_displays"] == ["profile.displayName"]
    assert generation_payload["semantic_summary"]["unmatched_data_displays"] == []
    assert "field displayName -> paste input.displayName" in generation_payload["generation_trace"]
    assert "display displayName -> assert_text text_from input.displayName" in generation_payload["generation_trace"]
    assert "text_from: input.displayName" in generation_payload["yaml"]

    verified = run_cli(
        "verify-impl",
        "--workspace-root",
        str(workspace),
        "--repo-root",
        str(repo),
        "--task-description",
        "Verify profile form saves and displays the updated profile name",
        "--base-url",
        "fixtures/profile.html",
        "--run-profile",
        "dry-run",
        "--min-quality-score",
        "0",
        "--no-untracked",
        cwd=repo,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr
    verify_payload = json_output(verified)

    assert verify_payload["result"] == "pass"
    assert verify_payload["inputs_source"] == "generated_template"
    assert verify_payload["inputs_path"]
    assert verify_payload["workflow_path"]
    assert verify_payload["report_path"].endswith(f"{verify_payload['run_id']}.json")
    assert verify_payload["report_markdown_path"].endswith(f"{verify_payload['run_id']}.md")
    assert verify_payload["quality"]["data_display_assertions"] == 1
    assert verify_payload["semantic_summary"]["matched_data_displays"] == ["profile.displayName"]
    assert "display displayName -> assert_text text_from input.displayName" in verify_payload["generation_trace"]

    assert Path(verify_payload["workflow_path"]).exists()
    assert Path(verify_payload["inputs_path"]).exists()
    assert Path(verify_payload["report_path"]).exists()
    assert Path(verify_payload["report_markdown_path"]).exists()

    status_path = workspace / ".vscode-agent-status.json"
    assert status_path.exists()
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["result"] == "pass"
    assert status["inputs_source"] == "generated_template"
    assert status["inputs_path"] == verify_payload["inputs_path"]
    assert status["report_path"] == verify_payload["report_path"]
    assert status["report_markdown_path"] == verify_payload["report_markdown_path"]
    assert status["report_hint"] == verify_payload["report_hint"]
    assert status["semantic_summary"]["matched_data_displays"] == ["profile.displayName"]
    assert "display displayName -> assert_text text_from input.displayName" in status["generation_trace"]


@pytest.mark.parametrize(
    "sample",
    [
        {
            "name": "react_user_delete",
            "file_path": "src/UsersTable.jsx",
            "task": "Verify React user row delete confirmation shows success feedback",
            "base_url": "fixtures/react_user_delete.html",
            "fixture": """
            <table>
              <tbody>
                <tr><td>Ada Lovelace</td><td><button type="button">Delete Ada</button></td></tr>
              </tbody>
            </table>
            <div role="dialog">
              <button type="button">Cancel</button>
              <button type="button">Confirm Delete</button>
            </div>
            <p>User deleted successfully</p>
            """,
            "before": """
            export function UsersTable() {
              return <table><tbody><tr><td>Ada Lovelace</td></tr></tbody></table>;
            }
            """,
            "after": """
            export function UsersTable() {
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
                  <div role="dialog">
                    <button type="button">Cancel</button>
                    <button type="button">Confirm Delete</button>
                  </div>
                  <p>User deleted successfully</p>
                </section>
              );
            }
            """,
            "framework": "react",
            "field": "",
            "matched_display": "",
            "expected_display_count": 0,
            "expected_yaml": ["text: Delete Ada", "text: Confirm Delete", "id: click_confirm_2"],
        },
        {
            "name": "react_settings",
            "file_path": "src/SettingsForm.jsx",
            "task": "Verify React settings form saves and displays the chosen timezone",
            "base_url": "fixtures/react_settings.html",
            "fixture": """
            <form>
              <label for="displayName">Display name</label>
              <input id="displayName" name="displayName" value="Demo User">
              <label for="timezone">Timezone</label>
              <input id="timezone" name="timezone" value="UTC">
              <button type="submit">Save settings</button>
            </form>
            <table>
              <tr><th>Name</th><td>Demo User</td></tr>
              <tr><th>Timezone</th><td>UTC</td></tr>
            </table>
            <p>Settings saved successfully</p>
            """,
            "before": """
            export function SettingsForm() {
              return <form><Field name="displayName" /></form>;
            }
            """,
            "after": """
            export function SettingsForm() {
              return (
                <form>
                  <TextField name="displayName" label="Display name" required minLength="3" />
                  <Form.Field name="timezone" label="Timezone" />
                  <button type="submit">Save settings</button>
                  <table>
                    <tbody>
                      <tr><th>Name</th><td>{settings.displayName}</td></tr>
                      <tr><th>Timezone</th><td>{settings.timezone}</td></tr>
                    </tbody>
                  </table>
                  <p>Settings saved successfully</p>
                </form>
              );
            }
            """,
            "framework": "react",
            "field": "displayName",
            "matched_display": "settings.displayName",
            "extra_matched_displays": ["settings.timezone"],
            "extra_text_from": ["input.timezone"],
        },
        {
            "name": "react_hook_form_register",
            "file_path": "src/AccountForm.tsx",
            "task": "Verify React Hook Form account form saves",
            "base_url": "fixtures/react_hook_form_register.html",
            "fixture": "<form><label for='email'>Email</label><input id='email' name='email' type='email'><label for='displayName'>Display name</label><input id='displayName' name='displayName'><button type='submit'>Save account</button></form><p>Account saved successfully</p>",
            "before": "export function AccountForm() { return <form />; }",
            "after": """
            export function AccountForm() {
              const { register } = useForm();
              return (
                <form>
                  <input type="email" placeholder="Email" {...register("email", { required: true })} />
                  <input placeholder="Display name" {...register("displayName", { minLength: 3 })} />
                  <button type="submit">Save account</button>
                  <p>Account saved successfully</p>
                </form>
              );
            }
            """,
            "framework": "react",
            "field": "",
            "input_field": "email",
            "matched_display": "",
            "expected_display_count": 0,
            "expected_min_fields": 2,
            "expected_yaml": ["value_from: input.email", "value_from: input.displayName"],
        },
        {
            "name": "react_hook_form_controller_select",
            "file_path": "src/BillingForm.tsx",
            "task": "Verify React Hook Form billing plan saves",
            "base_url": "fixtures/react_hook_form_controller_select.html",
            "fixture": "<form><label for='plan'>Plan</label><select id='plan' name='plan'><option>pro</option></select><button type='submit'>Save billing</button></form><p>Billing saved successfully</p>",
            "before": "export function BillingForm() { return <form />; }",
            "after": """
            export function BillingForm() {
              return (
                <form>
                  <Controller name="plan" control={control} render={({ field }) => <Select {...field} options={plans} />} />
                  <button type="submit">Save billing</button>
                  <p>Billing saved successfully</p>
                </form>
              );
            }
            """,
            "framework": "react",
            "field": "",
            "input_field": "plan",
            "matched_display": "",
            "expected_display_count": 0,
            "expected_min_fields": 1,
            "expected_yaml": ["value_from: input.plan"],
        },
        {
            "name": "react_antd_select",
            "file_path": "src/ProductStatusForm.tsx",
            "task": "Verify React AntD status form saves",
            "base_url": "fixtures/react_antd_select.html",
            "fixture": "<form><label for='status'>Status</label><select id='status' name='status'><option>active</option></select><button type='submit'>Save product</button></form><p>Product saved successfully</p>",
            "before": "export function ProductStatusForm() { return <form />; }",
            "after": """
            export function ProductStatusForm() {
              return (
                <form>
                  <Select name="status" label="Status" />
                  <button type="submit">Save product</button>
                  <p>Product saved successfully</p>
                </form>
              );
            }
            """,
            "framework": "react",
            "field": "",
            "input_field": "status",
            "matched_display": "",
            "expected_display_count": 0,
            "expected_min_fields": 1,
            "expected_yaml": ["value_from: input.status"],
        },
        {
            "name": "react_antd_datepicker",
            "file_path": "src/BirthdateForm.tsx",
            "task": "Verify React AntD birthdate form saves",
            "base_url": "fixtures/react_antd_datepicker.html",
            "fixture": "<form><label for='birthdate'>Birth date</label><input id='birthdate' name='birthdate'><button type='submit'>Save profile</button></form><p>Profile saved successfully</p>",
            "before": "export function BirthdateForm() { return <form />; }",
            "after": """
            export function BirthdateForm() {
              return (
                <form>
                  <DatePicker name="birthdate" label="Birth date" />
                  <button type="submit">Save profile</button>
                  <p>Profile saved successfully</p>
                </form>
              );
            }
            """,
            "framework": "react",
            "field": "",
            "input_field": "birthdate",
            "matched_display": "",
            "expected_display_count": 0,
            "expected_min_fields": 1,
            "expected_yaml": ["value_from: input.birthdate"],
        },
        {
            "name": "react_antd_input_number",
            "file_path": "src/QuantityForm.tsx",
            "task": "Verify React AntD quantity form saves",
            "base_url": "fixtures/react_antd_input_number.html",
            "fixture": "<form><label for='quantity'>Quantity</label><input id='quantity' name='quantity' type='number'><button type='submit'>Save product</button></form><p>Product saved successfully</p>",
            "before": "export function QuantityForm() { return <form />; }",
            "after": """
            export function QuantityForm() {
              return (
                <form>
                  <InputNumber name="quantity" label="Quantity" min="1" max="99" />
                  <button type="submit">Save product</button>
                  <p>Product saved successfully</p>
                </form>
              );
            }
            """,
            "framework": "react",
            "field": "",
            "input_field": "quantity",
            "matched_display": "",
            "expected_display_count": 0,
            "expected_min_fields": 1,
            "expected_yaml": ["value_from: input.quantity"],
        },
        {
            "name": "react_antd_switch",
            "file_path": "src/EnabledForm.tsx",
            "task": "Verify React AntD enabled switch saves",
            "base_url": "fixtures/react_antd_switch.html",
            "fixture": "<form><label for='enabled'>Enabled</label><input id='enabled' name='enabled'><button type='submit'>Save settings</button></form><p>Settings saved successfully</p>",
            "before": "export function EnabledForm() { return <form />; }",
            "after": """
            export function EnabledForm() {
              return (
                <form>
                  <Switch checked={enabled} label="Enabled" />
                  <button type="submit">Save settings</button>
                  <p>Settings saved successfully</p>
                </form>
              );
            }
            """,
            "framework": "react",
            "field": "",
            "input_field": "enabled",
            "matched_display": "",
            "expected_display_count": 0,
            "expected_min_fields": 1,
            "expected_yaml": ["value_from: input.enabled"],
        },
        {
            "name": "react_antd_checkbox",
            "file_path": "src/SubscriptionForm.tsx",
            "task": "Verify React AntD subscription checkbox saves",
            "base_url": "fixtures/react_antd_checkbox.html",
            "fixture": "<form><label for='subscribed'>Subscribed</label><input id='subscribed' name='subscribed' type='checkbox'><button type='submit'>Save settings</button></form><p>Settings saved successfully</p>",
            "before": "export function SubscriptionForm() { return <form />; }",
            "after": """
            export function SubscriptionForm() {
              return (
                <form>
                  <Checkbox checked={subscribed} label="Subscribed" />
                  <button type="submit">Save settings</button>
                  <p>Settings saved successfully</p>
                </form>
              );
            }
            """,
            "framework": "react",
            "field": "",
            "input_field": "subscribed",
            "matched_display": "",
            "expected_display_count": 0,
            "expected_min_fields": 1,
            "expected_yaml": ["value_from: input.subscribed"],
        },
        {
            "name": "react_antd_radio_group",
            "file_path": "src/BillingPlanForm.tsx",
            "task": "Verify React AntD billing plan radio saves",
            "base_url": "fixtures/react_antd_radio_group.html",
            "fixture": "<form><label for='plan'>Plan</label><select id='plan' name='plan'><option>pro</option></select><button type='submit'>Save billing</button></form><p>Billing saved successfully</p>",
            "before": "export function BillingPlanForm() { return <form />; }",
            "after": """
            export function BillingPlanForm() {
              return (
                <form>
                  <Radio.Group name="plan" label="Plan" />
                  <button type="submit">Save billing</button>
                  <p>Billing saved successfully</p>
                </form>
              );
            }
            """,
            "framework": "react",
            "field": "",
            "input_field": "plan",
            "matched_display": "",
            "expected_display_count": 0,
            "expected_min_fields": 1,
            "expected_yaml": ["value_from: input.plan"],
        },
        {
            "name": "react_antd_slider",
            "file_path": "src/PriorityForm.tsx",
            "task": "Verify React AntD priority slider saves",
            "base_url": "fixtures/react_antd_slider.html",
            "fixture": "<form><label for='priority'>Priority</label><input id='priority' name='priority' type='number'><button type='submit'>Save priority</button></form><p>Priority saved successfully</p>",
            "before": "export function PriorityForm() { return <form />; }",
            "after": """
            export function PriorityForm() {
              return (
                <form>
                  <Slider name="priority" label="Priority" min="1" max="5" />
                  <button type="submit">Save priority</button>
                  <p>Priority saved successfully</p>
                </form>
              );
            }
            """,
            "framework": "react",
            "field": "",
            "input_field": "priority",
            "matched_display": "",
            "expected_display_count": 0,
            "expected_min_fields": 1,
            "expected_yaml": ["value_from: input.priority"],
        },
        {
            "name": "react_mui_autocomplete",
            "file_path": "src/AssigneeForm.tsx",
            "task": "Verify React MUI assignee autocomplete saves",
            "base_url": "fixtures/react_mui_autocomplete.html",
            "fixture": "<form><label for='assignee'>Assignee</label><select id='assignee' name='assignee'><option>Ada</option></select><button type='submit'>Save assignee</button></form><p>Assignee saved successfully</p>",
            "before": "export function AssigneeForm() { return <form />; }",
            "after": """
            export function AssigneeForm() {
              return (
                <form>
                  <Autocomplete name="assignee" label="Assignee" options={users} />
                  <button type="submit">Save assignee</button>
                  <p>Assignee saved successfully</p>
                </form>
              );
            }
            """,
            "framework": "react",
            "field": "",
            "input_field": "assignee",
            "matched_display": "",
            "expected_display_count": 0,
            "expected_min_fields": 1,
            "expected_yaml": ["value_from: input.assignee"],
        },
        {
            "name": "react_antd_upload",
            "file_path": "src/AvatarForm.tsx",
            "task": "Verify React AntD avatar upload form saves",
            "base_url": "fixtures/react_antd_upload.html",
            "fixture": "<form><label for='avatar'>Avatar</label><input id='avatar' name='avatar'><button type='submit'>Save avatar</button></form><p>Avatar saved successfully</p>",
            "before": "export function AvatarForm() { return <form />; }",
            "after": """
            export function AvatarForm() {
              return (
                <form>
                  <Upload name="avatar" label="Avatar" />
                  <button type="submit">Save avatar</button>
                  <p>Avatar saved successfully</p>
                </form>
              );
            }
            """,
            "framework": "react",
            "field": "",
            "input_field": "avatar",
            "matched_display": "",
            "expected_display_count": 0,
            "expected_min_fields": 1,
            "expected_yaml": ["value_from: input.avatar"],
        },
        {
            "name": "react_antd_modal_confirm",
            "file_path": "src/AntdUsersTable.tsx",
            "task": "Verify React AntD modal confirms user deletion",
            "base_url": "fixtures/react_antd_modal_confirm.html",
            "fixture": "<button type='button'>Delete Ada</button><p>User deleted successfully</p>",
            "before": "export function AntdUsersTable() { return <section />; }",
            "after": """
            export function AntdUsersTable() {
              return (
                <section>
                  <button type="button">Delete Ada</button>
                  <Modal open={confirmOpen} okText="Confirm Delete" title="Delete user" />
                  <p>User deleted successfully</p>
                </section>
              );
            }
            """,
            "framework": "react",
            "field": "",
            "matched_display": "",
            "expected_display_count": 0,
            "expected_yaml": ["text: Delete Ada", "text: Confirm Delete", "id: click_confirm_2"],
        },
        {
            "name": "vue_profile",
            "file_path": "src/Profile.vue",
            "task": "Verify Vue profile form saves and displays the updated name",
            "base_url": "fixtures/vue_profile.html",
            "fixture": """
            <form>
              <label for="displayName">Display name</label>
              <input id="displayName" name="displayName" value="Demo User">
              <button type="submit">Save profile</button>
            </form>
            <p>Profile saved successfully</p>
            <p>Demo User</p>
            """,
            "before": """
            <template>
              <form><input name="displayName"></form>
            </template>
            """,
            "after": """
            <template>
              <form>
                <label for="displayName">Display name</label>
                <input id="displayName" name="displayName" required minlength="3">
                <button type="submit">Save profile</button>
                <p>Profile saved successfully</p>
                <p>{{ profile.displayName }}</p>
                <p>Display name is required</p>
              </form>
            </template>
            """,
            "framework": "vue",
            "field": "displayName",
            "matched_display": "profile.displayName",
        },
        {
            "name": "remix_order",
            "file_path": "app/routes/orders._index.tsx",
            "task": "Verify Remix order form creates and displays the order id",
            "base_url": "fixtures/remix_order.html",
            "fixture": """
            <form>
              <label for="orderId">Order ID</label>
              <input id="orderId" name="orderId" value="111111">
              <button type="submit">Create order</button>
            </form>
            <p>Order created successfully</p>
            <p>111111</p>
            """,
            "before": """
            import { Form } from "@remix-run/react";
            export default function OrderRoute() {
              return <Form method="post"><input name="orderId" /></Form>;
            }
            """,
            "after": """
            import { Form } from "@remix-run/react";

            export async function action() {
              return { ok: true };
            }

            export default function OrderRoute() {
              return (
                <Form method="post">
                  <input name="orderId" placeholder="Order ID" required pattern="\\d{6}" />
                  <button type="submit">Create order</button>
                  <p>Order created successfully</p>
                  <p>{order.orderId}</p>
                  <p>Invalid order ID</p>
                </Form>
              );
            }
            """,
            "framework": "remix",
            "field": "orderId",
            "matched_display": "order.orderId",
        },
    ],
)
def test_e2e_real_frontend_samples_verify_impl_dry_run(tmp_path: Path, sample: dict[str, str]) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is required for this test")

    repo = tmp_path / sample["name"]
    repo.mkdir()
    workspace = repo / ".agent-workspace"

    init = run_cli("init-workspace", "--root", str(workspace), "--overwrite", "--no-demo", cwd=repo)
    assert init.returncode == 0, init.stdout + init.stderr

    fixture = workspace / sample["base_url"].replace("fixtures/", "fixtures/")
    fixture.write_text(sample["fixture"], encoding="utf-8")

    source = repo / sample["file_path"]
    source.parent.mkdir(parents=True)
    source.write_text(sample["before"], encoding="utf-8")
    git(repo, "init")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "add", sample["file_path"])
    git(repo, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
    source.write_text(sample["after"], encoding="utf-8")

    generated = run_cli(
        "generate-from-diff",
        "--workspace-root",
        str(workspace),
        "--repo-root",
        str(repo),
        "--task-description",
        sample["task"],
        "--base-url",
        sample["base_url"],
        "--dry-run",
        "--no-untracked",
        cwd=repo,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    generation_payload = json_output(generated)

    assert generation_payload["status"] == "success"
    assert generation_payload["changed_files"] == [sample["file_path"]]
    assert generation_payload["quality"]["score"] >= 0.6
    expected_display_count = int(sample.get("expected_display_count", 1 + len(sample.get("extra_matched_displays", []))))
    assert generation_payload["quality"]["data_display_assertions"] == expected_display_count
    assert generation_payload["quality"]["text_from_input_references"] == expected_display_count
    assert generation_payload["semantic_summary"]["framework"] == sample["framework"]
    matched_displays = [display for display in [sample["matched_display"], *sample.get("extra_matched_displays", [])] if display]
    assert generation_payload["semantic_summary"]["field_count"] >= int(sample.get("expected_min_fields", 0))
    assert generation_payload["semantic_summary"]["matched_data_displays"] == matched_displays
    assert generation_payload["semantic_summary"]["unmatched_data_displays"] == []
    if sample["field"]:
        assert f"text_from: input.{sample['field']}" in generation_payload["yaml"]
    if sample.get("input_field"):
        assert f"value_from: input.{sample['input_field']}" in generation_payload["yaml"]
    for reference in sample.get("extra_text_from", []):
        assert f"text_from: {reference}" in generation_payload["yaml"]
    for expected in sample.get("expected_yaml", []):
        assert expected in generation_payload["yaml"]

    verified = run_cli(
        "verify-impl",
        "--workspace-root",
        str(workspace),
        "--repo-root",
        str(repo),
        "--task-description",
        sample["task"],
        "--base-url",
        sample["base_url"],
        "--run-profile",
        "dry-run",
        "--min-quality-score",
        "0",
        "--no-untracked",
        cwd=repo,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr
    verify_payload = json_output(verified)

    assert verify_payload["result"] == "pass"
    if sample["field"]:
        assert verify_payload["inputs_source"] == "generated_template"
    assert verify_payload["quality"]["data_display_assertions"] == expected_display_count
    assert verify_payload["semantic_summary"]["framework"] == sample["framework"]
    assert verify_payload["semantic_summary"]["matched_data_displays"] == matched_displays
    assert Path(verify_payload["workflow_path"]).exists()
    if sample["field"] or sample.get("input_field"):
        assert Path(verify_payload["inputs_path"]).exists()
    else:
        assert verify_payload.get("inputs_path") is None
    assert Path(verify_payload["report_path"]).exists()


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
