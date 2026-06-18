from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .llm_providers import LLMBackend, resolve_llm_backend, run_llm_completion
from .workflow import parse_workflow_file, workflow_from_dict
from .workflow_quality import score_workflow_quality


DEFAULT_MODEL = "claude-haiku-4-5-20251001"

WORKFLOW_SYSTEM_PROMPT = """You generate Checkpoint workflow YAML for local UI verification.

Return only valid YAML, without markdown fences or explanation.

Use this schema:
schema_version: 1
min_runtime_version: "0.1.0"
name: snake_case_name
version: 1
description: "Human readable workflow description"
tags: [verification, fast]
visibility: private
author: ""
license: ""
steps:
  - id: observe_page
    action: observe_browser
    url: "http://localhost:3000"
  - id: browser_ready
    action: assert_browser_ready
    min_text_length: 1
    min_interactive: 1
  - id: fill_email
    action: type
    target:
      label: "Email"
      role: input
    value: "demo@example.com"
  - id: click_submit
    action: click
    target:
      text: "Submit"
      role: button
    wait_after_seconds: 0.5
    browser_post_action_observe: true
  - id: observe_result
    action: observe_browser
    reuse_page: true
  - id: verify_result
    action: assert_text
    text: "Success"

Available actions:
- observe_browser: url or reuse_page
- observe_dom: url
- click: target with selector, text, label, role, contains_text, row_text, or column_header
- click_visual: description, optionally provider=omniparser for desktop UI
- type: target plus value
- paste: target plus value
- press_key: keys
- click_text: text, label, or contains_text
- wait_for_text: text or contains_text
- upload_file: path plus selector (file input) or via_chooser: true
- select_option: selector plus value, label, or index
- drag: selector (source) plus to_selector (destination)
- assert_text: text
- assert_visual_text: text, optionally region for desktop UI
- assert_no_error
- assert_browser_ready
- assert_product_contract
- assert_visual_quality: zero-config visual audit (font size, overflow, broken images, occluded controls)
- request_api

Rules:
1. For browser workflows, start with observe_browser.
2. Add assert_browser_ready after the initial browser observation so blank pages fail.
3. After mutating browser actions, set browser_post_action_observe: true or add observe_browser with reuse_page: true before assertions.
4. Always include tags with verification. Add fast for short checks.
5. Always include visibility: private, author: "", and license: "".
6. Use only Checkpoint actions listed above.
7. Prefer semantic targets over coordinates.
8. End with assertions that verify the expected outcome.
"""

EXAMPLE_ROOT = Path(__file__).resolve().parents[2] / "workflows" / "examples"

EXAMPLE_GROUPS: dict[str, tuple[str, ...]] = {
    "auth": ("login_basic", "login_redirect", "register_form", "logout_flow", "password_reset"),
    "forms": ("contact_form", "search_form", "filter_panel", "multi_step_form", "inline_edit"),
    "navigation": ("home_smoke", "tab_switch", "breadcrumb", "pagination", "deep_link"),
    "ecommerce": ("product_list", "product_detail", "add_to_cart", "checkout_flow", "order_confirm"),
    "states": ("empty_list", "loading_skeleton", "error_boundary", "success_toast", "offline_fallback"),
    "admin": ("dashboard_smoke", "data_table", "create_record", "edit_record", "delete_confirm"),
    "mobile_h5": ("h5_home", "h5_login", "h5_list", "h5_detail", "h5_form"),
}

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "auth": ("login", "sign in", "signin", "logout", "register", "password", "auth", "登录", "登出", "注册", "密码"),
    "forms": ("form", "input", "search", "filter", "edit", "submit", "表单", "搜索", "筛选", "编辑", "提交"),
    "navigation": ("home", "tab", "breadcrumb", "pagination", "deep link", "route", "导航", "标签", "分页", "面包屑", "路由"),
    "ecommerce": ("product", "cart", "checkout", "order", "shop", "商品", "购物车", "结账", "订单", "电商"),
    "states": ("empty", "loading", "error", "toast", "offline", "状态", "空", "加载", "错误", "离线", "提示"),
    "admin": ("admin", "dashboard", "table", "record", "crud", "后台", "仪表盘", "数据表", "记录"),
    "mobile_h5": ("mobile", "h5", "375", "812", "phone", "移动", "手机"),
}


def generate_workflow_yaml(
    *,
    description: str,
    workspace_root: Path,
    output_path: Path | None = None,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
    page_type: str | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    clean_description = description.strip()
    if not clean_description:
        return {"status": "error", "message": "description is required"}

    backend = resolve_llm_backend(model)
    generated = _generate_validated_yaml(clean_description, model=model, page_type=page_type)
    if generated.get("status") != "success":
        return generated
    yaml_text = str(generated["yaml"])
    if url:
        yaml_text = apply_entry_url(yaml_text, url)
    validation = dict(generated["validation"])
    if url:
        validation = _validate_generated_yaml(yaml_text)
        if validation.get("status") != "success":
            return {
                "status": "error",
                "message": "generated YAML became invalid after applying URL",
                "validation": validation,
                "yaml": yaml_text,
                "source": generated["source"],
                "attempts": generated["attempts"],
            }
    source = str(generated["source"])

    quality = score_workflow_quality(yaml_text)
    similar = find_similar_workflows(workspace_root.parent, clean_description)
    saved_to: str | None = None
    if not dry_run:
        if output_path is None:
            name = str(validation["workflow_name"])
            output_path = workspace_root.parent / "workflows" / f"{name}.yaml"
        output_path = output_path.resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(yaml_text.rstrip() + "\n", encoding="utf-8")
        saved_to = str(output_path)
        parse_workflow_file(output_path)

    return {
        "status": "success",
        "source": source,
        "provider": backend.provider,
        "generation_attempts": generated["attempts"],
        "model": backend.model_id if source != "template_fallback" else None,
        "workflow_name": validation["workflow_name"],
        "page_url": url,
        "quality_score": int(round(quality.total_score * 100)),
        "quality": {
            "score": quality.total_score,
            "assertion_density": quality.assertion_density,
            "business_assertions": quality.business_assertion_count,
            "structural_assertions": quality.structural_assertion_count,
            "visual_action_count": quality.visual_action_count,
            "visual_assertion_count": quality.visual_assertion_count,
            "gaps": list(quality.gaps),
            "recommendation": quality.recommendation,
        },
        "similar_workflows": similar,
        "yaml": yaml_text,
        "saved_to": saved_to,
        "message": _generation_message(saved_to=saved_to, similar=similar),
    }


def generate_workflow_variant(
    *,
    workspace_root: Path,
    existing: str,
    variant: str,
    output_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    if variant != "mobile":
        return {"status": "error", "message": f"unsupported variant: {variant}"}
    try:
        source_path = _resolve_existing_workflow(workspace_root, existing)
    except FileNotFoundError as exc:
        return {"status": "error", "message": str(exc)}
    try:
        import yaml

        payload = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "error", "message": f"failed to read existing workflow: {type(exc).__name__}: {exc}"}
    if not isinstance(payload, dict):
        return {"status": "error", "message": "existing workflow YAML root must be an object"}
    base_name = str(payload.get("name") or source_path.stem)
    payload["name"] = f"{base_name}_mobile"
    payload["description"] = str(payload.get("description") or base_name) + " (mobile viewport variant)"
    tags = [str(item) for item in payload.get("tags", []) if str(item)] if isinstance(payload.get("tags"), list) else []
    for tag in ("mobile_h5", "variant"):
        if tag not in tags:
            tags.append(tag)
    payload["tags"] = tags
    payload["visibility"] = payload.get("visibility") or "private"
    payload["author"] = payload.get("author") or ""
    payload["license"] = payload.get("license") or ""
    steps = payload.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict) and step.get("action") == "observe_browser":
                step["viewport"] = {"width": 375, "height": 812}
                break
    yaml_text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).rstrip() + "\n"
    validation = _validate_generated_yaml(yaml_text)
    if validation.get("status") != "success":
        return {"status": "error", "message": "variant YAML is not valid", "validation": validation, "yaml": yaml_text}
    saved_to = None
    if not dry_run:
        if output_path is None:
            output_path = workspace_root.parent / "workflows" / f"{payload['name']}.yaml"
        output_path = output_path.resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(yaml_text, encoding="utf-8")
        saved_to = str(output_path)
        parse_workflow_file(output_path)
    return {
        "status": "success",
        "source": "variant",
        "workflow_name": payload["name"],
        "variant": variant,
        "base_workflow": str(source_path),
        "yaml": yaml_text,
        "saved_to": saved_to,
        "message": f"Saved to: {saved_to}" if saved_to else "Generated workflow variant YAML.",
    }


def generate_workflows_from_sitemap(
    *,
    sitemap_path: Path,
    workspace_root: Path,
    output_dir: Path | None = None,
    dry_run: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    urls = sitemap_urls(sitemap_path)[:limit]
    if not urls:
        return {"status": "error", "message": "sitemap contains no URLs", "sitemap": str(sitemap_path)}
    output_dir = output_dir or workspace_root.parent / "workflows" / "sitemap"
    generated: list[dict[str, Any]] = []
    for url in urls:
        name = "smoke_" + _description_to_name(url).removeprefix("http_").removeprefix("https_")
        yaml_text = _sitemap_smoke_workflow(name, url)
        validation = _validate_generated_yaml(yaml_text)
        item = {"url": url, "workflow_name": name, "status": validation.get("status"), "yaml": yaml_text if dry_run else None}
        if validation.get("status") == "success" and not dry_run:
            path = unique_output_path(output_dir / f"{name}.yaml")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml_text, encoding="utf-8")
            item["saved_to"] = str(path)
        generated.append(item)
    failed = [item for item in generated if item.get("status") != "success"]
    return {
        "status": "success" if not failed else "partial",
        "source": "sitemap",
        "sitemap": str(sitemap_path),
        "workflow_count": len(generated),
        "generated": generated,
    }


def _generate_validated_yaml(description: str, *, model: str, page_type: str | None) -> dict[str, Any]:
    backend = resolve_llm_backend(model)
    source = backend.provider
    attempts: list[dict[str, Any]] = []
    try:
        for attempt in range(1, 4):
            yaml_text = _strip_markdown_fences(_generate_with_llm_backend(description, backend=backend, page_type=page_type))
            validation = _validate_generated_yaml(yaml_text)
            attempts.append({"attempt": attempt, "status": validation.get("status"), "message": validation.get("message")})
            if validation.get("status") == "success":
                return {"status": "success", "yaml": yaml_text, "validation": validation, "source": source, "attempts": attempts}
    except ImportError:
        source = "template_fallback"
    except Exception as exc:
        # Missing credentials / backend config should degrade to the offline template
        # (this is the keyless and dry-run path); any other failure is a real error.
        if not _is_auth_configuration_error(exc):
            return {"status": "error", "message": f"workflow generation failed: {type(exc).__name__}: {exc}", "attempts": attempts}
        source = "template_fallback"

    yaml_text = _template_workflow(description, page_type=page_type)
    validation = _validate_generated_yaml(yaml_text)
    attempts.append({"attempt": len(attempts) + 1, "status": validation.get("status"), "source": source, "message": validation.get("message")})
    if validation.get("status") == "success":
        return {"status": "success", "yaml": yaml_text, "validation": validation, "source": source, "attempts": attempts}
    return {
        "status": "error",
        "message": "generated YAML is not a valid Checkpoint workflow",
        "validation": validation,
        "yaml": yaml_text,
        "source": source,
        "attempts": attempts,
    }


def _generate_with_llm_backend(description: str, *, backend: LLMBackend, page_type: str | None = None) -> str:
    return run_llm_completion(
        backend=backend,
        system_prompt=build_workflow_system_prompt(description, page_type=page_type),
        prompt=description,
        max_tokens=1600,
    )


def _template_workflow(description: str, *, page_type: str | None = None) -> str:
    name = _description_to_name(description)
    category = normalize_page_type(page_type) or select_example_category(description)
    tags = ["verification", "fast"]
    if category:
        tags.append(category)
    quoted_description = json.dumps(description, ensure_ascii=False)
    tag_text = ", ".join(tags)
    path = {
        "auth": "/login",
        "forms": "/contact",
        "navigation": "/",
        "ecommerce": "/products",
        "states": "/items",
        "admin": "/admin",
        "mobile_h5": "/",
    }.get(category, "")
    return f"""schema_version: 1
min_runtime_version: "0.1.0"
name: {name}
version: 1
description: {quoted_description}
tags: [{tag_text}]
visibility: private
author: ""
license: ""
steps:
  - id: observe_page
    action: observe_browser
    url: "http://localhost:3000{path}"
{_template_viewport_line(category)}
  - id: browser_ready
    action: assert_browser_ready
    min_text_length: 1
    min_interactive: 1
  - id: verify_expected_text
    action: assert_text
    text: "Expected text here"
"""


def apply_entry_url(yaml_text: str, url: str) -> str:
    try:
        import yaml

        payload = yaml.safe_load(yaml_text)
        if not isinstance(payload, dict):
            return yaml_text
        steps = payload.get("steps")
        if not isinstance(steps, list):
            return yaml_text
        for step in steps:
            if isinstance(step, dict) and step.get("action") == "observe_browser":
                step["url"] = url
                break
        return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).rstrip() + "\n"
    except Exception:
        return yaml_text


def build_workflow_system_prompt(description: str, *, page_type: str | None = None) -> str:
    category = normalize_page_type(page_type) or select_example_category(description)
    examples = load_few_shot_examples(category)
    if not examples:
        return WORKFLOW_SYSTEM_PROMPT
    category_label = category or "general"
    return (
        WORKFLOW_SYSTEM_PROMPT.rstrip()
        + "\n\n"
        + f"Few-shot examples for the detected page type ({category_label}). Use their structure, metadata, and assertion style.\n"
        + "Prefer the closest example in this category first, then adapt the step names, targets, and assertions to the user description.\n"
        + "\n".join(
            f"### Example {index + 1} ({category_label})\n{example}"
            for index, example in enumerate(examples)
        )
    )


def select_example_category(description: str) -> str | None:
    text = description.lower()
    best_category: str | None = None
    best_score = 0
    for category, keywords in CATEGORY_KEYWORDS.items():
        score = sum(1 for keyword in keywords if keyword.lower() in text)
        if category == "mobile_h5" and score:
            score += 2
        if score > best_score:
            best_score = score
            best_category = category
    return best_category


def normalize_page_type(page_type: str | None) -> str | None:
    value = str(page_type or "").strip().lower()
    aliases = {
        "auth": "auth",
        "form": "forms",
        "forms": "forms",
        "list": "navigation",
        "detail": "navigation",
        "ecommerce": "ecommerce",
    }
    return aliases.get(value)


def load_few_shot_examples(category: str | None, *, limit: int = 5) -> list[str]:
    categories = [category] if category else ["auth", "forms", "navigation"]
    examples: list[str] = []
    for item in categories:
        if not item:
            continue
        for name in EXAMPLE_GROUPS.get(item, ())[:limit]:
            path = EXAMPLE_ROOT / item / f"{name}.yaml"
            if path.exists():
                examples.append(path.read_text(encoding="utf-8").strip())
    return examples


def _template_viewport_line(category: str | None) -> str:
    if category == "mobile_h5":
        return "    viewport: { width: 375, height: 812 }"
    return ""


def _validate_generated_yaml(yaml_text: str) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        return {"status": "error", "message": f"PyYAML is required: {exc}"}
    try:
        payload = yaml.safe_load(yaml_text)
        if not isinstance(payload, dict):
            return {"status": "error", "message": "YAML root must be an object."}
        workflow = workflow_from_dict(payload)
    except Exception as exc:
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "success",
        "workflow_name": workflow.name,
        "step_count": len(workflow.steps),
        "tags": list(workflow.tags),
    }


def _description_to_name(description: str) -> str:
    text = description.lower()
    words = re.findall(r"[a-z0-9]+", text)
    if not words:
        words = ["generated", "workflow"]
    name = "_".join(words[:6])[:50].strip("_")
    return name or "generated_workflow"


def _strip_markdown_fences(text: str) -> str:
    stripped = text.strip()
    stripped = re.sub(r"^```(?:yaml|yml)?\s*", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def find_similar_workflows(project_root: Path, description: str) -> list[dict[str, str]]:
    try:
        import yaml
    except ImportError:
        return []
    workflows_dir = project_root / "workflows"
    if not workflows_dir.exists():
        return []
    matches: list[dict[str, str]] = []
    normalized = _normalize_for_similarity(description)
    for path in workflows_dir.rglob("*.yaml"):
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        existing_description = str(payload.get("description") or payload.get("name") or "")
        if not existing_description:
            continue
        distance = _edit_distance(normalized, _normalize_for_similarity(existing_description))
        if distance < 5:
            matches.append(
                {
                    "name": str(payload.get("name") or path.stem),
                    "path": str(path),
                    "description": existing_description,
                    "distance": str(distance),
                }
            )
    return matches[:5]


def _normalize_for_similarity(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())[:120]


def _edit_distance(left: str, right: str) -> int:
    if abs(len(left) - len(right)) >= 5:
        return 5
    previous = list(range(len(right) + 1))
    for i, lchar in enumerate(left, 1):
        current = [i]
        for j, rchar in enumerate(right, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (0 if lchar == rchar else 1)))
        previous = current
    return previous[-1]


def _generation_message(*, saved_to: str | None, similar: list[dict[str, str]]) -> str:
    base = f"Saved to: {saved_to}" if saved_to else "Generated workflow YAML."
    if not similar:
        return base
    first = similar[0]
    return base + f" Existing similar workflow found: {first['path']}"


def _resolve_existing_workflow(workspace_root: Path, existing: str) -> Path:
    candidate = Path(existing)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    project_root = workspace_root.parent
    candidates = [
        workspace_root / "workflows" / existing,
        workspace_root / "workflows" / f"{existing}.yaml",
        project_root / "workflows" / existing,
        project_root / "workflows" / f"{existing}.yaml",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(f"workflow not found: {existing}")


def sitemap_urls(path: Path) -> list[str]:
    tree = ElementTree.parse(path)
    root = tree.getroot()
    urls: list[str] = []
    for element in root.iter():
        if element.tag.endswith("loc") and element.text and element.text.strip():
            urls.append(element.text.strip())
    return urls


def _sitemap_smoke_workflow(name: str, url: str) -> str:
    return f"""schema_version: 1
min_runtime_version: "0.1.0"
name: {name}
version: 1
description: "Smoke check for {url}"
tags: [verification, fast, sitemap, smoke]
visibility: private
author: ""
license: ""
steps:
  - id: observe_page
    action: observe_browser
    url: "{url}"
  - id: browser_ready
    action: assert_browser_ready
    min_text_length: 1
    min_interactive: 1
  - id: assert_no_error
    action: assert_no_error
"""


def unique_output_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}_{len(list(path.parent.glob(path.stem + '*')))}{path.suffix}")


def _is_auth_configuration_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        part in text
        for part in (
            "api_key",
            "api key",
            "auth_token",
            "authentication method",
            "credentials",
            "missing base url",
            "missing endpoint",
        )
    )
