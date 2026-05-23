from dataclasses import dataclass, field
from typing import Any
from PIL import Image


@dataclass
class ToolResult:
    """What a tool returns to the orchestrator."""
    summary: str                              # short text for VLM
    data: dict[str, Any] = field(default_factory=dict)  # structured (boxes, marks, etc.)
    image: Image.Image | None = None          # annotated image to show VLM
    marks: list[dict] | None = None           # [{id:int, center:(x,y), bbox:[..], label:..}]


def clamp_region(region, w, h):
    if region is None:
        return (0, 0, w, h)
    x1, y1, x2, y2 = region
    x1 = max(0, min(w - 1, int(x1)))
    y1 = max(0, min(h - 1, int(y1)))
    x2 = max(x1 + 1, min(w, int(x2)))
    y2 = max(y1 + 1, min(h, int(y2)))
    return (x1, y1, x2, y2)
