from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from typing import Any

from PIL import Image

from .capture import apply_capture_region, capture_visual_region
from .models import Bounds, Observation, ProviderKind


def observe_ocr(params: dict[str, Any], run_dir: Path, *, synthetic_on_capture_fail: bool = False) -> Observation:
    image, path, region_metadata = load_or_capture_image(params, run_dir, synthetic_on_capture_fail=synthetic_on_capture_fail)
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
    elif engine in {"auto", "screen-ocr", "winrt"} and (engine != "auto" or detect_screen_ocr()["available"]):
        engine_used = "screen-ocr"
        engine_status = detect_screen_ocr()
        engine_available = bool(engine_status["available"])
        if engine_available:
            try:
                language = screen_ocr_language(params)
                elements = screen_ocr_elements(image, min_confidence=min_confidence, language=language)
                engine_status = {**engine_status, "language": language}
            except Exception as exc:
                elements = ()
                engine_available = False
                engine_status = {
                    **engine_status,
                    "available": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "install_hint": SCREEN_OCR_INSTALL_HINT,
                }
        else:
            elements = ()
    elif engine in {"auto", "tesseract"}:
        engine_used = "tesseract"
        engine_status = detect_tesseract()
        engine_available = bool(engine_status["available"])
        if engine_available:
            try:
                language = tesseract_language(params)
                elements = tesseract_elements(image, min_confidence=min_confidence, language=language)
                engine_status = {**engine_status, "language": language}
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
            "module_available": module_available("pytesseract") or module_available("screen_ocr"),
            "binary_path": shutil.which("tesseract"),
            "version": None,
            "error": f"Unsupported or unavailable OCR engine: {engine}",
            "install_hint": OCR_INSTALL_HINT,
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
            "install_hint": None if engine_available else engine_status.get("install_hint") or OCR_INSTALL_HINT,
            **region_metadata,
        },
    )


TESSERACT_INSTALL_HINT = "Install pytesseract and the Tesseract OCR binary, or pass mock_text for deterministic tests."
SCREEN_OCR_INSTALL_HINT = "Install screen-ocr[winrt] on Windows for fast native OCR with text coordinates."
OCR_INSTALL_HINT = f"{SCREEN_OCR_INSTALL_HINT} Or {TESSERACT_INSTALL_HINT}"


def detect_tesseract() -> dict[str, Any]:
    binary_path = find_tesseract_binary()
    status: dict[str, Any] = {
        "engine": "tesseract",
        "available": False,
        "module_available": module_available("pytesseract"),
        "binary_path": str(binary_path) if binary_path else None,
        "version": None,
        "error": None,
        "install_hint": TESSERACT_INSTALL_HINT,
    }
    if not status["module_available"]:
        status["error"] = "Python package pytesseract is not installed."
        return status
    try:
        import pytesseract

        if binary_path is not None:
            pytesseract.pytesseract.tesseract_cmd = str(binary_path)
        version = pytesseract.get_tesseract_version()
    except Exception as exc:
        status["error"] = f"{type(exc).__name__}: {exc}"
        return status
    status["available"] = True
    status["version"] = str(version)
    status["install_hint"] = None
    return status


def detect_screen_ocr() -> dict[str, Any]:
    status: dict[str, Any] = {
        "engine": "screen-ocr",
        "available": False,
        "module_available": module_available("screen_ocr"),
        "binary_path": None,
        "version": None,
        "error": None,
        "install_hint": SCREEN_OCR_INSTALL_HINT,
    }
    if not status["module_available"]:
        status["error"] = "Python package screen-ocr is not installed."
        return status
    try:
        import screen_ocr

        getattr(screen_ocr, "Reader")
    except Exception as exc:
        status["error"] = f"{type(exc).__name__}: {exc}"
        return status
    status["available"] = True
    status["version"] = str(getattr(screen_ocr, "__version__", "") or "unknown")
    status["install_hint"] = None
    return status


def tesseract_language(params: dict[str, Any]) -> str | None:
    explicit = params.get("language") or params.get("lang")
    if explicit:
        return str(explicit)
    languages = available_tesseract_languages()
    if "chi_sim" in languages and "eng" in languages:
        return "chi_sim+eng"
    if "chi_sim" in languages:
        return "chi_sim"
    if "eng" in languages:
        return "eng"
    return None


def screen_ocr_language(params: dict[str, Any]) -> str | None:
    explicit = params.get("language") or params.get("lang")
    if not explicit:
        return None
    text = str(explicit)
    if "chi_sim" in text or "zh" in text.lower():
        return "zh-Hans"
    if "eng" in text:
        return "en-US"
    return text


def available_tesseract_languages() -> set[str]:
    binary_path = find_tesseract_binary()
    if binary_path is None or not module_available("pytesseract"):
        return set()
    try:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = str(binary_path)
        return {str(language) for language in pytesseract.get_languages(config="")}
    except Exception:
        return set()


def load_or_capture_image(
    params: dict[str, Any],
    run_dir: Path,
    *,
    synthetic_on_capture_fail: bool,
) -> tuple[Image.Image, Path, dict[str, Any]]:
    if params.get("path"):
        path = Path(str(params["path"]))
        image = Image.open(path).convert("RGB")
        image, path, metadata = apply_capture_region(image, path, params, output_dir=run_dir, label="ocr-region")
        return image, path, metadata

    if "mock_text" in params:
        width = int(params.get("mock_width", 1280))
        height = int(params.get("mock_height", 720))
        image = Image.new("RGB", (width, height), color=(245, 247, 250))
        path = run_dir / "ocr-mock.png"
        image.save(path)
        image, path, metadata = apply_capture_region(image, path, params, output_dir=run_dir, label="ocr-region")
        return image, path, metadata

    image, path, metadata = capture_visual_region(
        params,
        output_dir=run_dir,
        label="ocr-region",
        synthetic_on_capture_fail=synthetic_on_capture_fail,
    )
    return image, path, metadata


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


def tesseract_elements(image: Image.Image, *, min_confidence: float, language: str | None = None) -> tuple[dict[str, Any], ...]:
    import pytesseract

    binary_path = find_tesseract_binary()
    if binary_path is not None:
        pytesseract.pytesseract.tesseract_cmd = str(binary_path)
    kwargs = {"lang": language} if language else {}
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT, **kwargs)
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


def screen_ocr_elements(image: Image.Image, *, min_confidence: float, language: str | None = None) -> tuple[dict[str, Any], ...]:
    import screen_ocr

    reader = create_screen_ocr_reader(screen_ocr, language=language)
    read_image = getattr(reader, "read_image", None)
    if read_image is None:
        raise RuntimeError("screen-ocr reader does not support read_image.")
    result = read_image(image)
    elements = []
    for word in iter_screen_ocr_words(result):
        text = str(getattr(word, "text", "") or getattr(word, "value", "") or "").strip()
        if not text:
            continue
        confidence = float(getattr(word, "confidence", 1.0) or 1.0)
        if confidence < min_confidence:
            continue
        bounds = screen_ocr_word_bounds(word)
        if bounds is None:
            continue
        elements.append(
            {
                "text": text,
                "role": "text",
                "confidence": confidence,
                "bounds": bounds,
                "engine": "screen-ocr",
            }
        )
    return tuple(elements)


def create_screen_ocr_reader(screen_ocr_module: Any, *, language: str | None = None) -> Any:
    reader_cls = screen_ocr_module.Reader
    create_reader = getattr(reader_cls, "create_reader", None)
    if create_reader is None:
        raise RuntimeError("screen-ocr Reader.create_reader is unavailable.")
    try:
        return create_reader(backend="winrt", language_tag=language) if language else create_reader(backend="winrt")
    except TypeError:
        return create_reader(language_tag=language) if language else create_reader()


def iter_screen_ocr_words(result: Any) -> tuple[Any, ...]:
    if hasattr(result, "words"):
        return tuple(getattr(result, "words") or ())
    words = []
    for line in getattr(result, "lines", []) or []:
        words.extend(getattr(line, "words", []) or [])
    return tuple(words)


def screen_ocr_word_bounds(word: Any) -> dict[str, int] | None:
    raw = getattr(word, "bounding_box", None) or getattr(word, "bounds", None) or getattr(word, "rect", None)
    if raw is None:
        return None
    if isinstance(raw, dict):
        left = int(raw.get("left", raw.get("x", 0)))
        top = int(raw.get("top", raw.get("y", 0)))
        if "width" in raw and "height" in raw:
            width = int(raw["width"])
            height = int(raw["height"])
        else:
            width = int(raw.get("right", left) - left)
            height = int(raw.get("bottom", top) - top)
    else:
        left = int(getattr(raw, "left", getattr(raw, "x", 0)))
        top = int(getattr(raw, "top", getattr(raw, "y", 0)))
        if hasattr(raw, "width") and hasattr(raw, "height"):
            width = int(getattr(raw, "width"))
            height = int(getattr(raw, "height"))
        else:
            width = int(getattr(raw, "right", left) - left)
            height = int(getattr(raw, "bottom", top) - top)
    if width <= 0 or height <= 0:
        return None
    return {"left": left, "top": top, "width": width, "height": height}


def module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def find_tesseract_binary() -> Path | None:
    path = shutil.which("tesseract")
    if path:
        return Path(path)
    candidates = (
        Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
        Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"),
    )
    return next((candidate for candidate in candidates if candidate.exists()), None)
