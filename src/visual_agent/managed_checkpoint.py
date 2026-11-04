"""Pacer Managed Checkpoint & Stateful Resumption Engine.

Provides persistent state tracking, crash-resilience, and zero-loss
resumption for long-running autonomous multi-wave DAG executions:
- Persists to `.pacer/managed_checkpoint.json`
- Supports task-level and wave-level resumption via `--resume`
- Validates workspace root, goal consistency, and AST integrity
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CHECKPOINT_FILENAME = "managed_checkpoint.json"
CHECKPOINT_SCHEMA_VERSION = 4


@dataclass
class ManagedCheckpoint:
    checkpoint_id: str
    goal: str
    workspace_root: str
    status: str = "in_progress"  # "in_progress", "completed", "failed"
    interruption_reason: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    current_wave_index: int = 0
    total_waves: int = 1
    completed_task_ids: list[str] = field(default_factory=list)
    failed_task_ids: list[str] = field(default_factory=list)
    pending_task_ids: list[str] = field(default_factory=list)
    patched_files: list[str] = field(default_factory=list)
    task_states: dict[str, Any] = field(default_factory=dict)
    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    dogfood_evidence: dict[str, Any] = field(default_factory=dict)
    plan_goal_hash: str = ""  # SHA-256 of the original plan goal for identity binding
    plan_task_count: int = 0  # Number of tasks in the original plan

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ManagedCheckpoint:
        fields = {
            k: v
            for k, v in data.items()
            if k in cls.__dataclass_fields__
        }
        return cls(**fields)


def get_checkpoint_path(workspace_root: str | Path) -> Path:
    """Return the absolute path to the workspace's managed checkpoint file."""
    root = Path(workspace_root).resolve()
    pacer_dir = root / ".pacer"
    pacer_dir.mkdir(parents=True, exist_ok=True)
    return pacer_dir / CHECKPOINT_FILENAME


def save_managed_checkpoint(
    workspace_root: str | Path,
    checkpoint: ManagedCheckpoint | dict[str, Any],
) -> Path:
    """Atomically save checkpoint to `.pacer/managed_checkpoint.json`."""
    path = get_checkpoint_path(workspace_root)
    if isinstance(checkpoint, ManagedCheckpoint):
        checkpoint.updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        payload = checkpoint.to_dict()
    else:
        payload = dict(checkpoint)
        payload["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    temp_path = path.with_suffix(".tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        temp_path.replace(path)
        logger.debug(f"[ManagedCheckpoint] Saved checkpoint to {path}")
    except Exception as exc:
        logger.warning(f"[ManagedCheckpoint] Failed to write checkpoint: {exc}")
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
    return path


def load_managed_checkpoint(
    workspace_root: str | Path,
    expected_goal: str = "",
) -> ManagedCheckpoint | None:
    """Load and validate the managed checkpoint if present.

    Strict identity binding:
    - Workspace root must match exactly (not just a warning).
    - Goal must match (via hash comparison when available, else substring).
    A mismatch returns None — different workspace or different goal means
    the checkpoint belongs to a different task and must not be resumed.
    """
    path = get_checkpoint_path(workspace_root)
    if not path.is_file():
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None

        chk = ManagedCheckpoint.from_dict(data)

        # Strict workspace check — mismatch means different project
        chk_root = Path(chk.workspace_root).resolve()
        curr_root = Path(workspace_root).resolve()
        if chk_root != curr_root:
            logger.warning(
                f"[ManagedCheckpoint] Workspace mismatch: {chk_root} vs {curr_root}. "
                "Refusing to resume — checkpoint belongs to a different workspace."
            )
            return None

        # Strict goal check — mismatch means different task
        if expected_goal:
            # Use hash comparison when available (more reliable)
            import hashlib
            expected_hash = hashlib.sha256(expected_goal.encode("utf-8")).hexdigest()
            if chk.plan_goal_hash and chk.plan_goal_hash != expected_hash:
                logger.warning(
                    f"[ManagedCheckpoint] Goal hash mismatch. "
                    "Refusing to resume — checkpoint belongs to a different goal."
                )
                return None
            # Fallback: substring matching for older checkpoints without hash
            if not chk.plan_goal_hash:
                s1 = expected_goal.strip().lower()
                s2 = chk.goal.strip().lower()
                if s1 not in s2 and s2 not in s1:
                    logger.warning(
                        f"[ManagedCheckpoint] Goal differs: '{chk.goal}' vs '{expected_goal}'. "
                        "Refusing to resume — checkpoint belongs to a different goal."
                    )
                    return None

        return chk
    except Exception as exc:
        logger.warning(f"[ManagedCheckpoint] Error reading checkpoint: {exc}")
        return None


def clear_managed_checkpoint(workspace_root: str | Path) -> bool:
    """Remove the active checkpoint file after successful plan completion."""
    path = get_checkpoint_path(workspace_root)
    if path.is_file():
        try:
            path.unlink()
            return True
        except Exception as exc:
            logger.warning(f"[ManagedCheckpoint] Could not delete checkpoint {path}: {exc}")
    return False


def is_checkpoint_resumable(checkpoint: ManagedCheckpoint | None) -> bool:
    """Return True if checkpoint has pending or failed tasks that can be resumed."""
    if not checkpoint:
        return False
    if checkpoint.status == "completed":
        return False
    if checkpoint.pending_task_ids or checkpoint.failed_task_ids:
        return True
    return False
