from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal


ChangeType = Literal["added", "modified", "deleted"]


@dataclass(frozen=True)
class CodeChange:
    file_path: str
    before: str | None
    after: str
    change_type: ChangeType


@dataclass(frozen=True)
class GenerationContext:
    task_description: str
    code_changes: tuple[CodeChange, ...]
    base_url: str
    project_root: str
    framework_hint: str | None = None


@dataclass(frozen=True)
class FormField:
    name: str
    label: str
    field_type: str = "text"
    required: bool = False
    validation_rules: tuple[str, ...] = ()
    is_sensitive: bool = False


@dataclass(frozen=True)
class SubmitAction:
    text: str
    selector: str | None = None


@dataclass(frozen=True)
class SuccessState:
    kind: str
    value: str
    source: str = "static"


@dataclass(frozen=True)
class ErrorState:
    text: str
    source: str = "static"


@dataclass(frozen=True)
class UISemanticModel:
    entry_url: str
    page_title: str | None
    form_fields: tuple[FormField, ...]
    submit_actions: tuple[SubmitAction, ...]
    success_states: tuple[SuccessState, ...]
    error_states: tuple[ErrorState, ...]
    data_displays: tuple[str, ...]
    framework: str
    confidence: float
    parse_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DataDisplaySummary:
    matched: tuple[str, ...] = ()
    unmatched: tuple[str, ...] = ()


class _FormExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.form_action: str | None = None
        self.fields: list[dict[str, object]] = []
        self.submit_texts: list[str] = []
        self.visible_texts: list[str] = []
        self._inside_label = False
        self._label_for: str | None = None
        self._pending_labels: dict[str, str] = {}
        self._current_button_is_submit = False
        self._button_text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs = {key.lower(): value for key, value in attrs_list}
        if tag == "form" and self.form_action is None:
            self.form_action = attrs.get("action")
        elif tag in {"input", "textarea", "select"}:
            field_type = (attrs.get("type") or ("textarea" if tag == "textarea" else "text")).lower()
            name = attrs.get("name") or attrs.get("id") or attrs.get("aria-label") or ""
            self.fields.append(
                {
                    "name": name,
                    "type": field_type,
                    "id": attrs.get("id"),
                    "required": "required" in attrs,
                    "minlength": attrs.get("minlength"),
                    "maxlength": attrs.get("maxlength"),
                    "min": attrs.get("min"),
                    "max": attrs.get("max"),
                    "pattern": attrs.get("pattern"),
                    "placeholder": attrs.get("placeholder"),
                    "aria_label": attrs.get("aria-label"),
                }
            )
            for key in (attrs.get("id"), name):
                if key and key in self._pending_labels:
                    self.fields[-1]["label"] = self._pending_labels[key]
                    break
        elif tag == "label":
            self._inside_label = True
            self._label_for = attrs.get("for")
        elif tag == "button":
            self._current_button_is_submit = (attrs.get("type") or "submit").lower() == "submit"
            self._button_text_parts = []

    def handle_data(self, data: str) -> None:
        text = _clean_text(data)
        if not text:
            return
        self.visible_texts.append(text)
        if self._inside_label:
            self._attach_label(text)
        if self._current_button_is_submit:
            self._button_text_parts.append(text)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "label":
            self._inside_label = False
            self._label_for = None
        elif tag == "button":
            if self._current_button_is_submit:
                text = _clean_text(" ".join(self._button_text_parts))
                if text:
                    self.submit_texts.append(text)
            self._current_button_is_submit = False
            self._button_text_parts = []

    def _attach_label(self, text: str) -> None:
        if self._label_for:
            for field in reversed(self.fields):
                if field.get("id") == self._label_for or field.get("name") == self._label_for:
                    field["label"] = text
                    return
            self._pending_labels[self._label_for] = text
            return
        if self.fields:
            self.fields[-1]["label"] = text


def ingest_context(ctx: GenerationContext) -> UISemanticModel:
    framework = detect_framework(ctx.code_changes, framework_hint=ctx.framework_hint)
    active = [change for change in ctx.code_changes if change.change_type != "deleted"]
    warnings: list[str] = []
    if not active:
        return UISemanticModel(
            entry_url=ctx.base_url,
            page_title=None,
            form_fields=(),
            submit_actions=(SubmitAction(text="Submit", selector="[type=submit]"),),
            success_states=(),
            error_states=(),
            data_displays=(),
            framework=framework,
            confidence=0.0,
            parse_warnings=("no active code changes supplied",),
        )

    models: list[UISemanticModel] = []
    for change in active:
        suffix = Path(change.file_path).suffix.lower()
        backend_framework = _detect_backend_framework_for_content(change.after)
        if suffix == ".py" and (framework in {"django", "fastapi", "flask"} or backend_framework):
            models.append(extract_backend_semantics(change.after, ctx.base_url, backend_framework or framework))
        elif framework == "html" or suffix in {".html", ".htm"}:
            models.append(extract_html_semantics(change.after, ctx.base_url))
        elif framework == "sveltekit" or suffix == ".svelte":
            models.append(extract_sveltekit_semantics(change.after, ctx.base_url))
        elif framework == "vue" or suffix == ".vue":
            models.append(extract_vue_semantics(change.after, ctx.base_url))
        elif framework == "nextjs":
            models.append(extract_nextjs_semantics(change.after, ctx.base_url))
        elif framework == "remix":
            models.append(extract_remix_semantics(change.after, ctx.base_url))
        elif framework == "react" or suffix in {".jsx", ".tsx", ".js", ".ts"}:
            models.append(extract_react_semantics(change.after, ctx.base_url))
    if not models:
        warnings.append(f"no parser available for framework '{framework}'")
        return UISemanticModel(
            entry_url=ctx.base_url,
            page_title=None,
            form_fields=(),
            submit_actions=(SubmitAction(text="Submit", selector="[type=submit]"),),
            success_states=(),
            error_states=(),
            data_displays=(),
            framework=framework,
            confidence=0.2,
            parse_warnings=tuple(warnings),
        )
    return merge_semantic_models(models, framework=framework, entry_url=ctx.base_url, warnings=tuple(warnings))


def detect_framework(changes: tuple[CodeChange, ...], *, framework_hint: str | None = None) -> str:
    if framework_hint:
        return framework_hint.strip().lower()
    paths = [change.file_path.lower() for change in changes]
    content = "\n".join(change.after for change in changes if change.change_type != "deleted")
    if any(path.endswith(".svelte") or path.endswith(("+page.ts", "+page.js", "+page.server.ts", "+page.server.js")) for path in paths):
        return "sveltekit"
    if "$app/navigation" in content or "@sveltejs/kit" in content:
        return "sveltekit"
    if any(path.endswith(".vue") for path in paths):
        return "vue"
    if any("/app/" in path.replace("\\", "/") or path.endswith(("page.tsx", "page.jsx", "layout.tsx", "layout.jsx")) for path in paths):
        return "nextjs"
    if "next/navigation" in content:
        return "nextjs"
    if "@remix-run/" in content or any(path.endswith((".route.tsx", ".route.jsx")) for path in paths):
        return "remix"
    if any(path.endswith((".jsx", ".tsx")) for path in paths):
        return "react"
    if "from django" in content or "django.urls" in content:
        return "django"
    if "from fastapi" in content or "FastAPI()" in content:
        return "fastapi"
    if "from flask" in content or "Flask(__name__)" in content:
        return "flask"
    if any(path.endswith((".html", ".htm")) for path in paths):
        return "html"
    if any(path.endswith((".js", ".ts")) for path in paths) and re.search(r"<[A-Za-z][^>]*>", content):
        return "react"
    return "unknown"


def _detect_backend_framework_for_content(content: str) -> str | None:
    if "from django" in content or "django.urls" in content or "django.shortcuts" in content:
        return "django"
    if "from fastapi" in content or "FastAPI()" in content:
        return "fastapi"
    if "from flask" in content or "Flask(__name__)" in content:
        return "flask"
    return None


def extract_html_semantics(content: str, base_url: str) -> UISemanticModel:
    extractor = _FormExtractor()
    extractor.feed(content)
    fields = tuple(_field_from_raw(field) for field in extractor.fields if _field_from_raw(field) is not None)
    success_states = list(_extract_success_texts(content))
    if extractor.form_action and extractor.form_action not in {"#", ""}:
        success_states.append(SuccessState(kind="url_redirect", value=extractor.form_action, source="html:form@action"))
    error_states = tuple(ErrorState(text=text, source="html:text") for text in _extract_error_texts(content))
    submit_actions = tuple(SubmitAction(text=text) for text in extractor.submit_texts) or (
        SubmitAction(text="Submit", selector="[type=submit]"),
    )
    warnings = _form_diagnostic_warnings(
        framework="html",
        fields=fields,
        has_explicit_submit=bool(extractor.submit_texts),
        success_states=tuple(success_states),
        data_displays=(),
    )
    confidence = 0.8 if fields else 0.35
    if success_states:
        confidence = min(0.95, confidence + 0.1)
    return UISemanticModel(
        entry_url=base_url,
        page_title=_extract_title(content),
        form_fields=fields,
        submit_actions=submit_actions,
        success_states=tuple(success_states),
        error_states=error_states,
        data_displays=(),
        framework="html",
        confidence=confidence,
        parse_warnings=warnings,
    )


def extract_react_semantics(content: str, base_url: str) -> UISemanticModel:
    fields = tuple(_extract_jsx_inputs(content))
    explicit_submit_actions = tuple(_extract_jsx_submit_buttons(content))
    submit_actions = explicit_submit_actions or (SubmitAction(text="Submit", selector="[type=submit]"),)
    success_states = tuple([*_extract_react_redirects(content), *_extract_success_texts(content)])
    error_states = tuple(ErrorState(text=text, source="react:text") for text in _extract_error_texts(content))
    data_displays = tuple(_extract_react_template_vars(content))
    warnings = _form_diagnostic_warnings(
        framework="react",
        fields=fields,
        has_explicit_submit=bool(explicit_submit_actions),
        success_states=success_states,
        data_displays=data_displays,
        source_has_form=_looks_like_react_form(content),
    )
    confidence = 0.72 if fields else 0.4
    if success_states:
        confidence = min(0.92, confidence + 0.1)
    return UISemanticModel(
        entry_url=base_url,
        page_title=_extract_react_title(content),
        form_fields=fields,
        submit_actions=submit_actions,
        success_states=success_states,
        error_states=error_states,
        data_displays=data_displays,
        framework="react",
        confidence=confidence,
        parse_warnings=warnings,
    )


def extract_nextjs_semantics(content: str, base_url: str) -> UISemanticModel:
    react_model = extract_react_semantics(content, base_url)
    success_states = _dedupe_states((*react_model.success_states, *_extract_nextjs_redirects(content)))
    confidence = max(react_model.confidence, 0.76 if react_model.form_fields or success_states else 0.42)
    if success_states:
        confidence = min(0.94, confidence + 0.05)
    warnings = _form_diagnostic_warnings(
        framework="react",
        fields=react_model.form_fields,
        has_explicit_submit=not any(action.selector == "[type=submit]" for action in react_model.submit_actions),
        success_states=success_states,
        data_displays=react_model.data_displays,
        source_has_form=_looks_like_react_form(content),
    )
    return UISemanticModel(
        entry_url=base_url,
        page_title=react_model.page_title,
        form_fields=react_model.form_fields,
        submit_actions=react_model.submit_actions,
        success_states=success_states,
        error_states=react_model.error_states,
        data_displays=react_model.data_displays,
        framework="nextjs",
        confidence=confidence,
        parse_warnings=warnings,
    )


def extract_remix_semantics(content: str, base_url: str) -> UISemanticModel:
    react_model = extract_react_semantics(content, base_url)
    success_states = _dedupe_states((*react_model.success_states, *_extract_remix_redirects(content)))
    confidence = max(react_model.confidence, 0.74 if react_model.form_fields or success_states else 0.42)
    if success_states:
        confidence = min(0.93, confidence + 0.05)
    warnings = _form_diagnostic_warnings(
        framework="react",
        fields=react_model.form_fields,
        has_explicit_submit=not any(action.selector == "[type=submit]" for action in react_model.submit_actions),
        success_states=success_states,
        data_displays=react_model.data_displays,
        source_has_form=_looks_like_react_form(content),
    )
    return UISemanticModel(
        entry_url=base_url,
        page_title=react_model.page_title,
        form_fields=react_model.form_fields,
        submit_actions=react_model.submit_actions,
        success_states=success_states,
        error_states=react_model.error_states,
        data_displays=react_model.data_displays,
        framework="remix",
        confidence=confidence,
        parse_warnings=warnings,
    )


def extract_sveltekit_semantics(content: str, base_url: str) -> UISemanticModel:
    template = _strip_svelte_script(content)
    html_model = extract_html_semantics(template, base_url)
    success_states = _dedupe_states((*html_model.success_states, *_extract_sveltekit_redirects(content), *_extract_success_texts(content)))
    error_states = _dedupe_errors((*html_model.error_states, *(ErrorState(text=text, source="sveltekit:text") for text in _extract_error_texts(content))))
    data_displays = tuple(_extract_svelte_template_vars(template))
    confidence = 0.72 if html_model.form_fields else 0.42
    if success_states:
        confidence = min(0.92, confidence + 0.1)
    warnings = _form_diagnostic_warnings(
        framework="vue",
        fields=html_model.form_fields,
        has_explicit_submit=not any(action.selector == "[type=submit]" for action in html_model.submit_actions),
        success_states=success_states,
        data_displays=data_displays,
        source_has_form="<form" in template.lower() or "<input" in template.lower(),
    )
    return UISemanticModel(
        entry_url=base_url,
        page_title=html_model.page_title,
        form_fields=html_model.form_fields,
        submit_actions=html_model.submit_actions,
        success_states=success_states,
        error_states=error_states,
        data_displays=data_displays,
        framework="sveltekit",
        confidence=confidence,
        parse_warnings=warnings,
    )


def extract_vue_semantics(content: str, base_url: str) -> UISemanticModel:
    template = _extract_vue_template(content) or content
    html_model = extract_html_semantics(template, base_url)
    success_states = tuple([*html_model.success_states, *_extract_vue_redirects(content), *_extract_success_texts(content)])
    error_states = tuple([*html_model.error_states, *(ErrorState(text=text, source="vue:text") for text in _extract_error_texts(content))])
    data_displays = tuple(_extract_vue_template_vars(template))
    warnings = _form_diagnostic_warnings(
        framework="vue",
        fields=html_model.form_fields,
        has_explicit_submit=not any(action.selector == "[type=submit]" for action in html_model.submit_actions),
        success_states=_dedupe_states(success_states),
        data_displays=data_displays,
        source_has_form="<form" in template.lower() or "<input" in template.lower(),
    )
    confidence = 0.72 if html_model.form_fields else 0.42
    if success_states:
        confidence = min(0.92, confidence + 0.1)
    return UISemanticModel(
        entry_url=base_url,
        page_title=html_model.page_title or _extract_react_title(template),
        form_fields=html_model.form_fields,
        submit_actions=html_model.submit_actions,
        success_states=_dedupe_states(success_states),
        error_states=_dedupe_errors(error_states),
        data_displays=data_displays,
        framework="vue",
        confidence=confidence,
        parse_warnings=warnings,
    )


def extract_backend_semantics(content: str, base_url: str, framework: str) -> UISemanticModel:
    success_states = tuple([*_extract_backend_redirects(content, framework), *_extract_backend_success_texts(content)])
    error_states = tuple(ErrorState(text=text, source=f"{framework}:message") for text in _extract_error_texts(content))
    confidence = 0.68 if success_states else 0.35
    return UISemanticModel(
        entry_url=_extract_backend_route(content, base_url, framework),
        page_title=None,
        form_fields=(),
        submit_actions=(),
        success_states=success_states,
        error_states=error_states,
        data_displays=(),
        framework=framework,
        confidence=confidence,
        parse_warnings=(),
    )


def merge_semantic_models(
    models: list[UISemanticModel],
    *,
    framework: str,
    entry_url: str,
    warnings: tuple[str, ...] = (),
) -> UISemanticModel:
    fields = _dedupe_by_name(field for model in models for field in model.form_fields)
    submit_actions = _dedupe_submit(action for model in models for action in model.submit_actions)
    success_states = _dedupe_states(state for model in models for state in model.success_states)
    error_states = _dedupe_errors(state for model in models for state in model.error_states)
    data_displays = tuple(dict.fromkeys(value for model in models for value in model.data_displays))
    title = next((model.page_title for model in models if model.page_title), None)
    confidence = max(model.confidence for model in models)
    merged_warnings = tuple(
        dict.fromkeys(
            (
                *warnings,
                *(warning for model in models for warning in model.parse_warnings),
                *_unmatched_data_display_warnings(fields, data_displays),
            )
        )
    )
    return UISemanticModel(
        entry_url=entry_url,
        page_title=title,
        form_fields=fields,
        submit_actions=submit_actions or (SubmitAction(text="Submit", selector="[type=submit]"),),
        success_states=success_states,
        error_states=error_states,
        data_displays=data_displays,
        framework=framework,
        confidence=confidence,
        parse_warnings=merged_warnings,
    )


def _field_from_raw(raw: dict[str, object]) -> FormField | None:
    name = str(raw.get("name") or "").strip()
    field_type = str(raw.get("type") or "text").lower()
    if field_type in {"hidden", "submit", "button", "reset"} or not name:
        return None
    label = str(raw.get("label") or raw.get("placeholder") or raw.get("aria_label") or name)
    lower_name = name.lower()
    is_sensitive = field_type == "password" or any(keyword in lower_name for keyword in ("password", "passwd", "secret", "token", "key"))
    return FormField(
        name=name,
        label=label,
        field_type=field_type,
        required=bool(raw.get("required")),
        validation_rules=_validation_rules_from_raw(raw, field_type=field_type),
        is_sensitive=is_sensitive,
    )


def _extract_jsx_inputs(content: str) -> list[FormField]:
    results: list[FormField] = []
    formik_bound_fields = _jsx_formik_bound_fields(content)
    input_tags = (
        "input",
        "textarea",
        "select",
        "Input",
        "TextInput",
        "TextField",
        "Field",
        "FormField",
        "Select",
        "Textarea",
        "DatePicker",
        "RangePicker",
        "InputNumber",
        "Switch",
        "Checkbox",
        "Radio.Group",
        "Slider",
        "Autocomplete",
        "Upload",
    )
    tag_pattern = "|".join(re.escape(tag) for tag in input_tags)
    for match in re.finditer(rf"<((?:[A-Za-z.]*\.)?(?:{tag_pattern}))\b([^>]*?)(?:/>|>)", content, re.DOTALL):
        raw_tag = match.group(1)
        tag = "Radio.Group" if raw_tag.endswith("Radio.Group") else raw_tag.split(".")[-1]
        attrs = match.group(2) or ""
        name = (
            _attr(attrs, "name")
            or _attr(attrs, "id")
            or _attr(attrs, "aria-label")
            or _jsx_register_name(attrs)
            or _jsx_formik_field_name(attrs, formik_bound_fields)
            or _jsx_bound_identifier(attrs, "value")
            or _jsx_bound_identifier(attrs, "checked")
            or ""
        )
        field_type = _jsx_field_type(tag, attrs)
        if not name or field_type in {"hidden", "submit", "button", "reset"}:
            continue
        label = _attr(attrs, "label") or _attr(attrs, "placeholder") or _attr(attrs, "aria-label") or name
        lower_name = name.lower()
        required = bool(re.search(r"\brequired(?:\s*=\s*{?true}?)?\b", attrs)) or _jsx_register_bool_option(attrs, "required")
        results.append(
            FormField(
                name=name,
                label=label,
                field_type=field_type,
                required=required,
                validation_rules=_validation_rules_from_attrs(attrs, field_type=field_type, required=required),
                is_sensitive=field_type == "password"
                or any(keyword in lower_name for keyword in ("password", "passwd", "secret", "token", "key")),
            )
        )
    results.extend(_extract_jsx_controller_fields(content))
    return results


def _jsx_formik_bound_fields(content: str) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for match in re.finditer(
        r"(?:const|let)\s+\[\s*([A-Za-z_][A-Za-z0-9_]*)[^\]]*]\s*=\s*useField\(\s*[\"']([A-Za-z_][A-Za-z0-9_.-]*)[\"']",
        content,
    ):
        bindings[match.group(1)] = match.group(2).split(".")[-1]
    for match in re.finditer(
        r"(?:const|let)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*useField\(\s*[\"']([A-Za-z_][A-Za-z0-9_.-]*)[\"']",
        content,
    ):
        bindings[match.group(1)] = match.group(2).split(".")[-1]
    return bindings


def _jsx_formik_field_name(attrs: str, bindings: dict[str, str]) -> str | None:
    direct = re.search(r"\bgetFieldProps\(\s*[\"']([A-Za-z_][A-Za-z0-9_.-]*)[\"']", attrs)
    if direct:
        return direct.group(1).split(".")[-1]
    for match in re.finditer(r"{\s*\.\.\.\s*([A-Za-z_][A-Za-z0-9_]*)\s*}", attrs):
        field_name = bindings.get(match.group(1))
        if field_name:
            return field_name
    return None


def _extract_jsx_controller_fields(content: str) -> list[FormField]:
    results: list[FormField] = []
    for match in re.finditer(r"<(?:[A-Za-z.]*\.)?Controller\b(.*?)(?:/\s*>|>\s*</(?:[A-Za-z.]*\.)?Controller>)", content, re.DOTALL):
        attrs = match.group(1) or ""
        name = _attr(attrs, "name") or ""
        if not name:
            continue
        field_type = _jsx_controller_field_type(attrs)
        label = _attr(attrs, "label") or name
        lower_name = name.lower()
        required = _jsx_register_bool_option(attrs, "required")
        results.append(
            FormField(
                name=name,
                label=label,
                field_type=field_type,
                required=required,
                validation_rules=_validation_rules_from_attrs(attrs, field_type=field_type, required=required),
                is_sensitive=field_type == "password"
                or any(keyword in lower_name for keyword in ("password", "passwd", "secret", "token", "key")),
            )
        )
    return results


def _jsx_field_type(tag: str, attrs: str) -> str:
    explicit = (_attr(attrs, "type") or "").lower()
    if explicit:
        return explicit
    component_type = {
        "textarea": "textarea",
        "Textarea": "textarea",
        "select": "select",
        "Controller": _jsx_controller_field_type(attrs),
        "Select": "select",
        "DatePicker": "date",
        "RangePicker": "date_range",
        "InputNumber": "number",
        "Switch": "boolean",
        "Checkbox": "boolean",
        "Radio.Group": "radio",
        "Slider": "number",
        "Autocomplete": "select",
        "Upload": "file",
    }.get(tag)
    return component_type or "text"


def _jsx_bound_identifier(attrs: str, name: str) -> str | None:
    match = re.search(rf"\b{re.escape(name)}\s*=\s*{{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*}}", attrs)
    if not match:
        return None
    return match.group(1).split(".")[-1]


def _jsx_register_name(attrs: str) -> str | None:
    match = re.search(r"\bregister\(\s*[\"']([A-Za-z_][A-Za-z0-9_.-]*)[\"']", attrs)
    if not match:
        return None
    return match.group(1).split(".")[-1]


def _jsx_register_bool_option(attrs: str, name: str) -> bool:
    pattern = rf"\b{re.escape(name)}\s*:\s*(?:true|[\"'][^\"']+[\"'])"
    return bool(re.search(pattern, attrs, re.IGNORECASE))


def _jsx_register_option(attrs: str, name: str) -> str | None:
    match = re.search(rf"\b{re.escape(name)}\s*:\s*(?:[\"']([^\"']+)[\"']|(\d+(?:\.\d+)?))", attrs)
    if not match:
        return None
    return next((group for group in match.groups() if group is not None), None)


def _jsx_controller_field_type(attrs: str) -> str:
    if re.search(r"<(?:[A-Za-z.]*\.)?Select\b|<(?:[A-Za-z.]*\.)?Autocomplete\b", attrs):
        return "select"
    if re.search(r"<(?:[A-Za-z.]*\.)?DatePicker\b", attrs):
        return "date"
    if re.search(r"<(?:[A-Za-z.]*\.)?(?:InputNumber|Slider)\b", attrs):
        return "number"
    if re.search(r"<(?:[A-Za-z.]*\.)?(?:Switch|Checkbox)\b", attrs):
        return "boolean"
    return "text"


def _extract_jsx_submit_buttons(content: str) -> list[SubmitAction]:
    results: list[SubmitAction] = []
    for match in re.finditer(r"<button\b([^>]*)>(.*?)</button>|<Button\b([^>]*)>(.*?)</Button>", content, re.DOTALL | re.IGNORECASE):
        attrs = match.group(1) or match.group(3) or ""
        button_type = (_attr(attrs, "type") or "submit").lower()
        text = _clean_text(re.sub(r"<[^>]+>", " ", match.group(2) or match.group(4) or ""))
        if button_type != "submit" and not _button_text_implies_action(text):
            continue
        if text:
            results.append(SubmitAction(text=text))
    results.extend(_extract_jsx_modal_confirm_actions(content))
    return results


def _extract_jsx_modal_confirm_actions(content: str) -> list[SubmitAction]:
    results: list[SubmitAction] = []
    for match in re.finditer(r"<(?:[A-Za-z.]*\.)?Modal\b([^>]*?)(?:/>|>)", content, re.DOTALL):
        attrs = match.group(1) or ""
        text = _attr(attrs, "okText") or _attr(attrs, "confirmText") or _attr(attrs, "title") or ""
        if text and _button_text_implies_action(text):
            results.append(SubmitAction(text=text))
    return results


def _extract_react_redirects(content: str) -> list[SuccessState]:
    patterns = (
        r"\bnavigate\(\s*[\"']([^\"']+)[\"']",
        r"\brouter\.push\(\s*[\"']([^\"']+)[\"']",
        r"\bwindow\.location(?:\.href)?\s*=\s*[\"']([^\"']+)[\"']",
    )
    states: list[SuccessState] = []
    for pattern in patterns:
        for match in re.finditer(pattern, content):
            states.append(SuccessState(kind="url_redirect", value=match.group(1), source="react:redirect"))
    return states


def _extract_nextjs_redirects(content: str) -> list[SuccessState]:
    patterns = (
        r"\bredirect\(\s*[\"']([^\"']+)[\"']",
        r"\bpermanentRedirect\(\s*[\"']([^\"']+)[\"']",
        r"\brouter\.replace\(\s*[\"']([^\"']+)[\"']",
    )
    states: list[SuccessState] = []
    for pattern in patterns:
        for match in re.finditer(pattern, content):
            states.append(SuccessState(kind="url_redirect", value=match.group(1), source="nextjs:redirect"))
    return _dedupe_states(states)


def _extract_remix_redirects(content: str) -> list[SuccessState]:
    states: list[SuccessState] = []
    for match in re.finditer(r"\bredirect\(\s*[\"']([^\"']+)[\"']", content):
        states.append(SuccessState(kind="url_redirect", value=match.group(1), source="remix:redirect"))
    return _dedupe_states(states)


def _extract_sveltekit_redirects(content: str) -> list[SuccessState]:
    patterns = (
        r"\bgoto\(\s*[\"']([^\"']+)[\"']",
        r"\bredirect\(\s*\d{3}\s*,\s*[\"']([^\"']+)[\"']",
        r"\bredirect\(\s*[\"']([^\"']+)[\"']",
    )
    states: list[SuccessState] = []
    for pattern in patterns:
        for match in re.finditer(pattern, content):
            states.append(SuccessState(kind="url_redirect", value=match.group(1), source="sveltekit:redirect"))
    return _dedupe_states(states)


def _extract_vue_template(content: str) -> str | None:
    match = re.search(r"<template[^>]*>(.*?)</template>", content, re.DOTALL | re.IGNORECASE)
    return match.group(1) if match else None


def _extract_vue_redirects(content: str) -> list[SuccessState]:
    patterns = (
        r"\brouter\.push\(\s*[\"']([^\"']+)[\"']",
        r"\bthis\.\$router\.push\(\s*[\"']([^\"']+)[\"']",
        r"\bthis\.\$router\.replace\(\s*[\"']([^\"']+)[\"']",
        r"\bwindow\.location(?:\.href)?\s*=\s*[\"']([^\"']+)[\"']",
    )
    states: list[SuccessState] = []
    for pattern in patterns:
        for match in re.finditer(pattern, content):
            states.append(SuccessState(kind="url_redirect", value=match.group(1), source="vue:redirect"))
    return _dedupe_states(states)


def _extract_backend_route(content: str, base_url: str, framework: str) -> str:
    if framework == "flask":
        match = re.search(r"@[\w.]+\.route\(\s*[\"']([^\"']+)[\"']", content)
        if match:
            return match.group(1)
    if framework == "fastapi":
        match = re.search(r"@[\w.]+\.(?:get|post|put|patch|delete)\(\s*[\"']([^\"']+)[\"']", content)
        if match:
            return match.group(1)
    if framework == "django":
        match = re.search(r"path\(\s*[\"']([^\"']+)[\"']", content)
        if match:
            route = match.group(1)
            return "/" + route.lstrip("/")
    return base_url


def _extract_backend_redirects(content: str, framework: str) -> list[SuccessState]:
    patterns = {
        "django": (
            r"\bredirect\(\s*[\"']([^\"']+)[\"']",
            r"\bHttpResponseRedirect\(\s*[\"']([^\"']+)[\"']",
            r"\breverse\(\s*[\"']([^\"']+)[\"']",
        ),
        "fastapi": (
            r"\bRedirectResponse\(\s*(?:url\s*=\s*)?[\"']([^\"']+)[\"']",
            r"\bresponse\.headers\[[\"']Location[\"']\]\s*=\s*[\"']([^\"']+)[\"']",
        ),
        "flask": (
            r"\bredirect\(\s*[\"']([^\"']+)[\"']",
            r"\burl_for\(\s*[\"']([^\"']+)[\"']",
        ),
    }
    states: list[SuccessState] = []
    for pattern in patterns.get(framework, ()):
        for match in re.finditer(pattern, content):
            states.append(SuccessState(kind="url_redirect", value=match.group(1), source=f"{framework}:redirect"))
    return _dedupe_states(states)


def _extract_backend_success_texts(content: str) -> list[SuccessState]:
    states = list(_extract_success_texts(content))
    for match in re.finditer(r"messages\.(?:success|info)\([^,]+,\s*[\"']([^\"']+)[\"']", content):
        text = _clean_text(match.group(1))
        if text:
            states.append(SuccessState(kind="text", value=text, source="backend:messages"))
    for match in re.finditer(r"[\"'](?:message|status|detail)[\"']\s*:\s*[\"']([^\"']+)[\"']", content):
        text = _clean_text(match.group(1))
        lower = text.lower()
        if any(keyword in lower for keyword in _SUCCESS_KEYWORDS):
            states.append(SuccessState(kind="text", value=text, source="backend:json"))
    return _dedupe_states(states)


def _extract_success_texts(content: str) -> list[SuccessState]:
    results: list[SuccessState] = []
    for text in _quoted_or_tagged_texts(content):
        lower = text.lower()
        if any(keyword in lower for keyword in _SUCCESS_KEYWORDS):
            results.append(SuccessState(kind="text", value=text, source="static:text"))
    return _dedupe_states(results)


def _extract_error_texts(content: str) -> list[str]:
    results: list[str] = []
    for text in _quoted_or_tagged_texts(content):
        if _is_error_text_candidate(text):
            results.append(text)
    return list(dict.fromkeys(results))


def _is_error_text_candidate(text: str) -> bool:
    lower = text.lower()
    if any(keyword in lower for keyword in _SUCCESS_KEYWORDS):
        return False
    return any(keyword in lower for keyword in ("error", "failed", "invalid", "required", "失败", "错误", "无效", "必填"))


_SUCCESS_KEYWORDS = (
    "success",
    "saved",
    "created",
    "updated",
    "deleted",
    "removed",
    "archived",
    "welcome",
    "dashboard",
    "成功",
    "完成",
    "已保存",
    "欢迎",
)


def _button_text_implies_action(text: str) -> bool:
    lower = text.lower()
    return any(keyword in lower for keyword in ("delete", "remove", "archive", "confirm", "save", "create", "update"))


def _quoted_or_tagged_texts(content: str) -> list[str]:
    texts: list[str] = []
    for match in re.finditer(r">([^<>{]{3,120})<", content):
        text = _clean_text(match.group(1))
        if text:
            texts.append(text)
    for match in re.finditer(r"['\"]([^'\"]{3,120})['\"]", content):
        text = _clean_text(match.group(1))
        if text and not re.match(r"^[A-Za-z0-9_./:#?=&-]+$", text):
            texts.append(text)
    return texts


def _extract_react_template_vars(content: str) -> list[str]:
    results: list[str] = []
    for match in re.finditer(r"{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*}", content):
        line_prefix = content[content.rfind("\n", 0, match.start()) + 1 : match.start()]
        attr_match = re.search(r"([A-Za-z_:][A-Za-z0-9_:-]*)\s*=\s*$", line_prefix)
        if re.search(r"\bimport\s*$", line_prefix):
            continue
        if attr_match and _jsx_attr_binding_is_not_display(attr_match.group(1)):
            continue
        if match.group(1) in {"register", "control", "getFieldProps", "setFieldValue", "useField"}:
            continue
        if match.group(1) in {"field", "fields", "meta", "helpers"} and ("{..." in line_prefix or "render=" in line_prefix):
            continue
        results.append(match.group(1))
    return list(dict.fromkeys(results))


def _jsx_attr_binding_is_not_display(attr_name: str) -> bool:
    lower = attr_name.lower()
    return lower.startswith("on") or lower in {
        "action",
        "checked",
        "defaultchecked",
        "disabled",
        "loading",
        "open",
        "options",
        "datasource",
        "data",
        "items",
        "columns",
        "control",
        "render",
        "rules",
        "visible",
        "required",
        "selected",
        "readonly",
    }


def _extract_vue_template_vars(content: str) -> list[str]:
    return list(dict.fromkeys(match.group(1) for match in re.finditer(r"{{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*}}", content)))


def _extract_svelte_template_vars(content: str) -> list[str]:
    results: list[str] = []
    for match in re.finditer(r"{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*}", content):
        line_prefix = content[content.rfind("\n", 0, match.start()) + 1 : match.start()]
        attr_match = re.search(r"([A-Za-z_:][A-Za-z0-9_:-]*)\s*=\s*$", line_prefix)
        if attr_match and attr_match.group(1) in {"action", "on:submit", "on:click", "on:change", "on:input"}:
            continue
        results.append(match.group(1))
    return list(dict.fromkeys(results))


def _strip_svelte_script(content: str) -> str:
    return re.sub(r"<script[^>]*>.*?</script>", "", content, flags=re.DOTALL | re.IGNORECASE)


def _form_diagnostic_warnings(
    *,
    framework: str,
    fields: tuple[FormField, ...],
    has_explicit_submit: bool,
    success_states: tuple[SuccessState, ...],
    data_displays: tuple[str, ...],
    source_has_form: bool = False,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if framework in {"react", "vue"} and source_has_form and not fields:
        warnings.append("no form fields extracted")
    if fields and not has_explicit_submit:
        warnings.append("form fields found but no submit action")
    if (fields or has_explicit_submit or data_displays) and not success_states:
        warnings.append("submit action found but no success state")
    return tuple(dict.fromkeys(warnings))


def _unmatched_data_display_warnings(fields: tuple[FormField, ...], data_displays: tuple[str, ...]) -> tuple[str, ...]:
    unmatched = unmatched_data_displays(fields, data_displays)
    if not unmatched:
        return ()
    return ("unmatched data displays: " + ", ".join(unmatched[:5]),)


def summarize_data_displays(model: UISemanticModel) -> DataDisplaySummary:
    return DataDisplaySummary(
        matched=matched_data_displays(model.form_fields, model.data_displays),
        unmatched=unmatched_data_displays(model.form_fields, model.data_displays),
    )


def matched_data_displays(fields: tuple[FormField, ...], data_displays: tuple[str, ...]) -> tuple[str, ...]:
    if not data_displays:
        return ()
    non_sensitive_names = {field.name.lower() for field in fields if not field.is_sensitive}
    matched = [display for display in data_displays if _data_display_matches_field(display, non_sensitive_names)]
    return tuple(dict.fromkeys(matched))


def unmatched_data_displays(fields: tuple[FormField, ...], data_displays: tuple[str, ...]) -> tuple[str, ...]:
    if not data_displays:
        return ()
    field_names = {field.name.lower() for field in fields}
    unmatched = [display for display in data_displays if not _data_display_matches_field(display, field_names)]
    return tuple(dict.fromkeys(unmatched))


def data_display_matches_field_name(display: str, field_name: str) -> bool:
    return _data_display_matches_field(display, {field_name.lower()})


def _data_display_matches_field(display: str, field_names: set[str]) -> bool:
    lower = display.lower()
    return lower in field_names or any(lower.endswith(f".{name}") for name in field_names)


def _looks_like_react_form(content: str) -> bool:
    return bool(re.search(r"<(?:form|Form|input|[A-Za-z.]*Input)\b", content))


def _extract_title(html: str) -> str | None:
    match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    return _clean_text(match.group(1)) if match else None


def _extract_react_title(content: str) -> str | None:
    match = re.search(r"<h1[^>]*>(.*?)</h1>", content, re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    return _clean_text(re.sub(r"<[^>]+>", " ", match.group(1)))


def _attr(attrs: str, name: str) -> str | None:
    match = re.search(rf"\b{re.escape(name)}\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|{{`([^`]*)`}}|{{['\"]([^'\"]*)['\"]}})", attrs)
    if not match:
        return None
    return next((group for group in match.groups() if group is not None), None)


def _validation_rules_from_raw(raw: dict[str, object], *, field_type: str) -> tuple[str, ...]:
    rules: list[str] = []
    if bool(raw.get("required")):
        rules.append("required")
    if field_type == "email":
        rules.append("email_format")
    for attr_name, rule_name in (
        ("minlength", "min_length"),
        ("maxlength", "max_length"),
        ("min", "min"),
        ("max", "max"),
        ("pattern", "pattern"),
    ):
        value = raw.get(attr_name)
        if value is not None and str(value) != "":
            rules.append(f"{rule_name}:{value}")
    return tuple(dict.fromkeys(rules))


def _validation_rules_from_attrs(attrs: str, *, field_type: str, required: bool) -> tuple[str, ...]:
    rules: list[str] = []
    if required:
        rules.append("required")
    if field_type == "email":
        rules.append("email_format")
    for attr_name, rule_name in (
        ("minLength", "min_length"),
        ("minlength", "min_length"),
        ("maxLength", "max_length"),
        ("maxlength", "max_length"),
        ("min", "min"),
        ("max", "max"),
        ("pattern", "pattern"),
    ):
        value = _attr(attrs, attr_name)
        if not value and attr_name in {"minLength", "minlength"}:
            value = _jsx_register_option(attrs, "minLength") or _jsx_register_option(attrs, "minlength")
        if not value and attr_name in {"maxLength", "maxlength"}:
            value = _jsx_register_option(attrs, "maxLength") or _jsx_register_option(attrs, "maxlength")
        if not value and attr_name in {"min", "max", "pattern"}:
            value = _jsx_register_option(attrs, attr_name)
        if value:
            rules.append(f"{rule_name}:{value}")
    return tuple(dict.fromkeys(rules))


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _dedupe_by_name(fields: object) -> tuple[FormField, ...]:
    result: dict[str, FormField] = {}
    for field in fields:
        if isinstance(field, FormField) and field.name not in result:
            result[field.name] = field
    return tuple(result.values())


def _dedupe_submit(actions: object) -> tuple[SubmitAction, ...]:
    result: dict[tuple[str, str | None], SubmitAction] = {}
    for action in actions:
        if isinstance(action, SubmitAction):
            result.setdefault((action.text, action.selector), action)
    return tuple(result.values())


def _dedupe_states(states: object) -> tuple[SuccessState, ...]:
    result: dict[tuple[str, str], SuccessState] = {}
    for state in states:
        if isinstance(state, SuccessState):
            result.setdefault((state.kind, state.value), state)
    return tuple(result.values())


def _dedupe_errors(states: object) -> tuple[ErrorState, ...]:
    result: dict[str, ErrorState] = {}
    for state in states:
        if isinstance(state, ErrorState):
            result.setdefault(state.text, state)
    return tuple(result.values())
