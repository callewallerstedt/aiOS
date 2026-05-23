from __future__ import annotations
import base64
import io
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from PIL import Image, ImageDraw

from . import config, vlm
from .prompts import SYSTEM_PROMPT, RAW_SYSTEM_PROMPT_NO_CROP, RAW_SYSTEM_PROMPT_WITH_CROP
from .tools import TOOLS
from .tools.base import ToolResult


@dataclass
class Round:
    n: int
    thought: str = ""
    tool: str = ""
    args: dict = field(default_factory=dict)
    result_summary: str = ""
    result_image_b64: str = ""
    raw_response: str = ""
    error: str = ""
    elapsed_ms: int = 0


def _img_to_b64(img: Image.Image | None) -> str:
    if img is None:
        return ""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _draw_final(img: Image.Image, x: int, y: int) -> Image.Image:
    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out, "RGBA")
    R = 24
    d.ellipse([x - R, y - R, x + R, y + R], outline=(0, 255, 0, 255), width=4)
    d.line([x - R, y, x + R, y], fill=(0, 255, 0, 255), width=2)
    d.line([x, y - R, x, y + R], fill=(0, 255, 0, 255), width=2)
    d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=(0, 255, 0, 255))
    return out


def run_task(
    img: Image.Image,
    task: str,
    on_event: Callable[[dict], None] | None = None,
    model: str | None = None,
    max_rounds: int | None = None,
    mode: str = "full",          # "full" | "raw"
    allow_crop: bool = True,     # only relevant when mode == "raw"
) -> dict:
    """Run the click-targeting loop. Returns final result dict.
    Streams events via on_event(dict) for the UI.
    """
    on_event = on_event or (lambda e: None)
    model = model or config.MODEL
    max_rounds = max_rounds or config.MAX_ROUNDS

    img = img.convert("RGB")
    W, H = img.size

    # Pick system prompt + allowed tools by mode
    if mode == "raw":
        system_prompt = RAW_SYSTEM_PROMPT_WITH_CROP if allow_crop else RAW_SYSTEM_PROMPT_NO_CROP
        allowed_tools = {"crop": TOOLS["crop"]} if allow_crop else {}
    else:
        system_prompt = SYSTEM_PROMPT
        allowed_tools = TOOLS

    on_event({"type": "start", "task": task, "model": model, "image_size": [W, H],
              "mode": mode, "allow_crop": allow_crop})

    # Conversation: keep a running list of messages.
    messages: list[dict] = []
    full_data_url, _ = vlm.encode_image(img)
    messages.append({"role": "user", "content": [
        vlm.text_part(
            f"TASK: {task}\n"
            f"Original screenshot size: {W} x {H} pixels (x: 0..{W-1}, y: 0..{H-1}).\n"
            "Below is the full screenshot. Reason about the target, pick a tool or commit."
        ),
        vlm.image_part(full_data_url),
    ]})

    last_marks: list[dict] = []      # from most recent marks-emitting tool
    last_marks_source: str = ""

    for n in range(1, max_rounds + 1):
        rd = Round(n=n)
        t0 = time.time()

        # 1) Get raw model text. Network/API failures bail.
        try:
            raw = vlm.chat_raw(system_prompt, messages, model=model)
            rd.raw_response = raw
        except Exception as e:
            rd.error = f"VLM API error: {e}"
            rd.elapsed_ms = int((time.time() - t0) * 1000)
            on_event({"type": "round", "round": rd.__dict__})
            return {"ok": False, "error": rd.error, "rounds": n}

        # 2) Parse JSON. On failure, surface to the model and let it retry.
        try:
            parsed = vlm.parse_json_lenient(raw)
            rd.thought = str(parsed.get("thought", ""))[:2000]
            rd.tool = str(parsed.get("tool", ""))
            rd.args = parsed.get("args", {}) or {}
        except Exception as e:
            rd.error = f"JSON parse failed: {e}"
            rd.elapsed_ms = int((time.time() - t0) * 1000)
            on_event({"type": "round", "round": rd.__dict__})
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content":
                f"Your last message was not valid JSON ({e}). "
                "Reply with EXACTLY ONE JSON object: "
                '{"thought":"...","tool":"...","args":{...}} — nothing else.'})
            continue

        on_event({"type": "thinking", "round": n, "thought": rd.thought,
                  "tool": rd.tool, "args": rd.args})

        # --- COMMITS ---
        if rd.tool == "commit":
            x = int(rd.args.get("x", -1)); y = int(rd.args.get("y", -1))
            if 0 <= x < W and 0 <= y < H:
                final_img = _draw_final(img, x, y)
                rd.result_summary = f"COMMIT ({x},{y}): {rd.args.get('reason','')}"
                rd.result_image_b64 = _img_to_b64(final_img)
                rd.elapsed_ms = int((time.time() - t0) * 1000)
                on_event({"type": "round", "round": rd.__dict__})
                on_event({"type": "done", "x": x, "y": y,
                          "reason": rd.args.get("reason", ""),
                          "image_b64": rd.result_image_b64})
                return {"ok": True, "x": x, "y": y, "rounds": n,
                        "reason": rd.args.get("reason", "")}
            else:
                rd.error = f"commit out of bounds: ({x},{y}) not in {W}x{H}"
                # Feed error back; let it retry
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": rd.error})
                rd.elapsed_ms = int((time.time() - t0) * 1000)
                on_event({"type": "round", "round": rd.__dict__})
                continue

        if rd.tool == "commit_mark":
            mid = int(rd.args.get("mark_id", -1))
            chosen = next((m for m in last_marks if m["id"] == mid), None)
            if chosen is None:
                rd.error = f"commit_mark id={mid} not found in last marks ({last_marks_source})."
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": rd.error})
                rd.elapsed_ms = int((time.time() - t0) * 1000)
                on_event({"type": "round", "round": rd.__dict__})
                continue
            x, y = chosen["center"]
            final_img = _draw_final(img, x, y)
            rd.result_summary = (f"COMMIT_MARK #{mid} -> ({x},{y}) "
                                 f"from {last_marks_source}. {rd.args.get('reason','')}")
            rd.result_image_b64 = _img_to_b64(final_img)
            rd.elapsed_ms = int((time.time() - t0) * 1000)
            on_event({"type": "round", "round": rd.__dict__})
            on_event({"type": "done", "x": x, "y": y,
                      "reason": rd.args.get("reason", ""),
                      "image_b64": rd.result_image_b64})
            return {"ok": True, "x": x, "y": y, "rounds": n, "mark": chosen,
                    "reason": rd.args.get("reason", "")}

        # --- TOOLS ---
        fn = allowed_tools.get(rd.tool)
        if fn is None:
            avail = list(allowed_tools) + ["commit"] + (["commit_mark"] if mode != "raw" else [])
            rd.error = f"Unknown/disallowed tool in mode={mode!r}: {rd.tool!r}. Allowed: {avail}"
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": rd.error})
            rd.elapsed_ms = int((time.time() - t0) * 1000)
            on_event({"type": "round", "round": rd.__dict__})
            continue

        try:
            res: ToolResult = fn(img, **rd.args)
        except Exception as e:
            rd.error = f"{rd.tool} failed: {e}"
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": rd.error})
            rd.elapsed_ms = int((time.time() - t0) * 1000)
            on_event({"type": "round", "round": rd.__dict__})
            continue

        if res.marks:
            last_marks = res.marks
            last_marks_source = rd.tool

        rd.result_summary = res.summary
        rd.result_image_b64 = _img_to_b64(res.image)
        rd.elapsed_ms = int((time.time() - t0) * 1000)

        # Build the next user message with the tool result (text + image)
        content: list[dict] = [vlm.text_part(
            f"TOOL RESULT for {rd.tool}({rd.args}):\n{res.summary}\n\n"
            "Coordinates above are in ORIGINAL image space. "
            "Continue: call another tool, or commit / commit_mark."
        )]
        if res.image is not None:
            data_url, _ = vlm.encode_image(res.image)
            content.append(vlm.image_part(data_url))
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": content})

        on_event({"type": "round", "round": rd.__dict__})

    on_event({"type": "done", "error": "max_rounds_exceeded"})
    return {"ok": False, "error": "max_rounds_exceeded", "rounds": max_rounds}
