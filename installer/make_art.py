"""Draw the setup wizard's images from the app logo: the tall welcome/finish panel and the small header logo.

Usage: python make_art.py <output folder>
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ASSETS = Path(__file__).resolve().parents[1] / "voice_control" / "assets"
BG = (22, 22, 27)          # overlay.C["bg"]
GLOW = (110, 84, 255)      # the logo's violet
TEXT = (243, 243, 246)     # overlay.C["text"]
MUTED = (163, 163, 178)    # overlay.C["muted"]


def font(names: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def logo(size: int) -> Image.Image:
    with Image.open(ASSETS / "jev-voice-logo.png") as source:
        image = source.convert("RGBA")
    image = image.crop(image.getbbox())
    image.thumbnail((size, size), Image.Resampling.LANCZOS)
    return image


def panel(width: int = 410, height: int = 785) -> Image.Image:
    """Welcome and finish pages: the logo on a dark field with a soft glow, then the name. Aspect 164:314."""
    image = Image.new("RGB", (width, height), BG)
    glow = Image.new("L", (width, height), 0)
    cx, cy = width // 2, int(height * 0.36)
    ImageDraw.Draw(glow).ellipse((cx - 170, cy - 170, cx + 170, cy + 170), fill=120)
    glow = glow.filter(ImageFilter.GaussianBlur(70))
    image.paste(Image.new("RGB", (width, height), GLOW), (0, 0), glow)
    mark = logo(250)
    image.paste(mark, (cx - mark.width // 2, cy - mark.height // 2), mark)

    draw = ImageDraw.Draw(image)
    title = font(("segoeuisb.ttf", "segoeuib.ttf", "arialbd.ttf"), 52)
    tagline = font(("segoeui.ttf", "arial.ttf"), 24)
    y = cy + 190
    for text, face, color, gap in (("Jev Voice", title, TEXT, 18), ("Hold Right Ctrl.", tagline, MUTED, 8),
                                   ("Say what you want.", tagline, MUTED, 0)):
        left, top, right, bottom = draw.textbbox((0, 0), text, font=face)
        draw.text((cx - (right - left) // 2 - left, y - top), text, font=face, fill=color)
        y += bottom - top + gap
    return image


def header(size: int = 174) -> Image.Image:
    """Top-right logo on the inner pages; transparent so it suits light and dark wizards."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mark = logo(int(size * 0.92))
    image.paste(mark, ((size - mark.width) // 2, (size - mark.height) // 2), mark)
    return image


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    panel().save(out / "wizard.png")
    header().save(out / "wizard-small.png")


if __name__ == "__main__":
    main()
