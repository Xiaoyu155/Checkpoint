from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import strftime
from typing import Any

import mss
from PIL import Image


@dataclass(frozen=True)
class Screenshot:
    image: Image.Image
    path: Path
    width: int
    height: int


class ScreenCapture:
    def __init__(self, output_dir: str | Path = ".runs") -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def capture_primary(self) -> Screenshot:
        with mss.MSS() as screen:
            monitor = screen.monitors[1]
            raw = screen.grab(monitor)
            image = Image.frombytes("RGB", raw.size, raw.rgb)

        path = self.output_dir / f"screenshot-{strftime('%Y%m%d-%H%M%S')}.png"
        image.save(path)
        return Screenshot(image=image, path=path, width=image.width, height=image.height)

    def capture_synthetic(self, width: int = 1280, height: int = 720) -> Screenshot:
        image = Image.new("RGB", (width, height), color=(245, 247, 250))
        path = self.output_dir / f"synthetic-screen-{strftime('%Y%m%d-%H%M%S')}.png"
        image.save(path)
        return Screenshot(image=image, path=path, width=image.width, height=image.height)


def crop_image(image: Image.Image, crop: dict[str, Any]) -> tuple[Image.Image, dict[str, int]]:
    region = normalized_crop_region(crop, width=image.width, height=image.height)
    box = (
        region["left"],
        region["top"],
        region["left"] + region["width"],
        region["top"] + region["height"],
    )
    return image.crop(box), region


def normalized_crop_region(crop: dict[str, Any], *, width: int, height: int) -> dict[str, int]:
    if not isinstance(crop, dict):
        raise ValueError("crop must be an object.")

    if any(key in crop for key in ("left_percent", "top_percent", "width_percent", "height_percent")):
        left = int(float(crop.get("left_percent", 0.0)) * width)
        top = int(float(crop.get("top_percent", 0.0)) * height)
        crop_width = int(float(crop.get("width_percent", 1.0)) * width)
        crop_height = int(float(crop.get("height_percent", 1.0)) * height)
    else:
        left = int(crop.get("left", 0))
        top = int(crop.get("top", 0))
        crop_width = int(crop.get("width", width - left))
        crop_height = int(crop.get("height", height - top))

    left = max(0, min(left, width - 1))
    top = max(0, min(top, height - 1))
    right = max(left + 1, min(left + max(1, crop_width), width))
    bottom = max(top + 1, min(top + max(1, crop_height), height))
    return {"left": left, "top": top, "width": right - left, "height": bottom - top}


def save_cropped_image(
    image: Image.Image,
    source_path: Path,
    *,
    crop: dict[str, Any],
    output_dir: str | Path,
    label: str = "crop",
) -> tuple[Image.Image, Path, dict[str, int]]:
    cropped, region = crop_image(image, crop)
    path = Path(output_dir) / f"{source_path.stem}-{label}.png"
    cropped.save(path)
    return cropped, path, region


def apply_capture_region(
    image: Image.Image,
    source_path: Path,
    params: dict[str, Any],
    *,
    output_dir: str | Path,
    label: str,
) -> tuple[Image.Image, Path, dict[str, Any]]:
    metadata: dict[str, Any] = {}
    current = image
    current_path = source_path

    uia_window = params.get("uia_window") or params.get("window")
    if isinstance(uia_window, dict):
        from .uia import find_uia_element_bounds

        try:
            bounds = find_uia_element_bounds(
                uia_window,
                max_depth=int(uia_window.get("max_depth", params.get("uia_max_depth", 3))),
                max_elements=int(uia_window.get("max_elements", params.get("uia_max_elements", 800))),
            )
            window_crop = {
                "left": bounds.left,
                "top": bounds.top,
                "width": bounds.width,
                "height": bounds.height,
            }
            min_width = int(uia_window.get("min_width", params.get("min_window_width", 1)))
            min_height = int(uia_window.get("min_height", params.get("min_window_height", 1)))
            min_area = int(uia_window.get("min_area", params.get("min_window_area", min_width * min_height)))
            if bounds.width < min_width or bounds.height < min_height or bounds.width * bounds.height < min_area:
                raise RuntimeError(
                    f"UIA window match is too small: {bounds.width}x{bounds.height}; "
                    f"minimum is {min_width}x{min_height}, area {min_area}."
                )
            current, region = crop_image(current, window_crop)
            metadata["uia_window_region"] = region
            current_path = Path(output_dir) / f"{source_path.stem}-{label}-window.png"
            current.save(current_path)
        except Exception as exc:
            if not bool(params.get("fallback_to_screen", False)):
                raise
            metadata["uia_window_fallback"] = {
                "used": True,
                "reason": f"{exc.__class__.__name__}: {exc}",
            }

    crop = params.get("crop") or params.get("simulator_crop")
    if isinstance(crop, dict):
        current, region = crop_image(current, crop)
        metadata["crop_region"] = region
        current_path = Path(output_dir) / f"{source_path.stem}-{label}.png"
        current.save(current_path)

    return current, current_path, metadata
