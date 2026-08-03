"""Legacy dashboard module — backward-compatible shim.

This file re-exports all public names from the refactored dashboard package
so that existing imports (``from .dashboard import serve_dashboard``) continue
to work without changes.
"""

from .dashboard import DASHBOARD_HTML, _bind_dashboard_server, serve_dashboard
from .dashboard.api import (
    build_mission_detail,
    start_workbench_mission,
    get_active_workspace,
    set_active_workspace,
    list_known_workspaces,
    record_launch,
    retry_mission,
    run_chat,
    get_notifications_config,
    save_notifications_config,
    test_notification,
    build_diagnostic_bundle,
    start_worker,
    stop_worker,
    log_error,
)
from .dashboard.data import (
    build_dashboard_data_cached as build_dashboard_data,
    build_dashboard_data_cached,
    get_agents_cached,
    _launch_snapshot,
    worker_status,
)

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
    "get_notifications_config",
    "save_notifications_config",
    "test_notification",
    "build_diagnostic_bundle",
    "start_worker",
    "stop_worker",
    "log_error",
    "build_dashboard_data_cached",
    "get_agents_cached",
    "_launch_snapshot",
    "worker_status",
]
