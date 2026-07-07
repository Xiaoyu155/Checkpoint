from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import Bounds, Observation, ProviderKind
from .playwright_env import ensure_playwright_browsers_path


INTERACTIVE_SELECTOR = ",".join(
    [
        "a",
        "button",
        "input",
        "textarea",
        "select",
        "label",
        "dialog",
        "[role]",
        "[aria-label]",
        "[data-testid]",
        "tr",
        "[role='row']",
    ]
)


@dataclass(frozen=True)
class DomProvider:
    """Read structured web page state through Playwright.

    Playwright is optional so the core project remains testable without a
    browser runtime. Install with `pip install -e .[web]` when using this.
    """

    headless: bool = True
    timeout_ms: int = 10_000

    def observe_url(self, url: str) -> Observation:
        ensure_playwright_browsers_path()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed. Run: pip install -e .[web]") from exc

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.headless)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            elements = tuple(page.evaluate(_COLLECT_ELEMENTS_SCRIPT, INTERACTIVE_SELECTOR))
            observation = Observation(
                provider=ProviderKind.DOM,
                source=url,
                width=page.viewport_size["width"] if page.viewport_size else None,
                height=page.viewport_size["height"] if page.viewport_size else None,
                elements=elements,
                metadata={"title": page.title(), "url": page.url},
            )
            browser.close()
            return observation


def normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def element_bounds(element: dict[str, Any]) -> Bounds | None:
    raw = element.get("bounds")
    if not isinstance(raw, dict):
        return None
    try:
        width = int(raw.get("width", 0))
        height = int(raw.get("height", 0))
        if width <= 0 or height <= 0:
            return None
        return Bounds(
            left=int(raw.get("left", 0)),
            top=int(raw.get("top", 0)),
            width=width,
            height=height,
        )
    except (TypeError, ValueError):
        return None


def element_accessible_name(element: dict[str, Any]) -> str:
    candidates = [
        element.get("text"),
        element.get("label"),
        element.get("aria_label"),
        element.get("placeholder"),
        element.get("value"),
        element.get("name"),
        element.get("test_id"),
    ]
    return normalize_text(" ".join(str(item) for item in candidates if item))


_COLLECT_ELEMENTS_SCRIPT = """
(selector) => {
  const nodes = Array.from(document.querySelectorAll(selector));
  return nodes
    .filter((node) => {
      const rect = node.getBoundingClientRect();
      const style = window.getComputedStyle(node);
      return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    })
    .slice(0, 500)
    .map((node, index) => {
      const rect = node.getBoundingClientRect();
      const tag = node.tagName.toLowerCase();
      const cssPath = (el) => {
        if (el.id) {
          return `#${CSS.escape(el.id)}`;
        }
        const dataTestId = el.getAttribute('data-testid');
        if (dataTestId && document.querySelectorAll(`[data-testid="${CSS.escape(dataTestId)}"]`).length === 1) {
          return `[data-testid="${CSS.escape(dataTestId)}"]`;
        }
        const name = el.getAttribute('name');
        if (name && ['input', 'textarea', 'select', 'button'].includes(el.tagName.toLowerCase())) {
          return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
        }
        const parts = [];
        let current = el;
        while (current && current.nodeType === Node.ELEMENT_NODE && current !== document.body) {
          const currentTag = current.tagName.toLowerCase();
          const siblings = Array.from(current.parentElement ? current.parentElement.children : [])
            .filter((item) => item.tagName === current.tagName);
          if (siblings.length > 1) {
            parts.unshift(`${currentTag}:nth-of-type(${siblings.indexOf(current) + 1})`);
          } else {
            parts.unshift(currentTag);
          }
          current = current.parentElement;
        }
        return parts.join(' > ') || el.tagName.toLowerCase();
      };
      const testId = node.getAttribute('data-testid');
      const dialog = node.closest('dialog,[role="dialog"],[aria-modal="true"]');
      const row = node.closest('tr,[role="row"]');
      const rows = row ? Array.from(document.querySelectorAll('tr,[role="row"]')) : [];
      const cell = node.closest('td,th,[role="cell"],[role="gridcell"],[role="columnheader"]');
      const cellIndex = row && cell ? Array.from(row.children).indexOf(cell) : null;
      const table = row ? row.closest('table,[role="table"],[role="grid"]') : null;
      const headerRows = table ? Array.from(table.querySelectorAll('thead tr,[role="row"]')).filter((item) => {
        return item.querySelector('th,[role="columnheader"]');
      }) : [];
      const headerCells = headerRows.length ? Array.from(headerRows[headerRows.length - 1].children) : [];
      const headerCell = cellIndex !== null && cellIndex >= 0 ? headerCells[cellIndex] : null;
      const columnHeader = headerCell ? (headerCell.innerText || headerCell.textContent || '').trim() : '';
      const role = node.getAttribute('role') || (
        tag === 'tr' ? 'row' :
        tag === 'button' ? 'button' :
        tag === 'label' ? 'label' :
        tag === 'dialog' ? 'dialog' :
        tag === 'a' ? 'link' :
        ['input', 'textarea', 'select'].includes(tag) ? 'input' :
        null
      );
      return {
        index,
        tag,
        role,
        selector: cssPath(node),
        text: (node.innerText || node.textContent || '').trim().slice(0, 200),
        label: node.getAttribute('aria-label') || node.getAttribute('title') || '',
        aria_label: node.getAttribute('aria-label') || '',
        placeholder: node.getAttribute('placeholder') || '',
        name: node.getAttribute('name') || '',
        value: node.value || '',
        test_id: testId || '',
        row_text: row ? (row.innerText || row.textContent || '').trim().slice(0, 500) : '',
        row_index: row ? rows.indexOf(row) : null,
        row_selector: row ? cssPath(row) : '',
        column_header: columnHeader,
        column_index: cellIndex,
        scope_selector: dialog ? cssPath(dialog) : '',
        scope_role: dialog ? (dialog.getAttribute('role') || 'dialog') : '',
        scope_text: dialog ? (dialog.innerText || dialog.textContent || '').trim().slice(0, 500) : '',
        bounds: {
          left: Math.round(rect.left),
          top: Math.round(rect.top),
          width: Math.round(rect.width),
          height: Math.round(rect.height)
        }
      };
    });
}
"""
