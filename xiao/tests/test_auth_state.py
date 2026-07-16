from __future__ import annotations

import json

import pytest

from visual_agent.auth_state import (
    auth_state_probe_to_markdown,
    auth_state_path,
    build_auth_state_import_plan,
    import_auth_state,
    inspect_storage_state,
    probe_storage_state,
    safe_auth_state_name,
)
from visual_agent.cli import main
from visual_agent.external_samples import external_samples_readiness


def write_storage_state(path) -> None:
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "session",
                        "value": "secret-cookie-value",
                        "domain": ".seller.sandbox.example.com",
                        "path": "/",
                    }
                ],
                "origins": [
                    {
                        "origin": "https://seller.sandbox.example.com",
                        "localStorage": [{"name": "token", "value": "secret-token-value"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_inspect_storage_state_returns_redacted_metadata(tmp_path) -> None:
    state = tmp_path / "state.json"
    write_storage_state(state)

    metadata = inspect_storage_state(state)

    assert metadata["valid"] is True
    assert metadata["cookie_count"] == 1
    assert metadata["origin_count"] == 1
    assert metadata["session_cookie_count"] == 1
    assert metadata["persistent_cookie_count"] == 0
    assert metadata["expired_cookie_count"] == 0
    assert metadata["has_session_material"] is True
    assert metadata["domains"] == ["seller.sandbox.example.com"]
    assert metadata["origin_hosts"] == ["seller.sandbox.example.com"]
    assert metadata["redacted"] is True
    assert "secret-cookie-value" not in json.dumps(metadata)
    assert "secret-token-value" not in json.dumps(metadata)


def test_inspect_storage_state_reports_expired_cookie_metadata(tmp_path) -> None:
    state = tmp_path / "expired.json"
    state.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "session",
                        "value": "expired-secret",
                        "domain": ".seller.sandbox.example.com",
                        "path": "/",
                        "expires": 1,
                    }
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )

    metadata = inspect_storage_state(state)

    assert metadata["persistent_cookie_count"] == 1
    assert metadata["session_cookie_count"] == 0
    assert metadata["expired_cookie_count"] == 1
    assert metadata["earliest_cookie_expires_at"] == 1.0
    assert "expired-secret" not in json.dumps(metadata)


def test_import_auth_state_writes_agent_auth_and_manifest(tmp_path) -> None:
    source = tmp_path / "source.json"
    write_storage_state(source)

    result = import_auth_state(source, name="seller-sandbox-state", workspace_root=tmp_path)
    target = tmp_path / ".agent-auth" / "seller-sandbox-state.json"
    manifest = tmp_path / ".agent-auth" / "seller-sandbox-state.json.manifest.json"

    assert result["imported"] is True
    assert target.exists()
    assert manifest.exists()
    assert result["metadata"]["domains"] == ["seller.sandbox.example.com"]
    assert "secret-cookie-value" not in json.dumps(result)


def test_import_auth_state_refuses_overwrite_by_default(tmp_path) -> None:
    source = tmp_path / "source.json"
    write_storage_state(source)
    import_auth_state(source, name="seller", workspace_root=tmp_path)

    with pytest.raises(FileExistsError):
        import_auth_state(source, name="seller", workspace_root=tmp_path)


def test_import_auth_state_satisfies_external_readiness(tmp_path) -> None:
    source = tmp_path / "source.json"
    write_storage_state(source)
    import_auth_state(source, name="seller-sandbox-state", workspace_root=tmp_path)

    readiness = external_samples_readiness(workspace_root=tmp_path)

    assert readiness["ready_samples"] == 2
    assert readiness["blocked_samples"] == 2
    ecommerce = next(entry for entry in readiness["entries"] if entry["sample_id"] == "external_ecommerce_orders_readonly")
    assert ecommerce["ready"] is True


def test_build_auth_state_import_plan_is_redacted_and_safe(tmp_path) -> None:
    source = tmp_path / "state.json"
    write_storage_state(source)

    plan = build_auth_state_import_plan(source, name="seller sandbox", workspace_root=tmp_path)

    assert plan["source_exists"] is True
    assert plan["safe_target"] is True
    assert plan["target"].endswith(".agent-auth\\seller-sandbox.json") or plan["target"].endswith(".agent-auth/seller-sandbox.json")
    assert "secret-cookie-value" not in json.dumps(plan)


def test_auth_state_name_must_not_be_empty() -> None:
    with pytest.raises(ValueError):
        safe_auth_state_name("...")


def test_auth_state_path_stays_under_agent_auth(tmp_path) -> None:
    path = auth_state_path("../seller", workspace_root=tmp_path)

    assert path.name == "seller.json"
    assert path.parent == tmp_path / ".agent-auth"


def test_auth_state_cli_import_and_inspect(tmp_path, capsys) -> None:
    source = tmp_path / "source.json"
    write_storage_state(source)

    import_code = main(
        [
            "auth-state-import",
            "--source",
            str(source),
            "--name",
            "seller-sandbox-state",
            "--workspace-root",
            str(tmp_path),
        ]
    )
    import_output = capsys.readouterr().out
    target = tmp_path / ".agent-auth" / "seller-sandbox-state.json"
    inspect_code = main(["auth-state-inspect", "--path", str(target)])
    inspect_output = capsys.readouterr().out

    assert import_code == 0
    assert inspect_code == 0
    assert target.exists()
    assert "secret-cookie-value" not in import_output
    assert "secret-token-value" not in inspect_output


class FakeRoute:
    def fulfill(self, **_kwargs):
        return None


class FakePage:
    url = "https://seller.sandbox.example.com/probe"

    def route(self, _pattern, handler):
        handler(FakeRoute())

    def goto(self, url, **_kwargs):
        self.url = url

    def title(self):
        return "Auth State Probe"


class FakeBrowserContext:
    def __init__(self, recorder):
        self.recorder = recorder

    def new_page(self):
        return FakePage()

    def close(self):
        self.recorder["context_closed"] = True


class FakeBrowser:
    def __init__(self, recorder):
        self.recorder = recorder

    def new_context(self, **kwargs):
        self.recorder["context_options"] = kwargs
        return FakeBrowserContext(self.recorder)

    def close(self):
        self.recorder["browser_closed"] = True


class FakeChromium:
    def __init__(self, recorder):
        self.recorder = recorder

    def launch(self, **kwargs):
        self.recorder["launch_options"] = kwargs
        return FakeBrowser(self.recorder)


class FakePlaywright:
    def __init__(self, recorder):
        self.chromium = FakeChromium(recorder)
        self.recorder = recorder

    def stop(self):
        self.recorder["stopped"] = True


class FakeSyncPlaywright:
    def __init__(self, recorder):
        self.recorder = recorder

    def __call__(self):
        return self

    def start(self):
        return FakePlaywright(self.recorder)


def test_probe_storage_state_loads_browser_context_and_reports_ready(tmp_path, monkeypatch) -> None:
    state = tmp_path / "state.json"
    write_storage_state(state)
    recorder = {}
    monkeypatch.setattr("visual_agent.auth_state.get_sync_playwright", lambda: FakeSyncPlaywright(recorder))

    result = probe_storage_state(state, url="https://seller.sandbox.example.com/probe")
    text = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "ready"
    assert result["loaded"] is True
    assert result["domain_match"] is True
    assert recorder["context_options"]["storage_state"] == str(state)
    assert recorder["context_options"]["accept_downloads"] is False
    assert recorder["browser_closed"] is True
    assert recorder["context_closed"] is True
    assert recorder["stopped"] is True
    assert "secret-cookie-value" not in text
    assert "secret-token-value" not in text


def test_probe_storage_state_blocks_domain_mismatch_but_still_tests_load(tmp_path, monkeypatch) -> None:
    state = tmp_path / "state.json"
    write_storage_state(state)
    recorder = {}
    monkeypatch.setattr("visual_agent.auth_state.get_sync_playwright", lambda: FakeSyncPlaywright(recorder))

    result = probe_storage_state(state, url="https://inventory.sandbox.example.com/probe")

    assert result["status"] == "blocked"
    assert result["loaded"] is True
    assert result["blockers"] == ["domain_mismatch"]
    assert result["domain_match"] is False


def test_auth_state_probe_cli_outputs_markdown_without_secrets(tmp_path, capsys, monkeypatch) -> None:
    state = tmp_path / "state.json"
    write_storage_state(state)
    monkeypatch.setattr("visual_agent.auth_state.get_sync_playwright", lambda: FakeSyncPlaywright({}))

    code = main(
        [
            "auth-state-probe",
            "--path",
            str(state),
            "--url",
            "https://seller.sandbox.example.com/probe",
            "--format",
            "markdown",
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert "# Auth State Probe" in output
    assert "Loaded in browser context: `True`" in output
    assert "secret-cookie-value" not in output
    assert "secret-token-value" not in output


def test_auth_state_probe_markdown_shows_blockers() -> None:
    markdown = auth_state_probe_to_markdown(
        {
            "status": "blocked",
            "loaded": True,
            "domain_match": False,
            "has_session_material": True,
            "all_cookies_expired": False,
            "allowed_domain": "inventory.sandbox.example.com",
            "blockers": ["domain_mismatch"],
            "page": {"url": "https://inventory.sandbox.example.com/probe"},
        }
    )

    assert "Blockers: `domain_mismatch`" in markdown
