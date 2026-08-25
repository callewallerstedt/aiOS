"""LocateAnything client for the operator's `la_click` tool.

The brain (Grok/GPT) describes a click target with plain text (visible text,
rough screen area, colour/shape). Instead of burning expensive GPT-5.5 coordinate
tokens, we crop a BROAD region of the monitor around the area the brain named and
send only that crop to a locally running LocateAnything server
(NVIDIA LocateAnything-3B, default http://127.0.0.1:7860). It returns a bounding
box / click point; we map that point back to monitor-local pixels and click its
centre.

Cropping the region (instead of the full monitor) keeps the upload small so the
3B model answers fast, and gives it less to disambiguate.

Coordinate spaces:
  - monitor-local: (0,0) = top-left of the captured monitor image (what the brain
    and `actions.execute` use).
  - crop-local: (0,0) = top-left of the region crop we upload.
  - LocateAnything returns click points in the ORIGINAL uploaded-image space,
    which IS crop-local. We add the crop offset to get monitor-local.
"""
from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass, field

import requests
from PIL import Image


DEFAULT_URL = os.getenv("LOCATEANYTHING_URL", "http://127.0.0.1:7860").rstrip("/")

# Each 3x3 region spans 60% of the monitor in each axis, so neighbouring regions
# overlap heavily (mid column/row covers 20%..80%). A target the brain places in
# roughly the wrong cell is still almost always inside the crop.
_REGION_HALF = 0.30  # half-span as a fraction of the axis (full span = 0.60)

# col index 0/1/2 -> centre fraction; row likewise.
_COL = {"l": 0, "m": 1, "c": 1, "r": 2}
_ROW = {"t": 0, "m": 1, "c": 1, "b": 2}
_CENTERS = (1.0 / 6.0, 0.5, 5.0 / 6.0)

# Region aliases the model might emit. Canonical codes: TL TM TR ML C MR BL BM BR.
_REGION_ALIASES = {
    "tl": "TL", "topleft": "TL", "top-left": "TL", "top_left": "TL",
    "tm": "TM", "tc": "TM", "top": "TM", "topmid": "TM", "topcenter": "TM",
    "tr": "TR", "topright": "TR", "top-right": "TR", "top_right": "TR",
    "ml": "ML", "left": "ML", "midleft": "ML", "centerleft": "ML",
    "c": "C", "center": "C", "centre": "C", "mid": "C", "middle": "C", "mm": "C", "cc": "C",
    "mr": "MR", "right": "MR", "midright": "MR", "centerright": "MR",
    "bl": "BL", "bottomleft": "BL", "bottom-left": "BL", "bottom_left": "BL",
    "bm": "BM", "bc": "BM", "bottom": "BM", "bottommid": "BM", "bottomcenter": "BM",
    "br": "BR", "bottomright": "BR", "bottom-right": "BR", "bottom_right": "BR",
}

CANONICAL_REGIONS = ["TL", "TM", "TR", "ML", "C", "MR", "BL", "BM", "BR"]


def normalize_region(region: str | None) -> str | None:
    """Map a free-form region string to a canonical code, or None for whole-screen."""
    if not region:
        return None
    key = "".join(ch for ch in str(region).lower() if ch.isalnum())
    if not key:
        return None
    if key in ("full", "all", "screen", "whole", "everywhere", "any"):
        return None
    code = _REGION_ALIASES.get(key)
    if code:
        return code
    # Try a 2-char "<row><col>" form not already in the alias table.
    if len(key) == 2 and key[0] in _ROW and key[1] in _COL:
        row = "T" if key[0] == "t" else ("B" if key[0] == "b" else "M")
        col = "L" if key[1] == "l" else ("R" if key[1] == "r" else "M")
        cand = (row + col).replace("MM", "C")
        if cand in CANONICAL_REGIONS:
            return cand
    return None


def region_crop_box(region: str | None, width: int, height: int) -> tuple[int, int, int, int]:
    """Return (left, top, right, bottom) for a broad, overlapping region crop.

    region=None (or 'full') returns the whole image. Codes are TL..BR.
    """
    code = normalize_region(region)
    if code is None:
        return (0, 0, width, height)
    if code == "C":
        row = col = 1
    else:
        row = _ROW.get(code[0].lower(), 1)
        col = _COL.get(code[1].lower(), 1)
    cx = _CENTERS[col] * width
    cy = _CENTERS[row] * height
    half_w = _REGION_HALF * width
    half_h = _REGION_HALF * height
    left = int(max(0, round(cx - half_w)))
    top = int(max(0, round(cy - half_h)))
    right = int(min(width, round(cx + half_w)))
    bottom = int(min(height, round(cy + half_h)))
    # Guard against degenerate crops on tiny monitors.
    if right - left < 32:
        left, right = 0, width
    if bottom - top < 32:
        top, bottom = 0, height
    return (left, top, right, bottom)


@dataclass
class LocateResult:
    ok: bool
    # monitor-local click point (centre of the located box), if found.
    x: int | None = None
    y: int | None = None
    # monitor-local bounding box of the located target, if any.
    box: dict | None = None  # {"x1","y1","x2","y2"} in monitor-local px
    crop_box: tuple[int, int, int, int] = (0, 0, 0, 0)  # region crop in monitor-local px
    region: str | None = None
    phrase: str = ""
    answer: str = ""
    elapsed_ms: int = 0
    # Annotated crop returned by the server (data URL), good for the flow UI.
    annotated_data_url: str = ""
    detail: str = ""
    error: str = ""


def is_available(url: str = DEFAULT_URL, timeout: float = 1.5) -> tuple[bool, str]:
    """Quick health check against the LocateAnything server."""
    try:
        resp = requests.get(f"{url.rstrip('/')}/api/status", timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            return True, f"LocateAnything up ({data.get('model', '?')})"
        return False, f"LocateAnything HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001 - surfaced to UI/log only
        return False, f"LocateAnything unreachable: {exc}"


def locate_in_region(
    img: Image.Image,
    phrase: str,
    region: str | None = None,
    *,
    url: str = DEFAULT_URL,
    task: str = "ui",
    generation_mode: str = "slow",
    timeout: float = 30.0,
) -> LocateResult:
    """Crop `region` from the monitor image, ask LocateAnything for the target,
    and return a result with the click point in MONITOR-LOCAL pixels.
    """
    url = (url or DEFAULT_URL).rstrip("/")
    width, height = img.size
    crop_box = region_crop_box(region, width, height)
    left, top, right, bottom = crop_box
    crop = img.crop(crop_box).convert("RGB")

    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    buf.seek(0)

    files = {"image": ("region.png", buf, "image/png")}
    data = {
        "phrase": phrase,
        "task": task,
        "generation_mode": generation_mode,
        # Cap the edge so the crop upload stays fast on the 3B model.
        "max_image_edge": str(min(1280, max(crop.size))),
    }
    try:
        resp = requests.post(f"{url}/api/locate", files=files, data=data, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return LocateResult(ok=False, region=normalize_region(region), phrase=phrase,
                            crop_box=crop_box, error=f"request failed: {exc}")
    if resp.status_code != 200:
        snippet = (resp.text or "")[:200]
        return LocateResult(ok=False, region=normalize_region(region), phrase=phrase,
                            crop_box=crop_box,
                            error=f"HTTP {resp.status_code}: {snippet}")
    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        return LocateResult(ok=False, region=normalize_region(region), phrase=phrase,
                            crop_box=crop_box, error=f"bad JSON: {exc}")

    elapsed_ms = int(float(payload.get("elapsed_seconds") or 0.0) * 1000)
    answer = str(payload.get("answer") or "")
    annotated = str(payload.get("image") or "")

    # click_points / boxes_original are in the uploaded (crop-local) space.
    click_points = payload.get("click_points") or []
    boxes = payload.get("boxes_original") or []

    if not click_points:
        return LocateResult(
            ok=False, region=normalize_region(region), phrase=phrase,
            crop_box=crop_box, elapsed_ms=elapsed_ms, answer=answer,
            annotated_data_url=annotated,
            detail="LocateAnything found no target in the region",
        )

    pt = click_points[0]
    mx = int(round(left + float(pt["x"])))
    my = int(round(top + float(pt["y"])))
    mx = max(0, min(width - 1, mx))
    my = max(0, min(height - 1, my))

    box_local = None
    if boxes:
        b = boxes[0]
        box_local = {
            "x1": int(round(left + float(b["x1"]))),
            "y1": int(round(top + float(b["y1"]))),
            "x2": int(round(left + float(b["x2"]))),
            "y2": int(round(top + float(b["y2"]))),
        }

    return LocateResult(
        ok=True, x=mx, y=my, box=box_local, crop_box=crop_box,
        region=normalize_region(region), phrase=phrase,
        elapsed_ms=elapsed_ms, answer=answer, annotated_data_url=annotated,
        detail=f"located at ({mx},{my})",
    )
