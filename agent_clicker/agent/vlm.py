import base64
import io
import json
import re
from typing import Any

from PIL import Image

from . import config

_client: Any = None


def client():
    global _client
    if _client is None:
        if not config.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY missing. Copy .env.example to .env and set it.")
        from openai import OpenAI
        _client = OpenAI(api_key=config.OPENAI_API_KEY)
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


def chat_raw(system: str, messages: list[dict], model: str | None = None,
             backend: str = "api", reasoning_effort: str | None = None) -> str:
    """Send a chat with vision messages; return raw assistant text.

    backend = 'api'  -> standard api.openai.com (billed to OPENAI_API_KEY)
    backend = 'codex' -> chatgpt.com/backend-api/codex/responses, auth from
                         ~/.codex/auth.json (billed to ChatGPT subscription).
                         Undocumented; can break with Codex updates.

    reasoning_effort: 'minimal' | 'low' | 'medium' | 'high' for gpt-5.x.
    None = let the API default (medium on gpt-5.x).
    """
    model = model or config.MODEL
    if backend == "codex":
        from . import codex_backend
        return codex_backend.chat_raw(
            system, messages, model=model, reasoning_effort=reasoning_effort
        )

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
    return resp.choices[0].message.content or ""


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
