"""What one tool call runs with: the runtime of the server, and the person behind the token.

`call()` turns the access token into the person's VK Teams session. A tool never sees a token, a
host or a session argument (manifesto rule 16): the person is whoever the token belongs to.
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
from vkteams_mcp.config import Settings
from vkteams_mcp.core.cursor import Cursors
from vkteams_mcp.core.errors import VKError
from vkteams_mcp.core.guard import Guard
from vkteams_mcp.oauth.store import Store


@dataclass
class Runtime:
    settings: Settings
    store: Store
    http: httpx.AsyncClient
    guard: Guard = field(default_factory=Guard)
    clock: Callable[[], float] = time.time
    cursors: Cursors = field(init=False)
    links: Any = None  # links.Links, set by the app
    resolve: Callable[[str], list[str]] | None = None  # DNS for installation checks; None = the system's

    def __post_init__(self) -> None:
        self.cursors = Cursors(hashlib.sha256(b"cursor-key\n" + self.settings.secret.encode()).digest())


_RUNTIME: Runtime | None = None
# Set by tests (and nothing else) to act as a person without an HTTP request.
person_override: ContextVar[str | None] = ContextVar("person_override", default=None)


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

    def guard(self, *, kind: str, user_confirmed: bool | None, people: int = 0, target: str | None = None) -> dict:
        return self.rt.guard.check(self.person, kind=kind, user_confirmed=user_confirmed, people=people, target=target)

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
    person = person_override.get()
    if person is None:
        token = get_access_token()
        person = token.subject if token else None
    if not person:
        raise VKError("reconnect")
    session = rt.store.load_session(person)
    if session is None:
        raise VKError("reconnect")

    async def saved(s) -> None:
        rt.store.save_session(s)

    yield Call(tool=tool, person=person, vk=VK(session, rt.http, on_renew=saved), rt=rt)
