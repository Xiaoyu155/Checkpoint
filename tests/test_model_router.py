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


def test_dirty_tree_small_offline_coverage_task_stays_standard() -> None:
    d = route_task(
        objective="Small offline-testable change: add coverage for JSON extraction.",
        changed_files=[f"src/stale_{index}.py" for index in range(35)],
    )

    assert d.tier == "standard"
    assert d.signals["product_file_count"] == 35
    assert "coverage" in d.signals["small_scope_terms"]


def test_repeated_failure_escalates_to_strong() -> None:
    d = route_task(objective="fix typo", changed_files=["a.py"], repeated_failure=True)
    assert d.tier == "strong"


def test_default_is_standard() -> None:
    d = route_task(objective="Add a discount field to the checkout total", changed_files=["src/checkout.py"])
    assert d.tier == "standard"


def test_exact_one_file_function_contract_routes_cheap() -> None:
    d = route_task(
        objective=(
            "Implement personalized_greeting(prefix, name) in greetings.py so it returns exactly "
            "Hello, <prefix> <name>!, preserves existing functions, and makes all tests pass."
        ),
        acceptance_criteria=["pytest passes"],
    )

    assert d.tier == "cheap"
    assert d.signals["objective_files"] == ["greetings.py"]
    assert d.signals["narrow_testable_contract"] is True


def test_workspace_artifacts_do_not_count_as_files() -> None:
    d = route_task(objective="rename label", changed_files=["src/x.py", ".agent-workspace/runs/r/report.json"])
    assert d.signals["product_file_count"] == 1
    assert d.tier == "cheap"


def test_tier_task_kind_mapping() -> None:
    assert tier_task_kind("cheap") == "fast"
    assert tier_task_kind("standard") == "balanced"
    assert tier_task_kind("strong") == "implementation"


def test_money_and_credential_work_is_never_routed_cheap() -> None:
    from visual_agent.model_router import route_task

    # "修复支付回调验签失败" matched no term and went to the balanced tier: a
    # cheap model quietly handling signature verification is the case this
    # ladder exists to prevent.
    for goal in (
        "修复支付回调验签失败的问题",
        "调整退款对账逻辑",
        "更新 API 密钥的读取方式",
        "fix the payment webhook signature check",
    ):
        assert route_task(objective=goal, changed_files=["a.py"]).tier == "strong", goal


def test_security_wins_even_when_the_edit_sounds_mechanical() -> None:
    from visual_agent.model_router import route_task

    decision = route_task(objective="修正支付金额提示语的错别字", changed_files=["a.py"])

    assert decision.tier == "strong"


def test_common_chinese_wording_for_mechanical_edits_routes_cheap() -> None:
    from visual_agent.model_router import route_task

    for goal in ("把 README 里的拼写错误改一下", "调整一下代码排版", "给这个常量换个名字"):
        assert route_task(objective=goal, changed_files=["a.py"]).tier == "cheap", goal
