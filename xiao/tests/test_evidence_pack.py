from __future__ import annotations

from visual_agent.chief_dispatch import chief_dispatch_to_markdown
from visual_agent.evidence_pack import (
    build_evidence_pack,
    evidence_markdown_lines,
    humanize_behavior_name,
    parse_checked_behaviors,
)

# Real output shape from `node --import ./test/setup.js --test`, including the
# file-level result the runner also emits.
NODE_TAP = """
TAP version 13
# Subtest: test\\birthdayCard.test.js
    ok 1 - a pet without a usable birth date gets no card
    ok 2 - a birthday card never carries medical advice
    not ok 3 - the card shows the pet's age
ok 1 - test\\birthdayCard.test.js
1..1
# tests 3
"""

PYTEST_V = """
xiao/tests/test_a.py::test_the_cart_totals_include_tax PASSED             [ 50%]
xiao/tests/test_a.py::test_an_empty_cart_shows_a_hint FAILED              [100%]
"""


def test_behaviors_are_read_out_of_a_real_node_run() -> None:
    behaviors = parse_checked_behaviors(NODE_TAP)

    names = [item["name"] for item in behaviors]
    assert "A pet without a usable birth date gets no card" in names
    assert "A birthday card never carries medical advice" in names
    assert [item["status"] for item in behaviors if "age" in item["name"]] == ["fail"]


def test_the_test_file_itself_is_not_reported_as_a_behavior() -> None:
    # "test\\birthdayCard.test.js" is not a sentence about the product, and a
    # person reading the report would have no idea what to do with it.
    assert all(".js" not in item["name"] for item in parse_checked_behaviors(NODE_TAP))


def test_pytest_names_become_sentences() -> None:
    behaviors = parse_checked_behaviors(PYTEST_V)

    assert behaviors[0] == {"name": "The cart totals include tax", "status": "pass"}
    assert behaviors[1] == {"name": "An empty cart shows a hint", "status": "fail"}


def test_go_and_jest_shapes_are_understood() -> None:
    go = parse_checked_behaviors("--- PASS: TestUserCanLogIn (0.01s)\n--- FAIL: TestUserSeesError (0.02s)")
    jest = parse_checked_behaviors("  ✓ renders the empty state (12 ms)\n  ✗ saves the draft")

    assert go[0]["name"] == "User can log in" and go[0]["status"] == "pass"
    assert go[1]["status"] == "fail"
    assert jest[0]["name"] == "Renders the empty state"
    assert jest[1]["status"] == "fail"


def test_humanize_handles_the_common_identifier_shapes() -> None:
    assert humanize_behavior_name("test_a_change_is_caught") == "A change is caught"
    assert humanize_behavior_name("TestUserCanLogIn") == "User can log in"
    assert humanize_behavior_name("test_totals[with-tax]") == "Totals（with-tax）"
    assert humanize_behavior_name("已经是人话的名字") == "已经是人话的名字"


def test_the_report_states_what_it_cannot_prove() -> None:
    pack = build_evidence_pack(objective="x", command="npm test", output=NODE_TAP, exit_code=1)

    text = "\n".join(evidence_markdown_lines(pack))

    # The whole point is a person judging the product, so the report must not
    # let "the checks passed" masquerade as "this is the feature you wanted".
    assert "不能说明" in text
    assert "这功能是不是你真正想要的那个" in text
    assert "❌" in text


def test_a_report_with_no_named_behaviors_says_so_instead_of_pretending() -> None:
    pack = build_evidence_pack(objective="x", command="npm test", output="....\n4 passed", exit_code=0)

    text = "\n".join(evidence_markdown_lines(pack))

    assert pack["readable"] is False
    assert "你只能选择信或不信" in text


def test_no_output_at_all_renders_nothing() -> None:
    assert evidence_markdown_lines(None) == []
    assert evidence_markdown_lines(build_evidence_pack(objective="x", command="", output="")) == []


def test_delivery_confirmation_is_mentioned_only_when_it_was_actually_checked() -> None:
    checked = build_evidence_pack(
        objective="x", command="npm test", output=NODE_TAP, delivery_status="delivered"
    )
    unchecked = build_evidence_pack(objective="x", command="npm test", output=NODE_TAP)

    assert "干净环境里也跑得起来" in "\n".join(evidence_markdown_lines(checked))
    assert "干净环境里也跑得起来" not in "\n".join(evidence_markdown_lines(unchecked))


def test_a_translator_can_put_the_behaviors_in_the_reader_s_language() -> None:
    pack = build_evidence_pack(
        objective="x",
        command="npm test",
        output=PYTEST_V,
        translator=lambda names: ["购物车合计含税", "空购物车会给出提示"],
    )

    assert [item["name"] for item in pack["confirmed"]] == ["购物车合计含税"]
    assert [item["name"] for item in pack["failed"]] == ["空购物车会给出提示"]


def test_a_broken_translator_leaves_the_evidence_intact() -> None:
    def explode(_names: list[str]) -> list[str]:
        raise RuntimeError("provider down")

    pack = build_evidence_pack(objective="x", command="npm test", output=PYTEST_V, translator=explode)

    assert pack["confirmed"][0]["name"] == "The cart totals include tax"


def test_a_translator_returning_the_wrong_number_of_lines_is_ignored() -> None:
    # Pairing a behavior with someone else's sentence would tell the reader a
    # thing was checked that never was.
    pack = build_evidence_pack(
        objective="x", command="npm test", output=PYTEST_V, translator=lambda names: ["只有一句"]
    )

    assert pack["confirmed"][0]["name"] == "The cart totals include tax"


def test_the_evidence_reaches_the_dispatch_report() -> None:
    pack = build_evidence_pack(objective="x", command="npm test", output=NODE_TAP)

    markdown = chief_dispatch_to_markdown({"status": "verified", "completion_evidence": pack})

    assert "我替你确认了什么" in markdown
    assert "A birthday card never carries medical advice" in markdown
