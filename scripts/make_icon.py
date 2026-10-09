"""Render the ChatBridge icon (same artwork as packaging/*.svg) to chatbridge/data/chatbridge.ico. Needs Pillow.

python scripts/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SCALE = 8  # supersampling for smooth edges
SIZE = 128


def render(px: int) -> Image.Image:
    canvas = px * SCALE
    k = canvas / SIZE
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, canvas - 1, canvas - 1), radius=28 * k, fill="#1e1e2e")
    draw.rounded_rectangle((12 * k, 30 * k, 86 * k, 76 * k), radius=10 * k, fill="#89b4fa")
    draw.polygon([(30 * k, 70 * k), (46 * k, 76 * k), (30 * k, 92 * k)], fill="#89b4fa")
    draw.rounded_rectangle((42 * k, 56 * k, 116 * k, 102 * k), radius=10 * k, fill="#d97757")
    draw.polygon([(98 * k, 98 * k), (98 * k, 118 * k), (80 * k, 102 * k)], fill="#d97757")
    line = int(6 * k)
    draw.line([(60 * k, 80 * k), (88 * k, 80 * k)], fill="#1e1e2e", width=line)
    draw.line([(88 * k, 80 * k), (79 * k, 71 * k)], fill="#1e1e2e", width=line)
    draw.line([(88 * k, 80 * k), (79 * k, 89 * k)], fill="#1e1e2e", width=line)
    for x, y in ((60, 80), (88, 80), (79, 71), (79, 89)):
        r = line / 2
        draw.ellipse((x * k - r, y * k - r, x * k + r, y * k + r), fill="#1e1e2e")
    return image.resize((px, px), Image.Resampling.LANCZOS)


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "chatbridge" / "data" / "chatbridge.ico"
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [render(s) for s in sizes]
    images[-1].save(out, format="ICO", sizes=[(s, s) for s in sizes], append_images=images[:-1])
    print(out)


if __name__ == "__main__":
    main()
