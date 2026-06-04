from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Any, Literal

from .dispatcher import ActionDispatcher
from .providers import default_provider_registry


CapabilityKind = Literal["provider", "action", "assertion", "file", "auth", "extractor", "command", "dependency"]
RiskLevel = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class Capability:
    name: str
    kind: CapabilityKind
    available: bool
    description: str
    required: bool = True
    dependency: str | None = None
    install_hint: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    dry_run_supported: bool = False
    risk_level: RiskLevel = "low"
    planner_visible: bool = False


@dataclass(frozen=True)
class CapabilityManifest:
    capabilities: tuple[Capability, ...]

    @property
    def available_count(self) -> int:
        return sum(1 for capability in self.capabilities if capability.available)

    @property
    def missing_count(self) -> int:
        return sum(1 for capability in self.capabilities if not capability.available)


def build_capability_manifest() -> CapabilityManifest:
    capabilities: list[Capability] = []
    capabilities.extend(provider_capabilities())
    capabilities.extend(action_capabilities())
    capabilities.extend(workflow_atomic_capabilities())
    capabilities.extend(command_capabilities())
    capabilities.extend(dependency_capabilities())
    return CapabilityManifest(capabilities=tuple(capabilities))


def build_atomic_capability_manifest() -> CapabilityManifest:
    capabilities = [
        capability
        for capability in build_capability_manifest().capabilities
        if capability.planner_visible and capability.kind != "dependency"
    ]
    return CapabilityManifest(capabilities=tuple(capabilities))


def provider_capabilities() -> tuple[Capability, ...]:
    registry = default_provider_registry()
    descriptions = {
        "observe_screen": "Capture the primary screen into an Observation.",
        "observe_browser": "Open a persistent Playwright browser page for native DOM actions.",
        "observe_dom": "Read live web DOM with Playwright.",
        "observe_uia": "Read Windows UI Automation controls.",
        "observe_ocr": "Extract text boxes from a screenshot or image.",
        "observe_vision": "Describe screenshot state with a VLM provider.",
        "observe_state": "Convert the latest observation into structured page state: text, buttons, inputs, dialogs, errors, and loading/empty signals.",
        "observe_fixture": "Replay a saved Observation fixture.",
        "observe_html": "Read deterministic local HTML into DOM-like Observation.",
    }
    dependency_by_action = {
        "observe_dom": "playwright",
        "observe_browser": "playwright",
        "observe_uia": "uiautomation",
    }
    install_hint_by_dependency = {
        "playwright": "pip install -e .[web] && python -m playwright install chromium",
        "uiautomation": "pip install -e .[desktop]",
    }

    result = []
    for action in registry.actions:
        dependency = dependency_by_action.get(action)
        available = module_available(dependency) if dependency else True
        result.append(
            Capability(
                name=action,
                kind="provider",
                available=available,
                description=descriptions.get(action, action),
                required=False if dependency else True,
                dependency=dependency,
                install_hint=install_hint_by_dependency.get(dependency or ""),
                input_schema=provider_input_schema(action),
                output_schema={"type": "object", "fields": {"observation": "Observation"}},
                dry_run_supported=False,
                risk_level="low",
                planner_visible=action
                in {"observe_browser", "observe_dom", "observe_html", "observe_uia", "observe_ocr", "observe_vision", "observe_state"},
            )
        )
    return tuple(result)


def action_capabilities() -> tuple[Capability, ...]:
    dispatcher = ActionDispatcher()
    descriptions = {
        "click": "Click a resolved target.",
        "type": "Type text into a resolved target.",
        "paste": "Paste text into a resolved target.",
        "press_key": "Press a key or hotkey against a resolved target.",
        "click_text": "OCR text, locate it on screen, and click its center.",
        "wait_for_text": "Poll OCR until text appears on screen.",
    }
    return tuple(
        Capability(
            name=action,
            kind="action",
            available=True,
                description=descriptions.get(action, action),
                required=True,
                input_schema=action_input_schema(action),
                output_schema={"type": "object", "fields": {"action_result": "ActionResult"}},
                dry_run_supported=True,
                risk_level="medium" if action in {"click", "press_key", "click_text"} else "low",
                planner_visible=True,
            )
        for action in dispatcher.actions_available
    )


def workflow_atomic_capabilities() -> tuple[Capability, ...]:
    specs = [
        Capability(
            name="observe_state",
            kind="extractor",
            available=True,
            description="Convert the latest or named observation into structured product state: text, buttons, inputs, dialogs, errors, loading, and empty signals.",
            input_schema={"type": "object", "fields": {"observation": "string?", "max_text_items": "integer?"}},
            output_schema={"type": "object", "fields": {"state": "ProductState", "observation": "Observation"}},
            dry_run_supported=False,
            risk_level="low",
            planner_visible=True,
        ),
        Capability(
            name="resolve",
            kind="extractor",
            available=True,
            description="Resolve a semantic Target against the latest or named observation.",
            input_schema={"type": "object", "required": ["target"], "fields": {"target": "Target", "observation": "string?"}},
            output_schema={"type": "object", "fields": {"resolved_target": "ResolvedTarget"}},
            dry_run_supported=True,
            risk_level="low",
            planner_visible=True,
        ),
        Capability(
            name="assert_text",
            kind="assertion",
            available=True,
            description="Assert that the latest or named observation contains text.",
            input_schema={"type": "object", "required": ["text"], "fields": {"text": "string", "observation": "string?"}},
            output_schema={"type": "object", "fields": {"status": "success|failed"}},
            dry_run_supported=False,
            risk_level="low",
            planner_visible=True,
        ),
        Capability(
            name="assert_text_contract",
            kind="assertion",
            available=True,
            description="Assert required/forbidden text contracts against an observation with optional region and confidence filters.",
            input_schema={
                "type": "object",
                "fields": {
                    "text": "string?",
                    "required_all": "string[]|string?",
                    "required_any": "string[]|string?",
                    "forbidden_any": "string[]|string?",
                    "text_region": "CropRegion?",
                    "min_confidence": "number?",
                    "observation": "string?",
                },
            },
            output_schema={"type": "object", "fields": {"text_contract": "TextContractResult"}},
            dry_run_supported=False,
            risk_level="low",
            planner_visible=True,
        ),
        Capability(
            name="request_api",
            kind="action",
            available=True,
            description="Call an HTTP API from a workflow and record the response as a network event for assert_response.",
            input_schema={
                "type": "object",
                "required": ["url"],
                "fields": {
                    "url": "string",
                    "method": "GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS?",
                    "headers": "object?",
                    "body": "string?",
                    "json": "object|string|array?",
                    "timeout_seconds": "number?",
                    "mock_status": "integer?",
                },
            },
            output_schema={"type": "object", "fields": {"event": "NetworkEvent"}},
            dry_run_supported=True,
            risk_level="medium",
            planner_visible=True,
        ),
        Capability(
            name="assert_response",
            kind="assertion",
            available=True,
            description="Assert that a captured browser network response matches URL/method/status constraints.",
            input_schema={
                "type": "object",
                "fields": {
                    "url_contains": "string?",
                    "method": "string?",
                    "status": "integer?",
                    "status_min": "integer?",
                    "status_max": "integer?",
                    "ok": "boolean?",
                    "timeout_seconds": "number?",
                },
            },
            output_schema={"type": "object", "fields": {"event": "NetworkEvent"}},
            dry_run_supported=False,
            risk_level="low",
            planner_visible=True,
        ),
        Capability(
            name="assert_no_error",
            kind="assertion",
            available=True,
            description="Assert that page state and captured network events contain no visible error or failed request.",
            input_schema={"type": "object", "fields": {"observation": "string?"}},
            output_schema={"type": "object", "fields": {"no_error": "NoErrorStateResult"}},
            dry_run_supported=False,
            risk_level="low",
            planner_visible=True,
        ),
        Capability(
            name="assert_product_contract",
            kind="assertion",
            available=True,
            description="Assert a product quality contract: required sections/actions, forbidden entries, no-error state, and minimum primary actions.",
            input_schema={
                "type": "object",
                "fields": {
                    "required_sections": "string[]|string?",
                    "must_have_actions": "string[]|string?",
                    "forbidden_entries": "string[]|string?",
                    "forbidden_any": "string[]|string?",
                    "no_error_state": "boolean?",
                    "min_primary_actions": "integer?",
                    "observation": "string?",
                },
            },
            output_schema={"type": "object", "fields": {"product_contract": "ProductContractResult"}},
            dry_run_supported=False,
            risk_level="low",
            planner_visible=True,
        ),
        Capability(
            name="assert_ai_response_quality",
            kind="assertion",
            available=True,
            description="Assert basic AI answer quality: non-empty, relevant to the question/context, not repetitive, and not template-like.",
            input_schema={
                "type": "object",
                "fields": {
                    "text": "string?",
                    "response": "string?",
                    "question": "string?",
                    "previous_context": "string?",
                    "min_length": "integer?",
                    "require_answer_relevance": "boolean?",
                    "require_context_reference": "boolean?",
                    "require_specific_advice": "boolean?",
                    "forbidden_phrases": "string[]|string?",
                    "observation": "string?",
                },
            },
            output_schema={"type": "object", "fields": {"ai_response_quality": "AIResponseQualityResult"}},
            dry_run_supported=False,
            risk_level="low",
            planner_visible=True,
        ),
        Capability(
            name="expect_download",
            kind="file",
            available=True,
            description="Click a DOM target and save the resulting browser download into the run directory.",
            input_schema={
                "type": "object",
                "required": ["target"],
                "fields": {"target": "Target", "save_as": "string?", "timeout_seconds": "number?"},
            },
            output_schema={"type": "object", "fields": {"path": "string", "size_bytes": "integer"}},
            dry_run_supported=True,
            risk_level="medium",
            planner_visible=True,
        ),
        Capability(
            name="assert_file_exists",
            kind="file",
            available=True,
            description="Assert that a file exists and optionally verify extension and minimum size.",
            input_schema={
                "type": "object",
                "fields": {"path": "string?", "from_download": "string?", "extension": "string?", "min_bytes": "integer?"},
            },
            output_schema={"type": "object", "fields": {"path": "string", "size_bytes": "integer"}},
            dry_run_supported=False,
            risk_level="low",
            planner_visible=True,
        ),
        Capability(
            name="save_storage_state",
            kind="auth",
            available=True,
            description="Save Playwright browser context storage state for later authenticated runs.",
            input_schema={"type": "object", "fields": {"path": "string?"}},
            output_schema={"type": "object", "fields": {"path": "string", "size_bytes": "integer"}},
            dry_run_supported=False,
            risk_level="high",
            planner_visible=True,
        ),
        Capability(
            name="wait_for",
            kind="assertion",
            available=True,
            description="Poll until text, target, selector, URL, or network response conditions are satisfied.",
            input_schema={
                "type": "object",
                "required": ["condition|conditions"],
                "fields": {
                    "condition": "text|target|selector|url|response?",
                    "conditions": "WaitCondition[]?",
                    "match": "all|any?",
                    "text": "string?",
                    "target": "Target?",
                    "selector": "string?",
                    "url": "string?",
                    "url_contains": "string?",
                    "url_regex": "string?",
                    "method": "GET|POST|PUT|PATCH|DELETE?",
                    "status": "integer?",
                    "status_min": "integer?",
                    "status_max": "integer?",
                    "ok": "boolean?",
                    "observation": "string?",
                    "timeout_seconds": "number?",
                    "interval_seconds": "number?",
                },
            },
            output_schema={"type": "object", "fields": {"status": "success|failed", "attempts": "integer"}},
            dry_run_supported=False,
            risk_level="low",
            planner_visible=True,
        ),
        Capability(
            name="locate_table_cell",
            kind="extractor",
            available=True,
            description="Locate a DOM target constrained by table row and column semantics.",
            input_schema={
                "type": "object",
                "required": ["target"],
                "fields": {
                    "target.row_contains_text": "string?",
                    "target.row_text_regex": "string?",
                    "target.column_header": "string?",
                    "target.column_text_regex": "string?",
                },
            },
            output_schema={"type": "object", "fields": {"resolved_target": "ResolvedTarget"}},
            dry_run_supported=True,
            risk_level="low",
            planner_visible=True,
        ),
        Capability(
            name="locate_relative_target",
            kind="extractor",
            available=True,
            description="Locate a DOM target constrained by nearby text or dialog scope.",
            input_schema={
                "type": "object",
                "required": ["target"],
                "fields": {
                    "target.near_text": "string?",
                    "target.near_contains_text": "string?",
                    "target.near_text_regex": "string?",
                    "target.scope_role": "string?",
                    "target.scope_text": "string?",
                    "target.scope_contains_text": "string?",
                },
            },
            output_schema={"type": "object", "fields": {"resolved_target": "ResolvedTarget"}},
            dry_run_supported=True,
            risk_level="low",
            planner_visible=True,
        ),
    ]
    return tuple(specs)


def command_capabilities() -> tuple[Capability, ...]:
    commands = {
        "run-workflow": "Run an audited workflow.",
        "preflight-workflow": "Run validation and capability checks without executing.",
        "validate-workflow": "Validate workflow structure without running it.",
        "list-runs": "List audited workflow runs.",
        "show-run": "Show a run summary.",
        "report-run": "Show a schema-versioned detailed run report.",
        "inspect-dom": "Inspect live web DOM.",
        "inspect-uia": "Inspect Windows UIA controls.",
        "workspace-record-browser": "Record a headed browser session into a workflow draft.",
    }
    return tuple(
        Capability(name=name, kind="command", available=True, description=description)
        for name, description in sorted(commands.items())
    )


def provider_input_schema(action: str) -> dict[str, Any]:
    schemas = {
        "observe_browser": {
            "type": "object",
            "required": ["url|reuse_page"],
            "fields": {
                "url": "string",
                "reuse_page": "boolean?",
                "storage_state": "string?",
                "routes": "RouteMock[]?",
                "headed": "boolean?",
                "timeout_ms": "integer?",
            },
        },
        "observe_dom": {"type": "object", "required": ["url"], "fields": {"url": "string", "headed": "boolean?"}},
        "observe_html": {"type": "object", "required": ["path"], "fields": {"path": "string"}},
        "observe_state": {"type": "object", "fields": {"observation": "string?", "max_text_items": "integer?"}},
        "observe_uia": {"type": "object", "fields": {"max_depth": "integer?"}},
        "observe_ocr": {
            "type": "object",
            "fields": {
                "path": "string?",
                "engine": "auto|screen-ocr|winrt|tesseract|mock?",
                "mock_text": "string?",
                "mock_bounds": "Bounds?",
                "min_confidence": "number?",
                "language": "string?",
                "lang": "string?",
                "window": "UIAWindowMatch?",
                "post_capture": "minimize|keep?",
                "minimize_after": "boolean?",
                "keep_open": "boolean?",
                "crop": "CropRegion?",
                "simulator_crop": "CropRegion?",
                "fallback_to_screen": "boolean?",
                "*_from": "input.path?",
                "*_default": "fallback value?",
                "language": "tesseract language?",
                "lang": "tesseract language?",
            },
        },
        "observe_vision": {
            "type": "object",
            "fields": {
                "path": "string?",
                "screenshot_from": "latest|step_id|page?",
                "engine": "auto|mock|qwen2-vl|moondream?",
                "local_engine": "qwen2-vl|moondream?",
                "model_path": "string?",
                "adapter": "diagnostic?",
                "prompt": "string?",
                "mock_description": "string?",
                "mock_status": "string?",
                "mock_bounds": "Bounds?",
                "parse_targets": "boolean?",
                "candidate_labels": "string[]|string?",
                "fallback_local_engine": "qwen2-vl|moondream?",
                "fallback_mock": "boolean?",
                "fallback_mock_description": "string?",
                "window": "UIAWindowMatch?",
                "post_capture": "minimize|keep?",
                "minimize_after": "boolean?",
                "keep_open": "boolean?",
                "crop": "CropRegion?",
                "simulator_crop": "CropRegion?",
                "fallback_to_screen": "boolean?",
                "*_from": "input.path?",
                "*_default": "fallback value?",
            },
        },
        "observe_fixture": {"type": "object", "required": ["path"], "fields": {"path": "string"}},
        "observe_screen": {
            "type": "object",
            "fields": {
                "synthetic_on_capture_fail": "boolean?",
                "window": "UIAWindowMatch?",
                "post_capture": "minimize|keep?",
                "minimize_after": "boolean?",
                "keep_open": "boolean?",
                "crop": "CropRegion?",
                "simulator_crop": "CropRegion?",
                "fallback_to_screen": "boolean?",
                "*_from": "input.path?",
                "*_default": "fallback value?",
            },
        },
    }
    return schemas.get(action, {"type": "object", "fields": {}})


def action_input_schema(action: str) -> dict[str, Any]:
    if action == "click":
        return {
            "type": "object",
            "required": ["target"],
            "fields": {
                "target": "Target",
                "dry_run": "boolean?",
                "allow_mock_target": "boolean?",
                "post_action_observe": "PostActionObserve?",
            },
        }
    if action in {"type", "paste"}:
        return {
            "type": "object",
            "required": ["target", "value|value_from"],
            "fields": {
                "target": "Target",
                "value": "string?",
                "value_from": "input.path?",
                "sensitive": "boolean?",
                "dry_run": "boolean?",
                "allow_mock_target": "boolean?",
                "post_action_observe": "PostActionObserve?",
            },
        }
    if action == "press_key":
        return {
            "type": "object",
            "required": ["keys"],
            "fields": {
                "target": "Target?",
                "keys": "string|string[]",
                "key": "string?",
                "dry_run": "boolean?",
                "allow_mock_target": "boolean?",
                "post_action_observe": "PostActionObserve?",
            },
        }
    if action == "click_text":
        return {
            "type": "object",
            "required": ["text|label|contains_text"],
            "fields": {
                "text": "string?",
                "label": "string?",
                "contains_text": "string?",
                "engine": "auto|screen-ocr|winrt|tesseract|mock?",
                "mock_text": "string?",
                "window": "UIAWindowMatch?",
                "crop": "CropRegion?",
                "dry_run": "boolean?",
                "post_action_observe": "PostActionObserve?",
            },
        }
    if action == "wait_for_text":
        return {
            "type": "object",
            "required": ["text|contains_text"],
            "fields": {
                "text": "string?",
                "contains_text": "string?",
                "timeout_seconds": "number?",
                "poll_seconds": "number?",
                "engine": "auto|screen-ocr|winrt|tesseract|mock?",
                "mock_text": "string?",
                "window": "UIAWindowMatch?",
                "crop": "CropRegion?",
            },
        }
    return {"type": "object", "fields": {}}


def dependency_capabilities() -> tuple[Capability, ...]:
    from .ocr import detect_screen_ocr, detect_tesseract

    tesseract_status = detect_tesseract()
    screen_ocr_status = detect_screen_ocr()
    dependencies = {
        "mss": ("Screen capture dependency.", True, None),
        "PIL": ("Image processing dependency.", True, "pip install Pillow"),
        "pyautogui": ("Mouse and keyboard automation dependency.", True, None),
        "pyperclip": ("Clipboard dependency.", True, None),
        "yaml": ("YAML workflow parser.", True, "pip install PyYAML"),
        "playwright": ("Live browser DOM automation dependency.", False, "pip install -e .[web]"),
        "uiautomation": ("Windows UI Automation dependency.", False, "pip install -e .[desktop]"),
        "pytesseract": ("Optional OCR Python wrapper; also requires the Tesseract binary.", False, "pip install pytesseract"),
        "screen_ocr": ("Optional Windows native OCR with text coordinates.", False, screen_ocr_status["install_hint"]),
        "tesseract": ("Optional Tesseract OCR binary for real OCR.", False, tesseract_status["install_hint"]),
        "torch": ("Optional local VLM runtime dependency.", False, "pip install torch"),
        "transformers": ("Optional local VLM model loading dependency.", False, "pip install transformers"),
    }
    capabilities = []
    for name, (description, required, install_hint) in sorted(dependencies.items()):
        if name == "tesseract":
            available = bool(tesseract_status["available"])
        elif name == "screen_ocr":
            available = bool(screen_ocr_status["available"])
        else:
            available = module_available(name)
        capabilities.append(
            Capability(
                name=name,
                kind="dependency",
                available=available,
                description=description,
                required=required,
                dependency=name,
                install_hint=install_hint,
            )
        )
    return tuple(capabilities)


def module_available(module_name: str | None) -> bool:
    if not module_name:
        return True
    return importlib.util.find_spec(module_name) is not None
