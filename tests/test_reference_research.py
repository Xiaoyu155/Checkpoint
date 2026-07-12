from __future__ import annotations

from pathlib import Path

from visual_agent.reference_research import build_reference_pack, reference_pack_to_markdown, save_reference_pack
from visual_agent.workspace import init_workspace


def test_reference_pack_generates_framework_keywords(tmp_path) -> None:
    (tmp_path / "pubspec.yaml").write_text("name: demo\n", encoding="utf-8")

    pack = build_reference_pack(objective="Flutter 语音悬浮窗历史记录", repo_root=tmp_path, task_id="task-001")

    assert pack["task_id"] == "task-001"
    assert any("Flutter" in item for item in pack["search_keywords"])
    assert "copy wholesale" in " ".join(pack["source_policy"])


def test_save_reference_pack_writes_markdown(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    pack = build_reference_pack(objective="Implement login", repo_root=tmp_path, task_id="task-001")

    saved = save_reference_pack(workspace_root=workspace.root, program_id="program-1", pack=pack)

    assert Path(saved["path"]).exists()
    assert "Reference Pack" in reference_pack_to_markdown(pack)
