from visual_agent.security import contains_secret_text, redact_secret_text, scrub_secrets, text_metadata, validate_workflow_url


def test_sensitive_text_metadata_hashes_without_length_or_preview() -> None:
    metadata = text_metadata("demo_password", sensitive=True)

    assert metadata["sensitive"] is True
    assert "sha256" in metadata
    assert "text_length" not in metadata
    assert "text_preview" not in metadata


def test_redact_secret_text_scrubs_known_patterns_and_extra_values() -> None:
    text = redact_secret_text(
        "token=abc12345 and api key sk-abcdefghijklmnopqrstuv",
        extra_secrets=("plain-recorded-password",),
    )
    extra = redact_secret_text("value plain-recorded-password", extra_secrets=("plain-recorded-password",))

    assert "abc12345" not in text
    assert "sk-abcdefghijklmnopqrstuv" not in text
    assert "plain-recorded-password" not in extra
    assert contains_secret_text("Authorization: bearer abcdefghijklmnop") is True


def test_scrub_secrets_redacts_sensitive_keys_and_string_values() -> None:
    payload = scrub_secrets(
        {"password": "plain", "message": "token=abc12345", "nested": [{"value": "plain-recorded-password"}]},
        extra_secrets=("plain-recorded-password",),
    )

    assert payload["password"]["redacted"] is True
    assert "abc12345" not in str(payload)
    assert "plain-recorded-password" not in str(payload)


def test_extract_secret_does_not_match_comments() -> None:
    from visual_agent.model_credentials import extract_secret_from_line

    assert extract_secret_from_line("# see docs at https://example.com") is None
    assert extract_secret_from_line("# xiaomimimo: visit website") is None


def test_route_handler_rejects_system_path() -> None:
    import pytest

    from visual_agent.providers import route_handler

    handler = route_handler({"url": "**/*", "body_from_file": "C:/Windows/System32/drivers/etc/hosts"})
    with pytest.raises((ValueError, FileNotFoundError)):
        handler(MockRoute())


def test_auth_state_path_must_be_under_agent_auth() -> None:
    from pathlib import Path

    from visual_agent.auth_state import is_auth_state_path

    assert is_auth_state_path(Path(".agent-auth/test.json"), workspace_root=Path("."))
    assert not is_auth_state_path(Path("/etc/passwd"), workspace_root=Path("."))
    assert not is_auth_state_path(Path("../evil.json"), workspace_root=Path("."))


def test_safe_auth_state_name_strips_path_separators() -> None:
    from visual_agent.auth_state import safe_auth_state_name

    result = safe_auth_state_name("../evil/name")

    assert "/" not in result and "\\" not in result
    assert ".." not in result


def test_normalize_url_rejects_out_of_project_path() -> None:
    import pytest

    from visual_agent.providers import normalize_url

    with pytest.raises(ValueError):
        normalize_url("C:/Windows/System32/drivers/etc/hosts")


def test_sensitive_step_stores_hash_not_plaintext() -> None:
    meta = text_metadata("my_secret_password", sensitive=True)

    assert "my_secret_password" not in str(meta)
    assert meta.get("sensitive") is True


def test_queue_task_redacts_sensitive_inline_inputs(tmp_path) -> None:
    from visual_agent.scheduler import list_queue_tasks, submit_queue_task
    from visual_agent.workspace import init_workspace

    workspace = init_workspace(tmp_path / "agent-workspace")
    submit_queue_task(workspace, "local_html_form_workflow", inputs={"password": "plain-secret", "user": "demo"})

    task = list_queue_tasks(workspace)["entries"][0]
    assert task["inputs"]["user"] == "demo"
    assert task["inputs"]["password"]["redacted"] is True
    assert "plain-secret" not in str(task)


def test_validate_workflow_url_allows_localhost_and_public_urls() -> None:
    ok_localhost, reason_localhost = validate_workflow_url("http://localhost:3000/login")
    ok_public, reason_public = validate_workflow_url("https://example.com/login")

    assert ok_localhost is True
    assert reason_localhost is None
    assert ok_public is True
    assert reason_public is None


def test_validate_workflow_url_allows_loopback_ips() -> None:
    # The documented verify-impl flow targets http://127.0.0.1:<port>; loopback must be
    # allowed for consistency with the "localhost" host name.
    ok_ipv4, reason_ipv4 = validate_workflow_url("http://127.0.0.1:5173/login")
    ok_ipv6, reason_ipv6 = validate_workflow_url("http://[::1]:5173/login")

    assert ok_ipv4 is True
    assert reason_ipv4 is None
    assert ok_ipv6 is True
    assert reason_ipv6 is None


def test_validate_workflow_url_blocks_private_and_reserved_hosts() -> None:
    blocked_private = validate_workflow_url("http://192.168.0.10/login")
    blocked_link_local = validate_workflow_url("http://169.254.169.254/latest/meta-data")
    blocked_local_suffix = validate_workflow_url("http://service.internal/login")

    assert blocked_private[0] is False
    assert "Blocked private IP address" in str(blocked_private[1])
    assert blocked_link_local[0] is False
    assert blocked_local_suffix[0] is False


class MockRoute:
    def fulfill(self, **_kwargs) -> None:
        raise AssertionError("unsafe route should not be fulfilled")
