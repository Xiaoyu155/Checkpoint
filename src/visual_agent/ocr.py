from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from typing import Any

from PIL import Image

from .capture import ScreenCapture
from .models import Bounds, Observation, ProviderKind


def observe_ocr(params: dict[str, Any], run_dir: Path, *, synthetic_on_capture_fail: bool = False) -> Observation:
    image, path = load_or_capture_image(params, run_dir, synthetic_on_capture_fail=synthetic_on_capture_fail)
    engine = str(params.get("engine") or "auto").lower()
    min_confidence = float(params.get("min_confidence", 0.5))

    if "mock_text" in params:
        elements = mock_ocr_elements(str(params["mock_text"]), params, image)
        engine_used = "mock"
        engine_available = True
        engine_status = {
            "engine": "mock",
            "available": True,
            "module_available": True,
            "binary_path": None,
            "version": None,
            "error": None,
            "install_hint": None,
        }
    elif engine in {"auto", "tesseract"}:
        engine_used = "tesseract"
        engine_status = detect_tesseract()
        engine_available = bool(engine_status["available"])
        if engine_available:
            try:
                elements = tesseract_elements(image, min_confidence=min_confidence)
            except Exception as exc:
                elements = ()
                engine_available = False
                engine_status = {
                    **engine_status,
                    "available": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "install_hint": TESSERACT_INSTALL_HINT,
                }
        else:
            elements = ()
    else:
        elements = ()
        engine_used = engine
        engine_available = False
        engine_status = {
            "engine": engine,
            "available": False,
            "module_available": module_available("pytesseract"),
            "binary_path": shutil.which("tesseract"),
            "version": None,
            "error": f"Unsupported or unavailable OCR engine: {engine}",
            "install_hint": TESSERACT_INSTALL_HINT,
        }

    return Observation(
        provider=ProviderKind.OCR,
        source=str(path),
        screenshot_path=path,
        width=image.width,
        height=image.height,
        elements=elements,
        metadata={
            "provider": "ocr",
            "engine": engine_used,
            "engine_available": engine_available,
            "engine_status": engine_status,
            "install_hint": None if engine_available else TESSERACT_INSTALL_HINT,
        },
    )


TESSERACT_INSTALL_HINT = "Install pytesseract and the Tesseract OCR binary, or pass mock_text for deterministic tests."


def detect_tesseract() -> dict[str, Any]:
    status: dict[str, Any] = {
        "engine": "tesseract",
        "available": False,
        "module_available": module_available("pytesseract"),
        "binary_path": shutil.which("tesseract"),
        "version": None,
        "error": None,
        "install_hint": TESSERACT_INSTALL_HINT,
    }
    if not status["module_available"]:
        status["error"] = "Python package pytesseract is not installed."
        return status
    try:
        import pytesseract

        version = pytesseract.get_tesseract_version()
    except Exception as exc:
        status["error"] = f"{type(exc).__name__}: {exc}"
        return status
    status["available"] = True
    status["version"] = str(version)
    status["install_hint"] = None
    return status


def load_or_capture_image(
    params: dict[str, Any],
    run_dir: Path,
    *,
    synthetic_on_capture_fail: bool,
) -> tuple[Image.Image, Path]:
    if params.get("path"):
        path = Path(str(params["path"]))
        image = Image.open(path).convert("RGB")
        return image, path

    if "mock_text" in params:
        width = int(params.get("mock_width", 1280))
        height = int(params.get("mock_height", 720))
        image = Image.new("RGB", (width, height), color=(245, 247, 250))
        path = run_dir / "ocr-mock.png"
        image.save(path)
        return image, path

    capture = ScreenCapture(output_dir=run_dir)
    try:
        screenshot = capture.capture_primary()
    except Exception:
        if not synthetic_on_capture_fail:
            raise
        screenshot = capture.capture_synthetic()
    return screenshot.image, screenshot.path


def mock_ocr_elements(text: str, params: dict[str, Any], image: Image.Image) -> tuple[dict[str, Any], ...]:
    bounds = params.get("mock_bounds")
    if isinstance(bounds, dict):
        parsed_bounds = {
            "left": int(bounds.get("left", 0)),
            "top": int(bounds.get("top", 0)),
            "width": int(bounds.get("width", image.width)),
            "height": int(bounds.get("height", image.height)),
        }
    else:
        parsed_bounds = {"left": 0, "top": 0, "width": image.width, "height": image.height}
    return (
        {
            "text": text,
            "role": "text",
            "confidence": float(params.get("mock_confidence", 0.99)),
            "bounds": parsed_bounds,
            "engine": "mock",
        },
    )


def tesseract_elements(image: Image.Image, *, min_confidence: float) -> tuple[dict[str, Any], ...]:
    import pytesseract

    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    elements = []
    for index, text in enumerate(data.get("text", [])):
        normalized = str(text or "").strip()
        if not normalized:
            continue
        raw_confidence = float(data.get("conf", [0])[index])
        confidence = raw_confidence / 100.0 if raw_confidence > 1 else raw_confidence
        if confidence < min_confidence:
            continue
        bounds = Bounds(
            left=int(data.get("left", [0])[index]),
            top=int(data.get("top", [0])[index]),
            width=int(data.get("width", [0])[index]),
            height=int(data.get("height", [0])[index]),
        )
        elements.append(
            {
                "text": normalized,
                "role": "text",
                "confidence": confidence,
                "bounds": {
                    "left": bounds.left,
                    "top": bounds.top,
                    "width": bounds.width,
                    "height": bounds.height,
                },
                "engine": "tesseract",
            }
        )
    return tuple(elements)


def module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None
