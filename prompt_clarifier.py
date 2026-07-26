"""Fast, low-cost intent clarification for aiOS Operator drafts."""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


MODEL = "gpt-5.4-nano"
CODEX_MODEL = MODEL
MAX_QUESTIONS = 10
CLARIFY_TIMEOUT = 8.0
MAX_OUTPUT_TOKENS = 320
SYSTEM_PROMPT = """You are aiOS Operator's fast intent checker. The user is drafting a task for a computer-use agent.

Return JSON only:
{"questions":[{"id":"short_id","question":"One short question?","answered":false}]}

Rules:
- Ask only when ambiguity would materially change what the agent does.
- Never ask for info already in the draft.
- question_limit is a ceiling, not a target; 0 is valid for a clear draft.
- Short single actions: 0-2 questions. Medium drafts: 2-5. Use 6-10 only for large multi-part drafts with that many real ambiguities.
- On refresh add at most 2 new unanswered questions; preserve previous ids/wording.
- Mark answered=true only when the draft actually answers it.
- Each question under 90 characters, same language as the draft.
- Do not ask for confirmation of details already written (paths, app names, destinations).
- Ask about approval only for external/destructive/costly actions when genuinely unclear.
"""


def question_limit(draft: str, previous: list[dict] | None = None) -> int:
    """Grow the question ceiling with draft complexity and prior conversation."""
    text = str(draft or "").strip()
    previous_count = min(len(previous or []), MAX_QUESTIONS)
    segments = len([part for part in re.split(r"[\n.!?;]+|(?:^|\s)[-*]\s+", text) if part.strip()])
    if len(text) < 120 and segments <= 1:
        complexity_limit = 2
    elif len(text) < 400 and segments <= 3:
        complexity_limit = 4
    elif len(text) < 1000 and segments <= 6:
        complexity_limit = 6
    else:
        complexity_limit = MAX_QUESTIONS
    return min(MAX_QUESTIONS, max(complexity_limit, previous_count + 2))


def _question_id(value: object, question: str, index: int) -> str:
    candidate = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    if candidate:
        return candidate[:40]
    words = re.sub(r"[^a-z0-9 ]+", "", question.lower()).split()[:5]
    return "_".join(words)[:40] or f"question_{index + 1}"


def normalize_questions(value: object) -> list[dict]:
    rows = value.get("questions") if isinstance(value, dict) else value
    if not isinstance(rows, list):
        return []
    output = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        question = " ".join(str(row.get("question") or "").strip().split())[:180]
        if not question:
            continue
        qid = _question_id(row.get("id"), question, len(output))
        if qid in seen:
            continue
        seen.add(qid)
        output.append({"id": qid, "question": question, "answered": bool(row.get("answered"))})
        if len(output) >= MAX_QUESTIONS:
            break
    return output


def _content_text(message: dict) -> str:
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    return ""


def _request_payload(draft: str, previous: list[dict] | None = None) -> tuple[dict, int]:
    normalized_previous = normalize_questions(previous or [])
    limit = question_limit(draft, normalized_previous)
    return {
        "draft": str(draft or "")[:8000],
        "previous_questions": normalized_previous,
        "question_limit": limit,
    }, limit


def _parse_result(raw: str, limit: int) -> list[dict]:
    text = str(raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start < 0:
        raise RuntimeError("The intent check returned invalid JSON.")
    try:
        parsed, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise RuntimeError("The intent check returned invalid JSON.") from exc
    return normalize_questions(parsed)[:limit]


def clarify_prompt(api_key: str, draft: str, previous: list[dict] | None = None, timeout: float = CLARIFY_TIMEOUT) -> dict:
    started = time.perf_counter()
    user_payload, limit = _request_payload(draft, previous)
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(detail).get("error", {}).get("message", detail)
        except json.JSONDecodeError:
            pass
        raise RuntimeError(str(detail or f"OpenAI returned HTTP {exc.code}")) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI is unreachable: {exc.reason}") from exc

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("The intent check returned no result.")
    raw = _content_text((choices[0] or {}).get("message") or {})
    return {
        "questions": _parse_result(raw, limit),
        "question_limit": limit,
        "model": MODEL,
        "provider": "api",
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


def clarify_prompt_codex(
    draft: str,
    previous: list[dict] | None = None,
    timeout: float = CLARIFY_TIMEOUT,
) -> dict:
    """Run the live intent check through the signed-in Codex account."""
    started = time.perf_counter()
    user_payload, limit = _request_payload(draft, previous)
    agent_root = Path(__file__).resolve().parent / "agent_clicker"
    if str(agent_root) not in sys.path:
        sys.path.insert(0, str(agent_root))
    from agent import codex_backend

    raw = codex_backend.chat_raw(
        SYSTEM_PROMPT,
        [{"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}],
        model=CODEX_MODEL,
        timeout=timeout,
        reasoning_effort="minimal",
    )
    return {
        "questions": _parse_result(raw, limit),
        "question_limit": limit,
        "model": CODEX_MODEL,
        "provider": "codex",
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


def clarify_prompt_for_provider(
    draft: str,
    previous: list[dict] | None = None,
    *,
    provider_mode: str = "api",
    api_key: str = "",
    timeout: float = CLARIFY_TIMEOUT,
) -> dict:
    mode = str(provider_mode or "api").strip().lower()
    if mode not in {"codex", "api", "codex_api_fallback"}:
        mode = "api"
    if mode == "api":
        if not api_key:
            raise RuntimeError("OpenAI API key is not configured.")
        return clarify_prompt(api_key, draft, previous, timeout=timeout)
    if mode == "codex":
        return clarify_prompt_codex(draft, previous, timeout=timeout)
    try:
        return clarify_prompt_codex(draft, previous, timeout=timeout)
    except Exception as codex_error:
        if not api_key:
            raise RuntimeError(f"Codex is unavailable and no API fallback is configured: {codex_error}") from codex_error
        result = clarify_prompt(api_key, draft, previous, timeout=timeout)
        result["fallback_from"] = "codex"
        return result
