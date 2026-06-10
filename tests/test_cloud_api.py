from __future__ import annotations

import json

from fastapi.testclient import TestClient

from cloud_api.auth import bearer_token, generate_api_key, verify_api_key
from cloud_api.main import create_app
from cloud_api.models import RunRequest, run_result_from_cloud_payload
from cloud_api.marketplace import catalog_path, load_catalog
from visual_agent.cloud import filter_remote_workflow_response
from visual_agent.visual_status import append_cloud_run_history, read_run_history
from visual_agent.workspace import init_workspace


def test_cloud_api_run_request_accepts_workflow_alias_and_normalizes_inputs() -> None:
    request = RunRequest.from_payload(
        {
            "workflow": "checkout",
            "run_profile": "supervised",
            "inputs": {"email": "demo@example.com"},
            "tags": ["ecommerce", 123],
        }
    )

    assert request.workflow_name == "checkout"
    assert request.run_profile == "supervised"
    assert request.inputs == {"email": "demo@example.com"}
    assert request.tags == ["ecommerce", "123"]


def test_cloud_api_result_accepts_api_run_id_shape() -> None:
    result = run_result_from_cloud_payload(
        {
            "id": "run-123",
            "status": "success",
            "workflow_name": "checkout",
            "steps_total": 4,
            "steps_passed": 4,
            "artifacts": [{"name": "screen.jpg", "kind": "screenshot", "url": "https://r2.test/screen.jpg"}],
        }
    )

    payload = result.to_dict()
    assert payload["id"] == "run-123"
    assert payload["passed"] is True
    assert payload["artifacts"][0]["kind"] == "screenshot"


def test_cloud_api_key_hash_round_trip_without_plaintext_storage() -> None:
    key = generate_api_key(salt="test-salt")

    assert key.token.startswith("va_cloud_")
    assert verify_api_key(key.token, expected_sha256=key.sha256, salt=key.salt) is True
    assert verify_api_key("wrong", expected_sha256=key.sha256, salt=key.salt) is False
    assert key.token not in key.sha256


def test_bearer_token_parses_authorization_header() -> None:
    assert bearer_token("Bearer va_cloud_secret") == "va_cloud_secret"
    assert bearer_token("Basic abc") == ""


def test_cloud_client_filter_accepts_api_runs_response_id() -> None:
    filtered = filter_remote_workflow_response(
        {
            "schema_version": 1,
            "id": "api-run-1",
            "status": "queued",
            "artifact_url": "/api/runs/api-run-1/artifacts",
            "message": "queued",
        }
    )

    assert filtered["status"] == "queued"
    assert filtered["run_id"] == "api-run-1"
    assert filtered["artifact_url"] == "/api/runs/api-run-1/artifacts"


def test_cloud_run_history_records_remote_result(tmp_path) -> None:
    append_cloud_run_history(
        tmp_path,
        {
            "run_id": "cloud-run-1",
            "workflow_name": "checkout",
            "workflow_source": "marketplace",
            "workflow_id": "wf_000123",
            "status": "success",
            "steps_total": 3,
            "steps_passed": 3,
            "report_url": "https://cloud.test/reports/cloud-run-1",
        },
    )

    records = read_run_history(tmp_path)
    assert records[0]["source"] == "cloud"
    assert records[0]["workflow_name"] == "checkout"
    assert records[0]["workflow_source"] == "marketplace"
    assert records[0]["workflow_id"] == "wf_000123"
    assert records[0]["passed"] is True
    assert records[0]["report_url"] == "https://cloud.test/reports/cloud-run-1"


def test_cloud_api_writes_redacted_audit_log(tmp_path) -> None:
    app = create_app(workspace_root=tmp_path / "workspace", audit_log=tmp_path / "audit" / "cloud_api.jsonl")
    assert app is not None

    audit_path = tmp_path / "audit" / "cloud_api.jsonl"
    assert audit_path.exists() is False

    client = TestClient(app)
    assert client.get("/api/health").status_code == 200
    token_response = client.post("/api/auth/token")
    assert token_response.status_code == 200
    token = token_response.json()["token"]

    text = audit_path.read_text(encoding="utf-8")
    events = [json.loads(line) for line in text.splitlines()]

    assert events[0]["path"] == "/api/health"
    assert events[1]["path"] == "/api/auth/token"
    assert token not in text


def test_cloud_api_marketplace_endpoints_list_search_and_publish(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow_yaml = """
schema_version: 1
min_runtime_version: "0.1.0"
name: public_profile
version: 1
description: Public profile save flow
tags: [verification, profile]
visibility: public
author: visual-agent-team
license: cc-by-4.0
steps:
  - id: observe
    action: observe_html
    path: fixtures/profile.html
  - id: wait_ready
    action: wait_for
    condition: text
    text: Ready
  - id: assert_ready
    action: assert_text
    text: Ready
  - id: assert_no_error
    action: assert_no_error
""".strip()
    workflow_path = workspace.workflows_dir / "public_profile.yaml"
    workflow_path.write_text(workflow_yaml, encoding="utf-8")
    app = create_app(workspace_root=workspace.root, audit_log=tmp_path / "audit" / "cloud_api.jsonl")
    client = TestClient(app)

    listed = client.get("/api/workflows")
    searched = client.get("/api/workflows/search", params={"q": "profile"})

    key = generate_api_key(salt="test-salt")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY_SHA256", key.sha256)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY_SALT", "test-salt")
    published = client.post(
        "/api/workflows/publish",
        headers={"Authorization": f"Bearer {key.token}"},
        json={"workflow_yaml": workflow_yaml, "min_quality_score": 0.6},
    )
    reloaded = TestClient(create_app(workspace_root=workspace.root, audit_log=tmp_path / "audit" / "cloud_api.jsonl"))
    detail = reloaded.get(f"/api/workflows/{published.json().get('id')}")
    listed_after_restart = reloaded.get("/api/workflows")

    assert listed.status_code == 200
    assert listed.json()["workflows"][0]["name"] == "public_profile"
    assert "workflow_yaml" not in listed.json()["workflows"][0]
    assert searched.status_code == 200
    assert searched.json()["workflows"][0]["name"] == "public_profile"
    assert searched.json()["workflows"][0]["score"] > 0
    assert published.status_code == 200
    assert published.json()["status"] == "published"
    assert published.json()["url"].endswith("/public_profile")
    assert detail.status_code == 200
    assert detail.json()["workflow"]["name"] == "public_profile"
    assert detail.json()["workflow"]["workflow_yaml"].strip().startswith("schema_version: 1")
    download = reloaded.get(f"/api/workflows/{published.json().get('id')}/download")
    assert download.status_code == 200
    assert download.json()["workflow_yaml"].strip().startswith("schema_version: 1")
    deleted = client.delete(
        f"/api/workflows/{published.json().get('id')}",
        headers={"Authorization": f"Bearer {key.token}"},
    )
    after_delete = reloaded.get("/api/workflows")
    assert listed_after_restart.status_code == 200
    assert listed_after_restart.json()["workflows"][0]["name"] == "public_profile"
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "deleted"
    assert after_delete.status_code == 200
    assert after_delete.json()["workflows"] == []


def test_cloud_api_marketplace_isolated_by_org(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow_yaml = """
schema_version: 1
min_runtime_version: "0.1.0"
name: team_shared_profile
version: 1
description: Team scoped profile save flow
tags: [verification, profile]
visibility: public
author: visual-agent-team
license: cc-by-4.0
steps:
  - id: observe
    action: observe_html
    path: fixtures/profile.html
  - id: wait_ready
    action: wait_for
    condition: text
    text: Ready
  - id: assert_ready
    action: assert_text
    text: Ready
  - id: assert_no_error
    action: assert_no_error
""".strip()
    app = create_app(workspace_root=workspace.root, audit_log=tmp_path / "audit" / "cloud_api.jsonl")
    client = TestClient(app)
    key = generate_api_key(salt="test-salt")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY_SHA256", key.sha256)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY_SALT", "test-salt")

    headers_a = {
        "Authorization": f"Bearer {key.token}",
        "X-Visual-Agent-Org": "team-a",
        "X-Visual-Agent-User": "alice",
    }
    headers_b = {
        "Authorization": f"Bearer {key.token}",
        "X-Visual-Agent-Org": "team-b",
        "X-Visual-Agent-User": "bob",
    }

    published_a = client.post(
        "/api/workflows/publish",
        headers=headers_a,
        json={"workflow_yaml": workflow_yaml, "min_quality_score": 0.6},
    )
    listed_a = client.get("/api/workflows", headers=headers_a)
    listed_b = client.get("/api/workflows", headers=headers_b)
    detail_a = client.get(f"/api/workflows/{published_a.json().get('id')}", headers=headers_a)
    detail_b = client.get(f"/api/workflows/{published_a.json().get('id')}", headers=headers_b)
    deleted_a = client.delete(f"/api/workflows/{published_a.json().get('id')}", headers=headers_a)
    listed_a_after_delete = client.get("/api/workflows", headers=headers_a)
    listed_b_after_delete = client.get("/api/workflows", headers=headers_b)

    assert published_a.status_code == 200
    assert published_a.json()["status"] == "published"
    assert published_a.json()["url"].endswith(f"/{published_a.json()['id']}")
    assert listed_a.status_code == 200
    assert listed_a.json()["workflows"][0]["name"] == "team_shared_profile"
    assert listed_a.json()["workflows"][0]["org"] == "team-a"
    assert listed_b.status_code == 200
    assert listed_b.json()["workflows"] == []
    assert detail_a.status_code == 200
    assert detail_a.json()["workflow"]["name"] == "team_shared_profile"
    assert detail_b.status_code == 404
    assert deleted_a.status_code == 200
    assert deleted_a.json()["status"] == "deleted"
    assert listed_a_after_delete.json()["workflows"] == []
    assert listed_b_after_delete.json()["workflows"] == []


def test_cloud_api_private_workflow_is_visible_within_org(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow_yaml = """
schema_version: 1
min_runtime_version: "0.1.0"
name: team_private_profile
version: 1
description: Team private profile flow
tags: [verification, private]
visibility: private
author: visual-agent-team
license: cc-by-4.0
steps:
  - id: observe
    action: observe_html
    path: fixtures/profile.html
  - id: assert_ready
    action: assert_text
    text: Ready
""".strip()
    app = create_app(workspace_root=workspace.root, audit_log=tmp_path / "audit" / "cloud_api.jsonl")
    client = TestClient(app)
    key = generate_api_key(salt="test-salt")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY_SHA256", key.sha256)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY_SALT", "test-salt")

    headers_a = {
        "Authorization": f"Bearer {key.token}",
        "X-Visual-Agent-Org": "team-a",
        "X-Visual-Agent-User": "alice",
    }
    headers_b = {
        "Authorization": f"Bearer {key.token}",
        "X-Visual-Agent-Org": "team-b",
        "X-Visual-Agent-User": "bob",
    }

    published = client.post(
        "/api/workflows/publish",
        headers=headers_a,
        json={"workflow_yaml": workflow_yaml, "visibility": "private", "min_quality_score": 0.6},
    )
    listed_a = client.get("/api/workflows", headers=headers_a)
    searched_a = client.get("/api/workflows/search", headers=headers_a, params={"q": "private"})
    listed_b = client.get("/api/workflows", headers=headers_b)

    assert published.status_code == 200
    assert published.json()["visibility"] == "private"
    assert published.json()["url"].endswith(f"/{published.json()['id']}")
    assert listed_a.status_code == 200
    assert listed_a.json()["workflows"][0]["name"] == "team_private_profile"
    assert listed_a.json()["workflows"][0]["visibility"] == "private"
    assert listed_a.json()["workflows"][0]["owner_user_id"] == "alice"
    assert searched_a.status_code == 200
    assert searched_a.json()["workflows"][0]["name"] == "team_private_profile"
    assert listed_b.status_code == 200
    assert listed_b.json()["workflows"] == []


def test_cloud_api_catalog_migrates_legacy_fields(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    path = catalog_path(workspace.root, org="team-a")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "public_workflows": [
                    {
                        "id": "wf_000001",
                        "name": "legacy_profile",
                        "visibility": "public",
                    }
                ],
                "withdrawn": ["old_profile"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    catalog = load_catalog(workspace.root, org="team-a")

    assert catalog["schema_version"] == 1
    assert catalog["org"] == "team-a"
    assert catalog["workflows"][0]["name"] == "legacy_profile"
    assert catalog["withdrawn_workflows"] == ["old_profile"]


def test_cloud_api_catalog_marks_future_schema_upgrade_required(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    path = catalog_path(workspace.root, org="team-a")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 999,
                "workflows": [],
                "withdrawn_workflows": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    catalog = load_catalog(workspace.root, org="team-a")

    assert catalog["status"] == "upgrade_required"
    assert catalog["reason"] == "unsupported_catalog_schema"
    assert "Run catalog-migrate" in catalog["migration_hint"]


def test_cloud_api_runs_are_scoped_by_org_and_user(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.fixtures_dir / "ready.html").write_text("<p>Ready</p>", encoding="utf-8")
    (workspace.workflows_dir / "ready.yaml").write_text(
        """
schema_version: 1
name: ready
version: 1
steps:
  - id: observe
    action: observe_html
    path: fixtures/ready.html
  - id: assert_ready
    action: assert_text
    text: Ready
""".strip(),
        encoding="utf-8",
    )
    app = create_app(workspace_root=workspace.root, audit_log=tmp_path / "audit" / "cloud_api.jsonl")
    client = TestClient(app)
    key = generate_api_key(salt="test-salt")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY_SHA256", key.sha256)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY_SALT", "test-salt")

    headers_a = {
        "Authorization": f"Bearer {key.token}",
        "X-Visual-Agent-Org": "team-a",
        "X-Visual-Agent-User": "alice",
    }
    headers_b = {
        "Authorization": f"Bearer {key.token}",
        "X-Visual-Agent-Org": "team-b",
        "X-Visual-Agent-User": "bob",
    }

    created_a = client.post(
        "/api/runs",
        headers=headers_a,
        json={"workflow_name": "ready", "workspace": str(workspace.root), "run_profile": "dry-run"},
    )
    run_id = created_a.json()["run_id"]
    same_scope = client.get(f"/api/runs/{run_id}", headers=headers_a)
    cross_scope = client.get(f"/api/runs/{run_id}", headers=headers_b)
    same_scope_artifacts = client.get(f"/api/runs/{run_id}/artifacts", headers=headers_a)
    cross_scope_artifacts = client.get(f"/api/runs/{run_id}/artifacts", headers=headers_b)

    assert created_a.status_code == 200
    assert created_a.json()["org"] == "team-a"
    assert created_a.json()["user_id"] == "alice"
    assert same_scope.status_code == 200
    assert same_scope.json()["org"] == "team-a"
    assert same_scope.json()["user_id"] == "alice"
    assert cross_scope.status_code == 404
    assert same_scope_artifacts.status_code == 200
    assert cross_scope_artifacts.status_code == 404


def test_cloud_api_run_response_preserves_workflow_source_and_id(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.fixtures_dir / "ready.html").write_text("<p>Ready</p>", encoding="utf-8")
    (workspace.workflows_dir / "ready.yaml").write_text(
        """
schema_version: 1
name: ready
version: 1
steps:
  - id: observe
    action: observe_html
    path: fixtures/ready.html
  - id: assert_ready
    action: assert_text
    text: Ready
""".strip(),
        encoding="utf-8",
    )
    app = create_app(workspace_root=workspace.root, audit_log=tmp_path / "audit" / "cloud_api.jsonl")
    client = TestClient(app)
    key = generate_api_key(salt="test-salt")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY_SHA256", key.sha256)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY_SALT", "test-salt")

    headers = {
        "Authorization": f"Bearer {key.token}",
        "X-Visual-Agent-Org": "team-a",
        "X-Visual-Agent-User": "alice",
    }
    created = client.post(
        "/api/runs",
        headers=headers,
        json={
            "workflow_name": "ready",
            "workflow_source": "marketplace",
            "workflow_id": "wf_000123",
            "workspace": str(workspace.root),
            "run_profile": "dry-run",
        },
    )
    run_id = created.json()["run_id"]
    fetched = client.get(f"/api/runs/{run_id}", headers=headers)

    assert created.status_code == 200
    assert created.json()["workflow_source"] == "marketplace"
    assert created.json()["workflow_id"] == "wf_000123"
    assert fetched.status_code == 200
    assert fetched.json()["workflow_source"] == "marketplace"
    assert fetched.json()["workflow_id"] == "wf_000123"
