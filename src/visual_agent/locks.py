from __future__ import annotations

import json
import os
import socket
import warnings
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep, time
from uuid import uuid4


@dataclass(frozen=True)
class RunLockInfo:
    lock_id: str
    path: Path
    owner: str
    created_at: float
    expires_at: float
    pid: int | None = None
    host: str | None = None
    cwd: str | None = None


@dataclass(frozen=True)
class RunQueueInfo:
    enabled: bool
    waited_seconds: float
    attempts: int
    timeout_seconds: float
    poll_seconds: float


class RunLock:
    def __init__(self, root: str | Path, *, name: str = "workflow.lock", ttl_seconds: float = 3600.0) -> None:
        self.root = Path(root)
        self.path = self.root / name
        self.ttl_seconds = ttl_seconds
        self.info: RunLockInfo | None = None

    def acquire_with_wait(
        self,
        *,
        owner: str,
        wait_seconds: float = 0.0,
        poll_seconds: float = 0.5,
    ) -> tuple[RunLockInfo, RunQueueInfo]:
        started = monotonic()
        attempts = 0
        wait_seconds = max(0.0, wait_seconds)
        poll_seconds = max(0.01, poll_seconds)

        while True:
            attempts += 1
            try:
                info = self.acquire(owner=owner)
                waited = monotonic() - started
                return info, RunQueueInfo(
                    enabled=wait_seconds > 0,
                    waited_seconds=round(waited, 6),
                    attempts=attempts,
                    timeout_seconds=wait_seconds,
                    poll_seconds=poll_seconds,
                )
            except RuntimeError:
                if wait_seconds <= 0 or monotonic() - started >= wait_seconds:
                    raise
                sleep(min(poll_seconds, max(0.0, wait_seconds - (monotonic() - started))))

    def acquire(self, *, owner: str) -> RunLockInfo:
        self.root.mkdir(parents=True, exist_ok=True)
        now = time()
        current = read_lock(self.path)
        if current is not None and current.expires_at > now:
            raise RuntimeError(
                f"Run lock is active: owner={current.owner}, expires_at={current.expires_at}, path={current.path}"
            )
        if current is not None:
            self.release_stale()

        info = RunLockInfo(
            lock_id=uuid4().hex,
            path=self.path,
            owner=owner,
            created_at=now,
            expires_at=now + self.ttl_seconds,
            pid=os.getpid(),
            host=socket.gethostname(),
            cwd=str(Path.cwd()),
        )
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(str(self.path), flags)
        except FileExistsError as exc:
            current = read_lock(self.path)
            detail = f" owner={current.owner}, expires_at={current.expires_at}" if current else ""
            raise RuntimeError(f"Run lock is active:{detail}, path={self.path}") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(lock_to_dict(info), handle, ensure_ascii=False, indent=2)
        self.info = info
        return info

    def release(self) -> None:
        if self.info is None:
            return
        current = read_lock(self.path)
        if current is not None and current.lock_id == self.info.lock_id:
            unlink_with_retries(self.path)
        self.info = None

    def release_stale(self) -> None:
        current = read_lock(self.path)
        if current is not None and current.expires_at <= time():
            unlink_with_retries(self.path)

    def __enter__(self) -> "RunLock":
        raise RuntimeError("Use acquire(owner=...) before entering RunLock.")

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


def read_lock(path: str | Path) -> RunLockInfo | None:
    lock_path = Path(path)
    if not lock_path.exists():
        return None
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        return RunLockInfo(
            lock_id=str(payload["lock_id"]),
            path=lock_path,
            owner=str(payload["owner"]),
            created_at=float(payload["created_at"]),
            expires_at=float(payload["expires_at"]),
            pid=int(payload["pid"]) if payload.get("pid") is not None else None,
            host=str(payload["host"]) if payload.get("host") is not None else None,
            cwd=str(payload["cwd"]) if payload.get("cwd") is not None else None,
        )
    except PermissionError as exc:
        warnings.warn(
            f"Lock file at {lock_path} is temporarily unreadable and will be treated as active: {exc}",
            stacklevel=2,
        )
        return RunLockInfo(
            lock_id="unknown",
            path=lock_path,
            owner="unreadable",
            created_at=0.0,
            expires_at=time() + 1.0,
            pid=None,
            host=None,
            cwd=None,
        )
    except Exception as exc:
        warnings.warn(
            f"Lock file at {lock_path} is unreadable (will be treated as stale and removed): {exc}",
            stacklevel=2,
        )
        try:
            lock_path.unlink()
        except OSError:
            pass
        return None


def lock_to_dict(info: RunLockInfo) -> dict[str, object]:
    return {
        "lock_id": info.lock_id,
        "path": str(info.path),
        "owner": info.owner,
        "created_at": info.created_at,
        "expires_at": info.expires_at,
        "pid": info.pid,
        "host": info.host,
        "cwd": info.cwd,
    }


def queue_to_dict(info: RunQueueInfo) -> dict[str, object]:
    return {
        "enabled": info.enabled,
        "waited_seconds": info.waited_seconds,
        "attempts": info.attempts,
        "timeout_seconds": info.timeout_seconds,
        "poll_seconds": info.poll_seconds,
    }


def unlink_with_retries(path: Path, *, attempts: int = 5, delay_seconds: float = 0.02) -> None:
    for attempt in range(attempts):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            sleep(delay_seconds)
