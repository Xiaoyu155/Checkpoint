from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from visual_agent.managed_checkpoint import (
    CHECKPOINT_FILENAME,
    CHECKPOINT_SCHEMA_VERSION,
    ManagedCheckpoint,
    clear_managed_checkpoint,
    get_checkpoint_path,
    is_checkpoint_resumable,
    load_managed_checkpoint,
    save_managed_checkpoint,
)
from visual_agent.model_health import (
    ModelHealthStatus,
    check_all_models_concurrently,
    check_codex_health,
    check_gemini4_vps_health,
    check_gemini_cli_health,
    check_grok_health,
    check_workbuddy_health,
)


def test_managed_checkpoint_lifecycle(tmp_path: Path) -> None:
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()

    # 1. No checkpoint initially
    assert load_managed_checkpoint(ws_root) is None
    assert clear_managed_checkpoint(ws_root) is False

    # 2. Save a new checkpoint
    chk = ManagedCheckpoint(
        checkpoint_id="chk_test_123",
        goal="Autonomous Refactor Goal",
        workspace_root=str(ws_root),
        status="in_progress",
        current_wave_index=1,
        total_waves=3,
        completed_task_ids=["task_1"],
        pending_task_ids=["task_2", "task_3"],
        patched_files=["src/app.py"],
        task_states={"task_1": {"status": "passed", "backend": "grok"}},
    )

    saved_path = save_managed_checkpoint(ws_root, chk)
    assert saved_path.exists()
    assert saved_path.name == CHECKPOINT_FILENAME

    # 3. Load checkpoint
    loaded = load_managed_checkpoint(ws_root, expected_goal="Autonomous Refactor Goal")
    assert loaded is not None
    assert loaded.checkpoint_id == "chk_test_123"
    assert loaded.status == "in_progress"
    assert loaded.completed_task_ids == ["task_1"]
    assert loaded.pending_task_ids == ["task_2", "task_3"]
    assert loaded.patched_files == ["src/app.py"]
    assert loaded.schema_version == CHECKPOINT_SCHEMA_VERSION
    assert is_checkpoint_resumable(loaded) is True

    # 4. Update status to completed
    loaded.status = "completed"
    loaded.pending_task_ids = []
    save_managed_checkpoint(ws_root, loaded)
    reloaded = load_managed_checkpoint(ws_root)
    assert reloaded is not None
    assert reloaded.status == "completed"
    assert is_checkpoint_resumable(reloaded) is False

    # 5. Clear checkpoint
    assert clear_managed_checkpoint(ws_root) is True
    assert load_managed_checkpoint(ws_root) is None


def test_model_health_status_dataclass() -> None:
    st = ModelHealthStatus(
        name="codex",
        status="online",
        role="heavy_code",
        latency_ms=120.5,
        details="codex-cli 0.164.0",
        error=None,
    )
    d = st.to_dict()
    assert d["name"] == "codex"
    assert d["status"] == "online"
    assert d["latency_ms"] == 120.5


def test_check_all_models_concurrently_aggregation() -> None:
    with patch("visual_agent.model_health.check_codex_health") as m_codex, \
         patch("visual_agent.model_health.check_grok_health") as m_grok, \
         patch("visual_agent.model_health.check_gemini4_vps_health") as m_vps, \
         patch("visual_agent.model_health.check_workbuddy_health") as m_wb, \
         patch("visual_agent.model_health.check_gemini_cli_health") as m_gemini:

        m_codex.return_value = ModelHealthStatus("codex", "online", "heavy", 100.0, "ok")
        m_grok.return_value = ModelHealthStatus("grok", "online", "fast", 200.0, "ok")
        m_vps.return_value = ModelHealthStatus("gemini4_vps", "offline", "arch", 0.0, "timeout", "timed out")
        m_wb.return_value = ModelHealthStatus("workbuddy", "online", "local", 50.0, "ok")
        m_gemini.return_value = ModelHealthStatus("gemini_cli", "online", "host", 30.0, "ok")

        res = check_all_models_concurrently(timeout=1.0)
        assert res["status"] == "ok"
        assert res["total_probed"] == 5
        assert res["online_count"] == 4
        assert "codex" in res["models"]
        assert res["models"]["codex"]["status"] == "online"
        assert res["models"]["gemini4_vps"]["status"] == "offline"


def test_check_codex_health_not_found() -> None:
    with patch("shutil.which", return_value=None), patch("os.path.exists", return_value=False):
        res = check_codex_health()
        assert res.status == "offline"
        assert "Executable not found" in str(res.error)


def test_check_grok_health_no_key() -> None:
    with patch("visual_agent.model_health.is_grok_available", return_value=False):
        res = check_grok_health()
        assert res.status == "offline"
        assert "Missing API Key" in str(res.error)


def test_check_gemini4_vps_health_failure() -> None:
    with patch("socket.create_connection", side_effect=OSError("connection refused")):
        res = check_gemini4_vps_health(timeout=0.1)
        assert res.status == "offline"
        assert "connection refused" in str(res.error)


def test_codex_version_success_is_degraded_not_online() -> None:
    """A working --version only proves the binary runs. It must not be
    reported as 'online' (which implies generation-ready)."""
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "codex-cli 0.166.0"
    fake.stderr = ""
    with patch("visual_agent.model_health.shutil.which", return_value="/usr/bin/codex"), \
         patch("visual_agent.model_health.subprocess.run", return_value=fake):
        res = check_codex_health()
        assert res.status == "degraded"
        assert res.installed is True
        assert res.generation_verified is False
        assert res.authenticated is None


def test_gemini4_empty_pool_is_degraded_not_online() -> None:
    """An empty account pool cannot serve generation — must not be green."""
    import io
    import json as _json

    class _Resp:
        def __init__(self, payload):
            self._b = io.BytesIO(_json.dumps(payload).encode())
        def getcode(self):
            return 200
        def read(self, *a):
            return self._b.read()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", return_value=_Resp({"available_accounts": 0, "pool_size": 6})):
        res = check_gemini4_vps_health(timeout=0.5)
        assert res.status == "degraded"
        assert res.reachable is True
        assert res.authenticated is False
        assert res.generation_verified is False


def test_gemini4_socket_only_is_degraded_not_online() -> None:
    """A raw TCP connection only proves reachability, not gateway/pool health."""
    with patch("urllib.request.urlopen", side_effect=OSError("https down")), \
         patch("socket.create_connection", return_value=MagicMock()):
        res = check_gemini4_vps_health(timeout=0.5)
        assert res.status == "degraded"
        assert res.reachable is True
        assert res.generation_verified is False


def test_health_aggregation_reports_capability_counts() -> None:
    with patch("visual_agent.model_health.check_codex_health") as m_codex, \
         patch("visual_agent.model_health.check_grok_health") as m_grok, \
         patch("visual_agent.model_health.check_gemini4_vps_health") as m_vps, \
         patch("visual_agent.model_health.check_workbuddy_health") as m_wb, \
         patch("visual_agent.model_health.check_gemini_cli_health") as m_gemini:
        m_codex.return_value = ModelHealthStatus("codex", "degraded", "r", 1.0, "d", installed=True, reachable=True)
        m_grok.return_value = ModelHealthStatus("grok", "offline", "r", 0.0, "d")
        m_vps.return_value = ModelHealthStatus("gemini4_vps", "online", "r", 2.0, "d", installed=True, reachable=True, authenticated=True)
        m_wb.return_value = ModelHealthStatus("workbuddy", "degraded", "r", 1.0, "d", installed=True, reachable=True)
        m_gemini.return_value = ModelHealthStatus("gemini_cli", "degraded", "r", 1.0, "d", installed=True, reachable=True)

        res = check_all_models_concurrently(timeout=1.0)
        assert res["online_count"] == 1
        assert res["degraded_count"] == 3
        assert res["offline_count"] == 1
        # None of the probes verified generation
        assert res["generation_ready_count"] == 0


def test_codex_nonzero_exit_code_is_offline_not_degraded() -> None:
    """`codex --version` exiting non-zero (auth failure) must not be reported
    as a merely 'degraded but reachable' backend."""
    fake = MagicMock()
    fake.returncode = 7
    fake.stdout = ""
    fake.stderr = "Error: not logged in. Please run `codex login`."
    with patch("visual_agent.model_health.shutil.which", return_value="/usr/bin/codex"), \
         patch("visual_agent.model_health.subprocess.run", return_value=fake):
        res = check_codex_health()
        assert res.status == "offline"
        assert res.installed is True
        assert res.reachable is False
        assert res.authenticated is False
        assert res.generation_verified is False
        assert "7" in str(res.details) or "7" in str(res.error)


def test_codex_nonzero_exit_code_without_auth_hint_is_not_authenticated() -> None:
    """A non-zero exit with no authentication wording must not claim to know
    the authentication state — it must report unknown, not 'failed auth'."""
    fake = MagicMock()
    fake.returncode = 3
    fake.stdout = ""
    fake.stderr = "unexpected internal failure"
    with patch("visual_agent.model_health.shutil.which", return_value="/usr/bin/codex"), \
         patch("visual_agent.model_health.subprocess.run", return_value=fake):
        res = check_codex_health()
        assert res.status == "offline"
        assert res.authenticated is None


def test_grok_401_is_offline_and_not_authenticated() -> None:
    """HTTP 401 from the Sub2API gateway must be offline, never online."""
    import urllib.error

    def _raise_401(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="https://example.invalid/v1/models",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )

    with patch("visual_agent.model_health.is_grok_available", return_value=True), \
         patch("visual_agent.grok_bridge.get_grok_api_base", return_value="https://example.invalid/v1"), \
         patch("visual_agent.grok_bridge.get_grok_api_key", return_value="sk-test"), \
         patch("urllib.request.urlopen", side_effect=_raise_401):
        res = check_grok_health(timeout=0.5)
        assert res.status == "offline"
        assert res.authenticated is False
        assert res.generation_verified is False


def test_grok_probe_and_generation_share_same_user_agent() -> None:
    """The health probe and the real generation call must present the same
    client identity, otherwise the probe passes while real calls are
    rejected by the gateway's bot filter."""
    import urllib.request

    from visual_agent.grok_bridge import GROK_USER_AGENT

    captured: dict[str, object] = {}

    class _Resp:
        def getcode(self):
            return 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _capture(req, *args, **kwargs):
        captured["ua"] = req.get_header("User-agent")
        return _Resp()

    with patch("visual_agent.model_health.is_grok_available", return_value=True), \
         patch("visual_agent.grok_bridge.get_grok_api_base", return_value="https://example.invalid/v1"), \
         patch("visual_agent.grok_bridge.get_grok_api_key", return_value="sk-test"), \
         patch("urllib.request.urlopen", side_effect=_capture):
        check_grok_health(timeout=0.5)

    assert captured.get("ua") == GROK_USER_AGENT

    # The real generation path must use the same identifier.
    import inspect

    from visual_agent import grok_bridge

    src = inspect.getsource(grok_bridge.invoke_grok)
    assert "GROK_USER_AGENT" in src


def test_invoke_grok_does_not_change_process_socket_timeout(monkeypatch) -> None:
    import socket

    from visual_agent import grok_bridge

    original = socket.getdefaulttimeout()

    class Response:
        fp = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self):
            if not hasattr(self, "_sent"):
                self._sent = True
                return b'data: {"choices":[{"delta":{"content":"ok"}}]}\n'
            return b"data: [DONE]\n"

    def fake_urlopen(_request, timeout):
        assert timeout == 0.25
        assert socket.getdefaulttimeout() == original
        return Response()

    monkeypatch.setattr(grok_bridge, "is_grok_available", lambda: True)
    monkeypatch.setattr(grok_bridge, "get_grok_api_base", lambda: "https://example.test/v1")
    monkeypatch.setattr(grok_bridge, "get_grok_api_key", lambda: "test-key")
    monkeypatch.setattr(grok_bridge.urllib.request, "urlopen", fake_urlopen)

    result = grok_bridge.invoke_grok("test", retries=0, timeout=0.25)

    assert result.success is True
    assert result.response == "ok"
    assert socket.getdefaulttimeout() == original


def test_health_aggregation_propagates_timeout_to_probes() -> None:
    """The aggregate timeout must be handed to each probe, and a probe that
    blows the deadline must be reported rather than silently dropped."""
    seen: dict[str, float] = {}

    def _probe(timeout: float = 2.5):
        seen["timeout"] = timeout
        return ModelHealthStatus(
            "probe", "degraded", "r", 1.0, "d",
            installed=True, reachable=True,
        )

    with patch("visual_agent.model_health.check_codex_health", _probe), \
         patch("visual_agent.model_health.check_grok_health", _probe), \
         patch("visual_agent.model_health.check_gemini4_vps_health", _probe), \
         patch("visual_agent.model_health.check_workbuddy_health", _probe), \
         patch("visual_agent.model_health.check_gemini_cli_health", _probe):
        res = check_all_models_concurrently(timeout=1.75)

    assert seen.get("timeout") == 1.75
    assert res["total_probed"] == 5
    assert len(res["models"]) == 5


def test_checkpoint_rejects_mismatched_workspace(tmp_path: Path) -> None:
    """A checkpoint from a different workspace must not be resumed."""
    ws_a = tmp_path / "workspace_a"
    ws_b = tmp_path / "workspace_b"
    ws_a.mkdir()
    ws_b.mkdir()

    chk = ManagedCheckpoint(
        checkpoint_id="chk_cross",
        goal="Some goal",
        workspace_root=str(ws_a.resolve()),
        status="in_progress",
        completed_task_ids=["task_1"],
        pending_task_ids=["task_2"],
    )
    save_managed_checkpoint(ws_a, chk)

    # Loading from workspace_b should return None (strict workspace check)
    loaded = load_managed_checkpoint(ws_b, expected_goal="Some goal")
    assert loaded is None


def test_checkpoint_rejects_mismatched_goal_hash(tmp_path: Path) -> None:
    """A checkpoint with a different goal hash must not be resumed,
    even if the workspace matches."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    import hashlib
    original_goal = "Original refactoring goal"
    original_hash = hashlib.sha256(original_goal.encode("utf-8")).hexdigest()

    chk = ManagedCheckpoint(
        checkpoint_id="chk_goal_test",
        goal=original_goal,
        workspace_root=str(ws.resolve()),
        plan_goal_hash=original_hash,
        plan_task_count=3,
        status="in_progress",
        completed_task_ids=["task_1"],
        pending_task_ids=["task_2", "task_3"],
    )
    save_managed_checkpoint(ws, chk)

    # Same workspace, different goal — must return None
    loaded = load_managed_checkpoint(ws, expected_goal="Completely different goal")
    assert loaded is None

    # Same workspace, same goal — must load successfully
    loaded_ok = load_managed_checkpoint(ws, expected_goal=original_goal)
    assert loaded_ok is not None
    assert loaded_ok.checkpoint_id == "chk_goal_test"
