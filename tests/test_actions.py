from visual_agent.actions import DesktopActions
from visual_agent.security import text_metadata
from visual_agent.models import ActionStatus, Point, ProviderKind, Target


def test_text_metadata_masks_preview() -> None:
    assert text_metadata("abcdef") == {"sensitive": False, "text_length": 6, "text_preview": "abc***"}
    assert text_metadata("ab") == {"sensitive": False, "text_length": 2, "text_preview": "***"}


def test_type_text_dry_run_records_metadata() -> None:
    result = DesktopActions().type_text(
        "demo_user",
        Target.from_text("用户名"),
        point=Point(10, 20),
        provider=ProviderKind.DOM,
        dry_run=True,
    )

    assert result.status == ActionStatus.DRY_RUN
    assert result.action == "type"
    assert result.metadata["text_length"] == 9


def test_paste_text_dry_run_records_metadata() -> None:
    result = DesktopActions().paste_text(
        "secret",
        Target.from_text("密码"),
        point=Point(10, 20),
        provider=ProviderKind.DOM,
        dry_run=True,
    )

    assert result.status == ActionStatus.DRY_RUN
    assert result.action == "paste"
    assert result.metadata["text_preview"] == "sec***"


def test_paste_text_sensitive_dry_run_hashes_metadata() -> None:
    result = DesktopActions().paste_text(
        "secret",
        Target.from_text("密码"),
        point=Point(10, 20),
        provider=ProviderKind.DOM,
        dry_run=True,
        sensitive=True,
    )

    assert result.metadata["sensitive"] is True
    assert "sha256" in result.metadata
    assert "text_length" not in result.metadata
