"""A store that cannot be reached closes the doors that need it, and only those.

The store here is a real Redis client pointed at a port nothing listens on, so the failure is the real one:
a refused connection, within the real timeouts. Sign-in, spending a code and sending refuse, with a sentence
that says to try again shortly and that nothing happened; reading and listing carry on; `/readyz` says 503 and
`/healthz` stays 200, so the pod leaves the Service but is not restarted.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from conftest import BIN_HOST, GROUP, SECRET, call_tool, public_resolver, text_of
from test_http import CB, ME, PUBLIC, VERIFIER, authorize, pending_of, register, vk_sign_in

from vkteams_mcp import observability as obs
from vkteams_mcp.app import build
from vkteams_mcp.config import Settings
from vkteams_mcp.core import context
from vkteams_mcp.core.state import StateConfig, StateUnavailable, build_store

DEAD = StateConfig(url="redis://127.0.0.1:1/0", prefix="t:")


def dead_store():
    return build_store(DEAD, SECRET)


@pytest.fixture
def dead_app(fake):
    app, _, provider = build(
        Settings(public_url=PUBLIC, secret=SECRET, state=DEAD), http=httpx.AsyncClient(transport=fake.transport())
    )
    context.runtime().resolve = public_resolver
    context.session_override.set(None)
    return app, provider


def web_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC)


def refusals(reason: str) -> float:
    return obs.GUARD_REFUSALS.labels(obs.SERVER, reason)._value.get()


async def test_sending_is_refused_and_nothing_is_sent(make_server, chats):
    server, _ = make_server(store=dead_store())
    r = await call_tool(server, "save_draft", {"chat": GROUP, "text": "hello"})
    assert not r.is_error, "saving a draft takes no limit: it needs no store"
    before = refusals("state")
    sent = await call_tool(
        server, "send_draft", {"chat": GROUP, "draft": r.structured_content["draft"], "user_confirmed": True}
    )
    assert sent.is_error
    text = text_of(sent)
    assert "shared state" in text and "Try again shortly" in text and "Nothing was sent" in text
    assert chats.calls("message/send") == []
    assert refusals("state") == before + 1


async def test_reading_still_works_with_the_store_down(make_server, chats):
    server, _ = make_server(store=dead_store())
    r = await call_tool(server, "list_chats", {})
    assert not r.is_error and "Project Alpha" in text_of(r)


async def test_the_token_endpoint_answers_503_and_leaves_the_code_unspent(fake):
    from test_http import CHALLENGE  # noqa: F401

    good, _, gp = build(Settings(public_url=PUBLIC, secret=SECRET), http=httpx.AsyncClient(transport=fake.transport()))
    context.runtime().resolve = public_resolver
    context.session_override.set(None)
    async with web_for(good) as web:
        cid = await register(web)
        rid = await authorize(web, cid)
        vk_sign_in(fake)
        step2 = await web.post(
            "/signin", data={"request": rid, "step": "address", "server": "teams.example.com", "email": ME}
        )
        done = await web.post(
            "/signin", data={"request": rid, "step": "code", "pending": pending_of(step2), "code": "ab12cd"}
        )
        from urllib.parse import parse_qs, urlsplit

        code = parse_qs(urlsplit(done.headers["location"]).query)["code"][0]
        working = gp.store
        gp.store = dead_store()  # the pod's store goes away between sign-in and exchange
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CB,
            "client_id": cid,
            "code_verifier": VERIFIER,
        }
        down = await web.post("/token", data=form)
        assert down.status_code == 503
        assert down.json()["error"] == "temporarily_unavailable" and down.headers["retry-after"]
        gp.store = working  # it comes back: the same code, never spent, is accepted
        up = await web.post("/token", data=form)
        assert up.status_code == 200, up.text
        again = await web.post("/token", data=form)
        assert again.status_code == 400


async def test_sign_in_refuses_a_step_it_cannot_count(fake, dead_app):
    app, _ = dead_app
    vk_sign_in(fake)
    async with web_for(app) as web:
        cid = await register(web)
        rid = await authorize(web, cid)
        r = await web.post(
            "/signin", data={"request": rid, "step": "address", "server": "teams.example.com", "email": ME}
        )
        assert r.status_code == 503 and "try again" in r.text.lower()
        ru = await web.post(
            "/signin",
            data={"request": rid, "step": "address", "server": "teams.example.com", "email": ME},
            headers={"Accept-Language": "ru"},
        )
        assert ru.status_code == 503 and "Попробуйте" in ru.text
    # no code was requested from VK Teams, and no VK Teams server was even looked up
    assert not [s for s in fake.seen if s.path.endswith("clientLogin")]


class Flaky:
    """The store of a pod whose Valkey comes and goes: it answers from memory, or raises as a lost connection does."""

    def __init__(self) -> None:
        self.inner = build_store(StateConfig(), SECRET)
        self.down = False

    def __getattr__(self, name):
        target = getattr(self.inner, name)

        async def call(*a, **kw):
            if self.down:
                raise StateUnavailable("ConnectionError")
            return await target(*a, **kw)

        return call


async def test_the_code_step_refuses_when_wrong_codes_cannot_be_counted_and_goes_on_when_they_can(fake):
    store = Flaky()
    app, _, _ = build(
        Settings(public_url=PUBLIC, secret=SECRET), http=httpx.AsyncClient(transport=fake.transport()), store=store
    )
    context.runtime().resolve = public_resolver
    context.session_override.set(None)
    vk_sign_in(fake)
    async with web_for(app) as web:
        rid = await authorize(web, await register(web))
        step2 = await web.post(
            "/signin", data={"request": rid, "step": "address", "server": "teams.example.com", "email": ME}
        )
        assert step2.status_code == 200
        logins = len([s for s in fake.seen if s.path.endswith("clientLogin")])
        store.down = True
        form = {"request": rid, "step": "code", "pending": pending_of(step2), "code": "ab12cd"}
        r = await web.post("/signin", data=form)
        assert r.status_code == 503 and "try again" in r.text.lower()
        assert len([s for s in fake.seen if s.path.endswith("clientLogin")]) == logins  # the code never left
        store.down = False
        r = await web.post("/signin", data=form)
        assert r.status_code == 302


async def test_readiness_does_not_follow_the_store_but_the_gauge_and_the_heartbeat_do(dead_app, monkeypatch):
    """One store outage must not empty the Service: /readyz stays 200 on both ports while the store is down; the
    signal is `mcp_state_store_up`, which the heartbeat keeps true to the second without any traffic."""
    from vkteams_mcp import app as app_module
    from vkteams_mcp.instrument import metrics_app

    app, _ = dead_app
    monkeypatch.setattr(app_module, "PROBE_EVERY", 0.05)
    obs.STATE_UP.labels(obs.SERVER).set(1)
    async with app.router.lifespan_context(app):
        async with web_for(app) as web:
            for _ in range(100):  # the heartbeat runs on its own: nobody asked the store
                if obs.STATE_UP.labels(obs.SERVER)._value.get() == 0:
                    break
                await asyncio.sleep(0.05)
            assert obs.STATE_UP.labels(obs.SERVER)._value.get() == 0, "the heartbeat did not notice the store was down"
            assert (await web.get("/healthz")).status_code == 200
            ready = await web.get("/readyz")
    assert ready.status_code == 200 and ready.json() == {"ok": True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=metrics_app), base_url="http://pod") as c:
        assert (await c.get("/readyz")).status_code == 200


async def test_readyz_is_200_in_memory_mode(fake):
    app, _, _ = build(Settings(public_url=PUBLIC, secret=SECRET), http=httpx.AsyncClient(transport=fake.transport()))
    async with web_for(app) as web:
        r = await web.get("/readyz")
    assert r.status_code == 200 and r.json() == {"ok": True}


async def test_an_upload_is_refused_with_503_and_nothing_reaches_vk_teams(fake, dead_app, session):
    app, _ = dead_app
    rt = context.runtime()
    up = rt.links.announce_uploads(session, GROUP, ["a.txt"])[0]
    async with web_for(app) as web:
        r = await web.put(up["url"].removeprefix(PUBLIC), content=b"bytes")
    assert r.status_code == 503 and "shortly" in r.json()["error"]
    assert not [s for s in fake.seen if BIN_HOST in s.host or "files/init" in s.path]
