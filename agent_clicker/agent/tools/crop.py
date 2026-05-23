from __future__ import annotations
from PIL import Image

from .base import ToolResult, clamp_region


def crop_tool(img: Image.Image, region, **_) -> ToolResult:
    W, H = img.size
    x1, y1, x2, y2 = clamp_region(region, W, H)
    crop = img.crop((x1, y1, x2, y2))
    cw, ch = crop.size
    # Upsample if small so VLM can see detail; preserve original coords for clicks.
    target = 1400
    if max(cw, ch) < target:
        scale = target / max(cw, ch)
        crop = crop.resize((int(cw * scale), int(ch * scale)), Image.LANCZOS)
    summary = (f"crop region {[x1,y1,x2,y2]} (original size {cw}x{ch}); "
               f"shown upscaled. ALL coordinates remain in original image space.")
    return ToolResult(summary=summary,
                      data={"region": [x1, y1, x2, y2], "size": [cw, ch]},
                      image=crop)
