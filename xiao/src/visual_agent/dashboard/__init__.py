"""Dashboard module for Pacer.

Refactored from a monolithic dashboard.py into modular components:
- data.py: Caching layer for expensive operations
- api.py: API endpoint handlers
- server.py: HTTP server and routing
- static/: Frontend HTML/CSS/JS files
"""

from pathlib import Path

from .server import _bind_dashboard_server, serve_dashboard
from .api import (
    build_mission_detail,
    start_workbench_mission,
    get_active_workspace,
    set_active_workspace,
    list_known_workspaces,
    record_launch,
    retry_mission,
    run_chat,
    get_user_profile,
    save_profile_config,
    get_commercial_config,
    save_commercial_config,
    get_notifications_config,
    save_notifications_config,
    test_notification,
    build_diagnostic_bundle,
    build_five_pillars_data,
    start_worker,
    stop_worker,
    log_error,
)
from .data import (
    build_dashboard_data_cached as build_dashboard_data,
    build_dashboard_data_cached,
    get_agents_cached,
    _launch_snapshot,
    worker_status,
)
from .observability import (
    get_observability_launch,
    get_observability_timeline,
    list_observability_launches,
)


def _load_dashboard_html() -> str:
    static_dir = Path(__file__).parent / "static"
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    css = (static_dir / "style.css").read_text(encoding="utf-8")
    js = (static_dir / "app.js").read_text(encoding="utf-8")
    html = html.replace('<link rel="stylesheet" href="/style.css">', f"<style>\n{css}\n</style>")
    html = html.replace('<script src="/app.js"></script>', f"<script>\n{js}\n</script>")
    return html


DASHBOARD_HTML = _load_dashboard_html()

__all__ = [
    "serve_dashboard",
    "_bind_dashboard_server",
    "DASHBOARD_HTML",
    "build_dashboard_data",
    "build_mission_detail",
    "start_workbench_mission",
    "get_active_workspace",
    "set_active_workspace",
    "list_known_workspaces",
    "record_launch",
    "retry_mission",
    "run_chat",
    "get_user_profile",
    "save_profile_config",
    "get_commercial_config",
    "save_commercial_config",
    "get_notifications_config",
    "save_notifications_config",
    "test_notification",
    "build_diagnostic_bundle",
    "build_five_pillars_data",
    "start_worker",
    "stop_worker",
    "log_error",
    "build_dashboard_data_cached",
    "get_agents_cached",
    "_launch_snapshot",
    "worker_status",
    "list_observability_launches",
    "get_observability_launch",
    "get_observability_timeline",
]
