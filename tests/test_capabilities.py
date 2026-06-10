from visual_agent.capabilities import build_atomic_capability_manifest, build_capability_manifest, module_available


def test_module_available_handles_missing_module() -> None:
    assert module_available("definitely_missing_visual_agent_dependency") is False


def test_capability_manifest_lists_core_capabilities() -> None:
    manifest = build_capability_manifest()
    names = {capability.name for capability in manifest.capabilities}

    assert "observe_html" in names
    assert "observe_browser" in names
    assert "observe_ocr" in names
    assert "observe_vision" in names
    assert "observe_fixture" in names
    assert "click" in names
    assert "click_visual" in names
    assert "run-workflow" in names
    assert manifest.available_count > 0


def test_optional_provider_capabilities_have_install_hints() -> None:
    manifest = build_capability_manifest()
    by_name = {capability.name: capability for capability in manifest.capabilities}

    assert by_name["observe_dom"].install_hint
    assert by_name["observe_uia"].install_hint
    assert by_name["pytesseract"].install_hint
    assert by_name["screen_ocr"].install_hint or by_name["screen_ocr"].available
    assert by_name["tesseract"].install_hint or by_name["tesseract"].available
    assert by_name["torch"].required is False
    assert by_name["transformers"].required is False
    assert by_name["observe_dom"].required is False
    assert by_name["observe_uia"].required is False
    assert by_name["screen_ocr"].required is False
    assert by_name["tesseract"].required is False


def test_capabilities_include_planner_visible_atomic_specs() -> None:
    manifest = build_capability_manifest()
    by_name = {capability.name: capability for capability in manifest.capabilities}

    assert by_name["observe_browser"].planner_visible is True
    assert by_name["observe_ocr"].planner_visible is True
    assert by_name["observe_vision"].planner_visible is True
    assert by_name["observe_browser"].input_schema is not None
    assert by_name["click"].dry_run_supported is True
    assert by_name["click_visual"].dry_run_supported is True
    assert by_name["press_key"].dry_run_supported is True
    assert by_name["press_key"].input_schema["required"] == ["keys"]
    assert by_name["press_key"].input_schema["fields"]["target"] == "Target?"
    assert by_name["refresh_browser"].planner_visible is True
    assert by_name["refresh_browser"].input_schema["fields"]["wait_until"] == "domcontentloaded|load|networkidle?"
    assert by_name["click_text"].input_schema["required"] == ["text|label|contains_text"]
    assert by_name["wait_for_text"].input_schema["required"] == ["text|contains_text"]
    assert by_name["request_api"].dry_run_supported is True
    assert by_name["request_api"].input_schema["required"] == ["url"]
    assert by_name["observe_state"].planner_visible is True
    assert by_name["assert_no_error"].kind == "assertion"
    assert by_name["assert_visual_text"].kind == "assertion"
    assert by_name["assert_browser_ready"].kind == "assertion"
    assert by_name["assert_product_contract"].kind == "assertion"
    assert by_name["assert_ai_response_quality"].kind == "assertion"
    assert by_name["save_storage_state"].risk_level == "high"
    assert by_name["assert_response"].kind == "assertion"
    assert by_name["resolve"].kind == "extractor"
    assert by_name["wait_for"].planner_visible is True
    assert by_name["locate_table_cell"].kind == "extractor"
    assert by_name["locate_relative_target"].kind == "extractor"


def test_atomic_capability_manifest_excludes_dependencies_and_hidden_commands() -> None:
    manifest = build_atomic_capability_manifest()
    names = {capability.name for capability in manifest.capabilities}
    kinds = {capability.kind for capability in manifest.capabilities}

    assert "click" in names
    assert "press_key" in names
    assert "refresh_browser" in names
    assert "click_text" in names
    assert "click_visual" in names
    assert "wait_for_text" in names
    assert "request_api" in names
    assert "observe_state" in names
    assert "assert_no_error" in names
    assert "assert_visual_text" in names
    assert "assert_browser_ready" in names
    assert "assert_product_contract" in names
    assert "assert_ai_response_quality" in names
    assert "assert_response" in names
    assert "expect_download" in names
    assert "resolve" in names
    assert "wait_for" in names
    assert "observe_ocr" in names
    assert "observe_vision" in names
    assert "locate_table_cell" in names
    assert "locate_relative_target" in names
    assert "playwright" not in names
    assert "dependency" not in kinds
    assert all(capability.planner_visible for capability in manifest.capabilities)
