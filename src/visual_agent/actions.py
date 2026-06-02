from __future__ import annotations

import pyautogui
import pyperclip

from .models import ActionResult, ActionStatus, Point, ProviderKind, Target
from .security import text_metadata


class DesktopActions:
    def __init__(self, pause_seconds: float = 0.15) -> None:
        pyautogui.PAUSE = pause_seconds

    def click(
        self,
        point: Point,
        target: Target,
        *,
        provider: ProviderKind,
        dry_run: bool = False,
    ) -> ActionResult:
        if not dry_run:
            pyautogui.click(x=point.x, y=point.y)
        return ActionResult(
            action="click",
            status=ActionStatus.DRY_RUN if dry_run else ActionStatus.SUCCESS,
            target=target.display_name,
            point=point,
            provider=provider,
            message="click skipped by dry-run" if dry_run else "clicked",
        )

    def type_text(
        self,
        text: str,
        target: Target,
        *,
        point: Point | None = None,
        provider: ProviderKind | None = None,
        dry_run: bool = False,
        interval_seconds: float = 0.01,
        sensitive: bool = False,
    ) -> ActionResult:
        if not dry_run:
            if point is not None:
                pyautogui.click(x=point.x, y=point.y)
            pyautogui.write(text, interval=interval_seconds)
        return ActionResult(
            action="type",
            status=ActionStatus.DRY_RUN if dry_run else ActionStatus.SUCCESS,
            target=target.display_name,
            point=point,
            provider=provider,
            message="type skipped by dry-run" if dry_run else "typed",
            metadata=text_metadata(text, sensitive=sensitive),
        )

    def paste_text(
        self,
        text: str,
        target: Target,
        *,
        point: Point | None = None,
        provider: ProviderKind | None = None,
        dry_run: bool = False,
        sensitive: bool = False,
    ) -> ActionResult:
        if not dry_run:
            if point is not None:
                pyautogui.click(x=point.x, y=point.y)
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
        return ActionResult(
            action="paste",
            status=ActionStatus.DRY_RUN if dry_run else ActionStatus.SUCCESS,
            target=target.display_name,
            point=point,
            provider=provider,
            message="paste skipped by dry-run" if dry_run else "pasted",
            metadata=text_metadata(text, sensitive=sensitive),
        )
