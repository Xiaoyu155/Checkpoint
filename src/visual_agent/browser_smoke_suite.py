from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .browser_smoke import browser_smoke_to_markdown, run_browser_smoke


CASE_KEYS = {
    "url",
    "headed",
    "timeout_ms",
    "wait_until",
    "min_text_length",
    "min_interactive",
    "expect_text",
    "expect_url_contains",
    "expect_text_after",
    "expect_url_contains_after",
    "wait_for_text_after",
    "wait_for_url_contains_after",
    "wait_timeout_seconds",
    "click_text",
    "click_selector",
    "fill",
    "fill_selector",
    "require_change_after_click",
    "wait_after_seconds",
}


def run_browser_smoke_suite(
    suite_file: str | Path,
    *,
    output_dir: str | Path = ".runs",
    headed: bool | None = None,
) -> dict[str, Any]:
    suite_path = Path(suite_file).resolve()
    suite = load_browser_smoke_suite(suite_path)
    root_dir = browser_smoke_suite_run_dir(output_dir)
    defaults = dict(suite.get("defaults") or {})
    if headed is not None:
        defaults["headed"] = headed
    cases = suite_cases(suite)
    results = []
    for index, case in enumerate(cases, start=1):
        case_id = str(case.get("id") or case.get("name") or f"case_{index}")
        params = smoke_case_params(defaults, case, suite_path=suite_path, root_dir=root_dir, case_id=case_id)
        result = run_browser_smoke(**params)
        result["case_id"] = case_id
        result["case_name"] = str(case.get("name") or case_id)
        results.append(result)

    failed = [item for item in results if item.get("status") != "success"]
    payload = {
        "status": "failed" if failed else "success",
        "suite_file": str(suite_path),
        "suite_name": str(suite.get("name") or suite_path.stem),
        "run_dir": str(root_dir),
        "case_count": len(results),
        "passed_count": len(results) - len(failed),
        "failed_count": len(failed),
        "results": results,
    }
    write_suite_reports(root_dir, payload)
    return payload


def load_browser_smoke_suite(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Browser smoke suite not found: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        payload = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("PyYAML is required for YAML browser smoke suites. Use JSON or install PyYAML.") from exc
        payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise ValueError("Browser smoke suite root must be an object.")
    return payload


def suite_cases(suite: dict[str, Any]) -> list[dict[str, Any]]:
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Browser smoke suite requires a non-empty 'cases' list.")
    result = []
    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise ValueError(f"Browser smoke suite case #{index} must be an object.")
        if not case.get("url"):
            raise ValueError(f"Browser smoke suite case #{index} is missing url.")
        result.append(case)
    return result


def smoke_case_params(
    defaults: dict[str, Any],
    case: dict[str, Any],
    *,
    suite_path: Path,
    root_dir: Path,
    case_id: str,
) -> dict[str, Any]:
    merged = {key: defaults[key] for key in CASE_KEYS if key in defaults}
    merged.update({key: case[key] for key in CASE_KEYS if key in case})
    url = str(merged.get("url") or "")
    if url and "://" not in url:
        candidate = (suite_path.parent / url).resolve()
        if candidate.exists():
            merged["url"] = str(candidate)
    merged["output_dir"] = root_dir / sanitize_case_id(case_id)
    for key in (
        "expect_text",
        "expect_url_contains",
        "expect_text_after",
        "expect_url_contains_after",
        "wait_for_text_after",
        "wait_for_url_contains_after",
        "fill",
        "fill_selector",
    ):
        merged[key] = string_list(merged.get(key))
    for key in ("headed", "require_change_after_click"):
        if key in merged:
            merged[key] = bool(merged[key])
    for key in ("timeout_ms", "min_text_length", "min_interactive"):
        if key in merged:
            merged[key] = int(merged[key])
    for key in ("wait_timeout_seconds", "wait_after_seconds"):
        if key in merged:
            merged[key] = float(merged[key])
    return merged


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item)]
    return [str(value)]


def browser_smoke_suite_run_dir(output_dir: str | Path) -> Path:
    root = Path(output_dir).resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    for index in range(100):
        suffix = "" if index == 0 else f"-{index}"
        run_dir = root / f"browser-smoke-suite-{stamp}{suffix}"
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            return run_dir
        except FileExistsError:
            continue
    raise FileExistsError(f"Could not create unique browser smoke suite run directory under {root}")


def write_suite_reports(run_dir: Path, payload: dict[str, Any]) -> None:
    (run_dir / "suite-result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (run_dir / "suite-result.md").write_text(browser_smoke_suite_to_markdown(payload), encoding="utf-8")


def browser_smoke_suite_to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Browser Smoke Suite",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- Suite: `{payload.get('suite_name')}`",
        f"- Cases: `{payload.get('passed_count')}/{payload.get('case_count')}` passed",
        f"- Run dir: `{payload.get('run_dir')}`",
        "",
        "## Cases",
        "",
    ]
    for item in payload.get("results", []):
        if not isinstance(item, dict):
            continue
        lines.append(f"- `{item.get('case_id')}`: `{item.get('status')}`")
        issues = item.get("issues") if isinstance(item.get("issues"), list) else []
        for issue in issues[:3]:
            if isinstance(issue, dict):
                lines.append(f"  - `{issue.get('type')}`: {issue.get('message')}")
        initial = item.get("initial") if isinstance(item.get("initial"), dict) else {}
        after = item.get("after_click") if isinstance(item.get("after_click"), dict) else {}
        screenshot = after.get("screenshot_path") or initial.get("screenshot_path")
        if screenshot:
            lines.append(f"  - Screenshot: `{screenshot}`")
    if payload.get("failed_count"):
        lines.extend(["", "## Failure Details", ""])
        for item in payload.get("results", []):
            if isinstance(item, dict) and item.get("status") != "success":
                lines.extend(browser_smoke_to_markdown(item).splitlines())
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def sanitize_case_id(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value.strip())
    return safe[:80] or "case"
