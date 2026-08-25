"""OpenRouter click-coordinate model comparison lab."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from PIL import Image

ROOT = Path(__file__).resolve().parent
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_MODELS = [
    "xiaomi/mimo-v2.5",
    "openai/gpt-5.6-luna",
    "minimax/minimax-m3",
    "stepfun/step-3.7-flash",
    "moonshotai/kimi-k3",
    "anthropic/claude-sonnet-5",
    "google/gemini-3.6-flash",
    "openai/gpt-5.6-terra",
    "google/gemini-2.5-flash",
    "x-ai/grok-4.5",
    "anthropic/claude-haiku-4.5",
    "google/gemini-3.5-flash",
]

SYSTEM_PROMPT = """You are a precise UI click locator.

Given a screenshot and a target description, find the single best pixel to click.

Rules:
- Coordinates are in original image pixels.
- Origin (0,0) is the top-left corner.
- X increases right, Y increases down.
- Click the visual center of the target, not a corner.
- Reply with JSON only. No markdown fences, no prose.

Required JSON shape:
{"x": <integer>, "y": <integer>, "label": "<short name of what you clicked>", "confidence": <0-1 number>}
"""

app = Flask(__name__, template_folder="templates", static_folder="static")


def _api_key_from_request() -> str:
    key = (
        request.headers.get("X-OpenRouter-Key")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        or os.environ.get("OPENROUTER_API_KEY", "")
    )
    return (key or "").strip()


def _openrouter_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:8765",
        "X-Title": "aiOS Click Model Lab",
    }


def _is_vision_model(model: dict) -> bool:
    arch = model.get("architecture") or {}
    mods = arch.get("input_modalities") or arch.get("modality") or []
    if isinstance(mods, str):
        mods = mods.replace("+", ",").split(",")
    mods = [str(m).strip().lower() for m in mods]
    modality = str(arch.get("modality") or "").lower()
    return "image" in mods or "image" in modality


def _model_summary(model: dict, *, rank: int = 0) -> dict:
    pricing = model.get("pricing") or {}
    arch = model.get("architecture") or {}
    modalities = arch.get("input_modalities") or []
    if isinstance(modalities, str):
        modalities = [m.strip() for m in modalities.replace("+", ",").split(",") if m.strip()]
    return {
        "id": model.get("id"),
        "name": model.get("name") or model.get("id"),
        "context_length": model.get("context_length"),
        "pricing": {
            "prompt": float(pricing.get("prompt") or 0),
            "completion": float(pricing.get("completion") or 0),
        },
        "input_modalities": modalities,
        "vision": _is_vision_model(model),
        "created": model.get("created"),
        "rank": rank,
    }


def extract_json_object(text: str) -> dict:
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start == -1:
        raise ValueError(f"no JSON object in model output: {text[:300]!r}")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise ValueError("top-level JSON is not an object")
    return obj


def coerce_point(value) -> tuple[float, float]:
    if isinstance(value, dict):
        if "x" in value and "y" in value:
            return float(value["x"]), float(value["y"])
        for key in ("point", "coordinate", "coordinates", "position", "click", "center"):
            if key in value:
                return coerce_point(value[key])
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return float(value[0]), float(value[1])
    raise ValueError(f"cannot read point from {value!r}")


def normalize_prediction(data: dict, width: int, height: int) -> dict:
    source = data
    actions = data.get("actions")
    if isinstance(actions, list):
        for action in actions:
            if isinstance(action, dict) and str(action.get("type", "")).lower() in {
                "click",
                "left_click",
                "right_click",
                "double_click",
                "point",
            }:
                source = action
                break

    if "bbox" in source and isinstance(source["bbox"], (list, tuple)) and len(source["bbox"]) >= 4:
        x = (float(source["bbox"][0]) + float(source["bbox"][2])) / 2
        y = (float(source["bbox"][1]) + float(source["bbox"][3])) / 2
    else:
        try:
            x, y = coerce_point(source)
        except ValueError:
            for key in ("point", "coordinate", "coordinates", "position", "click"):
                if key in source:
                    x, y = coerce_point(source[key])
                    break
            else:
                raise ValueError(f"no coordinate in prediction: {data}") from None

    # Normalize common alternate scales.
    if 0 <= x <= 1 and 0 <= y <= 1 and (width > 2 or height > 2):
        x, y = x * (width - 1), y * (height - 1)
    elif 0 <= x <= 1000 and 0 <= y <= 1000 and (x > 1 or y > 1) and max(width, height) > 1000:
        # Some models use a 0-1000 grid; only apply when image is larger than 1000.
        if x <= 1000 and y <= 1000 and (x > width or y > height):
            x = x / 1000 * (width - 1)
            y = y / 1000 * (height - 1)

    return {
        "x": max(0, min(width - 1, int(round(float(x))))),
        "y": max(0, min(height - 1, int(round(float(y))))),
        "label": str(source.get("label") or data.get("label") or data.get("thought") or "")[:120],
        "confidence": _safe_float(source.get("confidence", data.get("confidence"))),
    }


def _safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def build_user_prompt(target: str, width: int, height: int) -> str:
    return (
        f"Image size: {width}x{height} pixels.\n"
        f"Find the click target: {target}\n"
        "Return only the JSON object with integer pixel coordinates."
    )


def prepare_image(
    image_data_url: str,
    *,
    orig_width: int,
    orig_height: int,
    fast: bool,
) -> tuple[str, int, int, float, float]:
    """Return (data_url, sent_w, sent_h, scale_x, scale_y) to map sent→original coords."""
    if not fast:
        return image_data_url, orig_width, orig_height, 1.0, 1.0

    try:
        header, b64 = image_data_url.split(",", 1)
        raw = base64.b64decode(b64)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return image_data_url, orig_width, orig_height, 1.0, 1.0

    max_dim = 1280
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        w, h = img.size

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82, optimize=True)
    data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    scale_x = orig_width / w if w else 1.0
    scale_y = orig_height / h if h else 1.0
    return data_url, w, h, scale_x, scale_y


def _is_openai_gpt5_family(model: str) -> bool:
    mid = (model or "").lower()
    return "gpt-5" in mid


def _message_text(message: dict) -> str:
    """Pull visible text from chat message, including odd GPT-5/OR shapes."""
    if not isinstance(message, dict):
        return ""

    chunks: list[str] = []

    content = message.get("content")
    if isinstance(content, str) and content.strip():
        chunks.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, str) and part.strip():
                chunks.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content") or ""
                if text:
                    chunks.append(str(text))

    # Some providers put the answer in refusal / reasoning fields when content is empty.
    for key in ("refusal", "reasoning", "reasoning_content"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            chunks.append(value)

    details = message.get("reasoning_details")
    if isinstance(details, list):
        for item in details:
            if isinstance(item, dict):
                text = item.get("text") or item.get("summary") or item.get("content") or ""
                if text:
                    chunks.append(str(text))
            elif isinstance(item, str) and item.strip():
                chunks.append(item)

    return "\n".join(chunks).strip()


def call_model(
    *,
    api_key: str,
    model: str,
    image_data_url: str,
    target: str,
    width: int,
    height: int,
    fast: bool = True,
    timeout: float = 120.0,
    sent_url: str | None = None,
    sent_w: int | None = None,
    sent_h: int | None = None,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    client: httpx.Client | None = None,
) -> dict:
    # Image prep happens once per run when possible — don't bake it into model latency.
    if sent_url is None or sent_w is None or sent_h is None:
        sent_url, sent_w, sent_h, scale_x, scale_y = prepare_image(
            image_data_url,
            orig_width=width,
            orig_height=height,
            fast=fast,
        )
    detail = "low" if fast else "high"
    gpt5 = _is_openai_gpt5_family(model)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_user_prompt(target, sent_w, sent_h)},
                    {
                        "type": "image_url",
                        "image_url": {"url": sent_url, "detail": detail},
                    },
                ],
            },
        ],
    }
    # GPT-5 often rejects/ignores temperature=0; leave default for that family.
    if not gpt5:
        payload["temperature"] = 0

    if fast and gpt5:
        # Actual speed control for Luna/Sol/Terra — don't force provider routing;
        # latency-sort was sending vision to slower endpoints.
        payload["reasoning"] = {"effort": "minimal"}

    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=timeout)

    try:
        started = time.perf_counter()
        resp = client.post(
            f"{OPENROUTER_BASE}/chat/completions",
            headers=_openrouter_headers(api_key),
            json=payload,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        body_text = resp.text
        try:
            body = resp.json()
        except Exception:
            body = {"raw": body_text}

        if resp.status_code >= 400:
            err = body.get("error") if isinstance(body, dict) else None
            message = err.get("message") if isinstance(err, dict) else body_text[:400]
            return {
                "model": model,
                "ok": False,
                "latency_ms": latency_ms,
                "error": message or f"HTTP {resp.status_code}",
                "status_code": resp.status_code,
                "raw": body_text[:2000],
            }

        content = ""
        finish_reason = ""
        choices = body.get("choices") or []
        if choices:
            choice = choices[0] or {}
            finish_reason = str(choice.get("finish_reason") or "")
            content = _message_text(choice.get("message") or {})

        usage = body.get("usage") or {}
        usage_payload = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "cost": usage.get("cost") if usage.get("cost") is not None else usage.get("total_cost"),
        }
        try:
            if not content.strip():
                reason = finish_reason or "unknown"
                raise ValueError(f"empty model output (finish_reason={reason})")
            parsed = extract_json_object(content)
            point = normalize_prediction(parsed, sent_w, sent_h)
            # Map back to original screenshot pixels when we downscaled for speed.
            x = int(round(point["x"] * scale_x))
            y = int(round(point["y"] * scale_y))
            x = max(0, min(width - 1, x))
            y = max(0, min(height - 1, y))
            return {
                "model": model,
                "ok": True,
                "latency_ms": latency_ms,
                "x": x,
                "y": y,
                "label": point.get("label") or "",
                "confidence": point.get("confidence"),
                "raw": content,
                "usage": usage_payload,
                "provider": (body.get("provider") or ""),
                "fast": fast,
            }
        except Exception as parse_err:
            return {
                "model": model,
                "ok": False,
                "latency_ms": latency_ms,
                "error": f"parse error: {parse_err}",
                "raw": content or body_text[:2000],
                "usage": usage_payload,
                "finish_reason": finish_reason,
            }
    except Exception as exc:
        return {
            "model": model,
            "ok": False,
            "latency_ms": 0,
            "error": str(exc),
        }
    finally:
        if owns_client and client is not None:
            client.close()


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "defaults": DEFAULT_MODELS})


@app.get("/api/models")
def list_models():
    api_key = _api_key_from_request()
    headers = {"Accept": "application/json"}
    if api_key:
        headers.update(_openrouter_headers(api_key))
    try:
        with httpx.Client(timeout=45.0) as client:
            resp = client.get(f"{OPENROUTER_BASE}/models?sort=top-weekly", headers=headers)
        resp.raise_for_status()
        payload = resp.json()
        # Keep OpenRouter top-weekly order as popularity rank.
        models = [
            _model_summary(m, rank=i)
            for i, m in enumerate(payload.get("data", []))
            if m.get("id")
        ]
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "models": [], "defaults": DEFAULT_MODELS}), 502

    ids = {m["id"] for m in models}
    return jsonify(
        {
            "ok": True,
            "count": len(models),
            "vision_count": sum(1 for m in models if m.get("vision")),
            "defaults": [m for m in DEFAULT_MODELS if m in ids],
            "models": models,
        }
    )


@app.post("/api/run")
def run_models():
    data = request.get_json(force=True, silent=True) or {}
    api_key = (data.get("api_key") or _api_key_from_request() or "").strip()
    if not api_key:
        return jsonify({"ok": False, "error": "OpenRouter API key required"}), 400

    image = data.get("image") or ""
    target = (data.get("target") or "").strip()
    models = data.get("models") or []
    width = int(data.get("width") or 0)
    height = int(data.get("height") or 0)
    fast = bool(data.get("fast", True))

    if not image.startswith("data:image"):
        return jsonify({"ok": False, "error": "image must be a data:image URL"}), 400
    if not target:
        return jsonify({"ok": False, "error": "target description required"}), 400
    if not isinstance(models, list) or not models:
        return jsonify({"ok": False, "error": "select at least one model"}), 400
    if width <= 0 or height <= 0:
        return jsonify({"ok": False, "error": "image width/height required"}), 400

    models = [str(m).strip() for m in models if str(m).strip()]
    models = list(dict.fromkeys(models))  # stable unique
    if len(models) > 24:
        return jsonify({"ok": False, "error": "max 24 models per run"}), 400

    def event_stream():
        total = len(models)
        sent_url, sent_w, sent_h, scale_x, scale_y = prepare_image(
            image,
            orig_width=width,
            orig_height=height,
            fast=fast,
        )
        yield _sse(
            {
                "type": "start",
                "total": total,
                "models": models,
                "sent_size": [sent_w, sent_h],
                "fast": fast,
            }
        )

        done = 0
        with httpx.Client(timeout=120.0) as client:
            with ThreadPoolExecutor(max_workers=min(12, total)) as pool:
                futures = {
                    pool.submit(
                        call_model,
                        api_key=api_key,
                        model=model_id,
                        image_data_url=image,
                        target=target,
                        width=width,
                        height=height,
                        fast=fast,
                        sent_url=sent_url,
                        sent_w=sent_w,
                        sent_h=sent_h,
                        scale_x=scale_x,
                        scale_y=scale_y,
                        client=client,
                    ): model_id
                    for model_id in models
                }
                for fut in as_completed(futures):
                    model_id = futures[fut]
                    try:
                        result = fut.result()
                    except Exception as exc:
                        result = {
                            "model": model_id,
                            "ok": False,
                            "latency_ms": 0,
                            "error": str(exc),
                        }
                    done += 1
                    yield _sse({"type": "result", "index": done, "total": total, "result": result})

        yield _sse({"type": "done", "total": total})

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def main():
    port = int(os.environ.get("CLICK_LAB_PORT", "8765"))
    print(f"Click Model Lab -> http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
