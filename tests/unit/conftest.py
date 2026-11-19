"""A fake VK Teams installation: it answers only the routes a test registers and records every
request, so a test can assert exactly what reached the server — including that nothing did.

Response shapes are the ones recorded from a live server (docs/live-findings.md).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

HOST = "teams.example.com"
BIN_HOST = "teams-files.example.com"
API = 139
BASE = f"/api/v{API}"

CONFIG = {
    "api-version": API,
    "api-urls": {"main-api": f"https://{HOST}", "main-binary-api": f"https://{BIN_HOST}"},
    "is-on-premise": True,
    "message-send-api-enabled": False,
}


@dataclass
class Seen:
    method: str
    host: str
    path: str
    query: dict[str, str]
    form: dict[str, str]
    json: Any
    headers: dict[str, str]
    content: bytes

    @property
    def rapi(self) -> str | None:
        p = f"{BASE}/rapi/"
        return self.path[len(p) :] if self.path.startswith(p) else None

    @property
    def params(self) -> dict:
        return (self.json or {}).get("params", {})


def ok(results: dict | None = None, code: int = 20000) -> dict:
    body: dict[str, Any] = {"status": {"code": code}}
    if results is not None:
        body["results"] = results
    return body


def rapi_error(code: int, reason: str) -> dict:
    return {"status": {"code": code, "reason": reason}}


def wim_ok(data: dict | None = None) -> dict:
    return {"response": {"statusCode": 200, "statusText": "Ok", "data": data or {}}}


def wim_error(code: int, text: str) -> dict:
    return {"response": {"statusCode": code, "statusText": text, "data": {}}}


Handler = Callable[[Seen], Any]


@dataclass
class FakeVK:
    routes: dict[tuple[str, str], list[Any]] = field(default_factory=dict)
    hosts: dict[tuple[str, str], str] = field(default_factory=dict)
    seen: list[Seen] = field(default_factory=list)

    def on(self, method: str, path: str, *responses: Any, host: str | None = None) -> FakeVK:
        """Register answers for a route; several answers are given in order, the last one repeats.
        An answer is a dict (JSON body), an httpx.Response, or a callable taking the Seen request.
        A route answers only on its host: the main host unless another is given."""
        self.routes.setdefault((method.upper(), path), []).extend(responses)
        self.hosts[(method.upper(), path)] = host or HOST
        return self

    def rapi(self, method: str, *responses: Any) -> FakeVK:
        return self.on("POST", f"{BASE}/rapi/{method}", *responses)

    def wim(self, path: str, *responses: Any, method: str = "GET") -> FakeVK:
        return self.on(method, f"{BASE}/wim/{path}", *responses)

    def calls(self, rapi_method: str) -> list[Seen]:
        return [s for s in self.seen if s.rapi == rapi_method]

    def writes(self) -> list[Seen]:
        """Every request that could change something: anything that is not a known read."""
        reads = {
            "getHistory",
            "getHistoryBatch",
            "getChatInfo",
            "getChatMembers",
            "getUserInfo",
            "getIdInfo",
            "searchAllDialogs",
            "searchOneDialog",
            "searchThreadsFeed",
            "searchChatMembers",
            "reaction/get",
            "reaction/list",
            "poll/get",
            "folders/requestFolders",
            "archive/getContent",
            "findChat",
            "getCommonChats",
            "message/scheduled/history/get",
            "thread/get",
            "session/list",
            "getChatMemberInfo",
            "getMsgGallery",
        }
        return [
            s
            for s in self.seen
            if not (
                s.rapi in reads
                or "fetchEvents" in s.path
                or s.path.endswith("myteam-config.json")
                or "/files/info/" in s.path
                or s.path.endswith("preference/getPermitDeny")
            )
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        body = request.read()
        form: dict[str, str] = {}
        js = None
        ctype = request.headers.get("content-type", "")
        if "json" in ctype and body:
            js = json.loads(body)
        elif "form" in ctype and body:
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        seen = Seen(
            request.method,
            url.hostname or "",
            url.path,
            {k: v[0] for k, v in parse_qs(url.query).items()},
            form,
            js,
            dict(request.headers),
            body if "octet" in ctype else b"",
        )
        self.seen.append(seen)
        answers = self.routes.get((request.method, url.path))
        if answers and self.hosts.get((request.method, url.path)) != url.hostname:
            answers = None  # the right path on the wrong host is not the right request
        if not answers:
            return httpx.Response(404, text="<html>404 Not Found</html>")
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if callable(answer):
            answer = answer(seen)
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def fake() -> FakeVK:
    f = FakeVK()
    f.on("GET", "/myteam-config.json", CONFIG)
    return f


def public_resolver(host: str) -> list[str]:
    return ["93.184.215.14"]  # a public address; documentation ranges are not global and are refused


@pytest.fixture
def http(fake) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=fake.transport())


ME = "person@example.com"
GROUP = "1001@chat.agent"
GROUP2 = "1002@chat.agent"


@pytest.fixture
def session():
    from vkteams_mcp.api.session import VKSession

    return VKSession(
        host=HOST,
        binary_host=BIN_HOST,
        api_version=API,
        dev_id="dev-id",
        login=ME,
        token_a="token-a",
        session_key="session-key",
        time_offset=0,
        aimsid="aimsid-1",
        fetch_base_url=f"https://{HOST}{BASE}/bos/node-1/aim/fetchEvents?aimsid=aimsid-1&seqNum=1",
        my_sn=ME,
        my_name="Test Person",
    )


@pytest.fixture
def vk(session, http):
    from vkteams_mcp.api.client import VK

    return VK(session, http)


# ---------------------------------------------------------------- server harness

SECRET = "x" * 40


@pytest.fixture
def make_server(fake, session, tmp_path):
    """Build the MCP server on the fake VK, acting as the signed-in person; returns (server, runtime)."""
    from vkteams_mcp.config import Settings
    from vkteams_mcp.core import context
    from vkteams_mcp.core.state import MemoryStore, derive_key
    from vkteams_mcp.links.links import Links
    from vkteams_mcp.registry import parse_areas
    from vkteams_mcp.server import VKServer

    def build(areas: str | None = None, store=None):
        settings = Settings(public_url="https://mcp.example.org", secret=SECRET, areas=parse_areas(areas))
        rt = context.Runtime(
            settings=settings,
            http=httpx.AsyncClient(transport=fake.transport()),
            store=store or MemoryStore(derive_key(SECRET), clock=lambda: 0.0),
        )
        rt.links = Links("https://mcp.example.org", rt.sealer, store=rt.store)
        context.install(rt)
        context.session_override.set(session)
        return VKServer(settings.areas), rt

    return build


@pytest.fixture
def server(make_server):
    return make_server()[0]


async def call_tool(server, name: str, args: dict | None = None):
    """Call a tool through a real MCP client session, in process."""
    from mcp import Client

    async with Client(server) as client:
        return await client.call_tool(name, args or {})


def text_of(result) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


EVENTS_PATH = f"{BASE}/bos/node-1/aim/fetchEvents"


def dialogs(*rows: tuple[str, str, int, int, str]) -> dict:
    """A snapshot of the event stream: (sn, title, unread, time, last text) per chat."""
    events = [
        {
            "type": "buddylist",
            "eventData": {
                "groups": [
                    {
                        "name": "General",
                        "id": 1,
                        "buddies": [
                            {"aimId": sn, "friendly": title, "chatType": "group", "userType": "chat"}
                            for sn, title, *_ in rows
                            if sn.endswith("@chat.agent")
                        ],
                    }
                ]
            },
        }
    ]
    for sn, title, unread, ts, text in rows:
        events.append(
            {
                "type": "histDlgState",
                "eventData": {
                    "sn": sn,
                    "lastMsgId": ts,
                    "unreadCnt": unread,
                    "persons": [{"sn": sn, "friendly": title}],
                    "messages": [{"msgId": ts, "time": ts, "outgoing": False, "text": text}],
                },
            }
        )
    return wim_ok({"events": events})


@pytest.fixture
def chats(fake):
    fake.on(
        "GET",
        EVENTS_PATH,
        dialogs(
            (GROUP, "Project Alpha", 3, 1_800_000_300, "deadline moved"),
            (GROUP2, "Project Beta", 0, 1_800_000_200, "ok"),
            ("ann@example.com", "Ann Smith", 1, 1_800_000_100, "hi"),
        ),
    )
    return fake


def history_like_vk(messages: list[dict], **extra):
    """getHistory as the live server answers it: fromMsgId is EXCLUSIVE; count < 0 walks to older
    messages, count > 0 to newer ones; -1 as fromMsgId means "after the newest"."""

    def answer(seen):
        p = seen.params
        frm, count = p.get("fromMsgId", -1), p.get("count", -1)
        ids = sorted(m["msgId"] for m in messages)
        if count < 0:
            pool = [i for i in ids if frm == -1 or i < frm][count:]
        else:
            pool = [i for i in ids if frm != -1 and i > frm][:count]
        page = [m for m in messages if m["msgId"] in pool]
        older = min(pool) if pool else None
        return ok({"messages": page, "lastMsgId": ids[-1] if ids else 0, "olderMsgId": older, **extra})

    return answer


async def saved(server, args: dict | None = None, **kw) -> str:
    """save_draft, returning the sealed draft send_draft takes."""
    r = await call_tool(server, "save_draft", {**(args or {}), **kw})
    assert not r.is_error, r.content
    return r.structured_content["draft"]


@pytest.fixture
def restore_logging():
    """A test that runs the process's own logging setup leaves the root logger as it found it."""
    import logging
    import sys
    import threading

    root = logging.getLogger()
    saved = (root.handlers[:], root.level, sys.excepthook, threading.excepthook)
    yield
    root.handlers[:], root.level = saved[0], saved[1]
    sys.excepthook, threading.excepthook = saved[2], saved[3]
    logging.captureWarnings(False)
