from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .models import Observation, ProviderKind


INTERACTIVE_TAGS = {"a", "button", "input", "textarea", "select", "label", "dialog"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


@dataclass(frozen=True)
class HtmlFileProvider:
    """Read local HTML files into DOM-like observations.

    This is a deterministic test provider. It does not execute JavaScript or
    compute layout; demo pages can provide `data-bounds="left,top,width,height"`.
    """

    def observe_file(self, path: str | Path) -> Observation:
        html_path = Path(path)
        parser = HtmlObservationParser()
        parser.feed(html_path.read_text(encoding="utf-8"))
        return Observation(
            provider=ProviderKind.DOM,
            source=str(html_path),
            width=parser.viewport_width,
            height=parser.viewport_height,
            elements=tuple(parser.elements),
            metadata={"title": parser.title or html_path.name, "provider": "html_file"},
        )


class HtmlObservationParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[dict[str, Any]] = []
        self._stack: list[dict[str, Any]] = []
        self._title_parts: list[str] = []
        self._in_title = False
        self.viewport_width = 1280
        self.viewport_height = 720

    @property
    def title(self) -> str:
        return " ".join("".join(self._title_parts).split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        if tag == "title":
            self._in_title = True
        if tag == "main" and attr.get("data-viewport"):
            width, height = parse_pair(attr["data-viewport"], default=(1280, 720))
            self.viewport_width = width
            self.viewport_height = height

        role = attr.get("role") or inferred_role(tag, attr)
        is_interactive = tag in INTERACTIVE_TAGS or bool(attr.get("role")) or bool(attr.get("aria-label"))
        item = {
            "tag": tag,
            "attrs": attr,
            "text_parts": [],
            "capture": is_interactive,
            "role": role,
        }
        if tag in VOID_TAGS:
            if is_interactive:
                element = element_from_item(item, "", len(self.elements))
                if element is not None:
                    self.elements.append(element)
            return
        self._stack.append(item)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if not self._stack:
            return

        item = self._stack.pop()
        text = " ".join("".join(item["text_parts"]).split())
        if item["capture"]:
            element = element_from_item(item, text, len(self.elements))
            if element is not None:
                self.elements.append(element)

        if self._stack and text:
            self._stack[-1]["text_parts"].append(text)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._stack:
            self._stack[-1]["text_parts"].append(data)


def element_from_item(item: dict[str, Any], text: str, index: int) -> dict[str, Any] | None:
    attr = item["attrs"]
    bounds = parse_bounds(attr.get("data-bounds", ""))
    if bounds is None:
        return None

    tag = item["tag"]
    if attr.get("id"):
        selector = f"#{attr['id']}"
    elif attr.get("data-testid"):
        selector = f'[data-testid="{attr["data-testid"]}"]'
    elif attr.get("name") and tag in {"input", "textarea", "select", "button"}:
        selector = f'{tag}[name="{attr["name"]}"]'
    elif attr.get("for") and tag == "label":
        selector = f'label[for="{attr["for"]}"]'
    else:
        selector = tag
    return {
        "index": index,
        "tag": tag,
        "role": item["role"],
        "selector": selector,
        "text": text,
        "label": attr.get("aria-label") or attr.get("title") or "",
        "aria_label": attr.get("aria-label") or "",
        "placeholder": attr.get("placeholder") or "",
        "name": attr.get("name") or "",
        "value": attr.get("value") or "",
        "test_id": attr.get("data-testid") or "",
        "scope_selector": attr.get("data-scope-selector") or "",
        "scope_role": attr.get("data-scope-role") or "",
        "scope_text": attr.get("data-scope-text") or "",
        "row_text": attr.get("data-row-text") or "",
        "row_index": int(attr["data-row-index"]) if attr.get("data-row-index", "").isdigit() else None,
        "row_selector": attr.get("data-row-selector") or "",
        "column_header": attr.get("data-column-header") or "",
        "column_index": int(attr["data-column-index"]) if attr.get("data-column-index", "").isdigit() else None,
        "bounds": bounds,
    }


def inferred_role(tag: str, attr: dict[str, str]) -> str | None:
    if tag == "button":
        return "button"
    if tag == "label":
        return "label"
    if tag == "dialog":
        return "dialog"
    if tag == "a":
        return "link"
    if tag in {"input", "textarea", "select"}:
        input_type = attr.get("type", "").lower()
        if input_type in {"submit", "button"}:
            return "button"
        return "input"
    return None


def parse_bounds(value: str) -> dict[str, int] | None:
    if not value:
        return None
    left, top, width, height = parse_quad(value)
    if width <= 0 or height <= 0:
        return None
    return {"left": left, "top": top, "width": width, "height": height}


def parse_pair(value: str, *, default: tuple[int, int]) -> tuple[int, int]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2:
        return default
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return default


def parse_quad(value: str) -> tuple[int, int, int, int]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise ValueError(f"Invalid data-bounds value: {value}")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]
