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

    def press_key(
        self,
        keys: str | list[str],
        target: Target,
        *,
        provider: ProviderKind | None = None,
        dry_run: bool = False,
    ) -> ActionResult:
        """Press a single key or a key combination.

        ``keys`` accepts:
        - a single key name: ``"enter"``, ``"escape"``, ``"tab"``
        - a hotkey string with ``+`` separators: ``"ctrl+c"``, ``"alt+f4"``
        - a list of key names: ``["ctrl", "shift", "s"]``
        """
        if isinstance(keys, list):
            keys_repr = "+".join(keys)
            key_list = keys
        else:
            keys_repr = str(keys)
            key_list = [k.strip() for k in keys_repr.split("+")] if "+" in keys_repr else None
        if not dry_run:
            if key_list is not None:
                pyautogui.hotkey(*key_list)
            else:
                pyautogui.press(keys_repr)
        return ActionResult(
            action="press_key",
            status=ActionStatus.DRY_RUN if dry_run else ActionStatus.SUCCESS,
            target=target.display_name,
            point=None,
            provider=provider,
            message="press_key skipped by dry-run" if dry_run else f"pressed {keys_repr}",
            metadata={"keys": keys_repr},
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
