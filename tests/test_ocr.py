from PIL import Image

from visual_agent.models import ProviderKind
from visual_agent.ocr import detect_tesseract, observe_ocr


def test_detect_tesseract_returns_diagnostic_shape() -> None:
    status = detect_tesseract()

    assert status["engine"] == "tesseract"
    assert isinstance(status["available"], bool)
    assert isinstance(status["module_available"], bool)
    assert "install_hint" in status


def test_ocr_mock_keeps_engine_status_available(tmp_path) -> None:
    observation = observe_ocr({"mock_text": "登录成功"}, tmp_path, synthetic_on_capture_fail=True)

    assert observation.provider == ProviderKind.OCR
    assert observation.metadata["engine"] == "mock"
    assert observation.metadata["engine_available"] is True
    assert observation.metadata["engine_status"]["available"] is True


def test_ocr_unavailable_tesseract_returns_clear_diagnostic(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "sample.png"
    Image.new("RGB", (100, 40), "white").save(image_path)
    monkeypatch.setattr(
        "visual_agent.ocr.detect_tesseract",
        lambda: {
            "engine": "tesseract",
            "available": False,
            "module_available": True,
            "binary_path": None,
            "version": None,
            "error": "TesseractNotFoundError: missing binary",
            "install_hint": "install tesseract",
        },
    )

    observation = observe_ocr({"path": str(image_path), "engine": "tesseract"}, tmp_path)

    assert observation.elements == ()
    assert observation.metadata["engine"] == "tesseract"
    assert observation.metadata["engine_available"] is False
    assert "missing binary" in observation.metadata["engine_status"]["error"]


def test_ocr_tesseract_runtime_error_is_captured(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "sample.png"
    Image.new("RGB", (100, 40), "white").save(image_path)
    monkeypatch.setattr(
        "visual_agent.ocr.detect_tesseract",
        lambda: {
            "engine": "tesseract",
            "available": True,
            "module_available": True,
            "binary_path": "tesseract",
            "version": "test",
            "error": None,
            "install_hint": None,
        },
    )

    def fail_tesseract(*args, **kwargs):
        raise RuntimeError("ocr failed")

    monkeypatch.setattr("visual_agent.ocr.tesseract_elements", fail_tesseract)

    observation = observe_ocr({"path": str(image_path), "engine": "tesseract"}, tmp_path)

    assert observation.elements == ()
    assert observation.metadata["engine_available"] is False
    assert "ocr failed" in observation.metadata["engine_status"]["error"]
