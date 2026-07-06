"""HTTP server for the dashboard.

A zero-dependency, local-only web view served with the stdlib HTTP server.
Routes requests to API handlers in api.py and serves static files.
"""

from __future__ import annotations

import json
import sys
import webbrowser
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .api import (
    build_diagnostic_bundle,
    build_dashboard_data_cached,
    build_mission_detail,
    get_active_workspace,
    get_notifications_config,
    list_known_workspaces,
    log_error,
    retry_mission,
    run_chat,
    save_notifications_config,
    set_active_workspace,
    start_workbench_mission,
    start_worker,
    stop_worker,
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
        # 允许跨域请求，方便现代 React 前端直连后端 API
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        """处理浏览器的预检请求 (Preflight)"""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
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
        # HTML 文件不缓存，其他文件缓存 5 分钟
        if content_type.startswith("text/html"):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        else:
            self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        try:
            self._do_get_inner()
        except Exception as exc:
            log_error("backend", f"GET {self.path} failed", repr(exc))
            try:
                self._send_json({"ok": False, "error": f"服务器内部错误：{exc}"})
            except OSError:
                pass

    def _do_get_inner(self) -> None:
        path = self.path.split("?", 1)[0]
        root = get_active_workspace(self.server.workspace_root)

        # Static files
        if path == "/" or path == "/index.html":
            self._serve_static(_STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path == "/app.js":
            self._serve_static(_STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
            return
        if path == "/style.css":
            self._serve_static(_STATIC_DIR / "style.css", "text/css; charset=utf-8")
            return

        # API endpoints
        if path == "/api/data":
            self._send_json(build_dashboard_data_cached(root))
            return
        if path == "/api/diagnostic":
            self._send_json(build_diagnostic_bundle(root))
            return
        if path == "/api/mission":
            from urllib.parse import parse_qs, urlparse
            mission_id = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
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

        self.send_response(404)
        self.end_headers()

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
                self._send_json({"ok": False, "error": f"服务器内部错误：{exc}"})
            except OSError:
                pass

    def _do_post_inner(self) -> None:
        path = self.path.split("?", 1)[0]
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
                execute=bool(payload.get("execute")),
                merge_policy=str(payload.get("merge_policy") or "manual"),
                allow_dirty=bool(payload.get("allow_dirty")),
                spec=spec,
            )
            self._send_json(result)
        elif path == "/api/mission/merge":
            from ..workbench_board import merge_mission_now
            result = merge_mission_now(root, str(payload.get("mission_id") or ""))
            self._send_json(result)
        elif path == "/api/mission/delete":
            from ..workbench_board import archive_mission_now
            result = archive_mission_now(root, str(payload.get("mission_id") or ""))
            self._send_json(result)
        elif path == "/api/mission/delete-all":
            from ..workbench_board import archive_all_missions_now
            result = archive_all_missions_now(root)
            self._send_json(result)
        elif path == "/api/mission/retry":
            result = retry_mission(root, str(payload.get("mission_id") or ""))
            self._send_json(result)
        elif path == "/api/worker/start":
            self._send_json(start_worker(root))
        elif path == "/api/worker/stop":
            self._send_json(stop_worker())
        elif path == "/api/workspace/switch":
            self._send_json(set_active_workspace(str(payload.get("path") or "")))
        elif path == "/api/chat":
            self._send_json(run_chat(payload))
        elif path == "/api/notifications/config":
            self._send_json(save_notifications_config(payload))
        elif path == "/api/notifications/test":
            self._send_json(test_notification())
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


class _DashboardServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], workspace_root: Path):
        super().__init__(address, _DashboardHandler)
        self.workspace_root = workspace_root
        set_active_workspace(workspace_root)


def _bind_dashboard_server(host: str, port: int, root: Path) -> _DashboardServer:
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
