from __future__ import annotations

import json
import shutil
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import urlparse

from .playwright_env import ensure_playwright_browsers_path


DEFAULT_AUTH_DIR = ".agent-auth"


def build_auth_state_import_plan(
    source: str | Path,
    *,
    name: str,
    workspace_root: str | Path = ".",
    overwrite: bool = False,
) -> dict[str, Any]:
    source_path = Path(source)
    target_path = auth_state_path(name, workspace_root=workspace_root)
    return {
        "schema_version": 1,
        "source": str(source_path),
        "target": str(target_path),
        "name": name,
        "overwrite": overwrite,
        "source_exists": source_path.exists(),
        "target_exists": target_path.exists(),
        "safe_target": is_auth_state_path(target_path, workspace_root=workspace_root),
    }


def import_auth_state(
    source: str | Path,
    *,
    name: str,
    workspace_root: str | Path = ".",
    overwrite: bool = False,
) -> dict[str, Any]:
    plan = build_auth_state_import_plan(source, name=name, workspace_root=workspace_root, overwrite=overwrite)
    source_path = Path(plan["source"])
    target_path = Path(plan["target"])
    if not plan["source_exists"]:
        raise FileNotFoundError(f"Storage state source not found: {source_path}")
    if not plan["safe_target"]:
        raise ValueError(f"Storage state target must stay under {DEFAULT_AUTH_DIR}: {target_path}")
    payload = load_storage_state_payload(source_path)
    if target_path.exists() and not overwrite:
        raise FileExistsError(f"Storage state already exists: {target_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, target_path)
    metadata = inspect_storage_state(target_path)
    manifest_path = write_auth_state_manifest(target_path, metadata)
    return {
        "schema_version": 1,
        "imported": True,
        "name": name,
        "path": str(target_path),
        "manifest_path": str(manifest_path),
        "metadata": metadata,
        "cookie_count": len(payload.get("cookies", [])) if isinstance(payload.get("cookies"), list) else 0,
        "origin_count": len(payload.get("origins", [])) if isinstance(payload.get("origins"), list) else 0,
    }


def inspect_storage_state(path: str | Path) -> dict[str, Any]:
    state_path = Path(path)
    payload = load_storage_state_payload(state_path)
    stat = state_path.stat()
    cookies = payload.get("cookies") if isinstance(payload.get("cookies"), list) else []
    origins = payload.get("origins") if isinstance(payload.get("origins"), list) else []
    now = time()
    expiring_cookies = [cookie for cookie in cookies if cookie_expires_at(cookie) is not None]
    expired_cookies = [cookie for cookie in expiring_cookies if float(cookie_expires_at(cookie) or 0.0) <= now]
    session_cookies = [cookie for cookie in cookies if cookie_expires_at(cookie) is None]
    return {
        "schema_version": 1,
        "path": str(state_path),
        "filename": state_path.name,
        "size_bytes": stat.st_size,
        "modified_at": stat.st_mtime,
        "valid": True,
        "cookie_count": len(cookies),
        "origin_count": len(origins),
        "session_cookie_count": len(session_cookies),
        "persistent_cookie_count": len(expiring_cookies),
        "expired_cookie_count": len(expired_cookies),
        "earliest_cookie_expires_at": earliest_cookie_expires_at(cookies),
        "has_session_material": bool(cookies or origins),
        "domains": sorted({safe_cookie_domain(cookie) for cookie in cookies if safe_cookie_domain(cookie)}),
        "origin_hosts": sorted({safe_origin_host(origin) for origin in origins if safe_origin_host(origin)}),
        "contains_sensitive_values": bool(cookies or origins),
        "redacted": True,
    }


def probe_storage_state(
    path: str | Path,
    *,
    url: str,
    allowed_domain: str | None = None,
    headed: bool = False,
    timeout_ms: int = 10_000,
) -> dict[str, Any]:
    state_path = Path(path)
    metadata = inspect_storage_state(state_path)
    domain = (allowed_domain or urlparse(url).hostname or "").strip().lower()
    matched = storage_state_matches_domain(metadata, domain) if domain else False
    has_session = bool(metadata.get("has_session_material"))
    all_cookies_expired = (
        int(metadata.get("cookie_count") or 0) > 0
        and int(metadata.get("expired_cookie_count") or 0) >= int(metadata.get("cookie_count") or 0)
        and int(metadata.get("origin_count") or 0) == 0
    )
    result = {
        "schema_version": 1,
        "path": str(state_path),
        "url": url,
        "allowed_domain": domain,
        "metadata": metadata,
        "redacted": True,
        "domain_match": matched,
        "has_session_material": has_session,
        "all_cookies_expired": all_cookies_expired,
        "loaded": False,
        "status": "blocked",
        "blockers": [],
    }
    blockers: list[str] = []
    if not has_session:
        blockers.append("empty_storage_state")
    if domain and not matched:
        blockers.append("domain_mismatch")
    if all_cookies_expired:
        blockers.append("expired_cookies")
    if blockers:
        result["blockers"] = blockers

    try:
        ensure_playwright_browsers_path(state_path.parent)
        sync_playwright = get_sync_playwright()
    except ImportError:
        return {**result, "status": "error", "error": {"type": "ImportError", "message": "Playwright is not installed. Run: pip install -e .[web]"}}

    playwright = sync_playwright().start()
    browser = None
    browser_context = None
    try:
        browser = playwright.chromium.launch(headless=not bool(headed))
        browser_context = browser.new_context(storage_state=str(state_path), accept_downloads=False)
        page = browser_context.new_page()
        page.route("**/*", lambda route: route.fulfill(status=200, body=auth_probe_html(domain), headers={"content-type": "text/html"}))
        page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_ms))
        title = page.title()
        final_url = page.url
        loaded = True
        return {
            **result,
            "loaded": loaded,
            "status": "ready" if loaded and not blockers else "blocked",
            "blockers": blockers,
            "page": {
                "url": final_url,
                "title": title,
            },
        }
    except Exception as exc:
        return {**result, "status": "error", "error": {"type": exc.__class__.__name__, "message": str(exc)}}
    finally:
        if browser_context is not None:
            browser_context.close()
        if browser is not None:
            browser.close()
        playwright.stop()


def storage_state_matches_domain(metadata: dict[str, Any], allowed_domain: str) -> bool:
    allowed = allowed_domain.strip().lower().lstrip(".")
    hosts = list(metadata.get("domains") or []) + list(metadata.get("origin_hosts") or [])
    return any(auth_host_matches_allowed(str(host), allowed) for host in hosts)


def auth_host_matches_allowed(host: str, allowed_domain: str) -> bool:
    host = host.strip().lower().lstrip(".")
    allowed = allowed_domain.strip().lower().lstrip(".")
    return bool(host and allowed and (host == allowed or host.endswith(f".{allowed}") or allowed.endswith(f".{host}")))


def auth_probe_html(domain: str) -> str:
    title = "Auth State Probe"
    return f"<!doctype html><html><head><title>{title}</title></head><body><h1>{title}</h1><p>{domain}</p></body></html>"


def auth_state_probe_to_markdown(result: dict[str, Any]) -> str:
    page = result.get("page") if isinstance(result.get("page"), dict) else {}
    lines = [
        "# Auth State Probe",
        "",
        f"- Status: `{result.get('status') or 'unknown'}`",
        f"- Loaded in browser context: `{bool(result.get('loaded'))}`",
        f"- Domain match: `{bool(result.get('domain_match'))}`",
        f"- Has session material: `{bool(result.get('has_session_material'))}`",
        f"- All cookies expired: `{bool(result.get('all_cookies_expired'))}`",
        f"- Allowed domain: `{result.get('allowed_domain') or ''}`",
        f"- Final URL: `{page.get('url') or ''}`",
    ]
    blockers = result.get("blockers") if isinstance(result.get("blockers"), list) else []
    if blockers:
        lines.append("- Blockers: " + ", ".join(f"`{item}`" for item in blockers))
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    if error:
        lines.append(f"- Error: `{error.get('type') or 'Error'}` {error.get('message') or ''}")
    lines.append("")
    return "\n".join(lines)


def get_sync_playwright() -> Any:
    from playwright.sync_api import sync_playwright

    return sync_playwright


def load_storage_state_payload(path: str | Path) -> dict[str, Any]:
    state_path = Path(path)
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Storage state is not valid JSON: {state_path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Storage state must be a JSON object.")
    cookies = payload.get("cookies", [])
    origins = payload.get("origins", [])
    if not isinstance(cookies, list) or not isinstance(origins, list):
        raise ValueError("Storage state must contain list fields: cookies and origins.")
    return payload


def write_auth_state_manifest(target_path: Path, metadata: dict[str, Any]) -> Path:
    manifest_path = target_path.with_suffix(target_path.suffix + ".manifest.json")
    manifest = {
        "schema_version": 1,
        "created_at": time(),
        "storage_state": metadata,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def auth_state_path(name: str, *, workspace_root: str | Path = ".") -> Path:
    safe_name = safe_auth_state_name(name)
    return Path(workspace_root) / DEFAULT_AUTH_DIR / f"{safe_name}.json"


def safe_auth_state_name(name: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in name.strip()).strip("-_")
    if not safe:
        raise ValueError("Auth state name cannot be empty.")
    return safe


def is_auth_state_path(path: Path, *, workspace_root: str | Path = ".") -> bool:
    root = (Path(workspace_root) / DEFAULT_AUTH_DIR).resolve()
    target = path.resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return target.suffix == ".json"


def safe_cookie_domain(cookie: Any) -> str | None:
    if not isinstance(cookie, dict):
        return None
    domain = str(cookie.get("domain") or "").strip().lower()
    return domain.lstrip(".") or None


def safe_origin_host(origin: Any) -> str | None:
    if not isinstance(origin, dict):
        return None
    raw = str(origin.get("origin") or "").strip().lower()
    for prefix in ("https://", "http://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
    return raw.split("/", 1)[0] or None


def cookie_expires_at(cookie: Any) -> float | None:
    if not isinstance(cookie, dict) or "expires" not in cookie:
        return None
    try:
        value = float(cookie.get("expires"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def earliest_cookie_expires_at(cookies: list[Any]) -> float | None:
    values = [cookie_expires_at(cookie) for cookie in cookies]
    positive = [float(value) for value in values if value is not None]
    return min(positive) if positive else None
