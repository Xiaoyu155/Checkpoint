"""Generate DevPacer.ico using only Pillow."""
from pathlib import Path
import math

try:
    from PIL import Image, ImageDraw
except ImportError:
    raise SystemExit("pip install Pillow")

OUT = Path(__file__).parent.parent / "assets" / "DevPacer.ico"
OUT.parent.mkdir(exist_ok=True)

BG     = (15,  20,  35)
RING   = (42, 130, 220)
WHITE  = (255, 255, 255)


def make_frame(size: int) -> Image.Image:
    S   = size
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)

    pad     = S * 0.06
    cx, cy  = S / 2, S / 2
    r_out   = S / 2 - pad
    stroke  = max(1, round(S / 16))

    # background circle
    d.ellipse([cx - r_out, cy - r_out, cx + r_out, cy + r_out], fill=BG)

    # blue ring
    d.ellipse(
        [cx - r_out, cy - r_out, cx + r_out, cy + r_out],
        outline=RING, width=stroke,
    )

    # checkmark — three points: start, knee, end
    r_i = r_out * 0.62
    kx  = cx - r_i * 0.06
    ky  = cy + r_i * 0.34
    x1  = cx - r_i * 0.52
    y1  = cy + r_i * 0.04
    x3  = cx + r_i * 0.54
    y3  = cy - r_i * 0.44

    hw = max(1.0, stroke * 0.9)   # half-width of the tick stroke

    def thick_line(ax, ay, bx, by):
        steps = max(6, int(math.hypot(bx - ax, by - ay)))
        for i in range(steps + 1):
            t  = i / steps
            px = ax + (bx - ax) * t
            py = ay + (by - ay) * t
            d.ellipse([px - hw, py - hw, px + hw, py + hw], fill=WHITE)

    thick_line(x1, y1, kx, ky)
    thick_line(kx, ky, x3, y3)

    return img


SIZES = [16, 24, 32, 48, 64, 128, 256]
frames = {s: make_frame(s) for s in SIZES}

# PIL ICO save: pass the largest image, list desired sizes
biggest = frames[256]
biggest.save(
    OUT,
    format="ICO",
    sizes=[(s, s) for s in SIZES],
    append_images=[frames[s] for s in SIZES[:-1]],  # smaller first
)

kb = OUT.stat().st_size / 1024
print(f"Saved {OUT.name}  {kb:.1f} KB  ({len(SIZES)} sizes: {SIZES})")
