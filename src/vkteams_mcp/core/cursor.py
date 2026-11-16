"""Opaque cursors: the upstream position, bound to one tool, one person and the arguments that chose the
list (filter, chat, query), signed so it cannot be replayed in another list or edited to point elsewhere."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from vkteams_mcp.core.errors import invalid


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Cursors:
    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("a cursor key must be at least 32 bytes")
        self._key = key

    def _mac(self, payload: bytes) -> str:
        return _b64(hmac.new(self._key, b"cursor:" + payload, hashlib.sha256).digest()[:12])

    def make(self, tool: str, person: str, state: dict, scope: dict | None = None) -> str:
        payload = json.dumps({"t": tool, "p": person, "q": scope or {}, "s": state}, separators=(",", ":")).encode()
        return f"{_b64(payload)}.{self._mac(payload)}"

    def read(self, tool: str, person: str, cursor: str, scope: dict | None = None) -> dict:
        damaged = invalid("This cursor is damaged: pass it exactly as it came, or start the list again.")
        try:
            p64, sig = cursor.split(".", 1)
            payload = _unb64(p64)
            signed = hmac.compare_digest(self._mac(payload), sig)
        except (ValueError, TypeError):
            raise damaged from None
        if not signed:  # altered, or made by another server: either way not one this server can read
            raise damaged
        try:  # only a payload this server signed is parsed
            body = json.loads(payload)
        except ValueError:
            raise damaged from None
        try:
            if body["t"] != tool or body["p"] != person or body["q"] != (scope or {}):
                raise ValueError
            return body["s"]
        except (ValueError, KeyError, TypeError):
            raise invalid("This cursor does not belong to this list. Start the list again without a cursor.") from None
