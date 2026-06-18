from __future__ import annotations

from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

from visual_agent.cloud import (
    build_http_cloud_transport,
    build_remote_workflow_request,
    cloud_run_quota_status,
    cloud_config_status,
    execute_remote_workflow_plan,
    filter_remote_workflow_response,
    remote_client_from_env,
    run_remote_workflow,
)
from visual_agent.licensing import FeatureGatedError, check_feature, get_license, monthly_feature_limit, report_history_window_days, require_feature
from visual_agent.session import load_agent_session, record_cloud_run_usage


def test_get_license_returns_free_tier(monkeypatch) -> None:
    clear_license_env(monkeypatch)

    license_ = get_license()

    assert license_.tier == "free"
    assert license_.seats == 1
    assert license_.source == "default"


def test_check_feature_reports_free_and_paid_boundaries(monkeypatch) -> None:
    clear_license_env(monkeypatch)

    assert check_feature("local_run") is True
    assert check_feature("mcp_server") is True
    assert check_feature("generate_workflow") is True
    assert check_feature("cloud_run") is True
    assert check_feature("team_workspace") is False


def test_require_feature_allows_limited_cloud_run_on_free_tier(monkeypatch) -> None:
    clear_license_env(monkeypatch)

    require_feature("cloud_run")
    assert monthly_feature_limit("cloud_run") == 50


def test_require_feature_allows_cloud_run_on_pro_tier(monkeypatch) -> None:
    clear_license_env(monkeypatch)
    grant_pro_license(monkeypatch)

    require_feature("cloud_run")


def test_get_license_reads_env_tier(monkeypatch) -> None:
    clear_license_env(monkeypatch)
    monkeypatch.setenv("VISUAL_AGENT_LICENSE_TIER", "team")
    monkeypatch.setenv("VISUAL_AGENT_LICENSE_SEATS", "3")
    monkeypatch.setenv("VISUAL_AGENT_LICENSE_KEY", "va_test_secret")

    license_ = get_license()

    assert license_.tier == "team"
    assert license_.seats == 3
    assert license_.source == "env"
    assert license_.key_present is True
    assert check_feature("cloud_run") is True
    assert check_feature("team_workspace") is True


def test_get_license_prefers_checkpoint_env_tier(monkeypatch) -> None:
    clear_license_env(monkeypatch)
    monkeypatch.setenv("VISUAL_AGENT_LICENSE_TIER", "pro")
    monkeypatch.setenv("VISUAL_AGENT_LICENSE_SEATS", "2")
    monkeypatch.setenv("CHECKPOINT_LICENSE_TIER", "team")
    monkeypatch.setenv("CHECKPOINT_LICENSE_SEATS", "4")
    monkeypatch.setenv("CHECKPOINT_LICENSE_KEY", "checkpoint_test_secret")

    license_ = get_license()

    assert license_.tier == "team"
    assert license_.seats == 4
    assert license_.source == "env"
    assert license_.key_present is True


def test_get_license_reads_local_json_file(tmp_path: Path, monkeypatch) -> None:
    clear_license_env(monkeypatch)
    license_file = tmp_path / "license.json"
    license_file.write_text(
        '{"tier": "pro", "seats": 2, "expires_at": 4102444800, "license_key": "va_test_secret"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("VISUAL_AGENT_LICENSE_FILE", str(license_file))

    license_ = get_license()

    assert license_.tier == "pro"
    assert license_.seats == 2
    assert license_.source == str(license_file)
    assert license_.key_present is True
    assert check_feature("cloud_run") is True
    assert check_feature("team_workspace") is False
    assert check_feature("workflow_history_unlimited") is True
    assert report_history_window_days() is None


def test_expired_license_downgrades_feature_checks(tmp_path: Path, monkeypatch) -> None:
    clear_license_env(monkeypatch)
    license_file = tmp_path / "license.json"
    license_file.write_text('{"tier": "pro", "expires_at": 1}', encoding="utf-8")
    monkeypatch.setenv("VISUAL_AGENT_LICENSE_FILE", str(license_file))

    assert get_license().tier == "pro"
    assert check_feature("local_run") is True
    assert check_feature("cloud_run") is True
    assert monthly_feature_limit("cloud_run") == 50
    assert report_history_window_days() == 7


def test_feature_gated_error_message_is_available_for_future_gates() -> None:
    error = FeatureGatedError("cloud_run", required_tier="pro", current_tier="free")

    assert error.feature == "cloud_run"
    assert error.required_tier == "pro"
    assert error.current_tier == "free"
    assert "pro plan" in str(error)


def test_cloud_run_free_tier_records_usage_until_quota(tmp_path: Path, monkeypatch) -> None:
    clear_license_env(monkeypatch)
    calls = 0

    def fake_client(_workflow_name: str, _workspace_root: Path) -> dict:
        nonlocal calls
        calls += 1
        return {"status": "success", "run_id": "cloud-run-1"}

    result = run_remote_workflow("checkout", tmp_path, client=fake_client)
    session = load_agent_session(tmp_path)

    assert result["status"] == "success"
    assert result["usage_recorded"] is True
    assert calls == 1
    assert session is not None
    assert session.cloud_runs_used == 1
    assert cloud_run_quota_status(tmp_path)["remaining"] == 49


def test_cloud_run_returns_upgrade_required_when_free_quota_exceeded(tmp_path: Path, monkeypatch) -> None:
    clear_license_env(monkeypatch)
    record_cloud_run_usage(tmp_path, count=50)
    calls = 0

    def fake_client(_workflow_name: str, _workspace_root: Path) -> dict:
        nonlocal calls
        calls += 1
        return {"status": "success", "run_id": "cloud-run-1"}

    result = run_remote_workflow("checkout", tmp_path, client=fake_client)

    assert result["status"] == "upgrade_required"
    assert result["reason"] == "quota_exceeded"
    assert result["feature"] == "cloud_run"
    assert result["required_tier"] == "pro"
    assert result["current_tier"] == "free"
    assert result["quota"]["used"] == 50
    assert result["quota"]["remaining"] == 0
    assert result["usage_recorded"] is False
    assert calls == 0
    assert load_agent_session(tmp_path).cloud_runs_used == 50


def test_cloud_run_placeholder_still_raises_without_client_on_paid_tier(tmp_path: Path, monkeypatch) -> None:
    grant_pro_license(monkeypatch)

    with pytest.raises(NotImplementedError, match="Cloud runs are not yet available"):
        run_remote_workflow("checkout", tmp_path)

    assert load_agent_session(tmp_path) is None


def test_cloud_run_records_usage_only_after_success(tmp_path: Path, monkeypatch) -> None:
    grant_pro_license(monkeypatch)

    def fake_client(workflow_name: str, workspace_root: Path) -> dict:
        return {"status": "success", "run_id": "cloud-run-1", "workflow_name": workflow_name, "workspace": str(workspace_root)}

    result = run_remote_workflow("checkout", tmp_path, client=fake_client)
    session = load_agent_session(tmp_path)

    assert result["status"] == "success"
    assert result["usage_recorded"] is True
    assert session is not None
    assert session.runs_this_month == 0
    assert session.cloud_runs_used == 1


def test_cloud_run_failure_does_not_record_usage(tmp_path: Path, monkeypatch) -> None:
    grant_pro_license(monkeypatch)

    def fake_client(_workflow_name: str, _workspace_root: Path) -> dict:
        return {"status": "failed", "message": "remote unavailable"}

    result = run_remote_workflow("checkout", tmp_path, client=fake_client)

    assert result["status"] == "failed"
    assert result["usage_recorded"] is False
    assert load_agent_session(tmp_path) is None


def test_cloud_run_client_exception_does_not_record_usage(tmp_path: Path, monkeypatch) -> None:
    grant_pro_license(monkeypatch)

    def fake_client(_workflow_name: str, _workspace_root: Path) -> dict:
        raise RuntimeError("remote outage")

    with pytest.raises(RuntimeError, match="remote outage"):
        run_remote_workflow("checkout", tmp_path, client=fake_client)

    assert load_agent_session(tmp_path) is None


def test_cloud_config_status_reports_missing_config(monkeypatch) -> None:
    clear_license_env(monkeypatch)
    clear_cloud_env(monkeypatch)

    status = cloud_config_status()

    assert status["available"] is False
    assert status["endpoint"] == ""
    assert status["api_key_present"] is False
    assert status["blockers"] == ["missing_endpoint", "missing_api_key"]
    assert status["network_probe"] == "not_run"


def test_cloud_config_status_reports_ready_without_exposing_key(monkeypatch) -> None:
    clear_cloud_env(monkeypatch)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ORG", "team-a")

    status = cloud_config_status()

    assert status["available"] is True
    assert status["endpoint"] == "https://cloud.visualagent.test"
    assert status["api_key_present"] is True
    assert status["org"] == "team-a"
    assert status["blockers"] == []
    assert "va_cloud_secret" not in str(status)


def test_cloud_config_status_prefers_checkpoint_env(monkeypatch) -> None:
    clear_cloud_env(monkeypatch)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://legacy.visualagent.test")
    monkeypatch.setenv("CHECKPOINT_CLOUD_ENDPOINT", "https://checkpoint.visualagent.test")
    monkeypatch.setenv("CHECKPOINT_CLOUD_API_KEY", "checkpoint_secret")
    monkeypatch.setenv("CHECKPOINT_CLOUD_ORG", "team-checkpoint")

    status = cloud_config_status()

    assert status["available"] is True
    assert status["endpoint"] == "https://checkpoint.visualagent.test"
    assert status["api_key_present"] is True
    assert status["org"] == "team-checkpoint"
    assert "checkpoint_secret" not in str(status)


def test_remote_workflow_request_blocks_when_cloud_config_missing(tmp_path: Path, monkeypatch) -> None:
    clear_cloud_env(monkeypatch)

    request = build_remote_workflow_request("checkout", tmp_path, inputs={"username": "demo_user"})

    assert request["status"] == "blocked"
    assert request["cloud_config"]["blockers"] == ["missing_endpoint", "missing_api_key"]
    assert request["inputs"]["provided"] is True
    assert request["inputs"]["fields"] == ["username"]
    assert request["network_probe"] == "not_run"


def test_remote_workflow_request_redacts_inputs_and_cloud_key(tmp_path: Path, monkeypatch) -> None:
    clear_cloud_env(monkeypatch)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret")
    inputs = {
        "username": "demo_user",
        "password": "demo_password",
        "nested": {"api_key": "sk-secret-value"},
    }

    request = build_remote_workflow_request(
        "checkout",
        tmp_path,
        run_profile="approved",
        inputs=inputs,
        inputs_file="inputs/checkout.json",
    )

    raw = str(request)
    assert request["status"] == "ready"
    assert request["run_profile"] == "approved"
    assert request["inputs_file"] == "inputs/checkout.json"
    assert request["inputs"]["field_count"] == 3
    assert request["inputs"]["redacted"]["password"] == {"redacted": True}
    assert request["inputs"]["redacted"]["nested"]["api_key"] == {"redacted": True}
    assert "demo_password" not in raw
    assert "sk-secret-value" not in raw
    assert "va_cloud_secret" not in raw


def test_remote_response_filter_keeps_compact_fields_and_redacts_message() -> None:
    response = filter_remote_workflow_response(
        {
            "schema_version": "2026-06",
            "status": "success",
            "run_id": "run-123",
            "report_url": "https://cloud.visualagent.test/reports/run-123",
            "message": "done api_key=sk-secret-value",
            "huge_payload": "x" * 10000,
        }
    )

    assert response == {
        "schema_version": 1,
        "remote_schema_version": "2026-06",
        "status": "success",
        "run_id": "run-123",
        "report_url": "https://cloud.visualagent.test/reports/run-123",
        "message": "done api_key=[REDACTED]",
    }


def test_remote_client_missing_config_returns_blocked_without_transport(tmp_path: Path, monkeypatch) -> None:
    clear_cloud_env(monkeypatch)
    calls: list[dict] = []

    client = remote_client_from_env(transport=lambda request: calls.append(request) or {"status": "success"})
    result = client("checkout", tmp_path)

    assert result["status"] == "blocked"
    assert result["request"]["status"] == "blocked"
    assert calls == []


def test_remote_client_without_transport_returns_blocked(tmp_path: Path, monkeypatch) -> None:
    clear_cloud_env(monkeypatch)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret")

    result = remote_client_from_env()("checkout", tmp_path)

    assert result["status"] == "blocked"
    assert result["request"]["status"] == "ready"
    assert "transport is not enabled" in result["message"]


def test_remote_client_with_transport_filters_response_and_records_usage(tmp_path: Path, monkeypatch) -> None:
    clear_cloud_env(monkeypatch)
    grant_pro_license(monkeypatch)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret")
    seen_requests: list[dict] = []

    def fake_transport(request: dict) -> dict:
        seen_requests.append(request)
        return {
            "status": "success",
            "run_id": "cloud-run-1",
            "report_url": "https://cloud.visualagent.test/reports/cloud-run-1",
            "message": "ok password=demo_password",
            "ignored": {"large": "x" * 1000},
        }

    client = remote_client_from_env(transport=fake_transport, inputs={"password": "demo_password"})
    result = run_remote_workflow("checkout", tmp_path, client=client)
    session = load_agent_session(tmp_path)

    raw = str(result)
    assert result["status"] == "success"
    assert result["usage_recorded"] is True
    assert result["run_id"] == "cloud-run-1"
    assert "ignored" not in result
    assert "demo_password" not in raw
    assert "va_cloud_secret" not in raw
    assert seen_requests[0]["inputs"]["redacted"]["password"] == {"redacted": True}
    assert session is not None
    assert session.cloud_runs_used == 1


@pytest.mark.parametrize("status", ["queued", "running", "blocked", "failed", "unknown"])
def test_remote_terminal_and_pending_statuses_do_not_record_usage(tmp_path: Path, monkeypatch, status: str) -> None:
    clear_cloud_env(monkeypatch)
    grant_pro_license(monkeypatch)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret")

    def fake_transport(_request: dict) -> dict:
        return {
            "schema_version": "2026-06",
            "status": status,
            "run_id": "cloud-run-pending",
            "report_url": "https://cloud.visualagent.test/reports/cloud-run-pending",
            "message": "remote status",
        }

    result = run_remote_workflow("checkout", tmp_path, client=remote_client_from_env(transport=fake_transport))

    assert result["status"] == status
    assert result["remote_schema_version"] == "2026-06"
    assert result["usage_recorded"] is False
    assert load_agent_session(tmp_path) is None


def test_remote_unknown_status_is_normalized_and_does_not_record_usage(tmp_path: Path, monkeypatch) -> None:
    clear_cloud_env(monkeypatch)
    grant_pro_license(monkeypatch)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret")

    def fake_transport(_request: dict) -> dict:
        return {"status": "done", "run_id": "cloud-run-unknown", "message": "unexpected"}

    result = run_remote_workflow("checkout", tmp_path, client=remote_client_from_env(transport=fake_transport))

    assert result["status"] == "unknown"
    assert result["usage_recorded"] is False
    assert load_agent_session(tmp_path) is None


def test_execute_remote_workflow_plan_does_not_call_transport_without_execute(tmp_path: Path, monkeypatch) -> None:
    clear_cloud_env(monkeypatch)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret")
    calls: list[dict] = []

    payload = execute_remote_workflow_plan(
        "checkout",
        tmp_path,
        execute=False,
        transport=lambda request: calls.append(request) or {"status": "success"},
        inputs={"password": "demo_password"},
    )

    raw = str(payload)
    assert payload["execution_requested"] is False
    assert payload["network_sent"] is False
    assert payload["request"]["status"] == "ready"
    assert payload["adapter_diagnostic"]["status"] == "blocked"
    assert calls == []
    assert load_agent_session(tmp_path) is None
    assert "demo_password" not in raw
    assert "va_cloud_secret" not in raw


def test_execute_remote_workflow_plan_with_transport_records_success_usage(tmp_path: Path, monkeypatch) -> None:
    clear_cloud_env(monkeypatch)
    grant_pro_license(monkeypatch)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret")
    calls: list[dict] = []

    def fake_transport(request: dict) -> dict:
        calls.append(request)
        return {
            "status": "success",
            "run_id": "cloud-run-2",
            "message": "ok password=demo_password",
        }

    payload = execute_remote_workflow_plan(
        "checkout",
        tmp_path,
        execute=True,
        transport=fake_transport,
        inputs={"password": "demo_password"},
    )
    session = load_agent_session(tmp_path)
    raw = str(payload)

    assert payload["execution_requested"] is True
    assert payload["network_sent"] is True
    assert payload["result"]["status"] == "success"
    assert payload["result"]["run_id"] == "cloud-run-2"
    assert payload["result"]["usage_recorded"] is True
    assert len(calls) == 1
    assert session is not None
    assert session.cloud_runs_used == 1
    assert "demo_password" not in raw
    assert "va_cloud_secret" not in raw


def test_execute_remote_workflow_plan_with_injected_transport_does_not_require_cloud_env(tmp_path: Path, monkeypatch) -> None:
    clear_cloud_env(monkeypatch)
    grant_pro_license(monkeypatch)
    calls: list[dict] = []

    def fake_transport(request: dict) -> dict:
        calls.append(request)
        return {"status": "success", "run_id": "local-cloud-run"}

    payload = execute_remote_workflow_plan(
        "checkout",
        tmp_path,
        execute=True,
        transport=fake_transport,
    )

    assert payload["request"]["status"] == "ready"
    assert payload["request"]["cloud_config"]["endpoint"] == "<injected-transport>"
    assert payload["network_sent"] is True
    assert payload["result"]["status"] == "success"
    assert payload["result"]["run_id"] == "local-cloud-run"
    assert len(calls) == 1


def test_http_cloud_transport_posts_json_without_exposing_key(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b'{"status": "success", "run_id": "cloud-http-1", "message": "ok"}'

    def fake_opener(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        seen["authorization"] = request.headers.get("Authorization")
        seen["org"] = request.headers.get("X-visual-agent-org")
        seen["body"] = request.data.decode("utf-8")
        return FakeResponse()

    transport = build_http_cloud_transport(
        endpoint="https://cloud.visualagent.test/run",
        api_key="va_cloud_secret",
        org="team-a",
        timeout_seconds=12.5,
        opener=fake_opener,
    )

    response = transport({"workflow_name": "checkout", "inputs": {"password": {"redacted": True}}})

    assert response["status"] == "success"
    assert response["run_id"] == "cloud-http-1"
    assert seen["url"] == "https://cloud.visualagent.test/run"
    assert seen["timeout"] == 12.5
    assert seen["authorization"] == "Bearer va_cloud_secret"
    assert seen["org"] == "team-a"
    assert "checkout" in str(seen["body"])


def test_execute_remote_workflow_plan_http_failure_does_not_record_usage(tmp_path: Path, monkeypatch) -> None:
    clear_cloud_env(monkeypatch)
    grant_pro_license(monkeypatch)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test/run")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret")

    def failing_transport(_request: dict) -> dict:
        raise TimeoutError("timed out api_key=va_cloud_secret")

    payload = execute_remote_workflow_plan(
        "checkout",
        tmp_path,
        execute=True,
        transport=failing_transport,
        inputs={"password": "demo_password"},
    )

    raw = str(payload)
    assert payload["execution_requested"] is True
    assert payload["network_sent"] is True
    assert payload["result"]["status"] == "failed"
    assert payload["result"]["usage_recorded"] is False
    assert load_agent_session(tmp_path) is None
    assert "demo_password" not in raw
    assert "va_cloud_secret" not in raw


def test_http_cloud_transport_maps_http_error_and_redacts_body() -> None:
    def fake_opener(request, _timeout):
        raise HTTPError(
            request.full_url,
            403,
            "Forbidden",
            hdrs=None,
            fp=BytesIO(b'{"error": "bad api_key=va_cloud_secret"}'),
        )

    transport = build_http_cloud_transport(
        endpoint="https://cloud.visualagent.test/run",
        api_key="va_cloud_secret",
        opener=fake_opener,
    )

    response = transport({"workflow_name": "checkout"})

    raw = str(response)
    assert response["status"] == "blocked"
    assert "HTTP 403" in response["message"]
    assert "va_cloud_secret" not in raw
    assert "[REDACTED]" in response["message"]


def test_http_cloud_transport_non_json_response_fails_without_body_leak() -> None:
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b"not-json password=demo_password"

    transport = build_http_cloud_transport(
        endpoint="https://cloud.visualagent.test/run",
        api_key="va_cloud_secret",
        opener=lambda _request, _timeout: FakeResponse(),
    )

    response = transport({"workflow_name": "checkout"})

    raw = str(response)
    assert response["status"] == "failed"
    assert "not valid JSON" in response["message"]
    assert "demo_password" not in raw


def test_execute_remote_workflow_plan_http_500_does_not_record_usage(tmp_path: Path, monkeypatch) -> None:
    clear_cloud_env(monkeypatch)
    grant_pro_license(monkeypatch)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test/run")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret")

    class FakeResponse:
        status = 500

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b"server failed password=demo_password"

    transport = build_http_cloud_transport(
        endpoint="https://cloud.visualagent.test/run",
        api_key="va_cloud_secret",
        opener=lambda _request, _timeout: FakeResponse(),
    )
    payload = execute_remote_workflow_plan(
        "checkout",
        tmp_path,
        execute=True,
        transport=transport,
        inputs={"password": "demo_password"},
    )

    raw = str(payload)
    assert payload["network_sent"] is True
    assert payload["result"]["status"] == "failed"
    assert payload["result"]["usage_recorded"] is False
    assert load_agent_session(tmp_path) is None
    assert "demo_password" not in raw
    assert "va_cloud_secret" not in raw


def test_http_transport_retries_429_then_records_success_usage(tmp_path: Path, monkeypatch) -> None:
    clear_cloud_env(monkeypatch)
    grant_pro_license(monkeypatch)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test/run")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret")
    calls = 0

    class RetryResponse:
        status = 429

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b"rate limited"

    class SuccessResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b'{"status": "success", "run_id": "cloud-retry-1"}'

    def fake_opener(_request, _timeout):
        nonlocal calls
        calls += 1
        return RetryResponse() if calls == 1 else SuccessResponse()

    transport = build_http_cloud_transport(
        endpoint="https://cloud.visualagent.test/run",
        api_key="va_cloud_secret",
        max_retries=1,
        opener=fake_opener,
    )
    payload = execute_remote_workflow_plan("checkout", tmp_path, execute=True, transport=transport)
    session = load_agent_session(tmp_path)

    assert calls == 2
    assert payload["result"]["status"] == "success"
    assert payload["result"]["run_id"] == "cloud-retry-1"
    assert payload["result"]["usage_recorded"] is True
    assert session is not None
    assert session.cloud_runs_used == 1


def test_http_transport_does_not_retry_403_or_record_usage(tmp_path: Path, monkeypatch) -> None:
    clear_cloud_env(monkeypatch)
    grant_pro_license(monkeypatch)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test/run")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret")
    calls = 0

    def fake_opener(request, _timeout):
        nonlocal calls
        calls += 1
        raise HTTPError(request.full_url, 403, "Forbidden", hdrs=None, fp=BytesIO(b"forbidden"))

    transport = build_http_cloud_transport(
        endpoint="https://cloud.visualagent.test/run",
        api_key="va_cloud_secret",
        max_retries=3,
        opener=fake_opener,
    )
    payload = execute_remote_workflow_plan("checkout", tmp_path, execute=True, transport=transport)

    assert calls == 1
    assert payload["result"]["status"] == "blocked"
    assert payload["result"]["usage_recorded"] is False
    assert load_agent_session(tmp_path) is None


def test_http_transport_exhausted_5xx_retries_do_not_record_usage(tmp_path: Path, monkeypatch) -> None:
    clear_cloud_env(monkeypatch)
    grant_pro_license(monkeypatch)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test/run")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret")
    calls = 0

    class FailedResponse:
        status = 500

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b"server failed"

    def fake_opener(_request, _timeout):
        nonlocal calls
        calls += 1
        return FailedResponse()

    transport = build_http_cloud_transport(
        endpoint="https://cloud.visualagent.test/run",
        api_key="va_cloud_secret",
        max_retries=2,
        opener=fake_opener,
    )
    payload = execute_remote_workflow_plan("checkout", tmp_path, execute=True, transport=transport)

    assert calls == 3
    assert payload["result"]["status"] == "failed"
    assert payload["result"]["usage_recorded"] is False
    assert load_agent_session(tmp_path) is None


def clear_license_env(monkeypatch) -> None:
    for name in (
        "VISUAL_AGENT_LICENSE_TIER",
        "CHECKPOINT_LICENSE_TIER",
        "VISUAL_AGENT_LICENSE_SEATS",
        "CHECKPOINT_LICENSE_SEATS",
        "VISUAL_AGENT_LICENSE_EXPIRES_AT",
        "CHECKPOINT_LICENSE_EXPIRES_AT",
        "VISUAL_AGENT_LICENSE_KEY",
        "CHECKPOINT_LICENSE_KEY",
        "VISUAL_AGENT_LICENSE_FILE",
        "CHECKPOINT_LICENSE_FILE",
        "VISUAL_AGENT_HOME",
        "CHECKPOINT_HOME",
    ):
        monkeypatch.delenv(name, raising=False)


def clear_cloud_env(monkeypatch) -> None:
    for name in (
        "VISUAL_AGENT_CLOUD_ENDPOINT",
        "CHECKPOINT_CLOUD_ENDPOINT",
        "VISUAL_AGENT_CLOUD_API_KEY",
        "CHECKPOINT_CLOUD_API_KEY",
        "VISUAL_AGENT_CLOUD_ORG",
        "CHECKPOINT_CLOUD_ORG",
    ):
        monkeypatch.delenv(name, raising=False)


def grant_pro_license(monkeypatch) -> None:
    monkeypatch.setenv("VISUAL_AGENT_LICENSE_TIER", "pro")
