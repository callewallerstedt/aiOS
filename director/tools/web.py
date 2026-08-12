"""Reading the open web without spending a pixel-operator run on it.

Anything behind a login belongs to the operator (it owns the Chrome profile
with the saved sessions). These two tools cover the much more common case: a
public page, or a question that needs a quick search.
"""
from __future__ import annotations

import html
import json
import re
from urllib.parse import parse_qs, unquote, urlparse

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


def parse_search_results(markup: str, limit: int = 5) -> list[dict[str, str]]:
    """Pull title+url rows out of DuckDuckGo lite HTML."""
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for href, label in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', markup or "", re.S):
        target = href
        if target.startswith("//"):
            target = "https:" + target
        if "uddg=" in target:
            wrapped = parse_qs(urlparse(target).query).get("uddg") or []
            if wrapped:
                target = unquote(wrapped[0])
        if not target.startswith("http") or target in seen:
            continue
        if "duckduckgo.com" in urlparse(target).netloc:
            continue
        seen.add(target)
        title = readable_text(label)[:200] or target
        results.append({"title": title, "url": target})
        if len(results) >= limit:
            break
    return results


def format_search_results(query: str, results: list[dict[str, str]]) -> str:
    if not results:
        return f"No results for {query!r}."
    lines = [f"Results for {query!r}:"]
    for index, row in enumerate(results, 1):
        lines.append(f"{index}. {row['title']}\n   {row['url']}")
    return "\n".join(lines)


async def duckduckgo_search(query: str, count: int) -> list[dict[str, str]]:
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {"User-Agent": UA, "Accept": "text/html"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": query},
            allow_redirects=True,
        ) as resp:
            if resp.status >= 400:
                return []
            raw = await resp.text(errors="replace")
    return parse_search_results(raw, count)


async def openrouter_search(query: str, count: int, key: str) -> str:
    payload = {
        "model": "openai/gpt-4o-mini",
        "messages": [{"role": "user", "content": (
            f"Search the web for: {query}\n\n"
            "List the most relevant results with titles, links and one-line summaries."
        )}],
        "tools": [{"type": "openrouter:web_search",
                   "parameters": {"max_results": count}}],
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/callewallerstedt/aios",
        "X-Title": "aiOS Director",
    }
    timeout = aiohttp.ClientTimeout(total=90)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post("https://openrouter.ai/api/v1/chat/completions",
                                json=payload, headers=headers) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                detail = str((data or {}).get("error", {}).get("message") or data)[:300]
                raise RuntimeError(f"search failed (HTTP {resp.status}): {detail}")
    choices = (data or {}).get("choices") or []
    return str(((choices[0] if choices else {}).get("message") or {}).get("content") or "").strip()


@tool(
    "web_search",
    "Search the public web and get back result titles and links. Then use "
    "web_fetch to read a page. Do not drive the operator browser just to search.",
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
    count = max(1, min(int(max_results or 5), 20))
    results: list[dict[str, str]] = []
    try:
        results = await duckduckgo_search(text, count)
    except (aiohttp.ClientError, TimeoutError, UnicodeDecodeError):
        results = []
    if results:
        body = format_search_results(text, results)
        return ToolResult(
            output=body,
            card={"title": "search", "preview": text[:80], "meta": f"{len(results)} results",
                  "tone": "ok", "body": body},
        )
    key = config.openrouter_key(ctx.settings)
    if key:
        try:
            body = await openrouter_search(text, count, key)
        except (aiohttp.ClientError, TimeoutError, RuntimeError) as exc:
            return ToolResult(error=f"search failed: {exc}")
        if body:
            return ToolResult(
                output=body,
                card={"title": "search", "preview": text[:80], "meta": f"{count} results",
                      "tone": "ok", "body": body},
            )
    return ToolResult(error="search returned nothing — try web_fetch with a specific URL")
