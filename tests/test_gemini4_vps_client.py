from __future__ import annotations

from unittest.mock import MagicMock, patch
from visual_agent.gemini4_vps_client import (
    Gemini4Response,
    invoke_gemini4_vps,
    is_gemini4_vps_available,
)
from visual_agent.subagent_orchestrator import select_best_backend


def test_gemini4_vps_availability() -> None:
    # Availability checks SSH key presence
    assert isinstance(is_gemini4_vps_available(), bool)


def test_select_best_backend_routes_wish_and_review_to_gemini4() -> None:
    with patch("visual_agent.gemini4_vps_client.is_gemini4_vps_available", return_value=True):
        assert select_best_backend("architect", "design system") == "gemini4_vps"
        assert select_best_backend("wish", "analyze user wish") == "gemini4_vps"
        assert select_best_backend("reviewer", "review PR diff") == "gemini4_vps"


def test_invoke_gemini4_vps_https_mock() -> None:
    mock_response = {
        "model": "gemini-4",
        "choices": [{"message": {"content": "Architecture approved via HTTPS."}}],
        "usage": {"total_tokens": 42},
    }

    import json
    mock_resp = MagicMock()
    mock_resp.getcode.return_value = 200
    mock_resp.read.return_value = json.dumps(mock_response).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("visual_agent.gemini4_vps_client.urllib.request.urlopen", return_value=mock_resp):
        res = invoke_gemini4_vps("Review this architecture")
        assert res.success is True
        assert res.content == "Architecture approved via HTTPS."
        assert res.tokens_used == 42


def test_invoke_gemini4_vps_ssh_fallback_mock() -> None:
    mock_response = {
        "model": "gemini-4",
        "choices": [{"message": {"content": "Architecture approved via SSH."}}],
        "usage": {"total_tokens": 42},
    }

    with patch("visual_agent.gemini4_vps_client._invoke_via_direct_https", return_value=None), \
         patch("socks.socksocket") as mock_sock, \
         patch("paramiko.SSHClient") as mock_ssh_cls:
        mock_ssh = MagicMock()
        mock_ssh_cls.return_value = mock_ssh
        mock_stdout = MagicMock()
        import json
        mock_stdout.read.return_value = json.dumps(mock_response).encode("utf-8")
        mock_ssh.exec_command.return_value = (MagicMock(), mock_stdout, MagicMock())

        res = invoke_gemini4_vps("Review this architecture")
        assert res.success is True
        assert res.content == "Architecture approved via SSH."
        assert res.tokens_used == 42
