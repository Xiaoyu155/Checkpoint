from __future__ import annotations

import base64
import json
import importlib.util
from pathlib import Path
import re
from typing import Any
import urllib.request

from PIL import Image

from .capture import apply_capture_region, capture_visual_region
from .env import env_get
from .model_credentials import (
    normalize_provider,
    resolve_model_provider_config,
)
from .models import Observation, ProviderKind


VLM_INSTALL_HINT = (
    "Install torch/transformers and configure a local model_path for qwen2-vl or moondream; "
    "or pass mock_description for deterministic tests."
)
SUPPORTED_LOCAL_ENGINES = {"qwen2-vl", "moondream"}
SUPPORTED_CLOUD_ENGINES = {"cloud", "openai", "qwen", "xiaomimimo", "kimi", "deepseek", "volcengine"}


def observe_vision(params: dict[str, Any], run_dir: Path, *, synthetic_on_capture_fail: bool = False) -> Observation:
    image, path, region_metadata = load_or_capture_image(params, run_dir, synthetic_on_capture_fail=synthetic_on_capture_fail)
    engine = str(params.get("engine") or "mock").lower()
    prompt = str(params.get("prompt") or "Describe the current screen state.")
    fallback_chain: list[dict[str, Any]] = []

    if engine in {"auto", "mock"} and ("mock_description" in params or "mock_text" in params):
        description = str(params.get("mock_description") or params.get("mock_text") or "mock vision observation")
        status = str(params.get("mock_status") or "unknown")
        engine = "mock"
        engine_available = True
        install_hint = None
        engine_status = {
            "engine": "mock",
            "available": True,
            "module_available": True,
            "model_path": None,
            "error": None,
            "install_hint": None,
        }
    elif engine in SUPPORTED_CLOUD_ENGINES or (engine == "auto" and cloud_vision_configured(params)):
        engine_status = detect_cloud_vision_backend(params)
        engine = str(engine_status["engine"])
        engine_available = bool(engine_status["available"])
        install_hint = None if engine_available else "Configure model_credentials or set VISUAL_AGENT_VLM_* overrides."
        if engine_available:
            try:
                description = cloud_vision_query(
                    str(path),
                    prompt,
                    provider=str(engine_status["provider"]),
                    api_key=str(engine_status["_api_key"]),
                    base_url=str(engine_status["base_url"]),
                    model=str(engine_status["model"]),
                    timeout_seconds=float(params.get("timeout_seconds", 30.0)),
                )
                status = "success" if description else "empty"
            except Exception as exc:
                failed_status = {**engine_status, "available": False, "error": f"{exc.__class__.__name__}: {exc}"}
                fallback_chain.append(
                    {
                        "from": "cloud",
                        "status": "error",
                        "reason": failed_status["error"],
                    }
                )
                fallback = fallback_vision_description(image, path, prompt, params, failed_status)
                description = fallback["description"]
                status = fallback["status"]
                engine = fallback["engine"]
                engine_available = bool(fallback["engine_available"])
                engine_status = fallback["engine_status"]
                install_hint = fallback["install_hint"]
                fallback_chain.extend(fallback["fallback_chain"])
        else:
            fallback_chain.append(
                {
                    "from": "cloud",
                    "status": "unavailable",
                    "reason": engine_status.get("error"),
                }
            )
            fallback = fallback_vision_description(image, path, prompt, params, engine_status)
            description = fallback["description"]
            status = fallback["status"]
            engine = fallback["engine"]
            engine_available = bool(fallback["engine_available"])
            engine_status = fallback["engine_status"]
            install_hint = fallback["install_hint"]
            fallback_chain.extend(fallback["fallback_chain"])
    elif engine in {"auto", *SUPPORTED_LOCAL_ENGINES}:
        requested = str(params.get("local_engine") or "").lower()
        candidates = [requested] if requested else (["qwen2-vl", "moondream"] if engine == "auto" else [engine])
        engine_status = detect_vlm_backend(candidates[0], model_path=params.get("model_path"))
        if not engine_status["available"] and engine == "auto" and not requested:
            fallback = detect_vlm_backend("moondream", model_path=params.get("model_path"))
            if fallback["available"] or not engine_status.get("module_available"):
                engine_status = fallback
        engine = str(engine_status["engine"])
        engine_available = bool(engine_status["available"])
        install_hint = None if engine_available else VLM_INSTALL_HINT
        if engine_available:
            description, status = local_vlm_describe(image, prompt, params, engine_status=engine_status)
        else:
            description = ""
            status = "unavailable"
    else:
        description = ""
        status = "unavailable"
        engine_available = False
        install_hint = VLM_INSTALL_HINT
        engine_status = {
            "engine": engine,
            "available": False,
            "module_available": False,
            "model_path": str(params.get("model_path")) if params.get("model_path") else None,
            "error": f"Unsupported or unavailable VLM engine: {engine}",
            "install_hint": VLM_INSTALL_HINT,
        }

    elements = structured_vision_elements(description, params, image, engine=engine, status=status)
    structured_targets = [dict(element) for element in elements if element.get("role") == "vision_candidate"]

    return Observation(
        provider=ProviderKind.VISION,
        source=str(path),
        screenshot_path=path,
        width=image.width,
        height=image.height,
        elements=elements,
        metadata={
            "provider": "vision",
            "engine": engine,
            "engine_available": engine_available,
            "prompt": prompt,
            "status": status,
            "description": description,
            "structured_targets": structured_targets,
            "structured_target_count": len(structured_targets),
            "engine_status": public_engine_status(engine_status),
            "install_hint": install_hint,
            "fallback_chain": fallback_chain,
            **region_metadata,
        },
    )


def structured_vision_elements(
    description: str,
    params: dict[str, Any],
    image: Image.Image,
    *,
    engine: str,
    status: str,
) -> tuple[dict[str, Any], ...]:
    if not description:
        return ()

    bounds = vision_bounds(params, image)
    description_confidence = float(params.get("mock_confidence", 0.8)) if engine == "mock" else 0.0
    elements: list[dict[str, Any]] = [
        {
            "text": description,
            "role": "vision_description",
            "status": status,
            "confidence": description_confidence,
            "bounds": bounds,
            "engine": engine,
        }
    ]
    if params.get("parse_targets") is False:
        return tuple(elements)

    for candidate in parse_vision_candidates(description, params):
        elements.append(
            {
                "text": candidate["label"],
                "label": candidate["label"],
                "role": "vision_candidate",
                "target_role": candidate["target_role"],
                "status": status,
                "confidence": candidate["confidence"],
                "bounds": bounds,
                "engine": engine,
                "source": candidate["source"],
            }
        )
    return tuple(elements)


def parse_vision_candidates(description: str, params: dict[str, Any] | None = None) -> tuple[dict[str, Any], ...]:
    params = params or {}
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(label: str, *, source: str, confidence: float) -> None:
        normalized = normalize_candidate_label(label)
        if not normalized:
            return
        key = normalized.casefold()
        if key in seen:
            return
        seen.add(key)
        candidates.append(
            {
                "label": normalized,
                "target_role": infer_vision_target_role(normalized, description),
                "source": source,
                "confidence": confidence,
            }
        )

    for label in configured_candidate_labels(params):
        if label and label.lower() in description.lower():
            add(label, source="candidate_labels", confidence=0.72)

    for label in quoted_labels(description):
        add(label, source="quoted_label", confidence=0.68)

    for label, role_hint in ui_phrase_labels(description):
        normalized = normalize_candidate_label(label)
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "label": normalized,
                "target_role": role_hint or infer_vision_target_role(normalized, description),
                "source": "ui_phrase",
                "confidence": 0.62,
            }
        )

    return tuple(candidates)


def configured_candidate_labels(params: dict[str, Any]) -> tuple[str, ...]:
    raw = params.get("candidate_labels") or params.get("target_labels")
    if raw is None:
        return ()
    if isinstance(raw, str):
        return tuple(part.strip() for part in re.split(r"[,;\n]", raw) if part.strip())
    if isinstance(raw, (list, tuple)):
        return tuple(str(part).strip() for part in raw if str(part).strip())
    return ()


def quoted_labels(description: str) -> tuple[str, ...]:
    patterns = [
        r"[\"']([^\"']{1,40})[\"']",
        r"[“‘]([^”’]{1,40})[”’]",
        r"[「『]([^」』]{1,40})[」』]",
        r"《([^》]{1,40})》",
    ]
    labels: list[str] = []
    for pattern in patterns:
        labels.extend(match.group(1) for match in re.finditer(pattern, description))
    return tuple(labels)


def ui_phrase_labels(description: str) -> tuple[tuple[str, str], ...]:
    labels: list[tuple[str, str]] = []
    chinese_roles = {
        "按钮": "button",
        "链接": "link",
        "输入框": "textbox",
        "文本框": "textbox",
        "菜单": "menu",
        "标签页": "tab",
        "选项卡": "tab",
        "状态": "status",
    }
    for label, role_word in re.findall(r"([\w\u4e00-\u9fff][\w\s\u4e00-\u9fff-]{0,24}?)(按钮|链接|输入框|文本框|菜单|标签页|选项卡|状态)", description):
        labels.append((label, chinese_roles[role_word]))

    english_roles = {
        "button": "button",
        "link": "link",
        "input": "textbox",
        "field": "textbox",
        "textbox": "textbox",
        "menu": "menu",
        "tab": "tab",
        "status": "status",
    }
    for match in re.finditer(
        r"\b(button|link|input|field|textbox|menu|tab|status)\s+(?:labeled|called|named)?\s*['\"]?([A-Za-z0-9 _.-]{1,40})['\"]?",
        description,
        flags=re.IGNORECASE,
    ):
        labels.append((match.group(2), english_roles[match.group(1).lower()]))
    for match in re.finditer(
        r"\b([A-Za-z0-9][A-Za-z0-9 _.-]{0,39})\s+(button|link|input|field|textbox|menu|tab|status)\b",
        description,
        flags=re.IGNORECASE,
    ):
        raw_label = match.group(1)
        words = raw_label.split()
        lowered_words = [word.lower() for word in words]
        if "shows" in lowered_words:
            raw_label = " ".join(words[lowered_words.index("shows") + 1 :])
        elif len(words) > 3:
            raw_label = words[-1]
        labels.append((raw_label, english_roles[match.group(2).lower()]))
    return tuple(labels)


def normalize_candidate_label(label: str) -> str:
    value = " ".join(str(label or "").strip(" \t\r\n:：,，.。;；()[]{}").split())
    if not value or len(value) > 40:
        return ""
    stopwords = {
        "页面上有",
        "页面有",
        "可以看到",
        "there is",
        "there are",
        "visible",
        "a",
        "an",
        "the",
    }
    lowered = value.lower()
    for word in stopwords:
        if lowered == word:
            return ""
        if lowered.startswith(word + " "):
            value = value[len(word) + 1 :].strip()
            lowered = value.lower()
        elif value.startswith(word):
            value = value[len(word) :].strip()
            lowered = value.lower()
    return value.strip()


def infer_vision_target_role(label: str, description: str) -> str:
    index = description.lower().find(label.lower())
    if index < 0:
        index = description.find(label)
    if index >= 0:
        after = description[index + len(label) : index + len(label) + 12].lower()
        before = description[max(index - 12, 0) : index].lower()
        direct_roles = (
            ("button", ("button", "按钮")),
            ("link", ("link", "链接")),
            ("textbox", ("input", "field", "textbox", "输入框", "文本框")),
            ("menu", ("menu", "菜单")),
            ("tab", ("tab", "标签页", "选项卡")),
            ("status", ("status", "状态")),
        )
        for role, words in direct_roles:
            if any(word in after for word in words):
                return role
        for role, words in direct_roles:
            if any(word in before for word in words):
                return role
    start = max(index - 24, 0) if index >= 0 else 0
    end = min(index + len(label) + 24, len(description)) if index >= 0 else len(description)
    context = description[start:end].lower()
    role_words = (
        ("button", ("button", "按钮")),
        ("link", ("link", "链接")),
        ("textbox", ("input", "field", "textbox", "输入框", "文本框")),
        ("menu", ("menu", "菜单")),
        ("tab", ("tab", "标签页", "选项卡")),
        ("status", ("status", "状态")),
    )
    for role, words in role_words:
        if any(word in context for word in words):
            return role
    return "unknown"


def fallback_vision_description(
    image: Image.Image,
    image_path: Path,
    prompt: str,
    params: dict[str, Any],
    previous_status: dict[str, Any],
) -> dict[str, Any]:
    fallback_chain: list[dict[str, Any]] = []
    local_engine = str(params.get("fallback_local_engine") or params.get("local_engine") or "").lower()
    if local_engine:
        local_status = detect_vlm_backend(local_engine, model_path=params.get("model_path"))
        fallback_chain.append(
            {
                "to": local_engine,
                "status": "available" if local_status.get("available") else "unavailable",
                "reason": local_status.get("error"),
            }
        )
        if local_status.get("available"):
            description, status = local_vlm_describe(image, prompt, params, engine_status=local_status)
            return {
                "description": description,
                "status": status,
                "engine": str(local_status["engine"]),
                "engine_available": True,
                "engine_status": {**local_status, "fallback_from": previous_status.get("engine")},
                "install_hint": None,
                "fallback_chain": fallback_chain,
            }
    allow_mock = params.get("fallback_mock") is not False
    mock_description = params.get("fallback_mock_description") or params.get("mock_description") or params.get("mock_text")
    if allow_mock and mock_description:
        fallback_chain.append({"to": "mock", "status": "available", "reason": "fallback_mock_description configured"})
        return {
            "description": str(mock_description),
            "status": str(params.get("mock_status") or "fallback"),
            "engine": "mock",
            "engine_available": True,
            "engine_status": {
                "engine": "mock",
                "available": True,
                "module_available": True,
                "model_path": None,
                "error": None,
                "install_hint": None,
                "fallback_from": previous_status.get("engine"),
            },
            "install_hint": None,
            "fallback_chain": fallback_chain,
        }
    fallback_chain.append({"to": "none", "status": "unavailable", "reason": "no fallback provider configured"})
    return {
        "description": "",
        "status": "error" if previous_status.get("available") is False else "unavailable",
        "engine": str(previous_status.get("engine") or "cloud"),
        "engine_available": False,
        "engine_status": previous_status,
        "install_hint": "Configure a working cloud provider, local model_path, or fallback_mock_description.",
        "fallback_chain": fallback_chain,
    }


def detect_vlm_backend(engine: str = "qwen2-vl", *, model_path: Any = None) -> dict[str, Any]:
    normalized = str(engine or "qwen2-vl").lower()
    if normalized not in SUPPORTED_LOCAL_ENGINES:
        return {
            "engine": normalized,
            "available": False,
            "module_available": False,
            "model_path": str(model_path) if model_path else None,
            "error": f"Unsupported VLM engine: {normalized}",
            "install_hint": VLM_INSTALL_HINT,
        }
    modules = required_modules(normalized)
    missing = [module for module in modules if not module_available(module)]
    if missing:
        return {
            "engine": normalized,
            "available": False,
            "module_available": False,
            "missing_modules": missing,
            "model_path": str(model_path) if model_path else None,
            "error": "Missing Python modules: " + ", ".join(missing),
            "install_hint": VLM_INSTALL_HINT,
        }
    if not model_path:
        return {
            "engine": normalized,
            "available": False,
            "module_available": True,
            "missing_modules": [],
            "model_path": None,
            "error": "Missing local model_path.",
            "install_hint": VLM_INSTALL_HINT,
        }
    path = Path(str(model_path))
    if not path.exists():
        return {
            "engine": normalized,
            "available": False,
            "module_available": True,
            "missing_modules": [],
            "model_path": str(path),
            "error": f"Local model_path does not exist: {path}",
            "install_hint": VLM_INSTALL_HINT,
        }
    return {
        "engine": normalized,
        "available": True,
        "module_available": True,
        "missing_modules": [],
        "model_path": str(path),
        "error": None,
        "install_hint": None,
    }


def cloud_vision_configured(params: dict[str, Any] | None = None) -> bool:
    params = params or {}
    return detect_cloud_vision_backend(params).get("api_key_configured") is True


def detect_cloud_vision_backend(params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params or {}
    provider = normalize_provider(
        params.get("provider")
        or env_get("VISUAL_AGENT_VLM_PROVIDER")
        or env_get("VISUAL_AGENT_MODEL_PROVIDER")
        or "openai"
    )
    config = resolve_model_provider_config(
        source=params.get("credential_source") or env_get("VISUAL_AGENT_VLM_CREDENTIAL_FILE"),
        preferred_provider=provider,
        api_key=str(params.get("api_key") or env_get("VISUAL_AGENT_VLM_API_KEY") or ""),
        base_url=str(params.get("base_url") or env_get("VISUAL_AGENT_VLM_BASE_URL") or ""),
        model=str(params.get("model") or env_get("VISUAL_AGENT_VLM_MODEL") or ""),
    )
    blockers = list(config.get("blockers") or [])
    return {
        "engine": "cloud",
        "provider": config["provider"],
        "available": not blockers,
        "module_available": True,
        "base_url": config["base_url"],
        "model": config["model"],
        "_api_key": config["_api_key"],
        "api_key_configured": bool(config.get("api_key_configured")),
        "auth_headers_configured": bool(config.get("_auth_headers")),
        "credential_source": config["source"],
        "blockers": blockers,
        "error": ", ".join(blockers) if blockers else None,
        "install_hint": None if not blockers else "Configure model_credentials or set VISUAL_AGENT_VLM_* overrides.",
    }


def public_engine_status(status: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in status.items() if key != "_api_key"}


def vlm_doctor_summary(params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params or {}
    cloud = public_engine_status(detect_cloud_vision_backend(params))
    local = {
        "qwen2-vl": detect_vlm_backend("qwen2-vl", model_path=params.get("model_path")),
        "moondream": detect_vlm_backend("moondream", model_path=params.get("model_path")),
    }
    cloud_blockers = list(cloud.get("blockers") or [])
    local_ready = [name for name, status in local.items() if status.get("available")]
    cloud_ready = bool(cloud.get("available"))
    return {
        "ok": cloud_ready or bool(local_ready),
        "recommended_engine": "cloud" if cloud_ready else (local_ready[0] if local_ready else "mock"),
        "cloud": {
            "available": cloud_ready,
            "provider": cloud.get("provider"),
            "credential_source": cloud.get("credential_source"),
            "api_key_configured": bool(cloud.get("api_key_configured")),
            "auth_headers_configured": bool(cloud.get("auth_headers_configured")),
            "model": cloud.get("model"),
            "base_url": cloud.get("base_url"),
            "blockers": cloud_blockers,
            "error": cloud.get("error"),
            "install_hint": cloud.get("install_hint"),
        },
        "local": {
            name: {
                "available": bool(status.get("available")),
                "module_available": bool(status.get("module_available")),
                "missing_modules": list(status.get("missing_modules") or []),
                "model_path": status.get("model_path"),
                "error": status.get("error"),
                "install_hint": status.get("install_hint"),
            }
            for name, status in local.items()
        },
    }


def cloud_vision_query(
    image_path: str,
    prompt: str,
    *,
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
    timeout_seconds: float = 30.0,
) -> str:
    path = Path(image_path)
    with open(path, "rb") as handle:
        b64 = base64.b64encode(handle.read()).decode("utf-8")
    mime = image_mime_type(path)
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "max_tokens": 512,
        "temperature": 0,
    }
    url = base_url.rstrip("/") + "/chat/completions"
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **resolve_model_provider_config(preferred_provider=provider, api_key=api_key)["_auth_headers"]},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=float(timeout_seconds)) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
    first = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, list):
        parts = [str(item.get("text") or "") for item in content if isinstance(item, dict)]
        return "\n".join(part for part in parts if part).strip()
    return str(content or "").strip()


def image_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    return "image/png"


def required_modules(engine: str) -> tuple[str, ...]:
    if engine == "qwen2-vl":
        return ("torch", "transformers")
    if engine == "moondream":
        return ("torch", "transformers")
    return ()


def module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def local_vlm_describe(
    image: Image.Image,
    prompt: str,
    params: dict[str, Any],
    *,
    engine_status: dict[str, Any],
) -> tuple[str, str]:
    adapter = str(params.get("adapter") or "diagnostic").lower()
    if adapter != "diagnostic":
        raise RuntimeError(f"Unsupported local VLM adapter: {adapter}")
    description = (
        f"local {engine_status['engine']} backend configured; "
        f"image={image.width}x{image.height}; prompt={prompt}"
    )
    return description, "configured"


def load_or_capture_image(
    params: dict[str, Any],
    run_dir: Path,
    *,
    synthetic_on_capture_fail: bool,
) -> tuple[Image.Image, Path, dict[str, Any]]:
    if params.get("path"):
        path = Path(str(params["path"]))
        image = Image.open(path).convert("RGB")
        image, path, metadata = apply_capture_region(image, path, params, output_dir=run_dir, label="vision-region")
        return image, path, metadata

    if "mock_description" in params or "mock_text" in params:
        width = int(params.get("mock_width", 1280))
        height = int(params.get("mock_height", 720))
        image = Image.new("RGB", (width, height), color=(242, 244, 248))
        path = run_dir / "vision-mock.png"
        image.save(path)
        image, path, metadata = apply_capture_region(image, path, params, output_dir=run_dir, label="vision-region")
        return image, path, metadata

    image, path, metadata = capture_visual_region(
        params,
        output_dir=run_dir,
        label="vision-region",
        synthetic_on_capture_fail=synthetic_on_capture_fail,
    )
    return image, path, metadata


def vision_bounds(params: dict[str, Any], image: Image.Image) -> dict[str, int]:
    bounds = params.get("mock_bounds")
    if isinstance(bounds, dict):
        return {
            "left": int(bounds.get("left", 0)),
            "top": int(bounds.get("top", 0)),
            "width": int(bounds.get("width", image.width)),
            "height": int(bounds.get("height", image.height)),
        }
    return {"left": 0, "top": 0, "width": image.width, "height": image.height}
