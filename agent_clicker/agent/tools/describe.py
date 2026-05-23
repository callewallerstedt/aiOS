from __future__ import annotations
from PIL import Image
from .base import ToolResult, clamp_region
from .. import vlm


def describe_tool(img: Image.Image, region=None, question: str = "Describe what you see in detail.", **_) -> ToolResult:
    W, H = img.size
    reg = clamp_region(region, W, H)
    crop = img.crop(reg)
    data_url, _ = vlm.encode_image(crop)
    parsed, raw = vlm.chat_json(
        system='You are a careful UI describer. Reply JSON: {"description":"..."}.',
        messages=[{"role": "user", "content": [
            vlm.text_part(f"Region {list(reg)} of a screenshot. Question: {question}"),
            vlm.image_part(data_url),
        ]}],
    )
    desc = parsed.get("description", raw)
    return ToolResult(summary=f"describe({list(reg)}): {desc}", data={"description": desc})
