from __future__ import annotations

import json

from visual_agent.goal_grounding import (
    discover_plan_documents,
    extract_pending_items,
    goal_references_plan,
    ground_goal,
    grounding_to_markdown,
)


def test_goal_references_plan_detects_plan_citations():
    assert goal_references_plan("我要你参照最新的开发计划，继续给我推进开发")
    assert goal_references_plan("按照计划推进")
    assert goal_references_plan("follow the plan and continue")
    assert goal_references_plan("continue with the roadmap")


def test_goal_references_plan_ignores_normal_goals():
    assert not goal_references_plan("继续开发登录页，加「记住我」勾选框")
    assert not goal_references_plan("修复结算页金额显示")
    assert not goal_references_plan("add a logout button")


def seed_repo(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "roadmap_2026.md").write_text(
        "# 路线图\n\n## 剩余待办\n\n- [ ] 加登录页「记住我」勾选框\n- [ ] 修复结算页金额\n- [x] 已完成的事\n",
        encoding="utf-8",
    )
    (docs / "notes.md").write_text("# 随手记\n没什么计划。\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    return tmp_path


def test_discover_plan_documents_ranks_plan_names_first(tmp_path):
    seed_repo(tmp_path)
    docs = discover_plan_documents(tmp_path)
    rels = [item["rel_path"] for item in docs]
    assert rels[0] == "docs/roadmap_2026.md"
    assert "README.md" in rels
    # A plan-named file outranks a random note even if the note is newer.
    assert rels.index("docs/roadmap_2026.md") < rels.index("docs/notes.md")


def test_discover_plan_documents_empty_repo(tmp_path):
    assert discover_plan_documents(tmp_path) == []
    assert discover_plan_documents(tmp_path / "missing") == []


def test_extract_pending_items_checkboxes_and_sections():
    text = (
        "# Plan\n\n- [ ] task one\n- [x] done task\n\n## 下一步\n\n- section item\n1. numbered item\n\n## 其他\n\n- ignored\n"
    )
    items = extract_pending_items(text)
    assert "task one" in items
    assert "section item" in items
    assert "numbered item" in items
    assert "done task" not in items
    assert "ignored" not in items


def test_ground_goal_deterministic_reports_documents_without_resolving(tmp_path):
    seed_repo(tmp_path)
    payload = ground_goal("按照最新的开发计划继续推进", repo_root=tmp_path, enable_model=False)
    assert payload["resolved"] is False
    assert payload["source"] == "deterministic"
    assert payload["plan_document"] == "docs/roadmap_2026.md"
    assert any("记住我" in item for item in payload["pending_items"])
    assert payload["questions"]


def test_ground_goal_deterministic_no_documents_proposes_discussion(tmp_path):
    payload = ground_goal("按照计划开发", repo_root=tmp_path, enable_model=False)
    assert payload["resolved"] is False
    assert payload["documents_reviewed"] == []
    assert any("计划" in q for q in payload["questions"])


def test_ground_goal_model_resolves_next_task(tmp_path):
    seed_repo(tmp_path)

    def fake_call(*, goal, documents, timeout_seconds):
        assert any(doc["rel_path"] == "docs/roadmap_2026.md" for doc in documents)
        return json.dumps(
            {
                "found": True,
                "plan_document": "docs/roadmap_2026.md",
                "next_task_goal": "给登录页加「记住我」勾选框，登录后勾选状态保持",
                "acceptance_hint": "pytest -q 通过且登录页出现记住我勾选框",
                "evidence": "- [ ] 加登录页「记住我」勾选框",
            }
        )

    payload = ground_goal("参照最新的开发计划继续开发", repo_root=tmp_path, model_call=fake_call)
    assert payload["resolved"] is True
    assert payload["source"] == "model"
    assert payload["plan_document"] == "docs/roadmap_2026.md"
    assert "记住我" in payload["grounded_goal"]
    assert payload["acceptance_hint"]
    assert payload["evidence"]


def test_ground_goal_model_not_found_returns_proposed_plan(tmp_path):
    def fake_call(*, goal, documents, timeout_seconds):
        return (
            'Sure: {"found": false, "plan_document": "", "next_task_goal": "", '
            '"acceptance_hint": "", "evidence": "", '
            '"proposed_plan": ["先定验收命令", "实现登录"], "questions": ["先做哪个功能？"]}'
        )

    payload = ground_goal("按照计划开发", repo_root=tmp_path, model_call=fake_call)
    assert payload["resolved"] is False
    assert payload["source"] == "model"
    assert payload["proposed_plan"] == ["先定验收命令", "实现登录"]
    assert payload["questions"] == ["先做哪个功能？"]


def test_ground_goal_model_failure_degrades(tmp_path):
    seed_repo(tmp_path)

    def broken_call(*, goal, documents, timeout_seconds):
        raise RuntimeError("network down")

    payload = ground_goal("按照计划开发", repo_root=tmp_path, model_call=broken_call)
    assert payload["resolved"] is False
    assert payload["source"] == "deterministic"
    assert "network down" in payload["model_error"]
    # The deterministic scan still surfaced the documents.
    assert payload["plan_document"] == "docs/roadmap_2026.md"


def test_ground_goal_without_backend_stays_deterministic(tmp_path, monkeypatch):
    # conftest already points CHECKPOINT_MODEL_CREDENTIALS at a missing file, so
    # enable_model=True must not attempt any network call.
    seed_repo(tmp_path)
    payload = ground_goal("按照计划开发", repo_root=tmp_path, enable_model=True)
    assert payload["source"] == "deterministic"
    assert payload["resolved"] is False


def test_grounding_markdown_resolved_and_unresolved(tmp_path):
    seed_repo(tmp_path)
    resolved = {
        "resolved": True,
        "documents_reviewed": ["docs/roadmap_2026.md"],
        "plan_document": "docs/roadmap_2026.md",
        "grounded_goal": "加「记住我」勾选框",
        "acceptance_hint": "pytest 通过",
        "evidence": "- [ ] 加登录页「记住我」勾选框",
    }
    text = grounding_to_markdown(resolved)
    assert "计划审查" in text
    assert "落地为具体任务" in text
    assert "记住我" in text

    unresolved = ground_goal("按照计划开发", repo_root=tmp_path, enable_model=False)
    text = grounding_to_markdown(unresolved)
    assert "计划审查" in text
    assert "docs/roadmap_2026.md" in text
    assert "需要和你确认" in text
