"""Minimal VK Teams / VK WorkSpace client for the normal-user (non-bot) API.

Everything here was checked against a live on-prem server, not taken from the
reverse-engineered spec on faith. See tests/ for the evidence.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import itertools
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx

# The desktop client's public developer id, as VK publishes it in im-desktop (products/myteam/config.json).
DEV_IDS = {"desktop": "ic1DcTU-u6i6jrDP"}

SESSION_PATH = Path(os.environ.get("VKTEAMS_SESSION", "~/.config/vkteams-mcp/session.json")).expanduser()

START_SESSION_EVENTS = (
    "myInfo,presence,buddylist,typing,dataIM,userAddedToBuddyList,mchat,hist,"
    "hiddenChat,diff,permitDeny,imState,notification,apps"
)


class ApiError(RuntimeError):
    def __init__(self, where: str, code, reason: str, body):
        super().__init__(f"{where}: {code} {reason}")
        self.code = code
        self.reason = reason
        self.body = body


@dataclass
class Session:
    host: str
    api_version: int
    dev_id: str
    login: str
    aimsid: str = ""
    fetch_base_url: str = ""
    token_a: str = ""
    session_key: str = ""
    time_offset: int = 0
    my_sn: str = ""
    extra: dict = field(default_factory=dict)

    def save(self, path: Path = SESSION_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path = SESSION_PATH) -> "Session | None":
        if not path.exists():
            return None
        return cls(**json.loads(path.read_text()))


def discover(host: str, http: httpx.Client | None = None) -> dict:
    """GET /myteam-config.json — the on-prem client configuration (public)."""
    http = http or httpx.Client(timeout=15)
    r = http.get(f"https://{host}/myteam-config.json")
    r.raise_for_status()
    return r.json()


def sign(method: str, url: str, params: dict, session_key: str) -> str:
    """WIM request signature: HMAC-SHA256 over METHOD&quote(url)&quote(sorted query)."""
    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in sorted(params.items()))
    data = f"{method}&{quote(url, safe='')}&{quote(query, safe='')}"
    mac = hmac.new(session_key.encode(), data.encode(), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


def derive_session_key(password: str, session_secret: str) -> str:
    mac = hmac.new(password.encode(), session_secret.encode(), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


class Client:
    def __init__(self, session: Session, http: httpx.Client | None = None):
        self.s = session
        self.http = http or httpx.Client(timeout=30)
        self._req = itertools.count(1)

    # ---- paths -------------------------------------------------------------
    @property
    def base(self) -> str:
        return f"https://{self.s.host}/api/v{self.s.api_version}"

    # ---- login -------------------------------------------------------------
    def client_login(self, login: str, password: str, extra: dict | None = None) -> dict:
        """POST {base}/wim/auth/clientLogin → raw WIM response (dict)."""
        form = {
            "f": "json",
            "devId": self.s.dev_id,
            "s": login,
            "pwd": password,
            "idType": "ICQ",
            "clientName": "VKTeams",
        }
        form.update(extra or {})
        r = self.http.post(f"{self.base}/wim/auth/clientLogin", data=form)
        r.raise_for_status()
        return r.json()

    def start_session(self) -> dict:
        url = f"{self.base}/wim/aim/startSession"
        params = {
            "a": self.s.token_a,
            "f": "json",
            "k": self.s.dev_id,
            "ts": int(time.time()) + self.s.time_offset,
            "view": "online",
            "events": START_SESSION_EVENTS,
            "includePresenceFields": "aimId,displayId,friendly,state,userType,statusMsg",
            "clientName": "VKTeams",
            "language": "ru-RU",
            "deviceId": self.s.extra.setdefault("device_id", str(uuid.uuid4())),
            "sessionTimeout": 2592000,
            "rawMsg": 0,
            "mobile": 0,
        }
        params["sig_sha256"] = sign("POST", url, params, self.s.session_key)
        r = self.http.post(url, data=params)
        r.raise_for_status()
        return r.json()

    def end_session(self, invalidate: bool = False) -> dict:
        p = {"f": "json", "aimsid": self.s.aimsid}
        if invalidate:
            p["invalidateToken"] = 1
        return self.http.get(f"{self.base}/wim/aim/endSession", params=p).json()

    # ---- calls -------------------------------------------------------------
    def rapi_raw(self, method: str, params: dict | None = None, *, aimsid: str | None = None) -> httpx.Response:
        body = {"reqId": f"{next(self._req)}-{int(time.time())}", "aimsid": self.s.aimsid if aimsid is None else aimsid,
                "params": params or {}}
        return self.http.post(f"{self.base}/rapi/{method}", json=body)

    def rapi(self, method: str, params: dict | None = None) -> dict:
        r = self.rapi_raw(method, params)
        r.raise_for_status()
        j = r.json()
        st = j.get("status", {})
        if st.get("code") != 20000:
            raise ApiError(f"rapi/{method}", st.get("code"), st.get("reason", ""), j)
        return j

    def wim_raw(self, path: str, params: dict | None = None, *, method: str = "GET") -> httpx.Response:
        p = {"f": "json", "aimsid": self.s.aimsid, **(params or {})}
        if method == "GET":
            return self.http.get(f"{self.base}/wim/{path}", params=p)
        return self.http.post(f"{self.base}/wim/{path}", data=p)

    def wim(self, path: str, params: dict | None = None, *, method: str = "GET") -> dict:
        r = self.wim_raw(path, params, method=method)
        r.raise_for_status()
        j = r.json()
        code = j.get("response", {}).get("statusCode")
        if code != 200:
            raise ApiError(f"wim/{path}", code, j.get("response", {}).get("statusText", ""), j)
        return j

    def fetch_events(self, timeout_ms: int = 1000) -> dict:
        url = self.s.fetch_base_url
        sep = "&" if "?" in url else "?"
        r = self.http.get(f"{url}{sep}f=json&timeout={timeout_ms}", timeout=timeout_ms / 1000 + 15)
        r.raise_for_status()
        j = r.json()
        nxt = j.get("response", {}).get("data", {}).get("fetchBaseURL")
        if nxt:
            self.s.fetch_base_url = nxt
        return j
