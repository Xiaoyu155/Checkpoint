import json
import warnings
from threading import Thread
from time import time
from time import sleep as sleep_seconds

from visual_agent.locks import RunLock, RunLockInfo, lock_to_dict, read_lock


def test_run_lock_blocks_second_owner_until_release(tmp_path) -> None:
    first = RunLock(tmp_path)
    first_info = first.acquire(owner="first")

    second = RunLock(tmp_path)
    try:
        second.acquire(owner="second")
    except RuntimeError as exc:
        assert "Run lock is active" in str(exc)
    else:
        raise AssertionError("Expected active lock to block second owner.")

    assert read_lock(first_info.path).owner == "first"
    first.release()
    assert read_lock(first_info.path) is None


def test_run_lock_replaces_stale_lock(tmp_path) -> None:
    lock_path = tmp_path / "workflow.lock"
    stale = RunLockInfo(
        lock_id="stale",
        path=lock_path,
        owner="old",
        created_at=time() - 100,
        expires_at=time() - 1,
    )
    lock_path.write_text(json.dumps(lock_to_dict(stale)), encoding="utf-8")

    fresh = RunLock(tmp_path).acquire(owner="new")

    assert fresh.owner == "new"
    assert read_lock(lock_path).owner == "new"


def test_read_lock_deleted_between_exists_and_read_returns_none(tmp_path, monkeypatch) -> None:
    lock_path = tmp_path / "workflow.lock"
    lock_path.write_text("{}", encoding="utf-8")

    def missing_read_text(*_args, **_kwargs):
        raise FileNotFoundError(str(lock_path))

    monkeypatch.setattr(type(lock_path), "read_text", missing_read_text)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert read_lock(lock_path) is None

    assert caught == []


def test_run_lock_waits_until_release(tmp_path) -> None:
    first = RunLock(tmp_path)
    first.acquire(owner="first")

    def release_later() -> None:
        sleep_seconds(0.05)
        first.release()

    releaser = Thread(target=release_later)
    releaser.start()
    queued_lock = RunLock(tmp_path)
    try:
        fresh, queue = queued_lock.acquire_with_wait(
            owner="queued",
            wait_seconds=1.0,
            poll_seconds=0.01,
        )
    finally:
        releaser.join(timeout=1.0)

    assert fresh.owner == "queued"
    assert queue.enabled is True
    assert queue.attempts > 1
    assert queue.waited_seconds >= 0
    queued_lock.release()


def test_run_lock_wait_times_out(tmp_path) -> None:
    first = RunLock(tmp_path)
    first.acquire(owner="first")

    try:
        try:
            RunLock(tmp_path).acquire_with_wait(owner="queued", wait_seconds=0.02, poll_seconds=0.01)
        except RuntimeError as exc:
            assert "Run lock is active" in str(exc)
        else:
            raise AssertionError("Expected queued lock acquisition to time out.")
    finally:
        first.release()
