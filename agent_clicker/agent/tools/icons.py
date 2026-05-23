from __future__ import annotations
import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont

from .base import ToolResult, clamp_region


def _detect_icon_boxes(crop_bgr: np.ndarray, min_area=400, max_area_frac=0.25):
    H, W = crop_bgr.shape[:2]
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    # Multi-scale edges
    edges = cv2.Canny(gray, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    max_area = W * H * max_area_frac
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        a = w * h
        if a < min_area or a > max_area:
            continue
        ar = w / max(h, 1)
        if ar > 8 or ar < 0.12:
            continue
        boxes.append((x, y, x + w, y + h))
    # Non-max suppression by IoU
    boxes = _nms(boxes, 0.4)
    return boxes


def _nms(boxes, iou_thr):
    if not boxes:
        return []
    arr = np.array(boxes, dtype=np.float32)
    x1, y1, x2, y2 = arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = areas.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0, xx2 - xx1)
        h = np.maximum(0, yy2 - yy1)
        inter = w * h
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)
        order = order[1:][iou < iou_thr]
    return [tuple(map(int, arr[i])) for i in keep]


def _annotate(img: Image.Image, marks):
    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out, "RGBA")
    try:
        font = ImageFont.truetype("arial.ttf", 16)
    except Exception:
        font = ImageFont.load_default()
    for m in marks:
        x1, y1, x2, y2 = m["bbox"]
        d.rectangle([x1, y1, x2, y2], outline=(40, 180, 255, 255), width=2)
        label = str(m["id"])
        lx, ly = max(0, x1), max(0, y1 - 18)
        tb = d.textbbox((lx, ly), label, font=font)
        d.rectangle(tb, fill=(40, 180, 255, 230))
        d.text((lx, ly), label, fill=(255, 255, 255), font=font)
        cx, cy = m["center"]
        d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=(255, 215, 0, 255))
    return out


def find_icons_tool(img: Image.Image, region=None, **_) -> ToolResult:
    W, H = img.size
    x1, y1, x2, y2 = clamp_region(region, W, H)
    crop = np.array(img.crop((x1, y1, x2, y2)).convert("RGB"))
    bgr = cv2.cvtColor(crop, cv2.COLOR_RGB2BGR)
    boxes = _detect_icon_boxes(bgr)
    marks = []
    for i, (bx1, by1, bx2, by2) in enumerate(boxes):
        ax1, ay1, ax2, ay2 = bx1 + x1, by1 + y1, bx2 + x1, by2 + y1
        cx, cy = (ax1 + ax2) // 2, (ay1 + ay2) // 2
        marks.append({
            "id": i + 1,
            "bbox": [ax1, ay1, ax2, ay2],
            "center": [cx, cy],
        })
    annotated = _annotate(img, marks)
    summary = f"find_icons: {len(marks)} candidate regions in {[x1,y1,x2,y2]}."
    return ToolResult(summary=summary, data={"boxes": marks, "region": [x1, y1, x2, y2]},
                      image=annotated, marks=marks)
