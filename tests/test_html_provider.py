from visual_agent.html_provider import HtmlFileProvider, parse_bounds, parse_pair
from visual_agent.models import ProviderKind, Target
from visual_agent.selector import SelectorResolver


def test_parse_bounds_reads_data_bounds() -> None:
    assert parse_bounds("10,20,100,40") == {"left": 10, "top": 20, "width": 100, "height": 40}


def test_parse_pair_returns_default_on_invalid_value() -> None:
    assert parse_pair("bad", default=(1280, 720)) == (1280, 720)


def test_html_file_provider_reads_interactive_elements() -> None:
    observation = HtmlFileProvider().observe_file("examples/web/login_demo.html")

    assert observation.provider == ProviderKind.DOM
    assert observation.metadata["title"] == "客户管理系统"
    assert [element["selector"] for element in observation.elements] == ["#username", "#password", "#login"]


def test_html_observation_resolves_login_button() -> None:
    observation = HtmlFileProvider().observe_file("examples/web/login_demo.html")

    resolved = SelectorResolver().resolve(Target(text="登录", role="button"), observation)

    assert resolved.evidence.provider == ProviderKind.DOM
    assert resolved.evidence.handle == "#login"
    assert resolved.click_point.x == 640
    assert resolved.click_point.y == 364


def test_html_file_provider_reads_business_backend_scope_metadata() -> None:
    observation = HtmlFileProvider().observe_file("examples/web/business_backend_demo.html")

    label = next(element for element in observation.elements if element["selector"] == 'label[for="customer-filter"]')
    dismiss = next(element for element in observation.elements if element["selector"] == "#dismiss-error")

    assert label["role"] == "label"
    assert dismiss["scope_role"] == "dialog"
    assert dismiss["scope_selector"] == "#error-dialog"
    assert "缺少收货地址" in dismiss["scope_text"]
