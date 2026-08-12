"""Pairing and token auth for Director.

The Director endpoint is reachable from the public internet through Tailscale
Funnel, so every request that is not the pairing handshake must carry a bearer
token. Tokens are random 32-byte secrets; only their SHA-256 is stored, so a
copy of director.db does not let anyone in.

Pairing is deliberately old-fashioned: a short code with a short life, shown on
the box (log + `python -m director.cli pair`), typed into the phone once. There
is no password to phish and no account system to maintain — this is one
person's own machine.
"""
from __future__ import annotations

import hashlib
import secrets
import string

from . import store

CODE_TTL = 600.0          # a pairing code is good for ten minutes
CODE_ALPHABET = string.digits + "ABCDEFGHJKLMNPQRSTUVWXYZ"  # no I/O/0 lookalikes

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_pairing_code(*, kind: str = "phone", ttl: float = CODE_TTL) -> dict:
    """Mint a one-time pairing code.

    Only the hash is stored, and it lives in the database rather than in
    process memory so `director.cli pair` on the box and the running server
    agree about which codes are live.
    """
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
    record = store.add_pairing_code(hash_token(code), kind=kind, ttl=ttl)
    return {"code": code, **record}


def active_codes() -> list[dict]:
    return store.list_pairing_codes()


def redeem_pairing_code(code: str, *, name: str = "", kind: str = "") -> dict | None:
    """Trade a valid code for a device token. Returns None if the code is bad."""
    supplied = str(code or "").strip().upper()
    if not supplied:
        return None
    record = store.take_pairing_code(hash_token(supplied))
    if record is None:
        return None
    token = new_token()
    device = store.create_device(
        name=name or "Phone",
        kind=kind or record.get("kind") or "phone",
        token_hash=hash_token(token),
    )
    return {"token": token, "device": device}


def device_for_token(token: str) -> dict | None:
    token = str(token or "").strip()
    if not token:
        return None
    device = store.device_by_token_hash(hash_token(token))
    if device:
        store.touch_device(device["id"])
    return device


def machine_for_token(token: str) -> dict | None:
    token = str(token or "").strip()
    if not token:
        return None
    return store.machine_by_token_hash(hash_token(token))


def bearer(headers) -> str:
    """Pull a token out of an Authorization header or an X-Director-Token."""
    raw = str(headers.get("Authorization") or "").strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return str(headers.get("X-Director-Token") or "").strip()


def enroll_machine(*, name: str, platform: str, caps: dict | None = None) -> dict:
    """Create (or re-key) a machine client such as the Windows aiOS desktop."""
    token = new_token()
    machine = store.upsert_machine(
        name=name, platform=platform, token_hash=hash_token(token), caps=caps or {})
    return {"token": token, "machine": machine}
