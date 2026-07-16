import socketserver
import threading
from pathlib import Path

from visual_agent.preflight import check_build_freshness, check_port_alive, detect_project_type, inspect_environment, run_preflight
from visual_agent.workflow import workflow_from_dict


def test_preflight_accepts_valid_workflow() -> None:
    workflow = workflow_from_dict(
        {
            "schema_version": 1,
            "min_runtime_version": "0.1.0",
            "name": "ok",
            "steps": [
                {"id": "observe", "action": "observe_fixture", "path": "examples/fixtures/login_page_observation.json"},
                {"id": "assert", "action": "assert_text", "text": "客户管理系统"},
            ],
        }
    )

    result = run_preflight(workflow)

    assert result.ok
    assert result.validation.valid
    assert result.missing_required_capabilities == ()
    assert result.unavailable_used_capabilities == ()


def test_preflight_strict_rejects_missing_assertion() -> None:
    workflow = workflow_from_dict(
        {
            "schema_version": 1,
            "min_runtime_version": "0.1.0",
            "name": "missing-assert",
            "steps": [
                {"id": "observe", "action": "observe_fixture", "path": "examples/fixtures/login_page_observation.json"},
            ],
        }
    )

    result = run_preflight(workflow, strict=True)

    assert not result.ok
    assert any("verification assertion" in issue.message for issue in result.validation.issues)


def test_preflight_blocks_unavailable_capability_used_by_workflow(monkeypatch) -> None:
    def fake_module_available(module_name: str | None) -> bool:
        if module_name == "uiautomation":
            return False
        return True

    monkeypatch.setattr("visual_agent.capabilities.module_available", fake_module_available)
    workflow = workflow_from_dict(
        {
            "schema_version": 1,
            "min_runtime_version": "0.1.0",
            "name": "uia",
            "steps": [
                {"id": "observe", "action": "observe_uia"},
                {"id": "assert", "action": "assert_text", "text": "确定"},
            ],
        }
    )

    result = run_preflight(workflow)

    assert not result.ok
    assert any(capability.name == "observe_uia" for capability in result.unavailable_used_capabilities)


def test_environment_checks_detect_project_type_port_and_fresh_build(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"dependencies":{"vite":"5.0.0","vue":"3.0.0"}}', encoding="utf-8")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:  # type: ignore[override]
            self.request.recv(1)

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            port_check = check_port_alive("127.0.0.1", port)
            build_check = check_build_freshness(dist, max_age_minutes=10)
            environment = inspect_environment(tmp_path, host="127.0.0.1", port=port, max_age_minutes=10)
        finally:
            server.shutdown()
            thread.join(timeout=5)

    assert detect_project_type(tmp_path) == "vite"
    assert port_check["ok"] is True
    assert build_check["ok"] is True
    assert environment["ok"] is True
    assert environment["project_type"] == "vite"
    assert environment["build_checks"][0]["path"].endswith("dist")


def test_detect_project_type_keeps_python_project_with_auxiliary_package_json(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='api'\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"dependencies":{"vue":"3.0.0"}}', encoding="utf-8")

    assert detect_project_type(tmp_path) == "python"
