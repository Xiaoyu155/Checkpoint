import pytest

from visual_agent.canvas_manager import CanvasManager


def test_record_read_and_deduplicate(tmp_path):
    manager = CanvasManager(tmp_path)

    assert manager.read_lessons() == []
    assert manager.record_verified_lesson("testing", "Verify the result", ["src/app.py"])
    assert not manager.record_verified_lesson("testing", "Verify the result", ["other.py"])
    assert manager.read_lessons() == ["testing Verify the result"]
    assert (tmp_path / ".pacer" / "lessons.md").read_text(encoding="utf-8").count("<!--") == 1


def test_filters_paths_and_credentials(tmp_path):
    manager = CanvasManager(tmp_path)
    manager.record_verified_lesson(
        "security",
        "Inspect src/private.py and C:\\Users\\alice\\keys.txt "
        "api_key=supersecret Bearer abcdef sk-123456",
        ["src/private.py"],
    )

    stored = (tmp_path / ".pacer" / "lessons.md").read_text(encoding="utf-8")
    for sensitive in ("src/private.py", "alice", "supersecret", "abcdef", "sk-123456"):
        assert sensitive not in stored
    assert "[path]" in stored
    assert "[redacted]" in stored


def test_limits_the_whole_lesson_to_fifty_words(tmp_path):
    manager = CanvasManager(tmp_path)
    manager.record_verified_lesson("testing", " ".join(f"word{i}" for i in range(70)), [])

    assert len(manager.read_lessons()[0].split()) == 50


def test_empty_fields_are_rejected(tmp_path):
    manager = CanvasManager(tmp_path)
    with pytest.raises(ValueError):
        manager.record_verified_lesson(" ", "rule", [])
    with pytest.raises(ValueError):
        manager.record_verified_lesson("category", " ", [])


def test_existing_markdown_is_preserved(tmp_path):
    path = tmp_path / ".pacer" / "lessons.md"
    path.parent.mkdir()
    path.write_text("# Lessons", encoding="utf-8")
    manager = CanvasManager(tmp_path)

    manager.record_verified_lesson("tests", "Check outputs", [])

    assert path.read_text(encoding="utf-8").startswith("# Lessons\n")
    assert manager.read_lessons() == ["tests Check outputs"]
