"""End-to-end encryption for secrets sent from aiOS Remote to one PC."""

from __future__ import annotations

import base64
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PRIVATE_KEY_PATH = ROOT / ".aios-phone-secret.pem"
INFO = b"aiOS Phone API Key v1"
AAD = b"aiOS Phone API Key"


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: object) -> bytes:
    text = str(value or "").strip()
    if not text:
        raise ValueError("Encrypted secret data is incomplete.")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _private_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if PRIVATE_KEY_PATH.exists():
        return serialization.load_pem_private_key(PRIVATE_KEY_PATH.read_bytes(), password=None)

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    temp = PRIVATE_KEY_PATH.with_suffix(".pem.tmp")
    temp.write_bytes(pem)
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    try:
        temp.replace(PRIVATE_KEY_PATH)
    except OSError:
        if not PRIVATE_KEY_PATH.exists():
            raise
        temp.unlink(missing_ok=True)
        return serialization.load_pem_private_key(PRIVATE_KEY_PATH.read_bytes(), password=None)
    return key


def public_key_payload() -> dict:
    from cryptography.hazmat.primitives import serialization

    raw = _private_key().public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return {
        "transport_version": 1,
        "transport_public_key": _b64url_encode(raw),
    }


def decrypt_secret(payload: object) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    if not isinstance(payload, dict) or int(payload.get("version") or 0) != 1:
        raise ValueError("Unsupported encrypted secret format.")
    ephemeral = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(),
        _b64url_decode(payload.get("ephemeral_public_key")),
    )
    iv = _b64url_decode(payload.get("iv"))
    ciphertext = _b64url_decode(payload.get("ciphertext"))
    if len(iv) != 12:
        raise ValueError("Encrypted secret IV is invalid.")
    shared = _private_key().exchange(ec.ECDH(), ephemeral)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=iv, info=INFO).derive(shared)
    plaintext = AESGCM(key).decrypt(iv, ciphertext, AAD)
    return plaintext.decode("utf-8")
