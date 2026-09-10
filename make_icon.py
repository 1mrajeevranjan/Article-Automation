"""Generates the app icon: a document page with text lines and an AI spark,
rendered at 1024px and downscaled into a macOS .icns iconset.

Run: .venv/bin/python make_icon.py
"""

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024
OUT_DIR = Path(__file__).resolve().parent
ICONSET = OUT_DIR / "AppIcon.iconset"

INK = (28, 34, 48, 255)          # deep slate — page text
PAGE = (252, 251, 248, 255)      # warm paper white
ACCENT = (79, 110, 247, 255)     # blue — the "AI" spark
ACCENT_SOFT = (140, 162, 250, 255)
BG_TOP = (46, 58, 89, 255)
BG_BOTTOM = (24, 30, 47, 255)


def rounded_gradient(size: int, radius: int) -> Image.Image:
    """macOS-style squircle-ish background with a vertical gradient."""
    grad = Image.new("RGBA", (1, size))
    for y in range(size):
        t = y / max(size - 1, 1)
        grad.putpixel((0, y), tuple(
            int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(4)
        ))
    grad = grad.resize((size, size))

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def draw_icon() -> Image.Image:
    img = rounded_gradient(SIZE, radius=int(SIZE * 0.22))
    d = ImageDraw.Draw(img)

    # Document page, slightly tilted feel via offset shadow
    px0, py0, px1, py1 = 286, 208, 738, 838
    d.rounded_rectangle([px0 + 14, py0 + 18, px1 + 14, py1 + 18], radius=26, fill=(0, 0, 0, 70))
    d.rounded_rectangle([px0, py0, px1, py1], radius=26, fill=PAGE)

    # Title bar + text lines on the page
    d.rounded_rectangle([px0 + 58, py0 + 74, px0 + 268, py0 + 104], radius=15, fill=INK)

    line_x0, line_x1 = px0 + 58, px1 - 58
    y = py0 + 158
    for i in range(9):
        width_factor = 1.0 if i % 4 != 3 else 0.62
        d.rounded_rectangle(
            [line_x0, y, line_x0 + (line_x1 - line_x0) * width_factor, y + 18],
            radius=9, fill=(196, 201, 214, 255),
        )
        y += 46

    # AI spark — four-point star badge at the page's bottom-right corner
    cx, cy, r, w = 764, 800, 150, 40
    d.polygon([(cx, cy - r), (cx + w, cy - w), (cx + r, cy),
               (cx + w, cy + w), (cx, cy + r), (cx - w, cy + w),
               (cx - r, cy), (cx - w, cy - w)], fill=ACCENT)
    # smaller companion spark
    cx2, cy2, r2, w2 = 838, 636, 62, 16
    d.polygon([(cx2, cy2 - r2), (cx2 + w2, cy2 - w2), (cx2 + r2, cy2),
               (cx2 + w2, cy2 + w2), (cx2, cy2 + r2), (cx2 - w2, cy2 + w2),
               (cx2 - r2, cy2), (cx2 - w2, cy2 - w2)], fill=ACCENT_SOFT)

    return img


def build_icns():
    ICONSET.mkdir(exist_ok=True)
    master = draw_icon()
    master.save(OUT_DIR / "AppIcon.png")

    for size in (16, 32, 64, 128, 256, 512):
        master.resize((size, size), Image.LANCZOS).save(ICONSET / f"icon_{size}x{size}.png")
        master.resize((size * 2, size * 2), Image.LANCZOS).save(ICONSET / f"icon_{size}x{size}@2x.png")
    master.save(ICONSET / "icon_512x512@2x.png")

    subprocess.run(["iconutil", "-c", "icns", str(ICONSET), "-o", str(OUT_DIR / "AppIcon.icns")], check=True)
    print(f"Wrote {OUT_DIR / 'AppIcon.icns'}")


if __name__ == "__main__":
    build_icns()
