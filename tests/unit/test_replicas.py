"""Two replicas share nothing but the secret. What one hands out, the other accepts: a token, a draft, an
upload link, a sign-in step. Each test builds a second runtime with its own empty memory between the two
halves of a flow; a server that kept anything in memory or on disk fails here."""

from __future__ import annotations

import re

import httpx
import pytest
from conftest import BIN_HOST, GROUP, SECRET, call_tool, ok, public_resolver

PUBLIC = "https://mcp.example.org"


async def test_a_draft_saved_on_one_replica_is_sent_by_another(make_server, chats):
    one, rt_one = make_server()
    r = await call_tool(one, "save_draft", {"chat": GROUP, "text": "from replica one"})
    draft = r.structured_content["draft"]
    two, rt_two = make_server()  # a fresh runtime: nothing of replica one in memory
    assert rt_two is not rt_one
    chats.rapi("message/send", ok({"msgId": 5}))
    sent = await call_tool(two, "send_draft", {"chat": GROUP, "draft": draft, "user_confirmed": True})
    assert not sent.is_error, sent.content
    assert chats.calls("message/send")[0].params["parts"]["mainPart"]["text"]["plain"] == "from replica one"


async def test_an_upload_link_from_one_replica_is_served_by_another(fake, session):
    from vkteams_mcp.app import build
    from vkteams_mcp.config import Settings
    from vkteams_mcp.core import context

    settings = Settings(public_url=PUBLIC, secret=SECRET)
    build(settings, http=httpx.AsyncClient(transport=fake.transport()))
    up = context.runtime().links.announce_uploads(session, GROUP, ["a.txt"])[0]
    app_two, _, _ = build(settings, http=httpx.AsyncClient(transport=fake.transport()))  # replica two
    fake.on("GET", "/api/v139/files/init", {"status": {"code": 200}, "result": {"host": BIN_HOST, "url": "/up/9"}})
    fake.on(
        "POST",
        "/up/9",
        {"status": {"code": 200}, "result": {"fileid": "F9", "static_url": "https://f/get/F9"}},
        host=BIN_HOST,
    )
    path = up["url"].removeprefix(PUBLIC)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_two), base_url=PUBLIC) as c:
        r = await c.put(path, content=b"bytes")
    assert r.status_code == 200, r.text
    assert context.runtime().links.files([r.json()["file"]])[0]["uid"] == up["id"]


async def test_a_sign_in_started_on_one_replica_finishes_on_another(fake):
    """OAuth registration, authorize, the address step and the code step each land on a different replica."""
    from test_http import CB, CHALLENGE, ME, VERIFIER, vk_sign_in

    from vkteams_mcp.app import build
    from vkteams_mcp.config import Settings
    from vkteams_mcp.core import context

    settings = Settings(public_url=PUBLIC, secret=SECRET)
    vk_sign_in(fake)

    def replica():
        app, _, _ = build(settings, http=httpx.AsyncClient(transport=fake.transport()))
        context.runtime().resolve = public_resolver
        context.session_override.set(None)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC)

    async with replica() as web:
        reg = await web.post(
            "/register",
            json={
                "redirect_uris": [CB],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        cid = reg.json()["client_id"]
    async with replica() as web:
        auth = await web.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": cid,
                "redirect_uri": CB,
                "state": "s",
                "code_challenge": CHALLENGE,
                "code_challenge_method": "S256",
            },
        )
        rid = re.search(r"request=([^&]+)", auth.headers["location"]).group(1)
    async with replica() as web:
        step2 = await web.post(
            "/signin", data={"request": rid, "step": "address", "server": "teams.example.com", "email": ME}
        )
        pending = re.search(r'name="pending" value="([^"]+)"', step2.text).group(1)
    async with replica() as web:
        done = await web.post("/signin", data={"request": rid, "step": "code", "pending": pending, "code": "ab12cd"})
        assert done.status_code == 302, done.text
        code = re.search(r"code=([^&]+)", done.headers["location"]).group(1)
    async with replica() as web:
        tok = await web.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CB,
                "client_id": cid,
                "code_verifier": VERIFIER,
            },
        )
        assert tok.status_code == 200, tok.text
    async with replica() as web:
        r = await web.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": tok.json()["refresh_token"], "client_id": cid},
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------- what a stateless server must still refuse


def test_a_blob_too_large_to_open_is_never_made():
    import os

    import pytest

    from vkteams_mcp.core.seal import Sealer, TooLarge

    with pytest.raises(TooLarge):
        Sealer(SECRET).seal("draft", {"x": os.urandom(20_000).hex()}, 60)


async def test_a_long_poll_answer_is_refused_at_save(make_server, chats):
    server, _ = make_server()
    r = await call_tool(server, "save_draft", {"chat": GROUP, "text": "q?", "poll_options": ["yes", "n" * 301]})
    assert r.is_error


async def test_a_scheduled_poll_is_not_mistaken_for_queued_text(make_server, chats):
    """A queued plain "Lunch?" at the same time is not this poll: the queue does not show polls."""
    import datetime
    from zoneinfo import ZoneInfo

    server, _ = make_server()
    when = (datetime.datetime.now(ZoneInfo("Europe/Moscow")) + datetime.timedelta(days=2)).replace(
        second=0, microsecond=0
    )
    r = await call_tool(
        server,
        "save_draft",
        {"chat": GROUP, "text": "Lunch?", "poll_options": ["yes", "no"], "send_at": when.strftime("%Y-%m-%d %H:%M")},
    )
    queued = {"scheduledMsgId": 3, "scheduledTime": int(when.timestamp()), "parts": [{"text": "Lunch?"}]}
    chats.rapi("message/scheduled/history/get", ok({"messages": {"new": [queued]}}))
    chats.rapi("message/send", ok({"scheduled": {"scheduledMsgId": 4}}, code=20020))
    sent = await call_tool(
        server, "send_draft", {"chat": GROUP, "draft": r.structured_content["draft"], "user_confirmed": True}
    )
    assert not sent.is_error and sent.structured_content["scheduled_id"] == "4"


async def test_a_failed_disconnect_is_reported_not_swallowed(fake, session):
    import pytest
    from conftest import BASE, wim_error

    from vkteams_mcp.api.client import VK
    from vkteams_mcp.core.errors import VKError

    fake.wim("aim/endSession", wim_error(503, "Service Unavailable"))
    with pytest.raises(VKError):
        await VK(session, httpx.AsyncClient(transport=fake.transport())).end()
    assert BASE  # the fake answered at the versioned path


async def test_a_disconnect_of_a_dead_session_is_fine(fake, session):
    from conftest import wim_error

    from vkteams_mcp.api.client import VK

    fake.wim("aim/endSession", wim_error(401, "Authentication Required"))
    fake.on("POST", "/api/v139/wim/aim/startSession", wim_error(401, "Authentication Required"))
    await VK(session, httpx.AsyncClient(transport=fake.transport())).end()


async def test_a_full_code_cache_refuses_new_exchanges_and_never_forgets_a_spent_code(monkeypatch):
    from mcp.server.auth.provider import TokenError
    from test_oauth import a_client, code_for, registered

    from vkteams_mcp.core import state
    from vkteams_mcp.core.seal import Sealer
    from vkteams_mcp.core.state import StateUnavailable
    from vkteams_mcp.oauth import provider as prov

    monkeypatch.setattr(state, "MEMORY_MAX", 2)
    p = prov.Provider(Sealer(SECRET), SECRET, public_url=PUBLIC)
    client = await registered(p, a_client())
    from conftest import ME

    from vkteams_mcp.api.session import VKSession

    s = VKSession("teams.example.com", "files.example.com", 139, "d", ME, "a", "k", 0, "aim", "u", ME)
    spent = []
    for _ in range(2):
        code = await code_for(p, s, client)
        ac = await p.load_authorization_code(client, code)
        await p.exchange_authorization_code(client, ac)
        spent.append(ac)
    third = await p.load_authorization_code(client, await code_for(p, s, client))
    with pytest.raises(StateUnavailable):  # full of live marks: no new code is redeemed, none is forgotten
        await p.exchange_authorization_code(client, third)
    for ac in spent:
        with pytest.raises(TokenError):
            await p.exchange_authorization_code(client, ac)


async def test_a_disconnect_while_vk_teams_is_down_is_reported(fake, session):
    """endSession meets an expired aimsid (401) and the renewal meets VK Teams down (503): not a success."""
    import pytest
    from conftest import wim_error

    from vkteams_mcp.api.client import VK
    from vkteams_mcp.core.errors import VKError

    fake.wim("aim/endSession", wim_error(401, "Authentication Required"))
    fake.on("POST", "/api/v139/wim/aim/startSession", wim_error(503, "Service Unavailable"))
    with pytest.raises(VKError) as e:
        await VK(session, httpx.AsyncClient(transport=fake.transport())).end()
    assert e.value.key != "reconnect"


async def test_a_draft_of_another_person_altered_or_expired_is_refused(make_server, chats, session):
    from dataclasses import replace

    from vkteams_mcp.core import context

    server, rt = make_server()
    good = (await call_tool(server, "save_draft", {"chat": GROUP, "text": "mine"})).structured_content["draft"]
    altered = good[:-3] + ("AAA" if not good.endswith("AAA") else "BBB")
    context.session_override.set(replace(session, login="someone.else@example.com"))
    other = await call_tool(server, "send_draft", {"chat": GROUP, "draft": good, "user_confirmed": True})
    context.session_override.set(session)
    bad = await call_tool(server, "send_draft", {"chat": GROUP, "draft": altered, "user_confirmed": True})
    rt.sealer._clock = lambda: 10**12  # a day and more later
    old = await call_tool(server, "send_draft", {"chat": GROUP, "draft": good, "user_confirmed": True})
    assert other.is_error and bad.is_error and old.is_error and chats.writes() == []


async def test_save_draft_refuses_a_draft_too_large_to_seal(make_server, chats, monkeypatch):
    monkeypatch.setattr("vkteams_mcp.core.seal.MAX_BLOB", 200)
    server, _ = make_server()
    r = await call_tool(server, "save_draft", {"chat": GROUP, "text": "a fairly ordinary message " * 20})
    assert r.is_error and "too large" in r.content[0].text.lower()
