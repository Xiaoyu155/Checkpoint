from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import strftime

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
