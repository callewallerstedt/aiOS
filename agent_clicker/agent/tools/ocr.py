from __future__ import annotations
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .base import ToolResult, clamp_region
from .. import config

_reader = None


def _get_reader():
    global _reader
    if _reader is not None:
        return _reader
    if config.OCR_BACKEND != "easyocr":
        return None
    try:
        import easyocr
    except ImportError:
        return None
    _reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _reader


def _draw_marks(img: Image.Image, marks: list[dict]) -> Image.Image:
    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out, "RGBA")
    try:
        font = ImageFont.truetype("arial.ttf", 16)
    except Exception:
        font = ImageFont.load_default()
    for m in marks:
        x1, y1, x2, y2 = m["bbox"]
        d.rectangle([x1, y1, x2, y2], outline=(255, 40, 40, 255), width=2)
        label = str(m["id"])
        cx, cy = m["center"]
        # label background near top-left of box, clipped to image
        lx, ly = max(0, x1), max(0, y1 - 18)
        tb = d.textbbox((lx, ly), label, font=font)
        d.rectangle(tb, fill=(255, 40, 40, 230))
        d.text((lx, ly), label, fill=(255, 255, 255), font=font)
        # center dot
        d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=(255, 215, 0, 255))
    return out


def _preprocess(arr: np.ndarray) -> np.ndarray:
    """Upscale small text to help OCR. Keep RGB."""
    import cv2
    h, w = arr.shape[:2]
    target = 1600
    if max(h, w) < target:
        scale = target / max(h, w)
        arr = cv2.resize(arr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
        return arr, scale
    return arr, 1.0


def ocr_tool(img: Image.Image, region=None, **_) -> ToolResult:
    reader = _get_reader()
    if reader is None:
        return ToolResult(summary=("OCR unavailable — easyocr not installed yet. "
                                    "Use sam3, find_icons, grid, or describe instead."),
                          data={"results": []})

    W, H = img.size
    x1, y1, x2, y2 = clamp_region(region, W, H)
    crop = img.crop((x1, y1, x2, y2)).convert("RGB")
    arr = np.array(crop)
    arr_up, scale = _preprocess(arr)

    results = reader.readtext(arr_up, detail=1, paragraph=False)
    marks = []
    for i, (poly, text, conf) in enumerate(results):
        xs = [p[0] / scale for p in poly]
        ys = [p[1] / scale for p in poly]
        bx1, by1, bx2, by2 = min(xs), min(ys), max(xs), max(ys)
        # back to original image coords
        bx1 += x1; bx2 += x1; by1 += y1; by2 += y1
        cx, cy = int((bx1 + bx2) / 2), int((by1 + by2) / 2)
        marks.append({
            "id": i + 1,
            "text": text,
            "conf": float(conf),
            "bbox": [int(bx1), int(by1), int(bx2), int(by2)],
            "center": [cx, cy],
        })

    annotated = _draw_marks(img, marks)
    summary_lines = [f"OCR found {len(marks)} text boxes in region {[x1,y1,x2,y2]}."]
    for m in marks[:80]:
        summary_lines.append(f"  #{m['id']}: {m['text']!r} center={m['center']} conf={m['conf']:.2f}")
    return ToolResult(
        summary="\n".join(summary_lines),
        data={"results": marks, "region": [x1, y1, x2, y2]},
        image=annotated,
        marks=marks,
    )
