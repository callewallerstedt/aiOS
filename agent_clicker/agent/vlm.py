import base64
import io
import json
import os
import re
import threading
from typing import Any

from PIL import Image

from . import config

_client: Any = None
_usage_local = threading.local()


def client():
    global _client
    if _client is None:
        if not config.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY missing. Copy .env.example to .env and set it.")
        from openai import OpenAI
        # The SDK defaults to a 10-minute timeout with retries on top, so one
        # stalled request looks exactly like a frozen agent. Fail fast enough
        # that the loop can report it and move on.
        _client = OpenAI(
            api_key=config.OPENAI_API_KEY,
            timeout=float(os.environ.get("AIOS_MODEL_TIMEOUT", "150")),
            max_retries=2,
        )
    return _client


def encode_image(img: Image.Image, max_dim: int = config.VLM_MAX_DIM) -> tuple[str, float]:
    """PNG-encode (downscaled) and return (data_url, scale_used).
    scale_used = sent_size / original_size, so VLM-reported coords can be unscaled if needed.
    But we only ever expose ORIGINAL coords in tool outputs, so scale is informational.
    """
    w, h = img.size
    scale = 1.0
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}", scale


def parse_json_lenient(text: str) -> dict[str, Any]:
    """Parse the FIRST valid JSON object in text. Tolerates code fences,
    leading/trailing prose, and trailing extra objects/text after the first {}.
    """
    if not text:
        raise ValueError("empty model output")
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    start = s.find("{")
    if start == -1:
        raise ValueError(f"no '{{' in model output: {text[:200]!r}")
    # raw_decode parses one JSON value and ignores trailing data
    try:
        obj, _ = json.JSONDecoder().raw_decode(s[start:])
        if not isinstance(obj, dict):
            raise ValueError(f"top-level JSON is not an object: {type(obj).__name__}")
        return obj
    except json.JSONDecodeError as e:
        # last-ditch: try to find a balanced {...} substring
        depth = 0
        for i, ch in enumerate(s[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except Exception:
                        break
        raise ValueError(f"could not parse JSON: {e.msg} at char {e.pos}. "
                         f"head={text[:300]!r}") from e


def _usage_dict(usage, *, backend: str, model: str) -> dict:
    if usage is None:
        return {"requests": 1, "backend": backend, "model": model}
    details = getattr(usage, "prompt_tokens_details", None) or getattr(usage, "input_tokens_details", None)
    input_tokens = int(getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", 0) or 0)
    def detail_int(*names):
        for name in names:
            value = details.get(name) if isinstance(details, dict) else getattr(details, name, None)
            if value is not None:
                return int(value or 0)
        return 0

    cached_tokens = detail_int("cached_tokens")
    cache_write_tokens = detail_int("cache_write_tokens", "cache_creation_tokens")
    total_tokens = int(getattr(usage, "total_tokens", 0) or input_tokens + output_tokens)
    result = {
        "requests": 1, "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cached_input_tokens": cached_tokens, "cache_write_input_tokens": cache_write_tokens,
        "total_tokens": total_tokens,
        "backend": backend, "model": model,
    }
    if input_tokens > 272_000:
        result.update({
            "long_context_requests": 1,
            "long_context_input_tokens": input_tokens,
            "long_context_output_tokens": output_tokens,
            "long_context_cached_input_tokens": cached_tokens,
            "long_context_cache_write_input_tokens": cache_write_tokens,
        })
    return result


def chat_with_usage(system: str, messages: list[dict], model: str | None = None,
                    backend: str = "api", reasoning_effort: str | None = None) -> tuple[str, dict]:
    """Send a chat with vision messages; return raw assistant text.

    backend = 'api'  -> standard api.openai.com (billed to OPENAI_API_KEY)
    backend = 'codex' -> chatgpt.com/backend-api/codex/responses, auth from
                         ~/.codex/auth.json (billed to ChatGPT subscription).
                         Undocumented; can break with Codex updates.
    backend = 'codex_fallback' -> try Codex first, then use OPENAI_API_KEY.

    reasoning_effort: 'minimal' | 'low' | 'medium' | 'high' for gpt-5.x.
    None = let the API default (medium on gpt-5.x).
    """
    model = model or config.MODEL
    if backend in {"codex", "codex_fallback"}:
        from . import codex_backend
        try:
            return codex_backend.chat_with_usage(
                system, messages, model=model, reasoning_effort=reasoning_effort
            )
        except Exception as codex_error:
            if backend == "codex" or not config.OPENAI_API_KEY:
                raise
            fallback_reason = str(codex_error)[:240]
    else:
        fallback_reason = ""

    full = [{"role": "system", "content": system}] + messages
    kwargs: dict = {
        "model": model,
        "messages": full,
        "response_format": {"type": "json_object"},
    }
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    try:
        resp = client().chat.completions.create(**kwargs)
    except TypeError:
        # SDK too old for reasoning_effort kwarg — retry without it
        kwargs.pop("reasoning_effort", None)
        resp = client().chat.completions.create(**kwargs)
    usage = _usage_dict(resp.usage, backend="api", model=model)
    if fallback_reason:
        usage["fallback_from"] = "codex"
        usage["fallback_reason"] = fallback_reason
    return resp.choices[0].message.content or "", usage


def chat_raw(system: str, messages: list[dict], model: str | None = None,
             backend: str = "api", reasoning_effort: str | None = None) -> str:
    text, usage = chat_with_usage(
        system, messages, model=model, backend=backend,
        reasoning_effort=reasoning_effort,
    )
    _usage_local.last = usage
    return text


def take_last_usage() -> dict:
    """Return and clear usage from the current thread's last chat_raw call."""
    usage = getattr(_usage_local, "last", {})
    _usage_local.last = {}
    return dict(usage) if isinstance(usage, dict) else {}


def chat_json(system: str, messages: list[dict], model: str | None = None,
              backend: str = "api", reasoning_effort: str | None = None) -> tuple[dict, str]:
    """Returns (parsed_json, raw_text). Raises on unrecoverable parse errors."""
    raw = chat_raw(system, messages, model, backend=backend,
                   reasoning_effort=reasoning_effort)
    return parse_json_lenient(raw), raw


def image_part(data_url: str, detail: str = "high") -> dict:
    return {"type": "image_url", "image_url": {"url": data_url, "detail": detail}}


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}
