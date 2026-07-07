from __future__ import annotations

from visual_agent.model_router import route_task, tier_task_kind


def test_mechanical_small_task_routes_cheap() -> None:
    d = route_task(objective="Rename the variable and fix a typo in the header", changed_files=["src/ui/header.tsx"])
    assert d.tier == "cheap"


def test_reasoning_heavy_task_routes_strong() -> None:
    d = route_task(objective="Refactor the auth module for concurrency safety", changed_files=["src/auth.py"])
    assert d.tier == "strong"


def test_chinese_mechanical_routes_cheap() -> None:
    d = route_task(objective="把首页标题的文案从 Hello 改成 Welcome", changed_files=["web/index.html"])
    assert d.tier == "cheap"


def test_chinese_architecture_routes_strong() -> None:
    d = route_task(objective="重构订单模块的架构", changed_files=["src/order.py"])
    assert d.tier == "strong"


def test_broad_change_escalates_to_strong() -> None:
    d = route_task(objective="update copy text", changed_files=[f"src/f{i}.py" for i in range(7)])
    assert d.tier == "strong"  # many files beats the cheap keyword


def test_repeated_failure_escalates_to_strong() -> None:
    d = route_task(objective="fix typo", changed_files=["a.py"], repeated_failure=True)
    assert d.tier == "strong"


def test_default_is_standard() -> None:
    d = route_task(objective="Add a discount field to the checkout total", changed_files=["src/checkout.py"])
    assert d.tier == "standard"


def test_workspace_artifacts_do_not_count_as_files() -> None:
    d = route_task(objective="rename label", changed_files=["src/x.py", ".agent-workspace/runs/r/report.json"])
    assert d.signals["product_file_count"] == 1
    assert d.tier == "cheap"


def test_tier_task_kind_mapping() -> None:
    assert tier_task_kind("cheap") == "fast"
    assert tier_task_kind("standard") == "balanced"
    assert tier_task_kind("strong") == "implementation"
