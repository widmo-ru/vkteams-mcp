"""The only code that speaks HTTP to VK Teams.

Quirks it hides: the `/api/v<N>` prefix; errors that arrive as HTTP 200 with a status inside
(`status.code` for rapi, `response.statusCode` for wim); `200xx` meaning success, with per-item
`failures`; an expired `aimsid` (40200 / 401) renewed once from the long-term token without the
person; `fetchBaseURL` being a cursor that each answer moves on.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import httpx

from vkteams_mcp.api.session import VKSession, sign
from vkteams_mcp.core.errors import VKError

log = logging.getLogger("vkteams_mcp.api")
# httpx logs every URL at INFO, and wim URLs carry the aimsid: the client's own logger stays above INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)

SNAPSHOT_FOLLOW = 20  # stream batches read after the first when building the chat list

START_EVENTS = (
    "myInfo,presence,buddylist,typing,dataIM,userAddedToBuddyList,mchat,hist,hiddenChat,diff,"
    "permitDeny,imState,notification,apps"
)


class _Expired(Exception):
    """The aimsid is no longer valid; renew once."""


def _header_safe(name: str) -> str:
    return "".join(ch for ch in name if ch not in '"\\\r\n')[:200] or "file"


def _reason(text: Any) -> str:
    return " ".join(str(text or "").split())[:160] or "no reason given"


def _retry_after(value: str | None) -> int:
    """Retry-After is seconds or an HTTP date; anything else is 30 seconds."""
    if not value:
        return 30
    if value.strip().isdigit():
        return max(1, min(int(value), 3600))
    try:
        from email.utils import parsedate_to_datetime

        delta = parsedate_to_datetime(value).timestamp() - time.time()
        return max(1, min(int(delta), 3600))
    except (TypeError, ValueError):
        return 30


class VK:
    def __init__(self, session: VKSession, http: httpx.AsyncClient, *, on_renew=None) -> None:
        self.s = session
        self.http = http
        self._on_renew = on_renew  # persists the renewed session
        self._next_url: str | None = None  # where the last stream batch said the next one starts
        self.snapshot_partial = False  # the last snapshot stopped on an error before the stream's end

    # ---------------------------------------------------------------- rapi
    async def rapi(self, method: str, params: dict | None = None, *, write: bool = False) -> dict:
        try:
            try:
                return await self._rapi(method, params, write)
            except VKError as e:
                # A read that met a passing server error (50000, seen live on search) is tried once more;
                # a write is never repeated: it may have happened.
                if write or e.key != "failed_read":
                    raise
                return await self._rapi(method, params, write)
        except _Expired:
            await self.renew()
            try:
                return await self._rapi(method, params, write)
            except _Expired:
                raise VKError("reconnect") from None

    async def _rapi(self, method: str, params: dict | None, write: bool) -> dict:
        body = {"reqId": f"{uuid.uuid4().hex[:12]}-{int(time.time())}", "aimsid": self.s.aimsid, "params": params or {}}
        r = await self._send("POST", f"{self.s.base}/rapi/{method}", write, json=body)
        j = self._json(r, write)
        st = j.get("status") or {}
        code = st.get("code")
        log.info("rapi %s -> %s", method, code)
        if isinstance(code, int) and code // 100 == 200:
            return j.get("results") or {}
        if code == 40200:
            raise _Expired
        reason = _reason(st.get("reason"))
        if code == 40001:
            raise VKError("forbidden")
        if code in (40400, 40401):
            raise VKError("not_found")
        if isinstance(code, int) and code >= 50000:
            raise VKError("uncertain" if write else "failed_read")
        raise VKError("failed", detail=reason)

    # ---------------------------------------------------------------- wim
    async def wim(self, path: str, params: dict | None = None, *, method: str = "GET", write: bool = False) -> dict:
        try:
            return await self._wim(path, params, method, write)
        except _Expired:
            await self.renew()
            try:
                return await self._wim(path, params, method, write)
            except _Expired:
                raise VKError("reconnect") from None

    async def _wim(self, path: str, params: dict | None, method: str, write: bool) -> dict:
        p = {"f": "json", "aimsid": self.s.aimsid, **(params or {})}
        url = f"{self.s.base}/wim/{path}"
        r = await (
            self._send("GET", url, write, params=p) if method == "GET" else self._send("POST", url, write, data=p)
        )
        resp = self._json(r, write).get("response") or {}
        code = resp.get("statusCode")
        log.info("wim %s -> %s", path, code)
        if code == 200:
            return resp.get("data") or {}
        if code == 401:
            raise _Expired
        if isinstance(code, int) and code >= 500:
            raise VKError("uncertain" if write else "failed_read")
        raise VKError("failed", detail=_reason(resp.get("statusText")))

    # ---------------------------------------------------------------- session
    async def end(self) -> None:
        """Disconnect: end the session and invalidate its long-term token, so nothing carrying it works again."""
        try:
            await self.wim("aim/endSession", {"invalidateToken": 1}, write=True)
        except VKError as e:
            if e.key != "reconnect":  # only a refused token means it is gone; anything else, it may still work
                raise
            log.info("endSession: the session was already gone")

    async def renew(self) -> None:
        """startSession from the long-term token: a new aimsid without asking the person."""
        url = f"{self.s.base}/wim/aim/startSession"
        params = {
            "a": self.s.token_a,
            "f": "json",
            "k": self.s.dev_id,
            "ts": int(time.time()) + self.s.time_offset,
            "view": "online",
            "events": START_EVENTS,
            "clientName": "VKTeams",
            "language": "ru-RU",
            "deviceId": self.s.device_id or uuid.uuid4().hex,
            "sessionTimeout": 2592000,
            "rawMsg": 0,
            "mobile": 0,
            "includePresenceFields": "aimId,displayId,friendly,state,userType,statusMsg",
        }
        params["sig_sha256"] = sign("POST", url, params, self.s.session_key)
        r = await self._send("POST", url, True, data=params)
        resp = self._json(r, True).get("response") or {}
        code = resp.get("statusCode")
        if code != 200:
            log.info("startSession -> %s", code)
            # VK Teams down is not the token refused: only a refusal means the person must sign in again
            raise VKError("failed_read" if isinstance(code, int) and code >= 500 else "reconnect")
        d = resp.get("data") or {}
        self.s.aimsid = d["aimsid"]
        self.s.fetch_base_url = d.get("fetchBaseURL", self.s.fetch_base_url)
        info = d.get("myInfo") or {}
        self.s.my_sn = info.get("aimId", self.s.my_sn)
        self.s.my_name = info.get("friendly", self.s.my_name)
        if self._on_renew:
            await self._on_renew(self.s)

    async def _poll(self, url: str, timeout_ms: int) -> dict:
        sep = "&" if "?" in url else "?"
        r = await self._send("GET", f"{url}{sep}f=json&timeout={timeout_ms}", False, timeout=timeout_ms / 1000 + 15)
        return self._json(r, False).get("response") or {}

    async def _stream(self, timeout_ms: int, move: bool) -> list[dict]:
        if not self.s.fetch_base_url:
            await self.renew()
        resp = await self._poll(self.s.fetch_base_url, timeout_ms)
        if resp.get("statusCode") == 401:  # renew once; a second refusal is the person's to fix
            await self.renew()
            resp = await self._poll(self.s.fetch_base_url, timeout_ms)
            if resp.get("statusCode") == 401:
                raise VKError("reconnect")
        if resp.get("statusCode") != 200:
            raise VKError("failed_read")
        data = resp.get("data") or {}
        self._next_url = data.get("fetchBaseURL")
        if move and data.get("fetchBaseURL"):
            self.s.fetch_base_url = data["fetchBaseURL"]
        return data.get("events") or []

    async def events(self, timeout_ms: int = 500) -> list[dict]:
        """One poll of the event stream; moves the cursor on."""
        return await self._stream(timeout_ms, move=True)

    async def snapshot(self) -> list[dict]:
        """Events from the session's stream position without moving it: the chat list and recent
        dialogs replay from there (live 2026-11-15). The first batch is the state when the session began;
        the batches after it carry what happened since (live: last_at stuck at the session's start), so
        the stream is followed a few batches on, without saving where it got to."""
        events = await self._stream(1, move=False)
        self.snapshot_partial = False
        url = self._next_url
        for _ in range(SNAPSHOT_FOLLOW):
            if not url:
                break
            try:
                resp = await self._poll(url, 1)
            except VKError as e:
                if e.key != "failed_read":  # throttling and the like go to the caller as they are
                    raise
                resp = {}
            if resp.get("statusCode") != 200:
                # the first batch stands on its own, as it did before; the caller does not keep a partial list
                self.snapshot_partial = True
                break
            data = resp.get("data") or {}
            more = data.get("events") or []
            if not more:
                break
            events += more
            nxt = data.get("fetchBaseURL")
            url = nxt if nxt and nxt != url else None
        else:
            self.snapshot_partial = bool(url)  # stopped at the limit with more of the stream left
        return events

    # ---------------------------------------------------------------- files
    async def files_get(self, path: str, params: dict, *, binary: bool = False) -> dict:
        try:
            return await self._files_get(path, params, binary)
        except _Expired:
            await self.renew()
            try:
                return await self._files_get(path, params, binary)
            except _Expired:
                raise VKError("reconnect") from None

    async def _files_get(self, path: str, params: dict, binary: bool) -> dict:
        base = self.s.binary_base if binary else self.s.base
        r = await self._send("GET", f"{base}/files/{path}", False, params={"aimsid": self.s.aimsid, **params})
        if r.status_code == 401:
            raise _Expired
        j = self._json(r, False)
        code = (j.get("status") or {}).get("code")
        if code in (200, 20000):
            return j.get("result") or {}
        if code == 40200 or r.status_code == 401:
            raise _Expired
        raise VKError("failed", detail=_reason((j.get("status") or {}).get("reason")))

    async def upload(self, host: str, path: str, name: str, data: bytes) -> dict:
        r = await self._send(
            "POST",
            f"https://{host}{path}",
            True,
            params={"aimsid": self.s.aimsid, "f": "json", "k": self.s.dev_id, "r": uuid.uuid4().hex},
            headers={
                "Session-ID": uuid.uuid4().hex,
                "Content-Type": "application/octet-stream",
                # bytes: httpx would refuse a non-ASCII str, and a person's file is often called отчёт.pdf
                "Content-Disposition": f'attachment; filename="{_header_safe(name)}"'.encode(),
                "Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}",
            },
            content=data,
        )
        j = self._json(r, True)
        if (j.get("status") or {}).get("code") != 200:
            raise VKError("failed", detail="the file was not accepted")
        return j.get("result") or {}

    # ---------------------------------------------------------------- transport
    async def _send(self, method: str, url: str, write: bool, **kw) -> httpx.Response:
        try:
            r = await self.http.request(method, url, **kw)
        except httpx.TimeoutException:
            raise VKError("uncertain" if write else "failed_read") from None
        except httpx.HTTPError:
            raise VKError("uncertain" if write else "failed_read") from None
        if r.status_code == 429:
            raise VKError("throttled", seconds=_retry_after(r.headers.get("Retry-After")))
        if r.status_code >= 500:
            raise VKError("uncertain" if write else "failed_read")
        return r

    @staticmethod
    def _json(r: httpx.Response, write: bool) -> dict:
        try:
            j = r.json()
        except ValueError:
            raise VKError("uncertain" if write else "failed_read") from None
        return j if isinstance(j, dict) else {}
