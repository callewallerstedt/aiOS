from __future__ import annotations
import string
from PIL import Image, ImageDraw, ImageFont

from .base import ToolResult, clamp_region


def grid_tool(img: Image.Image, region=None, cols: int = 8, rows: int = 6, **_) -> ToolResult:
    W, H = img.size
    x1, y1, x2, y2 = clamp_region(region, W, H)
    rw, rh = x2 - x1, y2 - y1
    cols = max(1, min(26, int(cols)))
    rows = max(1, min(26, int(rows)))
    cell_w = rw / cols
    cell_h = rh / rows

    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out, "RGBA")
    try:
        font = ImageFont.truetype("arial.ttf", 18)
    except Exception:
        font = ImageFont.load_default()

    marks = []
    n = 1
    letters = string.ascii_uppercase
    for r in range(rows):
        for c in range(cols):
            cx = int(x1 + (c + 0.5) * cell_w)
            cy = int(y1 + (r + 0.5) * cell_h)
            bx1 = int(x1 + c * cell_w)
            by1 = int(y1 + r * cell_h)
            bx2 = int(x1 + (c + 1) * cell_w)
            by2 = int(y1 + (r + 1) * cell_h)
            label = f"{letters[c]}{r+1}"
            d.rectangle([bx1, by1, bx2, by2], outline=(0, 200, 0, 180), width=1)
            tb = d.textbbox((bx1 + 2, by1 + 2), label, font=font)
            d.rectangle(tb, fill=(0, 200, 0, 200))
            d.text((bx1 + 2, by1 + 2), label, fill=(255, 255, 255), font=font)
            marks.append({"id": n, "label": label, "bbox": [bx1, by1, bx2, by2], "center": [cx, cy]})
            n += 1
    summary = f"grid {cols}x{rows} in {[x1,y1,x2,y2]}: each cell ~{int(cell_w)}x{int(cell_h)} px."
    return ToolResult(summary=summary, data={"cells": marks, "region": [x1, y1, x2, y2],
                                              "cols": cols, "rows": rows},
                      image=out, marks=marks)
