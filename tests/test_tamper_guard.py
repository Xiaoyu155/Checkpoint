from __future__ import annotations

from pathlib import Path

from visual_agent.tamper_guard import TamperGuard


def _workspace(root: Path) -> tuple[Path, Path]:
    source = root / "src" / "app.py"
    test = root / "tests" / "test_contract.py"
    source.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    source.write_text("def value():\n    return 1\n", encoding="utf-8")
    test.write_text("def test_value():\n    assert True\n", encoding="utf-8")
    return source, test


def test_frozen_suite_restores_modified_test_and_keeps_collection_ids(tmp_path):
    _source, test = _workspace(tmp_path)
    original = test.read_bytes()
    guard = TamperGuard(tmp_path)

    manifest = guard.freeze_acceptance_suite(collect_timeout_seconds=30)

    assert manifest["acceptance_frozen"] is True
    assert manifest["test_id_count"] == 1
    assert "tests/test_contract.py::test_value" in guard.baseline_test_ids
    with guard.temporary_unlock():
        test.write_text("import pytest\ndef test_value():\n    pytest.skip('cheat bait')\n", encoding="utf-8")

    intact, message, changed = guard.verify_and_enforce()

    assert not intact
    assert any("MODIFIED" in item for item in changed)
    assert "pytest collection IDs" not in message
    assert test.read_bytes() == original
    guard.unlock_files()


def test_static_escape_scanner_rejects_new_aliased_skip_call(tmp_path):
    source, _test = _workspace(tmp_path)
    guard = TamperGuard(tmp_path)
    guard.freeze_acceptance_suite(collect_timeout_seconds=30)

    source.write_text("import pytest as pt\npt.skip('escape')\n", encoding="utf-8")
    intact, _message, changed = guard.verify_and_enforce()

    assert not intact
    assert any("NEW ESCAPE pytest.skip" in item for item in changed)
    guard.unlock_files()


def test_frozen_collection_rejects_new_test_file(tmp_path):
    _workspace(tmp_path)
    guard = TamperGuard(tmp_path)
    guard.freeze_acceptance_suite(collect_timeout_seconds=30)
    (tmp_path / "tests" / "test_added.py").write_text(
        "def test_added():\n    assert True\n", encoding="utf-8"
    )

    intact, _message, changed = guard.verify_and_enforce()

    assert not intact
    assert any("ADDED TEST FILE" in item for item in changed)
    assert any("pytest collection IDs" in item for item in changed)
    guard.unlock_files()
