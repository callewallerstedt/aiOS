import base64
from datetime import datetime
import json
import os
from pathlib import Path
import time


CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
KNOWN_CODEX_EMAILS = (
    "calle.wallerstedt@gmail.com",
    "contact.wallerstedt@gmail.com",
)
USAGE_CACHE_FILE = "aios-codex-usage-cache.json"


def _decode_id_token(token):
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", "replace"))
    except Exception:
        return {}


def codex_auth_details(home=CODEX_HOME):
    auth_path = Path(home) / "auth.json"
    if not auth_path.exists():
        return {"ok": False, "label": "not signed in"}
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "label": "auth unreadable"}
    tokens = data.get("tokens") or {}
    claims = _decode_id_token(tokens.get("id_token") or data.get("id_token"))
    auth_claims = claims.get("https://api.openai.com/auth") or {}
    email = (
        tokens.get("account_email")
        or tokens.get("email")
        or data.get("account_email")
        or data.get("email")
        or claims.get("email")
        or claims.get("name")
    )
    plan = (
        tokens.get("plan_type")
        or data.get("plan_type")
        or auth_claims.get("chatgpt_plan_type")
        or ""
    )
    account_id = tokens.get("account_id") or auth_claims.get("chatgpt_account_id") or ""
    label = email or "signed in"
    if plan:
        label = f"{label} · {plan}"
    return {
        "ok": True,
        "label": label,
        "email": email or "",
        "short": short_email(email),
        "plan": plan,
        "account_id": account_id,
    }


def codex_auth_info(home=CODEX_HOME):
    details = codex_auth_details(home)
    return bool(details.get("ok")), details.get("label", "not signed in")


def tail_lines(path, max_bytes=524288):
    try:
        size = path.stat().st_size
        with path.open("rb") as file:
            file.seek(max(0, size - max_bytes))
            data = file.read().decode("utf-8", errors="replace")
        return data.splitlines()
    except OSError:
        return []


def latest_codex_rate_limit_event(home=CODEX_HOME):
    sessions = Path(home) / "sessions"
    if not sessions.exists():
        return None
    try:
        files = sorted(
            sessions.rglob("*.jsonl"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for path in files[:40]:
        for line in reversed(tail_lines(path)):
            if '"rate_limits"' not in line or '"token_count"' not in line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = item.get("payload", {})
            if payload.get("type") != "token_count":
                continue
            limits = payload.get("rate_limits")
            if limits:
                return {
                    "limits": limits,
                    "timestamp": item.get("timestamp"),
                    "path": str(path),
                }
    return None


def latest_codex_rate_limits(home=CODEX_HOME):
    event = latest_codex_rate_limit_event(home)
    return event.get("limits") if event else None


def short_email(email):
    if not email:
        return "--"
    local = str(email).split("@", 1)[0]
    return (local.split(".", 1)[0] or local)[:12]


def format_duration(seconds):
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _limit_payload(limit):
    if not limit:
        return None
    used = float(limit.get("used_percent", 0) or 0)
    reset = limit.get("resets_at")
    return {
        "used": round(used),
        "remaining": round(max(0, 100 - used)),
        "reset": float(reset) if reset else None,
        "reset_in": format_duration(float(reset) - time.time()) if reset else "--",
    }


def _usage_cache_path(home):
    return Path(home) / USAGE_CACHE_FILE


def _load_usage_cache(home):
    try:
        return json.loads(_usage_cache_path(home).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_usage_cache(home, cache):
    try:
        _usage_cache_path(home).write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError:
        pass


def _cached_event_for(email, cache):
    record = cache.get(email) if email else None
    if not isinstance(record, dict) or not record.get("limits"):
        return None
    return {"limits": record.get("limits"), "timestamp": record.get("timestamp")}


def _account_payload(email, auth, event, cached=False):
    active = bool(email and email.casefold() == str(auth.get("email", "")).casefold())
    limits = event.get("limits") if event else None
    primary = _limit_payload((limits or {}).get("primary"))
    secondary = _limit_payload((limits or {}).get("secondary"))
    return {
        "email": email,
        "short": short_email(email),
        "active": active,
        "signed_in": active and bool(auth.get("ok")),
        "plan": (limits or {}).get("plan_type") or (auth.get("plan") if active else ""),
        "primary": primary,
        "secondary": secondary,
        "updated_at": event.get("timestamp") if limits and event else None,
        "cached": bool(cached),
    }


def codex_usage_payload(home=CODEX_HOME):
    auth = codex_auth_details(home)
    event = latest_codex_rate_limit_event(home)
    cache = _load_usage_cache(home)
    active_email = auth.get("email")
    if active_email and event and event.get("limits"):
        cache[active_email] = {
            "limits": event.get("limits"),
            "timestamp": event.get("timestamp"),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        _save_usage_cache(home, cache)
    emails = list(KNOWN_CODEX_EMAILS)
    if active_email and all(active_email.casefold() != email.casefold() for email in emails):
        emails.insert(0, active_email)
    accounts = []
    for email in emails:
        active = bool(email and email.casefold() == str(active_email or "").casefold())
        account_event = event if active else _cached_event_for(email, cache)
        accounts.append(_account_payload(email, auth, account_event, cached=not active and bool(account_event)))
    return {
        "ok": True,
        "active": active_email or "",
        "accounts": accounts,
        "refreshed_at": datetime.now().isoformat(timespec="seconds"),
    }


def desktop_account_text(account):
    short = account.get("short") or "--"
    primary = account.get("primary")
    secondary = account.get("secondary")
    if primary and secondary:
        mark = "*" if account.get("active") else ""
        return f"{short}{mark} {primary['remaining']}/{secondary['remaining']}"
    return f"{short} --"
