"""Fast, low-cost intent clarification for aiOS Operator drafts."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request


MODEL = "gpt-5.4-nano"
MAX_QUESTIONS = 3
SYSTEM_PROMPT = """You are the tiny intent-check assistant inside aiOS Operator.
Read the user's live draft before it is sent to a computer-use agent.

Return JSON only with this shape:
{"questions":[{"id":"short_stable_id","question":"One short question?","answered":false}]}

Rules:
- Ask only about ambiguity that could materially change what the Operator does.
- Never ask for information already present in the draft.
- Return at most 3 questions, ordered by importance. Zero is valid for a clear draft.
- Keep each question under 90 characters and easy to answer in the main draft.
- Use the same language as the draft.
- Mark answered=true only when the current draft actually answers the question.
- Evaluate every previous question first. When the draft answers it, return the exact same id and wording with answered=true. Never turn it into a confirmation question.
- Preserve a previous question's exact id and wording while it remains relevant, including after it is answered.
- Retire questions that are obsolete. Add a new one only when the evolving draft introduces a material ambiguity.
- A named app, file path, destination, visual direction, constraint, or success result counts as an answer. Do not ask the user to confirm information they already wrote.
- Useful categories include target, scope, success result, destination, and constraints.
- Ask about approval only for an external, destructive, costly, or publishing action whose authorization is genuinely unclear. Never ask who approves ordinary local edits or saved copies.
- Do not ask generic preference questions or request confirmation merely for confidence.

Example: if a previous question asks which file and the new draft names C:\\work\\logo.psd,
return that exact previous question with answered=true. Do not ask whether that path is correct.
"""


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


def clarify_prompt(api_key: str, draft: str, previous: list[dict] | None = None, timeout: float = 25) -> dict:
    started = time.perf_counter()
    user_payload = {
        "draft": str(draft or "")[:8000],
        "previous_questions": normalize_questions(previous or []),
    }
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "reasoning_effort": "low",
        "max_completion_tokens": 500,
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
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("The intent check returned invalid JSON.") from exc
    return {
        "questions": normalize_questions(parsed),
        "model": MODEL,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }
