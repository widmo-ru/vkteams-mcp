"""Sealed blobs: what the server would otherwise keep, carried by whoever needs it next.

The server keeps nothing between requests. The person's VK Teams session travels inside their OAuth
tokens, a draft inside the draft token the model passes back, the code step of the sign-in page inside
its form, a file link inside its URL. Each is JSON, compressed, encrypted and authenticated with
AES-GCM under a key derived from VKTEAMS_MCP_SECRET, and bound to its kind: a draft token cannot be
replayed as an access token. Any replica that shares the secret opens any blob; nobody without it can
read or change one.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import zlib
from collections.abc import Callable
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAX_BLOB = 16 * 1024  # larger is neither made nor opened


class TooLarge(ValueError):
    """The payload does not fit in one sealed blob."""


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Sealer:
    def __init__(self, secret: str, clock: Callable[[], float] = time.time) -> None:
        if len(secret) < 32:
            raise ValueError("the secret must be at least 32 characters")
        self._aead = AESGCM(hashlib.sha256(b"vkteams-mcp seal\n" + secret.encode()).digest())
        self._clock = clock

    def seal(self, kind: str, payload: dict[str, Any], ttl: float) -> str:
        body = json.dumps({**payload, "exp": int(self._clock() + ttl)}, separators=(",", ":")).encode()
        nonce = os.urandom(12)
        blob = _b64(nonce + self._aead.encrypt(nonce, zlib.compress(body, 9), kind.encode()))
        if len(blob) > MAX_BLOB:  # never hand out what open() would refuse
            raise TooLarge(len(blob))
        return blob

    def open(self, kind: str, blob: str | None) -> dict[str, Any] | None:
        """The payload, or None for anything forged, altered, of another kind or expired."""
        if not blob or len(blob) > MAX_BLOB:
            return None
        try:
            raw = _unb64(blob)
            body = json.loads(zlib.decompress(self._aead.decrypt(raw[:12], raw[12:], kind.encode())))
        except (ValueError, InvalidTag, zlib.error, json.JSONDecodeError, TypeError):
            return None
        if not isinstance(body, dict) or not isinstance(body.get("exp"), int) or body["exp"] < self._clock():
            return None
        return body


def person_id(host: str, login: str, secret: str) -> str:
    """A stable, opaque name for one person on one installation: keys rate limits and binds cursors."""
    return hashlib.sha256(f"person\n{secret}\n{host}\n{login.lower()}".encode()).hexdigest()[:32]
