from __future__ import annotations
from PIL import Image, ImageDraw, ImageFont

from .base import ToolResult, clamp_region
from .ocr import ocr_tool
from .icons import find_icons_tool


def _renumber_and_draw(img: Image.Image, marks):
    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out, "RGBA")
    try:
        font = ImageFont.truetype("arial.ttf", 16)
    except Exception:
        font = ImageFont.load_default()
    for m in marks:
        x1, y1, x2, y2 = m["bbox"]
        color = (40, 180, 255, 255) if m.get("kind") == "icon" else (255, 40, 40, 255)
        d.rectangle([x1, y1, x2, y2], outline=color, width=2)
        label = str(m["id"])
        lx, ly = max(0, x1), max(0, y1 - 18)
        tb = d.textbbox((lx, ly), label, font=font)
        d.rectangle(tb, fill=color[:3] + (230,))
        d.text((lx, ly), label, fill=(255, 255, 255), font=font)
        cx, cy = m["center"]
        d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=(255, 215, 0, 255))
    return out


def set_of_marks_tool(img: Image.Image, region=None, mode: str = "both", **_) -> ToolResult:
    W, H = img.size
    reg = clamp_region(region, W, H)
    all_marks = []
    if mode in ("ocr", "both"):
        r = ocr_tool(img, region=reg)
        for m in r.marks or []:
            all_marks.append({**m, "kind": "text"})
    if mode in ("icons", "both"):
        r = find_icons_tool(img, region=reg)
        for m in r.marks or []:
            all_marks.append({**m, "kind": "icon"})
    # renumber globally
    for i, m in enumerate(all_marks):
        m["id"] = i + 1
    annotated = _renumber_and_draw(img, all_marks)
    summary_lines = [f"set_of_marks ({mode}) in {list(reg)}: {len(all_marks)} marks."]
    for m in all_marks[:120]:
        t = m.get("text", "")
        summary_lines.append(f"  #{m['id']} [{m['kind']}] center={m['center']} {('text='+repr(t)) if t else ''}")
    return ToolResult(
        summary="\n".join(summary_lines),
        data={"marks": all_marks, "region": list(reg)},
        image=annotated,
        marks=all_marks,
    )
