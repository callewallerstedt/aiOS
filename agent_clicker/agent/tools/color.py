from __future__ import annotations
import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont

from .base import ToolResult


def color_mask_tool(img: Image.Image, rgb, tolerance: int = 25, **_) -> ToolResult:
    arr = np.array(img.convert("RGB"))
    r, g, b = int(rgb[0]), int(rgb[1]), int(rgb[2])
    lo = np.array([max(0, r - tolerance), max(0, g - tolerance), max(0, b - tolerance)])
    hi = np.array([min(255, r + tolerance), min(255, g + tolerance), min(255, b + tolerance)])
    mask = cv2.inRange(arr, lo, hi)
    # morphological cleanup
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    num, labels, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    marks = []
    for i in range(1, num):
        x, y, w, h, area = stats[i]
        if area < 30:
            continue
        cx, cy = int(cents[i][0]), int(cents[i][1])
        marks.append({"id": len(marks) + 1, "bbox": [int(x), int(y), int(x + w), int(y + h)],
                      "center": [cx, cy], "area": int(area)})
    # Visualize: dim image + highlight matches
    vis = arr.copy()
    overlay = np.zeros_like(arr)
    overlay[mask > 0] = (255, 215, 0)
    vis = cv2.addWeighted(vis, 0.55, overlay, 0.45, 0)
    out = Image.fromarray(vis)
    d = ImageDraw.Draw(out, "RGBA")
    try:
        font = ImageFont.truetype("arial.ttf", 14)
    except Exception:
        font = ImageFont.load_default()
    for m in marks:
        d.rectangle(m["bbox"], outline=(255, 0, 0, 255), width=2)
        d.text((m["bbox"][0] + 2, m["bbox"][1] + 2), str(m["id"]),
               fill=(255, 255, 255), font=font)
    summary = f"color_mask rgb={rgb} tol={tolerance}: {len(marks)} clusters."
    return ToolResult(summary=summary, data={"clusters": marks}, image=out, marks=marks)
