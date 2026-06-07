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
    if sample["field"]:
        assert Path(verify_payload["inputs_path"]).exists()
    else:
        assert verify_payload.get("inputs_path") is None
    assert Path(verify_payload["report_path"]).exists()


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
