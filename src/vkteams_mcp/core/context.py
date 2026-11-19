"""What one tool call runs with: the runtime of the server, and the person behind the token.

`call()` opens the access token and takes the person's VK Teams session out of it: the server keeps no
session of its own. A tool never sees a token, a host or a session argument (manifesto rule 16): the
person is whoever the token belongs to. When VK Teams renews a short `aimsid`, this replica remembers
the fresh one for an hour so the next calls do not renew again; the next token refresh carries it.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token

from vkteams_mcp.api.client import VK
from vkteams_mcp.api.session import VKSession
from vkteams_mcp.config import Settings
from vkteams_mcp.core.cursor import Cursors
from vkteams_mcp.core.errors import VKError
from vkteams_mcp.core.guard import Guard
from vkteams_mcp.core.seal import Sealer, person_id
from vkteams_mcp.core.state import MemoryStore, Observed, StateStore, derive_key

FRESH_TTL = 3600
FRESH_MAX = 10_000


@dataclass
class Runtime:
    settings: Settings
    http: httpx.AsyncClient
    store: StateStore | None = None  # None: memory, one replica (the app builds the configured store)
    guard: Guard | None = None
    clock: Callable[[], float] = time.time
    cursors: Cursors = field(init=False)
    sealer: Sealer = field(init=False)
    fresh: dict[str, tuple[float, VKSession]] = field(default_factory=dict)  # a cache, not a store
    links: Any = None  # links.Links, set by the app
    resolve: Callable[[str], list[str]] | None = None  # DNS for installation checks; None = the system's

    def __post_init__(self) -> None:
        self.cursors = Cursors(hashlib.sha256(b"cursor-key\n" + self.settings.secret.encode()).digest())
        self.sealer = Sealer(self.settings.secret, self.clock)
        if self.store is None:
            self.store = Observed(MemoryStore(derive_key(self.settings.secret), self.settings.state.prefix))
        if self.guard is None:
            self.guard = Guard(self.store)

    def person(self, session: VKSession) -> str:
        return person_id(session.host, session.login, self.settings.secret)

    def latest(self, person: str, session: VKSession) -> VKSession:
        """The freshest session this replica has seen for the person, else the one given."""
        hit = self.fresh.get(person)
        return hit[1] if hit and hit[0] > self.clock() and hit[1].token_a == session.token_a else session

    def remember(self, session: VKSession) -> None:
        now = self.clock()
        if len(self.fresh) >= FRESH_MAX:
            for k in [k for k, (exp, _) in self.fresh.items() if exp < now] or list(self.fresh)[: FRESH_MAX // 10]:
                del self.fresh[k]
        self.fresh[self.person(session)] = (now + FRESH_TTL, session)


_RUNTIME: Runtime | None = None
# Set by tests (and nothing else) to act as a person without an HTTP request.
session_override: ContextVar[VKSession | None] = ContextVar("session_override", default=None)


def install(rt: Runtime) -> None:
    global _RUNTIME
    _RUNTIME = rt


def runtime() -> Runtime:
    if _RUNTIME is None:
        raise RuntimeError("runtime not installed")
    return _RUNTIME


@dataclass
class Call:
    tool: str
    person: str
    vk: VK
    rt: Runtime

    async def guard(
        self, *, kind: str, user_confirmed: bool | None, people: int = 0, target: str | None = None
    ) -> dict:
        return await self.rt.guard.check(
            self.person, kind=kind, user_confirmed=user_confirmed, people=people, target=target
        )

    def cursor_in(self, cursor: str | None, scope: dict | None = None) -> dict | None:
        """The position a cursor holds; `scope` is the arguments that chose the list, and must match."""
        return self.rt.cursors.read(self.tool, self.person, cursor, scope) if cursor else None

    def cursor_out(self, state: dict | None, scope: dict | None = None) -> str | None:
        return self.rt.cursors.make(self.tool, self.person, state, scope) if state else None

    @property
    def me(self) -> str:
        return self.vk.s.my_sn

    @property
    def tz(self):
        from zoneinfo import ZoneInfo

        return ZoneInfo(self.rt.settings.timezone)


@asynccontextmanager
async def call(tool: str) -> AsyncIterator[Call]:
    rt = runtime()
    session = session_override.get()
    if session is None:
        token = get_access_token()
        body = rt.sealer.open("access", token.token) if token else None
        session = VKSession.from_json(body["s"]) if body else None
    if session is None:
        raise VKError("reconnect")
    person = rt.person(session)
    session = rt.latest(person, session)

    async def renewed(s: VKSession) -> None:
        rt.remember(s)

    yield Call(tool=tool, person=person, vk=VK(session, rt.http, on_renew=renewed), rt=rt)
