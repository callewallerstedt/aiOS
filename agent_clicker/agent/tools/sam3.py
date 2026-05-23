"""SAM3 text-prompted segmentation tool.

Spawns the existing D:/Volvo/sam3-venv segmenter as a subprocess so we don't
need to install torch/sam3 in our agent venv. Reads back result.json + masks
and converts them to numbered marks the VLM can pick.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .base import ToolResult, clamp_region
from .. import config


def sam3_available() -> tuple[bool, str]:
    if not config.SAM3_PYTHON or not Path(config.SAM3_PYTHON).is_file():
        return False, f"SAM3_PYTHON not found: {config.SAM3_PYTHON}"
    if not config.SAM3_SCRIPT or not Path(config.SAM3_SCRIPT).is_file():
        return False, f"SAM3_SCRIPT not found: {config.SAM3_SCRIPT}"
    if not config.SAM3_CHECKPOINT or not Path(config.SAM3_CHECKPOINT).is_file():
        return False, f"SAM3_CHECKPOINT not found: {config.SAM3_CHECKPOINT}"
    return True, "ok"


def _annotate(img: Image.Image, marks):
    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out, "RGBA")
    try:
        font = ImageFont.truetype("arial.ttf", 18)
    except Exception:
        font = ImageFont.load_default()
    for m in marks:
        x1, y1, x2, y2 = m["bbox"]
        d.rectangle([x1, y1, x2, y2], outline=(255, 0, 255, 255), width=3)
        label = f"{m['id']} ({m['score']:.2f})"
        lx, ly = max(0, x1), max(0, y1 - 20)
        tb = d.textbbox((lx, ly), label, font=font)
        d.rectangle(tb, fill=(255, 0, 255, 230))
        d.text((lx, ly), label, fill=(255, 255, 255), font=font)
        cx, cy = m["center"]
        d.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=(255, 215, 0, 255))
    return out


def sam3_tool(img: Image.Image, prompt: str, region=None, threshold: float = 0.25,
              max_dim: int = 1024, **_) -> ToolResult:
    ok, msg = sam3_available()
    if not ok:
        return ToolResult(summary=f"SAM3 unavailable: {msg}")

    W, H = img.size
    x1, y1, x2, y2 = clamp_region(region, W, H)
    crop = img.crop((x1, y1, x2, y2)).convert("RGB")
    cw, ch = crop.size

    workdir = Path(tempfile.mkdtemp(prefix="agent_clicker_sam3_"))
    try:
        img_path = workdir / "input.png"
        out_dir = workdir / "out"
        out_dir.mkdir()
        crop.save(img_path)

        cmd = [
            config.SAM3_PYTHON, config.SAM3_SCRIPT,
            "--image", str(img_path),
            "--prompt", prompt,
            "--out_dir", str(out_dir),
            "--checkpoint", config.SAM3_CHECKPOINT,
            "--max_dim", str(max_dim),
            "--threshold", str(threshold),
        ]
        t0 = time.time()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return ToolResult(summary="SAM3 timed out after 180s.")
        dur = time.time() - t0

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-2000:]
            return ToolResult(summary=f"SAM3 failed (rc={proc.returncode}):\n{tail}")

        rj = out_dir / "result.json"
        if not rj.is_file():
            return ToolResult(summary=f"SAM3 returned no result.json. stdout tail:\n{proc.stdout[-1000:]}")

        result = json.loads(rj.read_text(encoding="utf-8"))
        instances = result.get("instances", [])

        # SAM3 was run on the resized crop. We need to figure out the scale
        # from the crop size to what SAM3 actually processed. The input.jpg
        # written by the segmenter is the actually-processed image.
        sam_input = cv2.imread(str(out_dir / "input.jpg"))
        if sam_input is None:
            return ToolResult(summary="SAM3 missing input.jpg")
        sh, sw = sam_input.shape[:2]
        sx = cw / sw    # px in crop per px in sam input
        sy = ch / sh

        marks = []
        for i, inst in enumerate(instances):
            bx1, by1, bx2, by2 = inst["box"]
            # box is in sam-processed-image pixels -> back to crop -> back to original
            ox1 = int(bx1 * sx) + x1
            oy1 = int(by1 * sy) + y1
            ox2 = int(bx2 * sx) + x1
            oy2 = int(by2 * sy) + y1
            cx = (ox1 + ox2) // 2
            cy = (oy1 + oy2) // 2
            marks.append({
                "id": i + 1,
                "score": float(inst["score"]),
                "bbox": [ox1, oy1, ox2, oy2],
                "center": [cx, cy],
                "kind": "sam3",
            })

        marks.sort(key=lambda m: -m["score"])
        for i, m in enumerate(marks):
            m["id"] = i + 1

        annotated = _annotate(img, marks)
        lines = [f"SAM3 prompt={prompt!r} in {[x1,y1,x2,y2]} -> {len(marks)} instances "
                 f"(threshold={threshold}, {dur:.1f}s)."]
        for m in marks[:30]:
            lines.append(f"  #{m['id']} score={m['score']:.3f} bbox={m['bbox']} center={m['center']}")
        return ToolResult(summary="\n".join(lines),
                          data={"instances": marks, "region": [x1, y1, x2, y2],
                                "prompt": prompt, "elapsed_s": dur},
                          image=annotated, marks=marks)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
