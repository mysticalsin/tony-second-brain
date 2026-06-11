"""render_jpgs.py <input.pdf> <output_dir>

Render each PDF page to slide_NN.jpg using PyMuPDF. Pure Python, no system deps.
"""
from __future__ import annotations

import sys
from pathlib import Path

import fitz  # PyMuPDF


DPI = 150  # match Poppler default


def main(pdf_path: Path, out_dir: Path) -> int:
    # Clean stale jpgs so a shorter deck doesn't leave previous ones behind
    for stale in out_dir.glob("slide_*.jpg"):
        stale.unlink()

    doc = fitz.open(str(pdf_path))
    try:
        zoom = DPI / 72  # 72 dpi is fitz's default; scale up to target DPI
        matrix = fitz.Matrix(zoom, zoom)
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            out = out_dir / f"slide_{i:02d}.jpg"
            pix.save(str(out), jpg_quality=88)
        return doc.page_count
    finally:
        doc.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("usage: render_jpgs.py <pdf> <out_dir>")
    pdf = Path(sys.argv[1])
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    n = main(pdf, out)
    print(f"Rendered {n} slide jpgs to {out}")
