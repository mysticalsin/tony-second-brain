"""qa_contact_sheet.py <out_dir>

Generate a 4-column contact sheet from slide_NN.jpg files in <out_dir>.
Pure Pillow, no ImageMagick.
"""
from __future__ import annotations

import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


COLS = 4
THUMB_W = 480
PADDING = 16
LABEL_H = 28
BG = (248, 248, 248)
LABEL_COLOR = (60, 60, 62)


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main(out_dir: Path) -> Path:
    jpgs = sorted(out_dir.glob("slide_*.jpg"))
    if not jpgs:
        raise SystemExit(f"No slide_*.jpg in {out_dir}. Run with --render first.")

    with Image.open(jpgs[0]) as im:
        ratio = im.height / im.width
    thumb_h = int(THUMB_W * ratio)

    rows = (len(jpgs) + COLS - 1) // COLS
    grid_w = COLS * THUMB_W + (COLS + 1) * PADDING
    grid_h = rows * (thumb_h + LABEL_H) + (rows + 1) * PADDING

    sheet = Image.new("RGB", (grid_w, grid_h), BG)
    draw = ImageDraw.Draw(sheet)
    font = _load_font(16)

    for i, jpg in enumerate(jpgs):
        r, c = divmod(i, COLS)
        x = PADDING + c * (THUMB_W + PADDING)
        y = PADDING + r * (thumb_h + LABEL_H + PADDING)

        with Image.open(jpg) as im:
            im.thumbnail((THUMB_W, thumb_h * 2), Image.LANCZOS)
            sheet.paste(im, (x, y))

        label = f"Slide {i + 1:02d}"
        draw.text((x + 4, y + thumb_h + 4), label, fill=LABEL_COLOR, font=font)

    out = out_dir / "contact_sheet.jpg"
    sheet.save(out, "JPEG", quality=85, optimize=True)
    print(f"Contact sheet → {out} ({len(jpgs)} slides, {rows}×{COLS})")
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]).resolve())
