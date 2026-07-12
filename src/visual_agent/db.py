from __future__ import annotations

import sqlite3
from pathlib import Path


def open_workspace_db(workspace_root: Path) -> sqlite3.Connection:
    db_path = workspace_root / "agent.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS queue_tasks (
            task_id     TEXT PRIMARY KEY,
            workflow    TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            priority    INTEGER NOT NULL DEFAULT 0,
            run_profile TEXT NOT NULL DEFAULT 'dry-run',
            dry_run     INTEGER NOT NULL DEFAULT 1,
            inputs_json TEXT,
            inputs_file TEXT,
            metadata_json TEXT,
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL,
            attempts    INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 0,
            last_run_id TEXT,
            last_error  TEXT
        );

        CREATE TABLE IF NOT EXISTS queue_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id      TEXT NOT NULL,
            workflow     TEXT NOT NULL,
            status       TEXT NOT NULL,
            event        TEXT NOT NULL,
            attempts     INTEGER NOT NULL DEFAULT 0,
            run_profile  TEXT,
            completed_at REAL NOT NULL,
            last_run_id  TEXT,
            last_error   TEXT
        );
        """
    )
    conn.commit()
