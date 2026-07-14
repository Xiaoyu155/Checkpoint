"""Checkpoint — local-first visual automation SDK for AI coding agents."""

from .capture import Screenshot
from .locks import VisualLock
from .models import (
    ActionResult,
    ActionStatus,
    Bounds,
    LocationEvidence,
    Observation,
    Point,
    ProviderKind,
    ResolvedTarget,
    Target,
)
from .sdk import VisualSession
from .workflow import WorkflowRuntime, parse_workflow_file

__all__ = [
    "VisualSession",
    "ActionResult",
    "ActionStatus",
    "Bounds",
    "LocationEvidence",
    "Observation",
    "Point",
    "ProviderKind",
    "ResolvedTarget",
    "Target",
    "Screenshot",
    "VisualLock",
    "WorkflowRuntime",
    "parse_workflow_file",
    "__version__",
]

__version__ = "0.1.2"

