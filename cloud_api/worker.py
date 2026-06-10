from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from visual_agent.cloud_server import execute_cloud_run_request


def execute_workflow_run(request: dict[str, Any], *, workspace_root: str | Path = ".agent-workspace") -> dict[str, Any]:
    """Run a workflow with the same local semantics as the development cloud server."""

    server = SimpleNamespace(
        workspace_root=Path(workspace_root).resolve(),
        default_run_profile=str(request.get("run_profile") or "dry-run"),
        runs={},
        retention_max_reports=0,
        retention_days=0.0,
    )
    return execute_cloud_run_request(server, request)


try:
    from celery import Celery
except ImportError:
    celery_app = None
else:
    celery_app = Celery(
        "visual_agent_cloud",
        broker=os.environ.get("VISUAL_AGENT_CELERY_BROKER_URL", "redis://localhost:6379/0"),
        backend=os.environ.get("VISUAL_AGENT_CELERY_RESULT_BACKEND", "redis://localhost:6379/1"),
    )


if celery_app is not None:

    @celery_app.task(name="visual_agent_cloud.run_workflow")
    def run_workflow_task(request: dict[str, Any], workspace_root: str = ".agent-workspace") -> dict[str, Any]:
        return execute_workflow_run(request, workspace_root=workspace_root)

else:

    def run_workflow_task(request: dict[str, Any], workspace_root: str = ".agent-workspace") -> dict[str, Any]:
        return execute_workflow_run(request, workspace_root=workspace_root)
