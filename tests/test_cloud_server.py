from __future__ import annotations

import json
import os
from threading import Thread
from time import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from visual_agent.cloud import build_http_cloud_transport
from visual_agent.cloud_server import (
    CloudServerConfigError,
    CloudRequestError,
    create_cloud_server,
    resolve_request_run_profile,
    resolve_request_workspace,
)
from visual_agent.workspace import discover_workflows
from visual_agent.workspace import init_workspace


def test_cloud_server_health_endpoint(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    server = create_cloud_server(workspace_root=workspace.root, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1/health"
        with urlopen(url, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert payload["status"] == "ok"
    assert payload["workspace"] == str(workspace.root)


def test_cloud_server_run_endpoint_executes_workspace_workflow(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
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
    server = create_cloud_server(workspace_root=workspace.root, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/run"
        body = json.dumps({"workflow_name": "ready", "workspace": str(workspace.root), "run_profile": "dry-run"}).encode("utf-8")
        request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        with urlopen(f"http://127.0.0.1:{server.server_port}/v1/run/{payload['run_id']}", timeout=5) as response:
            run_payload = json.loads(response.read().decode("utf-8"))
        with urlopen(f"http://127.0.0.1:{server.server_port}/v1/run/{payload['run_id']}/report?format=json", timeout=5) as response:
            report_json = json.loads(response.read().decode("utf-8"))
            report_json_type = response.headers.get("Content-Type")
        with urlopen(f"http://127.0.0.1:{server.server_port}/v1/run/{payload['run_id']}/report?format=markdown", timeout=5) as response:
            report_markdown = response.read().decode("utf-8")
            report_markdown_type = response.headers.get("Content-Type")
        with urlopen(f"http://127.0.0.1:{server.server_port}/v1/runs?limit=5", timeout=5) as response:
            runs_payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert payload["status"] == "success"
    assert payload["workflow_name"] == "ready"
    assert payload["workflow_source"] == "workspace"
    assert payload["workflow_id"] == ""
    assert payload["steps_passed"] == 2
    assert payload["steps_total"] == 2
    assert run_payload["run_id"] == payload["run_id"]
    assert run_payload["report"]["summary"]["total_steps"] == 2
    assert report_json["run_id"] == payload["run_id"]
    assert "application/json" in report_json_type
    assert "Run Report" in report_markdown
    assert "text/markdown" in report_markdown_type
    assert runs_payload["status"] == "success"
    assert runs_payload["returned_reports"] == 1
    assert runs_payload["reports"][0]["run_id"] == payload["run_id"]


def test_cloud_server_run_endpoint_preserves_source_fields(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
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
    server = create_cloud_server(workspace_root=workspace.root, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/run"
        body = json.dumps(
            {
                "workflow_name": "ready",
                "workflow_source": "marketplace",
                "workflow_id": "wf_000123",
                "workspace": str(workspace.root),
                "run_profile": "dry-run",
            }
        ).encode("utf-8")
        request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        with urlopen(f"http://127.0.0.1:{server.server_port}/v1/run/{payload['run_id']}", timeout=5) as response:
            run_payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert payload["workflow_source"] == "marketplace"
    assert payload["workflow_id"] == "wf_000123"
    assert run_payload["workflow_source"] == "marketplace"
    assert run_payload["workflow_id"] == "wf_000123"


def test_cloud_server_inline_workflow_is_cleaned_up(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    (workspace.fixtures_dir / "ready.html").write_text("<p>Ready</p>", encoding="utf-8")
    workflow_yaml = """
schema_version: 1
name: inline_ready
version: 1
steps:
  - id: observe
    action: observe_html
    path: fixtures/ready.html
  - id: assert_ready
    action: assert_text
    text: Ready
""".strip()
    server = create_cloud_server(workspace_root=workspace.root, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/run"
        body = json.dumps({"workflow_yaml": workflow_yaml, "workspace": str(workspace.root), "run_profile": "dry-run"}).encode("utf-8")
        request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert payload["status"] == "success"
    assert payload["workflow_source"] == "inline"
    assert not list(workspace.workflows_dir.glob("cloud_request_*.yaml"))
    assert not list((workspace.workflows_dir / ".cloud_inline").glob("cloud_request_*.yaml"))
    assert all(".cloud_inline" not in ref.relative_path for ref in discover_workflows(workspace, include_slow=True))


def test_cloud_server_report_detail_respects_history_gate(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("VISUAL_AGENT_LICENSE_TIER", raising=False)
    monkeypatch.delenv("VISUAL_AGENT_LICENSE_FILE", raising=False)
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
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
    server = create_cloud_server(workspace_root=workspace.root, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/run"
        body = json.dumps({"workflow_name": "ready", "workspace": str(workspace.root), "run_profile": "dry-run"}).encode("utf-8")
        request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        old_timestamp = time() - 8 * 86400
        for suffix in (".json", ".md"):
            os.utime(workspace.reports_dir / f"{payload['run_id']}{suffix}", (old_timestamp, old_timestamp))
        try:
            urlopen(f"http://127.0.0.1:{server.server_port}/v1/run/{payload['run_id']}", timeout=5)
        except HTTPError as exc:
            blocked = json.loads(exc.read().decode("utf-8"))
            status_code = exc.code
        try:
            urlopen(f"http://127.0.0.1:{server.server_port}/v1/run/{payload['run_id']}/report?format=json", timeout=5)
        except HTTPError as exc:
            blocked_report = json.loads(exc.read().decode("utf-8"))
            report_status_code = exc.code
        with urlopen(f"http://127.0.0.1:{server.server_port}/v1/runs", timeout=5) as response:
            runs_payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert status_code == 403
    assert blocked["status"] == "upgrade_required"
    assert blocked["history_access"]["reason"] == "history_window_exceeded"
    assert report_status_code == 403
    assert blocked_report["status"] == "upgrade_required"
    assert blocked_report["history_access"]["reason"] == "history_window_exceeded"
    assert runs_payload["returned_reports"] == 0


def test_cloud_server_report_download_rejects_bad_format_and_path(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    server = create_cloud_server(workspace_root=workspace.root, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            urlopen(f"http://127.0.0.1:{server.server_port}/v1/run/missing/report?format=xml", timeout=5)
        except HTTPError as exc:
            bad_format = json.loads(exc.read().decode("utf-8"))
            bad_format_code = exc.code
        try:
            urlopen(f"http://127.0.0.1:{server.server_port}/v1/run/../report?format=json", timeout=5)
        except HTTPError as exc:
            bad_path = json.loads(exc.read().decode("utf-8"))
            bad_path_code = exc.code
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert bad_format_code == 400
    assert bad_format["reason"] == "unsupported_format"
    assert bad_path_code in {400, 404}
    assert bad_path["status"] in {"not_found", "failed"}


def test_cloud_server_auth_requires_bearer_token_and_org(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
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
    server = create_cloud_server(workspace_root=workspace.root, port=0, api_key="server-secret", required_org="team-a")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    unauthorized: dict = {}
    unauthorized_code = 0
    forbidden: dict = {}
    forbidden_code = 0
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/run"
        body = json.dumps({"workflow_name": "ready", "workspace": str(workspace.root), "run_profile": "dry-run"}).encode("utf-8")
        request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            urlopen(request, timeout=10)
        except HTTPError as exc:
            unauthorized = json.loads(exc.read().decode("utf-8"))
            unauthorized_code = exc.code

        wrong_org_request = Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer server-secret",
                "X-Visual-Agent-Org": "team-b",
            },
            method="POST",
        )
        try:
            urlopen(wrong_org_request, timeout=10)
        except HTTPError as exc:
            forbidden = json.loads(exc.read().decode("utf-8"))
            forbidden_code = exc.code

        authorized_request = Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer server-secret",
                "X-Visual-Agent-Org": "team-a",
            },
            method="POST",
        )
        with urlopen(authorized_request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        runs_request = Request(
            f"http://127.0.0.1:{server.server_port}/v1/runs",
            headers={"Authorization": "Bearer server-secret", "X-Visual-Agent-Org": "team-a"},
        )
        with urlopen(runs_request, timeout=5) as response:
            runs_payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert unauthorized_code == 401
    assert unauthorized["status"] == "unauthorized"
    assert "server-secret" not in json.dumps(unauthorized)
    assert forbidden_code == 403
    assert forbidden["reason"] == "org_forbidden"
    assert payload["status"] == "success"
    assert runs_payload["returned_reports"] == 1


def test_cloud_server_audit_log_records_redacted_request_events(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
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
    audit_log = tmp_path / "audit" / "cloud_server.jsonl"
    server = create_cloud_server(
        workspace_root=workspace.root,
        port=0,
        api_key="server-secret",
        required_org="team-a",
        audit_log=audit_log,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/run"
        body = json.dumps({"workflow_name": "ready", "workspace": str(workspace.root), "run_profile": "dry-run"}).encode("utf-8")
        unauthorized_request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            urlopen(unauthorized_request, timeout=10)
        except HTTPError as exc:
            assert exc.code == 401
            exc.read()

        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer server-secret",
            "X-Visual-Agent-Org": "team-a",
        }
        authorized_request = Request(endpoint, data=body, headers=headers, method="POST")
        with urlopen(authorized_request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        list_request = Request(
            f"http://127.0.0.1:{server.server_port}/v1/runs?limit=5",
            headers={"Authorization": "Bearer server-secret", "X-Visual-Agent-Org": "team-a"},
        )
        with urlopen(list_request, timeout=5):
            pass
        report_request = Request(
            f"http://127.0.0.1:{server.server_port}/v1/run/{payload['run_id']}/report?format=json",
            headers={"Authorization": "Bearer server-secret", "X-Visual-Agent-Org": "team-a"},
        )
        with urlopen(report_request, timeout=5):
            pass
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    raw_audit = audit_log.read_text(encoding="utf-8")
    events = [json.loads(line) for line in raw_audit.splitlines()]
    endpoints = {event["endpoint"] for event in events}
    assert {"auth", "run_create", "run_list", "report_download"}.issubset(endpoints)
    assert "server-secret" not in raw_audit
    assert any(event["endpoint"] == "auth" and event["http_status"] == 401 for event in events)
    create_event = next(event for event in events if event["endpoint"] == "run_create")
    assert create_event["status"] == "success"
    assert create_event["run_id"] == payload["run_id"]
    assert create_event["workflow_name"] == "ready"
    assert create_event["org"] == "team-a"
    assert create_event["workspace_provided"] is True
    assert isinstance(create_event["duration_ms"], float)


def test_cloud_server_retention_max_reports_prunes_old_report_pairs(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
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
    server = create_cloud_server(workspace_root=workspace.root, port=0, retention_max_reports=2)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    payloads: list[dict] = []
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/run"
        for _ in range(3):
            body = json.dumps({"workflow_name": "ready", "workspace": str(workspace.root), "run_profile": "dry-run"}).encode("utf-8")
            request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=10) as response:
                payloads.append(json.loads(response.read().decode("utf-8")))
        with urlopen(f"http://127.0.0.1:{server.server_port}/v1/runs?limit=5", timeout=5) as response:
            runs_payload = json.loads(response.read().decode("utf-8"))
        try:
            urlopen(f"http://127.0.0.1:{server.server_port}/v1/run/{payloads[0]['run_id']}/report?format=json", timeout=5)
        except HTTPError as exc:
            pruned_code = exc.code
            pruned_payload = json.loads(exc.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert payloads[-1]["retention"]["policy"]["max_reports"] == 2
    assert payloads[-1]["retention"]["deleted_reports"] == 1
    assert payloads[0]["run_id"] in payloads[-1]["retention"]["run_ids"]
    assert runs_payload["total_reports"] == 2
    assert runs_payload["returned_reports"] == 2
    assert {report["run_id"] for report in runs_payload["reports"]} == {payloads[1]["run_id"], payloads[2]["run_id"]}
    assert not (workspace.reports_dir / f"{payloads[0]['run_id']}.json").exists()
    assert not (workspace.reports_dir / f"{payloads[0]['run_id']}.md").exists()
    assert (workspace.reports_dir / "index.json").exists()
    assert pruned_code == 404
    assert pruned_payload["status"] == "not_found"


def test_cloud_server_retention_days_prunes_old_reports_only(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
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
    server = create_cloud_server(workspace_root=workspace.root, port=0, retention_days=1)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/run"
        body = json.dumps({"workflow_name": "ready", "workspace": str(workspace.root), "run_profile": "dry-run"}).encode("utf-8")
        request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=10) as response:
            old_payload = json.loads(response.read().decode("utf-8"))
        old_timestamp = time() - 2 * 86400
        for suffix in (".json", ".md"):
            os.utime(workspace.reports_dir / f"{old_payload['run_id']}{suffix}", (old_timestamp, old_timestamp))
        request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=10) as response:
            new_payload = json.loads(response.read().decode("utf-8"))
        with urlopen(f"http://127.0.0.1:{server.server_port}/v1/runs?limit=5", timeout=5) as response:
            runs_payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert new_payload["retention"]["policy"]["days"] == 1
    assert new_payload["retention"]["reasons"][old_payload["run_id"]] == "older_than_retention_days"
    assert not (workspace.reports_dir / f"{old_payload['run_id']}.json").exists()
    assert (workspace.reports_dir / f"{new_payload['run_id']}.json").exists()
    assert runs_payload["total_reports"] == 1
    assert runs_payload["reports"][0]["run_id"] == new_payload["run_id"]


def test_cloud_server_runs_endpoint_supports_pagination_and_filters(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
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
    (workspace.workflows_dir / "failing.yaml").write_text(
        """
schema_version: 1
name: failing
version: 1
steps:
  - id: observe
    action: observe_html
    path: fixtures/ready.html
  - id: assert_missing
    action: assert_text
    text: Missing
""".strip(),
        encoding="utf-8",
    )
    server = create_cloud_server(workspace_root=workspace.root, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/run"
        for workflow in ("ready", "failing", "ready"):
            body = json.dumps({"workflow_name": workflow, "workspace": str(workspace.root), "run_profile": "dry-run"}).encode("utf-8")
            request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=10):
                pass
        with urlopen(f"http://127.0.0.1:{server.server_port}/v1/runs?limit=2&offset=0", timeout=5) as response:
            page1 = json.loads(response.read().decode("utf-8"))
        with urlopen(f"http://127.0.0.1:{server.server_port}/v1/runs?limit=2&offset=2", timeout=5) as response:
            page2 = json.loads(response.read().decode("utf-8"))
        with urlopen(f"http://127.0.0.1:{server.server_port}/v1/runs?failed_only=true", timeout=5) as response:
            failed = json.loads(response.read().decode("utf-8"))
        with urlopen(f"http://127.0.0.1:{server.server_port}/v1/runs?workflow=ready&status=success", timeout=5) as response:
            ready_success = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert page1["total_reports"] == 3
    assert page1["returned_reports"] == 2
    assert page1["has_more"] is True
    assert page1["next_offset"] == 2
    assert page2["offset"] == 2
    assert page2["returned_reports"] == 1
    assert page2["has_more"] is False
    assert failed["filters"]["failed_only"] is True
    assert failed["total_reports"] == 1
    assert failed["reports"][0]["workflow_name"] == "failing"
    assert ready_success["filters"]["workflow"] == "ready"
    assert ready_success["filters"]["status"] == "success"
    assert ready_success["total_reports"] == 2


def test_http_cloud_transport_can_call_local_cloud_server(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
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
    server = create_cloud_server(workspace_root=workspace.root, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        transport = build_http_cloud_transport(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1/run",
            api_key="local-test-key",
            timeout_seconds=10,
        )
        payload = transport({"workflow_name": "ready", "workspace": str(workspace.root), "run_profile": "dry-run"})
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert payload["status"] == "success"
    assert payload["run_id"]
    assert payload["report_url"].startswith("/v1/run/")


def test_resolve_request_workspace_rejects_foreign_path(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    server = create_cloud_server(workspace_root=workspace.root, port=0)
    try:
        # Matching path (resolved) is accepted.
        opened = resolve_request_workspace(server, {"workspace": str(workspace.root)})
        assert opened.root == server.workspace_root
        # An arbitrary client path is rejected rather than opened.
        foreign = tmp_path / "elsewhere"
        foreign.mkdir()
        with pytest.raises(CloudRequestError) as excinfo:
            resolve_request_workspace(server, {"workspace": str(foreign)})
        assert excinfo.value.reason == "workspace_forbidden"
    finally:
        server.server_close()


def test_resolve_request_run_profile_clamps_to_server_default(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    server = create_cloud_server(workspace_root=workspace.root, port=0, run_profile="dry-run")
    try:
        assert resolve_request_run_profile(server, {"run_profile": "dry-run"}) == "dry-run"
        # A more privileged profile than the server default is rejected.
        with pytest.raises(CloudRequestError) as excinfo:
            resolve_request_run_profile(server, {"run_profile": "approved"})
        assert excinfo.value.reason == "run_profile_forbidden"
        # An unknown profile string is rejected instead of flowing into execution.
        with pytest.raises(CloudRequestError) as unknown:
            resolve_request_run_profile(server, {"run_profile": "root"})
        assert unknown.value.reason == "invalid_run_profile"
    finally:
        server.server_close()


def test_cloud_server_rejects_non_loopback_without_auth(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    with pytest.raises(CloudServerConfigError):
        create_cloud_server(workspace_root=workspace.root, host="0.0.0.0", port=0)


def test_cloud_server_allows_loopback_without_auth(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    server = create_cloud_server(workspace_root=workspace.root, host="127.0.0.1", port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()
