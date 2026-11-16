"""One person's VK Teams session on one installation, and the WIM request signature.

The session travels sealed inside the OAuth tokens: the long-term token `a` and the session key renew `aimsid`
without a new code, so all three are secrets.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import asdict, dataclass
from urllib.parse import quote


@dataclass
class VKSession:
    host: str
    binary_host: str
    api_version: int
    dev_id: str
    login: str
    token_a: str
    session_key: str
    time_offset: int
    aimsid: str
    fetch_base_url: str
    my_sn: str
    my_name: str = ""
    device_id: str = ""

    @property
    def base(self) -> str:
        return f"https://{self.host}/api/v{self.api_version}"

    @property
    def binary_base(self) -> str:
        return f"https://{self.binary_host}/api/v{self.api_version}"

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, s: str) -> VKSession:
        return cls(**json.loads(s))

    def __repr__(self) -> str:  # never print credentials
        return f"VKSession(host={self.host!r}, api_version={self.api_version})"


def sign(method: str, url: str, params: dict, session_key: str) -> str:
    """sig_sha256 as the desktop client computes it, accepted by a live startSession."""
    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in sorted(params.items()))
    data = f"{method}&{quote(url, safe='')}&{quote(query, safe='')}"
    return base64.b64encode(hmac.new(session_key.encode(), data.encode(), hashlib.sha256).digest()).decode()


def derive_session_key(code_or_password: str, session_secret: str) -> str:
    return base64.b64encode(
        hmac.new(code_or_password.encode(), session_secret.encode(), hashlib.sha256).digest()
    ).decode()
