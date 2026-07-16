"""HTTP server for the dashboard.

A zero-dependency, local-only web view served with the stdlib HTTP server.
Routes requests to API handlers in api.py and serves static files.
"""

from __future__ import annotations

import json
import os
import sys
import webbrowser
import threading
import ipaddress
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from ..missions import validate_mission_id
from .observability import (
    ObservabilityRequestError,
    get_observability_launch,
    get_observability_timeline,
    list_observability_launches,
)
from .api import (
    build_diagnostic_bundle,
    build_dashboard_data_cached,
    build_five_pillars_data,
    build_mission_detail,
    get_active_workspace,
    get_commercial_config,
    get_model_config,
    get_notifications_config,
    get_user_profile,
    list_known_workspaces,
    log_error,
    retry_mission,
    refine_goal_intake,
    run_chat,
    save_model_config,
    save_notifications_config,
    save_commercial_config,
    save_profile_config,
    set_active_workspace,
    start_workbench_mission,
    start_worker,
    stop_worker,
    test_model_config,
    test_notification,
)

# Static file directory - 兼容 exe 打包和开发环境
def _find_static_dir() -> Path:
    """查找静态文件目录，兼容 PyInstaller 打包和开发环境"""
    # 开发环境：相对于当前文件
    dev_path = Path(__file__).parent / "static"
    if dev_path.exists():
        return dev_path
    # PyInstaller 打包后：在 _internal 目录中查找
    import sys
    if getattr(sys, 'frozen', False):
        base = Path(sys._MEIPASS) if hasattr(sys, '_MEIPASS') else Path(sys.executable).parent / "_internal"
        candidate = base / "visual_agent" / "dashboard" / "static"
        if candidate.exists():
            return candidate
    # 兜底：返回开发路径
    return dev_path

_STATIC_DIR = _find_static_dir()


class _DashboardHandler(BaseHTTPRequestHandler):
    server: "_DashboardServer"

    def log_message(self, *args: Any) -> None:
        return

    def _send(self, body: bytes, content_type: str, *, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        origin = self._allowed_cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        """Handle browser preflight without opening the local API to the web."""
        path = unquote(self.path.split("?", 1)[0])
        reason = self._api_request_block_reason(path, require_json=False) if path.startswith("/api/") else ""
        if reason:
            self._send_json({"ok": False, "error": reason}, status=403)
            return
        self.send_response(204)
        origin = self._allowed_cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Vary", "Origin")
        self.end_headers()

    def _send_json(self, data: Any, *, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8-sig")
        self._send(body, "application/json; charset=utf-8", status=status)

    def _serve_static(self, file_path: Path, content_type: str) -> None:
        if not file_path.exists():
            self.send_response(404)
            self.end_headers()
            return
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The dashboard changes frequently during local dogfooding. Stale app.js
        # makes buttons call old handlers, so all workbench assets are no-cache.
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        try:
            self._do_get_inner()
        except Exception as exc:
            log_error("backend", f"GET {self.path} failed", repr(exc))
            try:
                self._send_json({"ok": False, "error": "服务器内部错误，请查看 Dashboard 日志。"}, status=500)
            except OSError:
                pass

    def _do_get_inner(self) -> None:
        path = unquote(self.path.split("?", 1)[0])
        root = get_active_workspace(self.server.workspace_root)

        # Static files
        if path in {"/", "/index.html", "/工作台", "/workbench", "/dashboard"}:
            self._serve_static(_STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path in {"/five-pillars", "/five-pillars.html"}:
            self._serve_static(_STATIC_DIR / "five-pillars.html", "text/html; charset=utf-8")
            return
        if path == "/app.js":
            self._serve_static(_STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
            return
        if path == "/style.css":
            self._serve_static(_STATIC_DIR / "style.css", "text/css; charset=utf-8")
            return
        if path == "/five-pillars.js":
            self._serve_static(_STATIC_DIR / "five-pillars.js", "application/javascript; charset=utf-8")
            return
        if path == "/five-pillars.css":
            self._serve_static(_STATIC_DIR / "five-pillars.css", "text/css; charset=utf-8")
            return

        # API endpoints
        if path.startswith("/api/"):
            reason = self._api_request_block_reason(path, require_json=False)
            if reason:
                self._send_json({"ok": False, "error": reason}, status=403)
                return
        if path == "/api/data":
            self._send_json(build_dashboard_data_cached(root))
            return
        if path == "/api/five-pillars":
            self._send_json(build_five_pillars_data(root))
            return
        if path == "/api/observability/launches":
            query = parse_qs(urlparse(self.path).query)
            self._send_observability(
                lambda: list_observability_launches(root, limit=(query.get("limit") or [20])[0])
            )
            return
        if path.startswith("/api/observability/launches/"):
            launch_id = path.removeprefix("/api/observability/launches/")
            self._send_observability(lambda: get_observability_launch(root, launch_id))
            return
        if path.startswith("/api/observability/sessions/") and path.endswith("/timeline"):
            session_id = path.removeprefix("/api/observability/sessions/").removesuffix("/timeline")
            query = parse_qs(urlparse(self.path).query)
            self._send_observability(
                lambda: get_observability_timeline(
                    root,
                    launch_id=(query.get("launch_id") or [""])[0],
                    session_id=session_id,
                    cursor=(query.get("cursor") or [0])[0],
                    limit=(query.get("limit") or [100])[0],
                )
            )
            return
        if path == "/api/events":
            self._stream_events(root)
            return
        if path == "/api/diagnostic":
            self._send_json(build_diagnostic_bundle(root))
            return
        if path == "/api/mission":
            mission_id = self._validated_mission_id((parse_qs(urlparse(self.path).query).get("id") or [""])[0])
            if mission_id is None:
                return
            self._send_json(build_mission_detail(root, mission_id))
            return
        if path == "/api/workspace":
            self._send_json({"workspace": str(root), "ok": True})
            return
        if path == "/api/workspaces":
            self._send_json({"workspaces": list_known_workspaces(), "current": str(root)})
            return
        if path == "/api/notifications/config":
            self._send_json(get_notifications_config())
            return
        if path == "/api/profile":
            self._send_json(get_user_profile())
            return
        if path == "/api/commercial-config":
            self._send_json(get_commercial_config())
            return
        if path == "/api/model-config":
            self._send_json(get_model_config())
            return

        self.send_response(404)
        self.end_headers()

    def _send_observability(self, builder: Any) -> None:
        try:
            payload = builder()
        except ObservabilityRequestError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=exc.status)
            return
        self._send_json(payload)

    def _stream_events(self, root: Path) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        origin = self._allowed_cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()

        last_signature = ""
        last_heartbeat = 0.0
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                data = build_dashboard_data_cached(root)
                signature = _dashboard_event_signature(data)
                now = time.time()
                if signature != last_signature:
                    last_signature = signature
                    self._write_sse(
                        "snapshot",
                        {
                            "type": "snapshot_changed",
                            "signature": signature,
                            "trace_count": len(data.get("work_traces") or []),
                            "mission_count": len(data.get("missions") or []),
                            "queue_count": len(data.get("queue") or []),
                        },
                    )
                    last_heartbeat = now
                elif now - last_heartbeat >= 15:
                    self._write_sse("heartbeat", {"type": "heartbeat", "signature": signature})
                    last_heartbeat = now
                time.sleep(1.0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    def _write_sse(self, event: str, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(f"event: {event}\n".encode("utf-8"))
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _read_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            parsed = json.loads(raw.decode("utf-8") or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, OSError):
            return {}

    def do_POST(self) -> None:
        try:
            self._do_post_inner()
        except Exception as exc:
            log_error("backend", f"POST {self.path} failed", repr(exc))
            try:
                self._send_json({"ok": False, "error": "服务器内部错误，请查看 Dashboard 日志。"}, status=500)
            except OSError:
                pass

    def _do_post_inner(self) -> None:
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/"):
            reason = self._api_request_block_reason(path, require_json=True)
            if reason:
                status = 415 if reason == "API requests must use Content-Type: application/json." else 403
                self._send_json({"ok": False, "error": reason}, status=status)
                return
        payload = self._read_body()
        root = get_active_workspace(self.server.workspace_root)

        if path == "/api/mission/start":
            from ..mission_pipeline import SpecValidationError, SpecValidator
            try:
                spec = SpecValidator().validate(payload.get("spec"))
            except SpecValidationError as exc:
                self._send_json(exc.to_response(), status=400)
                return
            result = start_workbench_mission(
                workspace_root=root,
                repo_root=str(payload.get("repo_root") or str(root.parent)),
                goal=str(payload.get("goal") or ""),
                test_command=str(payload.get("test_command") or ""),
                agent=str(payload.get("agent") or ""),
                dispatch_mode=str(payload.get("dispatch_mode") or "tracked"),
                execute=bool(payload.get("execute")),
                merge_policy=str(payload.get("merge_policy") or "manual"),
                allow_dirty=bool(payload.get("allow_dirty")),
                spec=spec,
                intake=payload.get("intake") if isinstance(payload.get("intake"), dict) else None,
                answers=payload.get("answers") if isinstance(payload.get("answers"), list) else None,
            )
            self._send_json(result)
        elif path == "/api/mission/merge":
            from ..workbench_board import merge_mission_now
            mission_id = self._validated_mission_id(payload.get("mission_id"))
            if mission_id is None:
                return
            result = merge_mission_now(root, mission_id)
            self._send_json(result)
        elif path == "/api/mission/delete":
            from ..workbench_board import archive_mission_now
            mission_id = self._validated_mission_id(payload.get("mission_id"))
            if mission_id is None:
                return
            result = archive_mission_now(root, mission_id)
            self._send_json(result)
        elif path == "/api/mission/delete-all":
            from ..workbench_board import archive_all_missions_now
            result = archive_all_missions_now(root)
            self._send_json(result)
        elif path == "/api/mission/retry":
            mission_id = self._validated_mission_id(payload.get("mission_id"))
            if mission_id is None:
                return
            result = retry_mission(root, mission_id)
            self._send_json(result)
        elif path == "/api/worker/start":
            self._send_json(start_worker(root))
        elif path == "/api/worker/stop":
            self._send_json(stop_worker())
        elif path == "/api/workspace/switch":
            self._send_json(set_active_workspace(str(payload.get("path") or "")))
        elif path == "/api/chat":
            self._send_json(run_chat(payload))
        elif path == "/api/goal/refine":
            self._send_json(refine_goal_intake(payload))
        elif path == "/api/notifications/config":
            self._send_json(save_notifications_config(payload))
        elif path == "/api/profile":
            self._send_json(save_profile_config(payload))
        elif path == "/api/commercial-config":
            self._send_json(save_commercial_config(payload))
        elif path == "/api/notifications/test":
            self._send_json(test_notification())
        elif path == "/api/model-config":
            self._send_json(save_model_config(payload))
        elif path == "/api/model-config/test":
            self._send_json(test_model_config(payload))
        elif path == "/api/client-error":
            log_error(
                "frontend",
                str(payload.get("message") or "unknown"),
                f"{payload.get('source') or ''}:{payload.get('line') or ''} {str(payload.get('stack') or '')[:1500]}",
            )
            self._send_json({"ok": True})
        else:
            self.send_response(404)
            self.end_headers()

    def _validated_mission_id(self, value: Any) -> str | None:
        try:
            return validate_mission_id(str(value or ""))
        except ValueError:
            self._send_json({"ok": False, "error": "mission_id 格式无效。"}, status=400)
            return None

    def _api_request_block_reason(self, path: str, *, require_json: bool) -> str:
        if not self._host_is_loopback():
            return "Blocked request Host; Pacer API only accepts loopback hosts."
        fetch_site = str(self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if fetch_site == "cross-site":
            return "Blocked cross-site browser request."
        origin = str(self.headers.get("Origin") or "").strip()
        if origin and not self._origin_is_allowed(origin):
            return "Blocked request Origin."
        referer = str(self.headers.get("Referer") or "").strip()
        if referer and not self._origin_is_allowed(referer):
            return "Blocked request Referer."
        if require_json and not self._content_type_is_json():
            return "API requests must use Content-Type: application/json."
        return ""

    def _content_type_is_json(self) -> bool:
        value = str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        return value == "application/json"

    def _host_is_loopback(self) -> bool:
        host = str(self.headers.get("Host") or "").strip()
        if not host:
            return False
        return _is_loopback_hostname(_hostname_from_netloc(host))

    def _origin_is_allowed(self, value: str) -> bool:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            return False
        host = str(parsed.hostname or "").strip()
        if not _is_loopback_hostname(host):
            return False
        expected_port = int(self.server.server_address[1])
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port == expected_port:
            return True
        return value.rstrip("/") in _extra_allowed_origins()

    def _allowed_cors_origin(self) -> str:
        origin = str(self.headers.get("Origin") or "").strip()
        return origin if origin and self._origin_is_allowed(origin) else ""


class _DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], workspace_root: Path):
        super().__init__(address, _DashboardHandler)
        self.workspace_root = workspace_root
        result = set_active_workspace(workspace_root)
        if not result.get("ok"):
            self.server_close()
            raise RuntimeError(str(result.get("error") or "无法激活 Dashboard 工作空间"))

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def _dashboard_event_signature(data: dict[str, Any]) -> str:
    relevant = {
        "missions": [
            {
                "id": item.get("mission_id"),
                "status": item.get("status"),
                "stop": item.get("stop_reason"),
                "updated": item.get("updated_at") or item.get("created_at"),
            }
            for item in data.get("missions", [])[:80]
        ],
        "launches": [
            {
                "id": item.get("launch_id"),
                "state": item.get("state"),
                "mission": item.get("mission_id"),
                "error": item.get("error"),
            }
            for item in data.get("launches", [])[:30]
        ],
        "queue": [
            {
                "id": item.get("mission_id"),
                "status": item.get("status"),
                "stop": item.get("stop_reason"),
            }
            for item in data.get("queue", [])[:80]
        ],
        "traces": [
            {
                "kind": item.get("kind"),
                "status": item.get("status"),
                "title": item.get("title"),
                "timestamp": item.get("timestamp"),
            }
            for item in data.get("work_traces", [])[:40]
        ],
        "worker": data.get("worker") or {},
        "quota": (data.get("subscription_quota") or {}).get("summary") or {},
    }
    return str(abs(hash(json.dumps(relevant, ensure_ascii=False, sort_keys=True, default=str))))


def _bind_dashboard_server(host: str, port: int, root: Path) -> _DashboardServer:
    if not _is_loopback_hostname(host):
        raise ValueError("Dashboard 仅允许绑定 localhost、127.0.0.1 或 ::1；远程访问请使用受认证的 Cloud API。")
    candidates = [port, 8787, 8080, 8899, 9797, 0]
    seen: set[int] = set()
    last_error: Exception | None = None
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            return _DashboardServer((host, candidate), root)
        except (PermissionError, OSError) as exc:
            last_error = exc
            continue
    raise RuntimeError(f"Could not bind the dashboard to any port on {host}: {last_error}")


def _hostname_from_netloc(value: str) -> str:
    parsed = urlparse(f"//{value}")
    return str(parsed.hostname or value.split(":", 1)[0]).strip("[]").lower()


def _is_loopback_hostname(host: str) -> bool:
    value = str(host or "").strip("[]").lower()
    if value in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _extra_allowed_origins() -> set[str]:
    raw = os.environ.get("PACER_DASHBOARD_ALLOWED_ORIGINS", "")
    return {item.strip().rstrip("/") for item in raw.split(",") if item.strip()}


def serve_dashboard(*, workspace_root: str | Path, host: str = "127.0.0.1", port: int = 8787, open_browser: bool = True) -> None:
    root = Path(workspace_root).expanduser().resolve()
    server = _bind_dashboard_server(host, port, root)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/"
    if actual_port != port:
        print(f"Port {port} was unavailable; using {actual_port} instead.")
    print(f"DevPacer dashboard: {url}")
    print(f"Workspace: {root}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
