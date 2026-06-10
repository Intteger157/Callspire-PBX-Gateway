"""Encrypt/decrypt sensitive strings at rest using the gateway JWT secret."""

from __future__ import annotations

import base64
import hashlib


def _key_bytes(secret: str) -> bytes:
    return hashlib.sha256((secret or "change-me").encode("utf-8")).digest()


def encrypt_secret(plain: str, secret: str) -> str:
    if not plain:
        return ""
    k = _key_bytes(secret)
    data = plain.encode("utf-8")
    xored = bytes(b ^ k[i % len(k)] for i, b in enumerate(data))
    return base64.urlsafe_b64encode(xored).decode("ascii")


def decrypt_secret(enc: str, secret: str) -> str:
    if not enc:
        return ""
    k = _key_bytes(secret)
    data = base64.urlsafe_b64decode(enc.encode("ascii"))
    plain = bytes(b ^ k[i % len(k)] for i, b in enumerate(data))
    return plain.decode("utf-8")
