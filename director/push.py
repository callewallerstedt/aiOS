"""Web push, so the phone hears about things while the app is closed.

Three kinds of push matter here:

    a reply       Director finished answering
    your turn     an approval or a question is blocking a run
    a routine     a reminder fired, or a scheduled job finished

VAPID keys are generated once and kept in settings.json. The browser needs the
public key as a base64url-encoded uncompressed P-256 point, which is what
`application_server_key` means.

Sends happen in a thread pool: pywebpush is synchronous and a slow push service
must not stall the event loop that is running the conversation.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from . import config, store

try:
    from py_vapid import Vapid01, b64urlencode
    from pywebpush import WebPushException, webpush
    from cryptography.hazmat.primitives import serialization
    AVAILABLE = True
except ImportError:  # pragma: no cover - installed by the deploy script
    AVAILABLE = False
    WebPushException = Exception

# Push services reject anything much larger, and the payload is only a preview.
MAX_BODY = 240
TTL = 3600


def ensure_keys(settings: dict[str, Any] | None = None) -> dict:
    """Return the VAPID keypair, generating it on first use."""
    cfg = settings if settings is not None else config.load_settings(refresh=True)
    push = dict(cfg.get("push") or {})
    if push.get("public_key") and push.get("private_pem"):
        return push
    if not AVAILABLE:
        return {}

    vapid = Vapid01()
    vapid.generate_keys()
    raw_public = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    push = {
        "public_key": b64urlencode(raw_public),
        "private_pem": vapid.private_pem().decode("ascii"),
        "subject": str(push.get("subject") or "mailto:calle.wallerstedt@gmail.com"),
        "enabled": True,
    }
    config.update_settings({"push": push})
    return push


def public_key(settings: dict[str, Any] | None = None) -> str:
    return str(ensure_keys(settings).get("public_key") or "")


def enabled(settings: dict[str, Any] | None = None) -> bool:
    cfg = settings if settings is not None else config.load_settings()
    if not AVAILABLE:
        return False
    return bool((cfg.get("push") or {}).get("enabled", True))


def _signer(keys: dict):
    """A Vapid object built from the stored PEM.

    pywebpush's `vapid_private_key` takes a Vapid instance, a *path* to a PEM
    file, or a base64url raw key — never PEM contents. Handing it the PEM text
    fails deep inside cryptography with "ASN.1 parsing error", which reads like
    a corrupt key rather than the wrong argument type.
    """
    return Vapid01.from_pem(str(keys["private_pem"]).encode("ascii"))


def _send_one(subscription: dict, payload: dict, keys: dict) -> tuple[bool, str]:
    endpoint = str(subscription.get("endpoint") or "")
    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps(payload),
            vapid_private_key=_signer(keys),
            vapid_claims={"sub": keys.get("subject") or "mailto:aios@localhost",
                          "exp": int(time.time()) + 12 * 3600},
            ttl=TTL,
            timeout=15,
        )
        return True, ""
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", 0)
        # 404/410 mean the browser threw the subscription away; so should we.
        if status in (404, 410):
            store.drop_push_subscription(endpoint)
            return False, "gone"
        store.note_push_failure(endpoint)
        return False, f"{status or 'error'}: {exc}"
    except Exception as exc:  # a broken key or a DNS blip
        store.note_push_failure(endpoint)
        return False, str(exc)


def send_sync(title: str, body: str, *, url: str = "", tag: str = "",
              settings: dict[str, Any] | None = None) -> dict:
    if not enabled(settings):
        return {"sent": 0, "skipped": "push disabled or unavailable"}
    keys = ensure_keys(settings)
    if not keys:
        return {"sent": 0, "skipped": "no VAPID keys"}
    payload = {
        "title": title[:80],
        # Explicit semantic field lets the phone render the speaker without
        # ever falling back to an application-name heading.
        "agent": title[:80],
        "body": (body or "")[:MAX_BODY],
        "url": url or "/",
        "tag": tag or "director",
        "at": time.time(),
    }
    sent, failed = 0, 0
    for row in store.list_push_subscriptions():
        ok, _detail = _send_one(row["subscription"], payload, keys)
        sent += 1 if ok else 0
        failed += 0 if ok else 1
    return {"sent": sent, "failed": failed}


async def send(title: str, body: str, *, url: str = "", tag: str = "",
               settings: dict[str, Any] | None = None) -> dict:
    """Fire and forget from the event loop."""
    if not enabled(settings):
        return {"sent": 0}
    return await asyncio.to_thread(send_sync, title, body, url=url, tag=tag,
                                   settings=settings)


def subscribe(subscription: dict, *, device_id: str = "") -> dict:
    return store.add_push_subscription(subscription, device_id=device_id)


def unsubscribe(endpoint: str) -> None:
    store.drop_push_subscription(endpoint)
