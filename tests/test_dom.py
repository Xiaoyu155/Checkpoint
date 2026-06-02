from visual_agent.dom import element_accessible_name, element_bounds, normalize_text
from visual_agent.models import Observation, ProviderKind, Target
from visual_agent.selector import SelectorResolver


def test_normalize_text_collapses_case_and_spaces() -> None:
    assert normalize_text("  Login   Now ") == "login now"


def test_element_accessible_name_uses_multiple_attributes() -> None:
    element = {
        "text": "",
        "label": "保存",
        "placeholder": "请输入客户",
        "role": "button",
    }

    assert element_accessible_name(element) == "保存 请输入客户"


def test_element_bounds_rejects_invisible_element() -> None:
    assert element_bounds({"bounds": {"left": 0, "top": 0, "width": 0, "height": 20}}) is None


def test_dom_selector_strategy_prefers_matching_dom_element() -> None:
    observation = Observation(
        provider=ProviderKind.DOM,
        source="https://example.test",
        width=1280,
        height=720,
        elements=(
            {
                "text": "取消",
                "role": "button",
                "selector": "#cancel",
                "bounds": {"left": 10, "top": 20, "width": 100, "height": 40},
            },
            {
                "text": "登录",
                "role": "button",
                "selector": "#login",
                "bounds": {"left": 300, "top": 200, "width": 120, "height": 48},
            },
        ),
    )

    resolved = SelectorResolver().resolve(Target(text="登录", role="button"), observation)

    assert resolved.evidence.provider == ProviderKind.DOM
    assert resolved.evidence.handle == "#login"
    assert resolved.click_point.x == 360
    assert resolved.click_point.y == 224


def test_dom_selector_strategy_resolves_explicit_selector() -> None:
    observation = Observation(
        provider=ProviderKind.DOM,
        source="https://example.test",
        width=1280,
        height=720,
        elements=(
            {
                "text": "保存",
                "role": "button",
                "selector": "#save",
                "bounds": {"left": 20, "top": 30, "width": 80, "height": 32},
            },
        ),
    )

    resolved = SelectorResolver().resolve(Target(selector="#save", preferred=(ProviderKind.DOM,)), observation)

    assert resolved.evidence.handle == "#save"
    assert resolved.evidence.confidence == 0.98


def test_dom_selector_strategy_resolves_test_id() -> None:
    observation = Observation(
        provider=ProviderKind.DOM,
        source="https://example.test",
        width=1280,
        height=720,
        elements=(
            {
                "text": "导出订单",
                "role": "button",
                "selector": '[data-testid="export-orders"]',
                "test_id": "export-orders",
                "bounds": {"left": 10, "top": 20, "width": 120, "height": 40},
            },
        ),
    )

    resolved = SelectorResolver().resolve(Target(test_id="export-orders", preferred=(ProviderKind.DOM,)), observation)

    assert resolved.evidence.handle == '[data-testid="export-orders"]'


def test_dom_selector_strategy_resolves_contains_text_and_regex() -> None:
    observation = Observation(
        provider=ProviderKind.DOM,
        source="https://example.test",
        width=1280,
        height=720,
        elements=(
            {
                "text": "订单号 A1001 下载",
                "role": "button",
                "selector": "#download-a1001",
                "bounds": {"left": 10, "top": 20, "width": 120, "height": 40},
            },
        ),
    )

    contains = SelectorResolver().resolve(Target(contains_text="A1001", preferred=(ProviderKind.DOM,)), observation)
    regex = SelectorResolver().resolve(Target(text_regex=r"订单号\s+a1001", preferred=(ProviderKind.DOM,)), observation)

    assert contains.evidence.handle == "#download-a1001"
    assert regex.evidence.handle == "#download-a1001"


def test_dom_selector_strategy_resolves_button_inside_matching_row() -> None:
    observation = Observation(
        provider=ProviderKind.DOM,
        source="https://example.test",
        width=1280,
        height=720,
        elements=(
            {
                "text": "下载",
                "role": "button",
                "selector": "table > tbody > tr:nth-of-type(1) > td:nth-of-type(4) > button",
                "test_id": "download-order",
                "row_text": "A1001 Acme 128.50 下载",
                "row_index": 1,
                "row_selector": "table > tbody > tr:nth-of-type(1)",
                "bounds": {"left": 410, "top": 80, "width": 80, "height": 32},
            },
            {
                "text": "下载",
                "role": "button",
                "selector": "table > tbody > tr:nth-of-type(2) > td:nth-of-type(4) > button",
                "test_id": "download-order",
                "row_text": "A1002 Globex 256.00 下载",
                "row_index": 2,
                "row_selector": "table > tbody > tr:nth-of-type(2)",
                "bounds": {"left": 410, "top": 120, "width": 80, "height": 32},
            },
        ),
    )

    resolved = SelectorResolver().resolve(
        Target(
            test_id="download-order",
            role="button",
            row_contains_text="A1002",
            preferred=(ProviderKind.DOM,),
        ),
        observation,
    )

    assert resolved.evidence.handle == "table > tbody > tr:nth-of-type(2) > td:nth-of-type(4) > button"
    assert resolved.evidence.metadata["element"]["row_index"] == 2


def test_dom_selector_strategy_resolves_button_by_row_and_column_header() -> None:
    observation = Observation(
        provider=ProviderKind.DOM,
        source="https://example.test",
        width=1280,
        height=720,
        elements=(
            {
                "text": "下载",
                "role": "button",
                "selector": "table > tbody > tr:nth-of-type(2) > td:nth-of-type(4) > button",
                "test_id": "row-action",
                "row_text": "A1002 Globex 256.00 下载 查看",
                "row_index": 2,
                "column_header": "下载",
                "column_index": 3,
                "bounds": {"left": 410, "top": 120, "width": 80, "height": 32},
            },
            {
                "text": "查看",
                "role": "button",
                "selector": "table > tbody > tr:nth-of-type(2) > td:nth-of-type(5) > button",
                "test_id": "row-action",
                "row_text": "A1002 Globex 256.00 下载 查看",
                "row_index": 2,
                "column_header": "查看",
                "column_index": 4,
                "bounds": {"left": 500, "top": 120, "width": 80, "height": 32},
            },
        ),
    )

    resolved = SelectorResolver().resolve(
        Target(
            test_id="row-action",
            role="button",
            row_contains_text="A1002",
            column_header="查看",
            preferred=(ProviderKind.DOM,),
        ),
        observation,
    )

    assert resolved.evidence.handle == "table > tbody > tr:nth-of-type(2) > td:nth-of-type(5) > button"
    assert resolved.evidence.metadata["element"]["column_header"] == "查看"


def test_dom_selector_strategy_resolves_input_near_label() -> None:
    observation = Observation(
        provider=ProviderKind.DOM,
        source="https://example.test",
        width=1280,
        height=720,
        elements=(
            {
                "text": "客户名称",
                "role": "label",
                "selector": "label[for='customer']",
                "bounds": {"left": 20, "top": 20, "width": 80, "height": 24},
            },
            {
                "text": "",
                "placeholder": "请输入客户",
                "role": "textbox",
                "selector": "#customer",
                "bounds": {"left": 120, "top": 18, "width": 220, "height": 32},
            },
            {
                "text": "",
                "placeholder": "请输入备注",
                "role": "textbox",
                "selector": "#note",
                "bounds": {"left": 120, "top": 80, "width": 220, "height": 32},
            },
        ),
    )

    resolved = SelectorResolver().resolve(
        Target(role="textbox", near_text="客户名称", preferred=(ProviderKind.DOM,)),
        observation,
    )

    assert resolved.evidence.handle == "#customer"
    assert resolved.evidence.metadata["score"] > 0.8


def test_dom_selector_strategy_resolves_button_near_text() -> None:
    observation = Observation(
        provider=ProviderKind.DOM,
        source="https://example.test",
        width=1280,
        height=720,
        elements=(
            {
                "text": "订单 A1001",
                "role": "text",
                "selector": "#order-a1001",
                "bounds": {"left": 20, "top": 20, "width": 100, "height": 24},
            },
            {
                "text": "下载",
                "role": "button",
                "selector": "#download-a1001",
                "bounds": {"left": 140, "top": 18, "width": 70, "height": 30},
            },
            {
                "text": "下载",
                "role": "button",
                "selector": "#download-a1002",
                "bounds": {"left": 140, "top": 100, "width": 70, "height": 30},
            },
        ),
    )

    resolved = SelectorResolver().resolve(
        Target(text="下载", role="button", near_contains_text="A1001", preferred=(ProviderKind.DOM,)),
        observation,
    )

    assert resolved.evidence.handle == "#download-a1001"


def test_dom_selector_strategy_respects_dialog_scope() -> None:
    observation = Observation(
        provider=ProviderKind.DOM,
        source="https://example.test",
        width=1280,
        height=720,
        elements=(
            {
                "text": "保存",
                "role": "button",
                "selector": "#page-save",
                "bounds": {"left": 20, "top": 20, "width": 80, "height": 32},
            },
            {
                "text": "编辑客户 保存",
                "role": "dialog",
                "selector": "#customer-dialog",
                "bounds": {"left": 300, "top": 100, "width": 400, "height": 260},
            },
            {
                "text": "保存",
                "role": "button",
                "selector": "#dialog-save",
                "scope_selector": "#customer-dialog",
                "scope_role": "dialog",
                "scope_text": "编辑客户 保存",
                "bounds": {"left": 580, "top": 300, "width": 80, "height": 32},
            },
        ),
    )

    resolved = SelectorResolver().resolve(
        Target(text="保存", role="button", scope_role="dialog", scope_contains_text="编辑客户", preferred=(ProviderKind.DOM,)),
        observation,
    )

    assert resolved.evidence.handle == "#dialog-save"


def test_selector_resolver_falls_back_to_mock_when_dom_has_no_match() -> None:
    observation = Observation(
        provider=ProviderKind.DOM,
        source="https://example.test",
        width=1280,
        height=720,
        elements=(
            {
                "text": "取消",
                "role": "button",
                "selector": "#cancel",
                "bounds": {"left": 10, "top": 20, "width": 100, "height": 40},
            },
        ),
    )

    resolved = SelectorResolver().resolve(Target.from_text("登录"), observation)

    assert resolved.evidence.provider == ProviderKind.MOCK
    assert resolved.click_point.x == 640
