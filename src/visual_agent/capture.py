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


def capture_visual_region(
    params: dict[str, Any],
    *,
    output_dir: str | Path,
    label: str,
    synthetic_on_capture_fail: bool = False,
) -> tuple[Image.Image, Path, dict[str, Any]]:
    from .locks import VisualLock

    metadata: dict[str, Any] = {}
    with VisualLock():
        try:
            metadata.update(prepare_capture_window(params))
            capture = ScreenCapture(output_dir=output_dir)
            try:
                screenshot = capture.capture_primary()
            except Exception:
                if not synthetic_on_capture_fail:
                    raise
                screenshot = capture.capture_synthetic()
            image, path, region_metadata = apply_capture_region(
                screenshot.image,
                screenshot.path,
                params,
                output_dir=output_dir,
                label=label,
            )
            metadata.update(region_metadata)
            return image, path, public_capture_metadata({**metadata, **finalize_capture_window(params, metadata)})
        except Exception:
            metadata.update(finalize_capture_window(params, metadata))
            raise


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
        from .uia import find_uia_window_match

        try:
            match = find_uia_window_match(
                uia_window,
                max_depth=int(uia_window.get("max_depth", params.get("uia_max_depth", 3))),
                max_elements=int(uia_window.get("max_elements", params.get("uia_max_elements", 800))),
            )
            bounds = match.bounds
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
            metadata["uia_window"] = {
                "name": match.name,
                "native_window_handle": match.native_window_handle,
            }
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


def public_capture_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metadata.items() if not str(key).startswith("_")}


def prepare_capture_window(params: dict[str, Any]) -> dict[str, Any]:
    uia_window = params.get("uia_window") or params.get("window")
    if not isinstance(uia_window, dict):
        return {}
    if not bool(uia_window.get("bring_to_front") or uia_window.get("foreground")):
        return {}
    from .uia import find_uia_window_match, get_foreground_window, save_window_placement

    # Snapshot the current foreground window so we can restore it after capture.
    prev_hwnd = get_foreground_window()
    prev_placement = save_window_placement(prev_hwnd) if prev_hwnd else None

    try:
        match = find_uia_window_match(
            uia_window,
            max_depth=int(uia_window.get("max_depth", params.get("uia_max_depth", 3))),
            max_elements=int(uia_window.get("max_elements", params.get("uia_max_elements", 800))),
        )
        result: dict[str, Any] = {
            "uia_window_pre_capture": {
                "foregrounded": True,
                "name": match.name,
                "native_window_handle": match.native_window_handle,
            }
        }
        if prev_placement is not None:
            # Internal key (underscore prefix) - consumed by finalize_capture_window
            # and excluded from public audit fields.
            result["_prev_foreground_placement"] = {
                "handle": prev_placement.handle,
                "show_cmd": prev_placement.show_cmd,
                "min_pt": list(prev_placement.min_pt),
                "max_pt": list(prev_placement.max_pt),
                "normal_rect": list(prev_placement.normal_rect),
            }
        return result
    except Exception as exc:
        if not bool(params.get("fallback_to_screen", False)):
            raise
        return {
            "uia_window_fallback": {
                "used": True,
                "phase": "pre_capture",
                "reason": f"{exc.__class__.__name__}: {exc}",
            }
        }


def finalize_capture_window(params: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    uia_window = params.get("uia_window") or params.get("window")
    result: dict[str, Any] = {}

    # Minimize target window by default after an explicit bring-to-front capture.
    # This keeps visual checks from occupying the user's desktop after evidence is collected.
    if isinstance(uia_window, dict):
        post_capture = str(
            uia_window.get("post_capture")
            or uia_window.get("after_capture")
            or params.get("post_capture")
            or params.get("after_capture")
            or ("minimize" if bool(uia_window.get("bring_to_front") or uia_window.get("foreground")) else "")
        ).strip().lower()
        keep_open = post_capture in {"keep", "keep_open", "none", "noop"} or bool(
            uia_window.get("keep_open") or params.get("keep_open")
        )
        minimize_after = bool(
            uia_window.get("minimize_after")
            or uia_window.get("minimize_after_capture")
            or params.get("minimize_after")
            or params.get("minimize_after_capture")
        )
        if (post_capture in {"minimize", "minimise"} or minimize_after) and not keep_open:
            handle = None
            window_meta = metadata.get("uia_window")
            if isinstance(window_meta, dict):
                handle = window_meta.get("native_window_handle")
            pre_capture = metadata.get("uia_window_pre_capture")
            if handle is None and isinstance(pre_capture, dict):
                handle = pre_capture.get("native_window_handle")
            try:
                parsed_handle = int(handle)
                from .uia import minimize_window

                ok = minimize_window(parsed_handle)
                result["uia_window_post_capture"] = {"action": "minimize", "status": "success" if ok else "failed"}
            except (TypeError, ValueError):
                result["uia_window_post_capture"] = {
                    "action": "minimize",
                    "status": "skipped",
                    "reason": "missing_window_handle",
                }

    # --- Restore the foreground window that was active before bring-to-front ---
    prev_data = metadata.get("_prev_foreground_placement")
    if isinstance(prev_data, dict) and prev_data.get("handle"):
        from .uia import WindowPlacement, restore_window_placement

        try:
            placement = WindowPlacement(
                handle=int(prev_data["handle"]),
                show_cmd=int(prev_data["show_cmd"]),
                min_pt=(int(prev_data["min_pt"][0]), int(prev_data["min_pt"][1])),
                max_pt=(int(prev_data["max_pt"][0]), int(prev_data["max_pt"][1])),
                normal_rect=(
                    int(prev_data["normal_rect"][0]),
                    int(prev_data["normal_rect"][1]),
                    int(prev_data["normal_rect"][2]),
                    int(prev_data["normal_rect"][3]),
                ),
            )
            ok = restore_window_placement(placement)
            result["uia_window_scene_restore"] = {
                "action": "restore_foreground",
                "status": "success" if ok else "failed",
                "handle": placement.handle,
            }
        except Exception as exc:
            result["uia_window_scene_restore"] = {
                "action": "restore_foreground",
                "status": "error",
                "reason": str(exc),
            }

    return result
