"""Sign-in by the one-time code VK Teams e-mails the person.

Shapes from the desktop client (im-desktop `client_login.cpp`) and a live run:
  1. clientLogin s=<address> pwd=none tokenType=otp_via_email → 200 with an EMPTY token: the code is sent.
  2. clientLogin s=<address> pwd=<CODE> → token.a, sessionSecret, hostTime.
     Sending the code with tokenType=otp_via_email again would send a NEW code and burn this one.
  3. startSession signed with HMAC(code, sessionSecret) → aimsid.
The code is used once here and never stored; only the long-term token and session key are.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from collections.abc import Callable

import httpx

from vkteams_mcp.api.client import VK
from vkteams_mcp.api.install import Installation
from vkteams_mcp.api.session import VKSession, derive_session_key
from vkteams_mcp.core.errors import invalid

CODES_PER_ADDRESS_PER_HOUR = 3
CODES_PER_NETWORK_PER_HOUR = 10


def _url(inst: Installation) -> str:
    return f"https://{inst.host}/api/v{inst.api_version}/wim/auth/clientLogin"


async def _client_login(inst: Installation, form: dict, http: httpx.AsyncClient) -> dict:
    try:
        r = await http.post(
            _url(inst),
            data={"f": "json", "idType": "ICQ", "clientName": "VKTeams", **form},
            timeout=20,
            follow_redirects=False,
        )
        j = r.json()
    except (httpx.HTTPError, ValueError):
        return {}
    # The installation was chosen on a public page: its answer is checked for shape before it is read.
    resp = j.get("response") if isinstance(j, dict) else None
    if not isinstance(resp, dict):
        return {}
    if not isinstance(resp.get("data"), dict):
        resp = {**resp, "data": {}}
    if not isinstance(resp["data"].get("token"), dict):
        resp["data"] = {**resp["data"], "token": {}}
    return resp


async def request_code(inst: Installation, address: str, http: httpx.AsyncClient, *, dev_id: str) -> None:
    resp = await _client_login(
        inst, {"devId": dev_id, "s": address.strip(), "pwd": "none", "tokenType": "otp_via_email"}, http
    )
    if resp.get("statusCode") != 200:
        # Same sentence whatever the reason: the page must not tell whether an address exists.
        raise invalid("VK Teams did not send a code. Check the address and the server, then try again.")


async def finish_sign_in(
    inst: Installation, address: str, code: str, http: httpx.AsyncClient, *, dev_id: str
) -> VKSession:
    code = "".join(code.split()).upper()
    if not code:
        raise invalid("Type the code from the e-mail.")
    resp = await _client_login(inst, {"devId": dev_id, "s": address.strip(), "pwd": code}, http)
    data = resp.get("data") or {}
    token = (data.get("token") or {}).get("a")
    if resp.get("statusCode") != 200 or not token or not data.get("sessionSecret"):
        raise invalid(
            "That code was not accepted. Check it against the latest e-mail and type it again; no new code was sent."
        )
    host_time = int(data.get("hostTime") or time.time())
    session = VKSession(
        host=inst.host,
        binary_host=inst.binary_host,
        api_version=inst.api_version,
        dev_id=dev_id,
        login=address.strip(),
        token_a=token,
        session_key=derive_session_key(code, data["sessionSecret"]),
        time_offset=host_time - int(time.time()),
        aimsid="",
        fetch_base_url="",
        my_sn=data.get("loginId") or address.strip(),
        device_id=uuid.uuid4().hex,
    )
    await VK(session, http).renew()
    return session


class CodeLimiter:
    """Sliding one-hour windows per address and per client network. In memory: one replica."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._by: dict[str, deque[float]] = {}

    def _hit(self, key: str, limit: int) -> bool:
        now = self._clock()
        q = self._by.setdefault(key, deque())
        while q and now - q[0] >= 3600:
            q.popleft()
        return len(q) >= limit

    def _prune(self) -> None:
        now = self._clock()
        for k in [k for k, q in self._by.items() if not q or now - q[-1] >= 3600]:
            del self._by[k]

    def take(self, *, address: str, network: str | None) -> None:
        if len(self._by) > 10_000:
            self._prune()
        a = "a:" + address.strip().lower()
        n = "n:" + network if network else None
        if self._hit(a, CODES_PER_ADDRESS_PER_HOUR) or (n and self._hit(n, CODES_PER_NETWORK_PER_HOUR)):
            raise invalid("Too many codes were requested. Use the code already sent, or try again in an hour.")
        now = self._clock()
        self._by[a].append(now)
        if n:
            self._by[n].append(now)
