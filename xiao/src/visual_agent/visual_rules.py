"""Visual acceptance rules: zero-config readability and layout checks.

The browser-side script only *collects* a structured snapshot (text metrics,
image states, overflow geometry, hit-test occlusion). All judgement happens in
pure Python so every rule and threshold is unit-testable without a browser.

False-positive design notes, per rule:

- font size: ignores invisible elements (display/visibility/opacity and the
  1px ``sr-only`` clip pattern, via rect size), script/style/svg containers,
  and single-character strings (icon fonts). Two thresholds: below the
  blocking minimum text is effectively unreadable (error); between blocking
  and recommended minimum is a warning.
- horizontal overflow: only blocks when the *document itself* scrolls
  horizontally. Individual wide elements are listed as culprits for diagnosis
  but never block on their own — off-canvas drawers, carousels and clipped
  containers are legitimate.
- broken images: an ``<img>`` that finished loading with no intrinsic size is
  broken (error). Lazy images that have not started loading are skipped;
  eager images still pending at audit time are warnings, not errors.
- occlusion: an interactive element whose center point hit-tests to an
  unrelated element cannot be clicked the way a user would click it. This is
  the same actionability signal Playwright uses. Reported as a warning by
  default because intentional layers (open modals, toasts) legitimately cover
  the page.
- content coverage: a coarse grid union (not a rect-area sum, which double
  counts) of text/image/control coverage in the viewport. Low coverage is a
  warning — sparse-but-valid pages such as login forms exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


VISUAL_GUARD_STEP_ID = "visual_guard"
VISUAL_GUARD_OPT_OUT_TAG = "skip-visual-guard"

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

RULE_FONT_TOO_SMALL = "font_too_small"
RULE_FONT_BELOW_RECOMMENDED = "font_below_recommended"
RULE_HORIZONTAL_OVERFLOW = "horizontal_overflow"
RULE_BROKEN_IMAGE = "broken_image"
RULE_PENDING_IMAGE = "pending_image"
RULE_OCCLUDED_CONTROL = "occluded_control"
RULE_LOW_CONTENT_COVERAGE = "low_content_coverage"

ALL_RULES = frozenset(
    {
        RULE_FONT_TOO_SMALL,
        RULE_FONT_BELOW_RECOMMENDED,
        RULE_HORIZONTAL_OVERFLOW,
        RULE_BROKEN_IMAGE,
        RULE_PENDING_IMAGE,
        RULE_OCCLUDED_CONTROL,
        RULE_LOW_CONTENT_COVERAGE,
    }
)


@dataclass(frozen=True)
class VisualRuleConfig:
    min_font_px: float = 12.0
    min_font_px_blocking: float = 9.0
    overflow_tolerance_px: float = 2.0
    min_content_coverage: float = 0.03
    max_findings_per_rule: int = 10
    strict: bool = False
    skip_rules: frozenset[str] = frozenset()

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "VisualRuleConfig":
        skip_raw = params.get("skip_rules")
        skip_rules = frozenset(str(item) for item in skip_raw) if isinstance(skip_raw, (list, tuple)) else frozenset()
        unknown = skip_rules - ALL_RULES
        if unknown:
            raise ValueError(f"Unknown visual rules in skip_rules: {', '.join(sorted(unknown))}. Valid: {', '.join(sorted(ALL_RULES))}")
        return cls(
            min_font_px=float(params.get("min_font_px", cls.min_font_px)),
            min_font_px_blocking=float(params.get("min_font_px_blocking", cls.min_font_px_blocking)),
            overflow_tolerance_px=float(params.get("overflow_tolerance_px", cls.overflow_tolerance_px)),
            min_content_coverage=float(params.get("min_content_coverage", cls.min_content_coverage)),
            max_findings_per_rule=int(params.get("max_findings_per_rule", cls.max_findings_per_rule)),
            strict=bool(params.get("strict", False)),
            skip_rules=skip_rules,
        )


@dataclass(frozen=True)
class VisualFinding:
    rule: str
    severity: str
    message: str
    selector: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "selector": self.selector,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class VisualAuditReport:
    findings: tuple[VisualFinding, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)
    snapshot_error: str = ""

    @property
    def error_count(self) -> int:
        return sum(1 for item in self.findings if item.severity == SEVERITY_ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for item in self.findings if item.severity == SEVERITY_WARNING)

    @property
    def passed(self) -> bool:
        return self.error_count == 0 and not self.snapshot_error

    def summary(self) -> str:
        if self.snapshot_error:
            return f"visual audit could not run: {self.snapshot_error}"
        if not self.findings:
            return "visual audit passed: no readability or layout issues detected"
        rules = sorted({item.rule for item in self.findings if item.severity == SEVERITY_ERROR})
        parts = []
        if self.error_count:
            parts.append(f"{self.error_count} blocking issue(s) [{', '.join(rules)}]")
        if self.warning_count:
            parts.append(f"{self.warning_count} warning(s)")
        return "visual audit: " + ", ".join(parts)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "findings": [item.to_dict() for item in self.findings],
            "metrics": self.metrics,
            "snapshot_error": self.snapshot_error,
        }


# Collected in one pass so the page is sampled at a single consistent moment.
VISUAL_AUDIT_SCRIPT = r"""
() => {
  try {
    const MAX_TEXTS = 2000;
    const MAX_IMAGES = 500;
    const MAX_CONTROLS = 500;
    const GRID = 40;

    const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;

    const rectOf = (el) => {
      const r = el.getBoundingClientRect();
      return { left: r.left, top: r.top, width: r.width, height: r.height, right: r.right, bottom: r.bottom };
    };
    const compactRect = (r) => ({
      left: Math.round(r.left), top: Math.round(r.top),
      width: Math.round(r.width), height: Math.round(r.height),
    });
    const isVisible = (el) => {
      const r = el.getBoundingClientRect();
      if (r.width <= 1 || r.height <= 1) return false; // also excludes 1px sr-only pattern
      if (typeof el.checkVisibility === 'function') {
        try {
          if (!el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
        } catch (e) { /* fall through to style checks */ }
      }
      const style = window.getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
      if (parseFloat(style.opacity || '1') < 0.05) return false;
      return true;
    };
    const cssPath = (el) => {
      const parts = [];
      let node = el;
      for (let depth = 0; node && node.nodeType === 1 && depth < 5; depth += 1) {
        let part = node.tagName.toLowerCase();
        if (node.id) { parts.unshift(part + '#' + node.id); break; }
        const cls = (node.className && typeof node.className === 'string')
          ? node.className.trim().split(/\s+/).slice(0, 2).join('.') : '';
        if (cls) part += '.' + cls;
        const parent = node.parentElement;
        if (parent) {
          const siblings = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
          if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
        }
        parts.unshift(part);
        node = parent;
      }
      return parts.join(' > ');
    };

    // --- text elements (via text nodes, deduped by parent element) ---
    const texts = [];
    let textsTruncated = false;
    const seenTextParents = new Set();
    const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT, {
      acceptNode: (node) => {
        const value = (node.nodeValue || '').trim();
        if (value.length < 2) return NodeFilter.FILTER_REJECT; // single chars are usually icon glyphs
        const parent = node.parentElement;
        if (!parent) return NodeFilter.FILTER_REJECT;
        const tag = parent.tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT' || tag === 'TEMPLATE' || tag === 'TITLE') {
          return NodeFilter.FILTER_REJECT;
        }
        if (parent.closest('svg')) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    while (walker.nextNode()) {
      const parent = walker.currentNode.parentElement;
      if (seenTextParents.has(parent)) continue;
      seenTextParents.add(parent);
      if (texts.length >= MAX_TEXTS) { textsTruncated = true; break; }
      if (!isVisible(parent)) continue;
      const style = window.getComputedStyle(parent);
      texts.push({
        selector: cssPath(parent),
        tag: parent.tagName.toLowerCase(),
        snippet: (walker.currentNode.nodeValue || '').trim().slice(0, 60),
        font_size: parseFloat(style.fontSize || '0'),
        rect: compactRect(rectOf(parent)),
      });
    }

    // --- images ---
    const images = [];
    let imagesTruncated = false;
    for (const img of Array.from(document.images)) {
      if (images.length >= MAX_IMAGES) { imagesTruncated = true; break; }
      const src = img.currentSrc || img.src || '';
      images.push({
        selector: cssPath(img),
        src: src.slice(0, 300),
        has_source: Boolean(src || img.srcset),
        complete: Boolean(img.complete),
        natural_width: img.naturalWidth || 0,
        loading: img.loading || 'auto',
        visible: isVisible(img),
        rect: compactRect(rectOf(img)),
      });
    }

    // --- horizontal overflow ---
    const docEl = document.documentElement;
    const scrollableAncestorX = (el) => {
      let node = el.parentElement;
      while (node && node !== document.body) {
        const style = window.getComputedStyle(node);
        const overflowX = style.overflowX;
        if (overflowX === 'auto' || overflowX === 'scroll' || overflowX === 'hidden' || overflowX === 'clip') return true;
        node = node.parentElement;
      }
      return false;
    };
    const overflowCulprits = [];
    if (docEl.scrollWidth > docEl.clientWidth + 1) {
      const all = document.body ? document.body.getElementsByTagName('*') : [];
      for (let i = 0; i < all.length && overflowCulprits.length < 10; i += 1) {
        const el = all[i];
        const r = el.getBoundingClientRect();
        if (r.width <= 1 || r.height <= 1) continue;
        if (r.right <= docEl.clientWidth + 1) continue;
        if (!isVisible(el)) continue;
        if (scrollableAncestorX(el)) continue; // clipped or intentionally scrollable container
        overflowCulprits.push({
          selector: cssPath(el),
          tag: el.tagName.toLowerCase(),
          rect: compactRect(r),
        });
      }
    }

    // --- interactive controls + occlusion hit-testing ---
    const controls = [];
    let controlsTruncated = false;
    const interactiveSelector = 'a[href], button, input:not([type=hidden]), select, textarea, [role=button], [role=link], [role=tab], [role=menuitem]';
    for (const el of Array.from(document.querySelectorAll(interactiveSelector))) {
      if (controls.length >= MAX_CONTROLS) { controlsTruncated = true; break; }
      if (!isVisible(el)) continue;
      if (el.disabled) continue;
      const r = el.getBoundingClientRect();
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      let occluded = false;
      let occludedBy = '';
      // hit-testing only works inside the current viewport
      if (cx >= 0 && cy >= 0 && cx < viewportWidth && cy < viewportHeight) {
        const hit = document.elementFromPoint(cx, cy);
        if (hit && hit !== el && !el.contains(hit) && !hit.contains(el)) {
          // a label wrapping/for its input is a legitimate hit
          const label = hit.closest ? hit.closest('label') : null;
          const isLabelFor = label && (label.contains(el) || (label.htmlFor && label.htmlFor === el.id));
          if (!isLabelFor) {
            occluded = true;
            occludedBy = cssPath(hit);
          }
        }
      }
      controls.push({
        selector: cssPath(el),
        tag: el.tagName.toLowerCase(),
        text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 60),
        rect: compactRect(r),
        occluded,
        occluded_by: occludedBy,
      });
    }

    // --- content coverage on a coarse grid (union, not double-counted sum) ---
    const grid = new Uint8Array(GRID * GRID);
    const cellW = viewportWidth / GRID;
    const cellH = viewportHeight / GRID;
    const mark = (rect) => {
      if (cellW <= 0 || cellH <= 0) return;
      const x0 = Math.max(0, Math.floor(rect.left / cellW));
      const y0 = Math.max(0, Math.floor(rect.top / cellH));
      const x1 = Math.min(GRID - 1, Math.ceil((rect.left + rect.width) / cellW) - 1);
      const y1 = Math.min(GRID - 1, Math.ceil((rect.top + rect.height) / cellH) - 1);
      for (let y = y0; y <= y1; y += 1) {
        for (let x = x0; x <= x1; x += 1) grid[y * GRID + x] = 1;
      }
    };
    for (const item of texts) mark(item.rect);
    for (const item of images) { if (item.visible) mark(item.rect); }
    for (const item of controls) mark(item.rect);
    let covered = 0;
    for (let i = 0; i < grid.length; i += 1) covered += grid[i];

    return {
      schema_version: 1,
      viewport: { width: viewportWidth, height: viewportHeight },
      document: {
        scroll_width: docEl.scrollWidth,
        client_width: docEl.clientWidth,
        scroll_height: docEl.scrollHeight,
        client_height: docEl.clientHeight,
      },
      texts,
      images,
      controls,
      overflow_culprits: overflowCulprits,
      content_coverage: grid.length ? covered / grid.length : 1,
      truncated: { texts: textsTruncated, images: imagesTruncated, controls: controlsTruncated },
    };
  } catch (error) {
    return { schema_version: 1, error: String(error) };
  }
}
"""


def collect_visual_snapshot(page: Any) -> dict[str, Any] | None:
    """Collect the audit snapshot from a live page. None when unavailable."""
    if page is None:
        return None
    try:
        snapshot = page.evaluate(VISUAL_AUDIT_SCRIPT)
    except Exception as exc:
        return {"schema_version": 1, "error": f"snapshot collection failed: {exc}"}
    if not isinstance(snapshot, dict) or "schema_version" not in snapshot:
        # not a real browser page (e.g. a test double that answers any script)
        return None
    return snapshot


def evaluate_visual_snapshot(snapshot: dict[str, Any], config: VisualRuleConfig | None = None) -> VisualAuditReport:
    config = config or VisualRuleConfig()
    if not isinstance(snapshot, dict):
        return VisualAuditReport(snapshot_error="snapshot is not a mapping")
    if snapshot.get("error"):
        return VisualAuditReport(snapshot_error=str(snapshot["error"]))

    findings: list[VisualFinding] = []
    findings.extend(_font_findings(snapshot, config))
    findings.extend(_overflow_findings(snapshot, config))
    findings.extend(_image_findings(snapshot, config))
    findings.extend(_occlusion_findings(snapshot, config))
    findings.extend(_coverage_findings(snapshot, config))
    if config.strict:
        findings = [
            VisualFinding(item.rule, SEVERITY_ERROR, item.message, item.selector, item.evidence)
            if item.severity == SEVERITY_WARNING
            else item
            for item in findings
        ]
    findings = [item for item in findings if item.rule not in config.skip_rules]
    return VisualAuditReport(findings=tuple(findings), metrics=_metrics(snapshot, findings))


def run_visual_audit(resources: dict[str, Any] | None, config: VisualRuleConfig | None = None) -> VisualAuditReport | None:
    """Audit the live page held in workflow resources. None when no page."""
    resources = resources or {}
    snapshot = collect_visual_snapshot(resources.get("playwright_page"))
    if snapshot is None:
        return None
    return evaluate_visual_snapshot(snapshot, config)


def _font_findings(snapshot: dict[str, Any], config: VisualRuleConfig) -> list[VisualFinding]:
    blocking: list[VisualFinding] = []
    warnings: list[VisualFinding] = []
    for item in _items(snapshot, "texts"):
        size = _as_float(item.get("font_size"))
        if size <= 0:
            continue
        if size < config.min_font_px_blocking and len(blocking) < config.max_findings_per_rule:
            blocking.append(
                VisualFinding(
                    rule=RULE_FONT_TOO_SMALL,
                    severity=SEVERITY_ERROR,
                    message=f"text is effectively unreadable at {size:g}px (blocking minimum {config.min_font_px_blocking:g}px): \"{item.get('snippet', '')}\"",
                    selector=str(item.get("selector") or ""),
                    evidence={"font_size": size, "snippet": item.get("snippet", ""), "rect": item.get("rect")},
                )
            )
        elif config.min_font_px_blocking <= size < config.min_font_px and len(warnings) < config.max_findings_per_rule:
            warnings.append(
                VisualFinding(
                    rule=RULE_FONT_BELOW_RECOMMENDED,
                    severity=SEVERITY_WARNING,
                    message=f"text below recommended minimum at {size:g}px (recommended {config.min_font_px:g}px): \"{item.get('snippet', '')}\"",
                    selector=str(item.get("selector") or ""),
                    evidence={"font_size": size, "snippet": item.get("snippet", ""), "rect": item.get("rect")},
                )
            )
    return blocking + warnings


def _overflow_findings(snapshot: dict[str, Any], config: VisualRuleConfig) -> list[VisualFinding]:
    document = snapshot.get("document") if isinstance(snapshot.get("document"), dict) else {}
    scroll_width = _as_float(document.get("scroll_width"))
    client_width = _as_float(document.get("client_width"))
    if client_width <= 0 or scroll_width <= client_width + config.overflow_tolerance_px:
        return []
    culprits = _items(snapshot, "overflow_culprits")[: config.max_findings_per_rule]
    return [
        VisualFinding(
            rule=RULE_HORIZONTAL_OVERFLOW,
            severity=SEVERITY_ERROR,
            message=(
                f"page scrolls horizontally: document width {scroll_width:g}px exceeds viewport {client_width:g}px"
                + (f"; widest offender: {culprits[0].get('selector')}" if culprits else "")
            ),
            selector=str(culprits[0].get("selector") or "") if culprits else "",
            evidence={
                "scroll_width": scroll_width,
                "client_width": client_width,
                "culprits": culprits,
            },
        )
    ]


def _image_findings(snapshot: dict[str, Any], config: VisualRuleConfig) -> list[VisualFinding]:
    broken: list[VisualFinding] = []
    pending: list[VisualFinding] = []
    for item in _items(snapshot, "images"):
        if not item.get("has_source"):
            continue
        complete = bool(item.get("complete"))
        natural_width = _as_float(item.get("natural_width"))
        if complete and natural_width == 0 and len(broken) < config.max_findings_per_rule:
            broken.append(
                VisualFinding(
                    rule=RULE_BROKEN_IMAGE,
                    severity=SEVERITY_ERROR,
                    message=f"image failed to load: {item.get('src', '')}",
                    selector=str(item.get("selector") or ""),
                    evidence={"src": item.get("src", ""), "visible": item.get("visible"), "rect": item.get("rect")},
                )
            )
        elif not complete and str(item.get("loading") or "") != "lazy" and len(pending) < config.max_findings_per_rule:
            pending.append(
                VisualFinding(
                    rule=RULE_PENDING_IMAGE,
                    severity=SEVERITY_WARNING,
                    message=f"image still loading at audit time: {item.get('src', '')}",
                    selector=str(item.get("selector") or ""),
                    evidence={"src": item.get("src", ""), "rect": item.get("rect")},
                )
            )
    return broken + pending


def _occlusion_findings(snapshot: dict[str, Any], config: VisualRuleConfig) -> list[VisualFinding]:
    findings: list[VisualFinding] = []
    for item in _items(snapshot, "controls"):
        if not item.get("occluded"):
            continue
        if len(findings) >= config.max_findings_per_rule:
            break
        findings.append(
            VisualFinding(
                rule=RULE_OCCLUDED_CONTROL,
                severity=SEVERITY_WARNING,
                message=(
                    f"control \"{item.get('text') or item.get('tag')}\" is covered by {item.get('occluded_by')} "
                    "and cannot be clicked at its center"
                ),
                selector=str(item.get("selector") or ""),
                evidence={
                    "control": item.get("text", ""),
                    "tag": item.get("tag", ""),
                    "occluded_by": item.get("occluded_by", ""),
                    "rect": item.get("rect"),
                },
            )
        )
    return findings


def _coverage_findings(snapshot: dict[str, Any], config: VisualRuleConfig) -> list[VisualFinding]:
    coverage = snapshot.get("content_coverage")
    if not isinstance(coverage, (int, float)):
        return []
    if coverage >= config.min_content_coverage:
        return []
    return [
        VisualFinding(
            rule=RULE_LOW_CONTENT_COVERAGE,
            severity=SEVERITY_WARNING,
            message=f"viewport is mostly blank: content covers {coverage:.1%} (minimum {config.min_content_coverage:.0%})",
            evidence={"content_coverage": coverage},
        )
    ]


def _metrics(snapshot: dict[str, Any], findings: list[VisualFinding]) -> dict[str, Any]:
    document = snapshot.get("document") if isinstance(snapshot.get("document"), dict) else {}
    truncated = snapshot.get("truncated") if isinstance(snapshot.get("truncated"), dict) else {}
    by_rule: dict[str, int] = {}
    for item in findings:
        by_rule[item.rule] = by_rule.get(item.rule, 0) + 1
    return {
        "text_count": len(_items(snapshot, "texts")),
        "image_count": len(_items(snapshot, "images")),
        "control_count": len(_items(snapshot, "controls")),
        "content_coverage": snapshot.get("content_coverage"),
        "document_scroll_width": document.get("scroll_width"),
        "document_client_width": document.get("client_width"),
        "findings_by_rule": by_rule,
        "collection_truncated": truncated,
    }


def _items(snapshot: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = snapshot.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
