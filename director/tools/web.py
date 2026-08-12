"""Reading the open web without spending a pixel-operator run on it.

Anything behind a login belongs to the operator (it owns the Chrome profile
with the saved sessions). These two tools cover the much more common case: a
public page, or a question that needs a quick search.
"""
from __future__ import annotations

import html
import json
import re

import aiohttp

from .. import config
from . import ToolContext, ToolResult, tool

MAX_TEXT = 12000
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")

_DROP_BLOCKS = re.compile(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>")
_TAGS = re.compile(r"(?s)<[^>]+>")
_SPACES = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")


def readable_text(markup: str) -> str:
    """Good-enough HTML to text: drop scripts, keep block breaks, unescape."""
    text = _DROP_BLOCKS.sub(" ", markup or "")
    text = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", text)
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANKS.sub("\n\n", text).strip()


def page_title(markup: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", markup or "")
    return html.unescape(_TAGS.sub("", match.group(1))).strip()[:120] if match else ""


@tool(
    "web_fetch",
    "Fetch a public web page or API and return its readable text. Use this "
    "before reaching for the operator: it is far faster than driving a browser. "
    "Pages behind a login need the operator instead.",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_chars": {"type": "integer", "description": "Default 12000."},
        },
        "required": ["url"],
    },
)
async def web_fetch(ctx: ToolContext, url: str = "", max_chars: int = MAX_TEXT) -> ToolResult:
    target = str(url or "").strip()
    if not target:
        return ToolResult(error="no url given")
    if not target.startswith(("http://", "https://")):
        target = f"https://{target}"
    limit = max(500, min(int(max_chars or MAX_TEXT), 60000))
    try:
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": UA}) as session:
            async with session.get(target, allow_redirects=True) as resp:
                status = resp.status
                ctype = str(resp.headers.get("Content-Type") or "")
                raw = await resp.text(errors="replace")
    except (aiohttp.ClientError, UnicodeDecodeError) as exc:
        return ToolResult(error=f"fetch failed: {exc}")
    except TimeoutError:
        return ToolResult(error="fetch timed out after 45s")

    if "json" in ctype.lower():
        try:
            body = json.dumps(json.loads(raw), indent=2)[:limit]
        except json.JSONDecodeError:
            body = raw[:limit]
        title = target
    else:
        title = page_title(raw) or target
        body = readable_text(raw)[:limit]

    return ToolResult(
        output=f"{title}\n{target}\nHTTP {status}\n\n{body}",
        card={"title": "web", "preview": title or target, "meta": f"HTTP {status}",
              "tone": "ok" if status < 400 else "danger", "url": target},
    )


@tool(
    "web_search",
    "Search the web and get back result titles, links and snippets. Needs an "
    "OpenRouter key; without one, use the operator to search in the browser.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "description": "Default 5, max 20."},
        },
        "required": ["query"],
    },
)
async def web_search(ctx: ToolContext, query: str = "", max_results: int = 5) -> ToolResult:
    text = str(query or "").strip()
    if not text:
        return ToolResult(error="no query given")
    key = config.openrouter_key(ctx.settings)
    if not key:
        return ToolResult(error="no OpenRouter key configured — use the operator to "
                                "search in the browser instead")
    count = max(1, min(int(max_results or 5), 20))
    # OpenRouter exposes search through the `web` plugin. There is no top-level
    # web_search request field.
    payload = {
        "model": "openai/gpt-4o-mini",
        "messages": [{"role": "user", "content": f"Search the web for: {text}\n\n"
                                                 "List the most relevant results with titles, "
                                                 "links and one-line summaries."}],
        "plugins": [{"id": "web", "max_results": count}],
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/callewallerstedt/aios",
        "X-Title": "aiOS Director",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=90)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post("https://openrouter.ai/api/v1/chat/completions",
                                    json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    detail = str((data or {}).get("error", {}).get("message") or data)[:300]
                    return ToolResult(error=f"search failed (HTTP {resp.status}): {detail}")
    except (aiohttp.ClientError, TimeoutError) as exc:
        return ToolResult(error=f"search failed: {exc}")

    choices = (data or {}).get("choices") or []
    body = str(((choices[0] if choices else {}).get("message") or {}).get("content") or "").strip()
    return ToolResult(
        output=body or "(no results)",
        card={"title": "search", "preview": text[:80], "meta": f"{count} results",
              "tone": "ok", "body": body},
    )
