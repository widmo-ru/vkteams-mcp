"""Two replicas, one real Valkey or Redis (standalone and behind Sentinel): what one did, the other knows.

Each `replica()` is a whole app built from its own Settings with its own store connection, the way two pods
are, and nothing but the sealing key and the Valkey address in common. The flows run through HTTP against the
fake VK Teams. Skipped without a Valkey, failed without one when VKTEAMS_TEST_REQUIRE_VALKEY=1
(see test_state_contract.py).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from conftest import BIN_HOST, GROUP, SECRET, public_resolver
from test_http import CB, ME, PUBLIC, VERIFIER, authorize, pending_of, register, vk_sign_in
from test_state_contract import config_for

from vkteams_mcp.app import build
from vkteams_mcp.config import Settings
from vkteams_mcp.core import context

pytestmark = pytest.mark.parametrize("backend", ["valkey", "sentinel", "redis", "redis-sentinel"], indirect=False)


class Replicas:
    def __init__(self, backend: str, fake) -> None:
        self.state = config_for(backend, f"r{uuid.uuid4().hex[:8]}:")
        self.fake = fake
        self.built: list = []

    def replica(self):
        """A new pod: its own app, runtime and store connection. Returns (client, runtime)."""
        app, _, _ = build(
            Settings(public_url=PUBLIC, secret=SECRET, state=self.state, trusted_proxies=("127.0.0.1",)),
            http=httpx.AsyncClient(transport=self.fake.transport()),
        )
        rt = context.runtime()
        rt.resolve = public_resolver
        context.session_override.set(None)
        web = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC)
        self.built.append((web, rt))
        return Pod(web, rt)

    async def close(self) -> None:
        for web, rt in self.built:
            await web.aclose()
            await rt.store.close()


class Pod:
    def __init__(self, web: httpx.AsyncClient, rt) -> None:
        self.web, self.rt = web, rt

    def __enter__(self):  # acting as this pod: the runtime a request sees is its own
        context.install(self.rt)
        return self.web

    def __exit__(self, *exc):
        return False


@pytest.fixture
async def pods(backend, fake):
    r = Replicas(backend, fake)
    yield r
    await r.close()


async def code_on(pod: Pod, fake, cid: str) -> tuple[str, str]:
    """A sign-in that ends on `pod`: its authorization code and the request id."""
    with pod as web:
        rid = await authorize(web, cid)
        vk_sign_in(fake)
        step2 = await web.post(
            "/signin", data={"request": rid, "step": "address", "server": "teams.example.com", "email": ME}
        )
        done = await web.post(
            "/signin", data={"request": rid, "step": "code", "pending": pending_of(step2), "code": "ab12cd"}
        )
    assert done.status_code == 302, done.text
    return parse_qs(urlsplit(done.headers["location"]).query)["code"][0], rid


def token_form(cid: str, code: str) -> dict:
    return {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CB,
        "client_id": cid,
        "code_verifier": VERIFIER,
    }


async def test_a_code_redeemed_on_one_replica_is_refused_on_the_other(pods, fake):
    a, b = pods.replica(), pods.replica()
    with a as web:
        cid = await register(web)
    code, _ = await code_on(a, fake, cid)
    with a as web:
        first = await web.post("/token", data=token_form(cid, code))
    assert first.status_code == 200, first.text
    with b as web:
        second = await web.post("/token", data=token_form(cid, code))
    assert second.status_code == 400 and second.json()["error"] == "invalid_grant"
    with a as web:  # and again on the one that issued it
        third = await web.post("/token", data=token_form(cid, code))
    assert third.status_code == 400
    # the tokens the first exchange issued work on the replica that did not issue them
    with b as web:
        refreshed = await web.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": first.json()["refresh_token"], "client_id": cid},
        )
    assert refreshed.status_code == 200


async def test_two_replicas_racing_for_one_code_have_one_winner(pods, fake):
    a, b = pods.replica(), pods.replica()
    with a as web:
        cid = await register(web)
    code, _ = await code_on(a, fake, cid)

    async def redeem(pod: Pod):
        # the token endpoint reaches only the provider, which holds its pod's own store connection
        return await pod.web.post("/token", data=token_form(cid, code))

    got = await asyncio.gather(redeem(a), redeem(b), redeem(a), redeem(b))
    assert sorted(r.status_code for r in got) == [200, 400, 400, 400], [r.text for r in got]


async def test_code_requests_are_limited_across_replicas(pods, fake):
    """Three per address an hour: spread over the two replicas, the fourth is refused wherever it lands."""
    a, b = pods.replica(), pods.replica()
    with a as web:
        rid = await authorize(web, await register(web))
    vk_sign_in(fake)
    seen = []
    for pod in (a, b, a, b, a):
        with pod as web:
            r = await web.post(
                "/signin", data={"request": rid, "step": "address", "server": "teams.example.com", "email": ME}
            )
        seen.append("Too many codes" in r.text)
    assert seen == [False, False, False, True, True]


async def test_one_network_cannot_mail_many_addresses_through_two_replicas(pods, fake):
    a, b = pods.replica(), pods.replica()
    with a as web:
        rid = await authorize(web, await register(web))
    vk_sign_in(fake)
    seen = []
    for i in range(12):
        with (a, b)[i % 2] as web:
            r = await web.post(
                "/signin",
                data={"request": rid, "step": "address", "server": "teams.example.com", "email": f"p{i}@example.com"},
            )
        seen.append("Too many codes" in r.text)
    assert seen == [False] * 10 + [True] * 2


async def test_wrong_codes_are_counted_across_replicas(pods, fake):
    """Five wrong codes per sign-in step; they may come through any replica, and the sixth is refused on all."""
    from vkteams_mcp.signin import MAX_CODE_TRIES

    a, b = pods.replica(), pods.replica()
    with a as web:
        rid = await authorize(web, await register(web))
    vk_sign_in(fake, code_ok=False)
    with a as web:
        step2 = await web.post(
            "/signin", data={"request": rid, "step": "address", "server": "teams.example.com", "email": ME}
        )
    pending = pending_of(step2)
    statuses = []
    for i in range(MAX_CODE_TRIES + 2):
        with (a, b)[i % 2] as web:
            r = await web.post("/signin", data={"request": rid, "step": "code", "pending": pending, "code": "WRONG1"})
        statuses.append(r.status_code)
    assert statuses == [400] * MAX_CODE_TRIES + [429, 429]
    vk_sign_in(fake)  # and the right code no longer helps this step, on either replica
    for pod in (a, b):
        with pod as web:
            r = await web.post("/signin", data={"request": rid, "step": "code", "pending": pending, "code": "ab12cd"})
        assert r.status_code == 429


async def test_the_send_allowance_is_one_bucket_across_replicas(pods):
    a, b = pods.replica(), pods.replica()
    assert a.rt.store is not b.rt.store and a.rt.guard is not b.rt.guard
    from vkteams_mcp.core.errors import VKError
    from vkteams_mcp.core.guard import CAPACITY

    sends = []
    for i in range(CAPACITY):
        pod = (a, b)[i % 2]
        sends.append((await pod.rt.guard.check("person-1", kind="message", user_confirmed=True))["sends_left"])
    assert sends == list(range(CAPACITY - 1, -1, -1))
    for pod in (a, b):
        with pytest.raises(VKError) as e:
            await pod.rt.guard.check("person-1", kind="message", user_confirmed=True)
        assert e.value.key == "rate"
    # another person has a bucket of their own
    assert (await b.rt.guard.check("person-2", kind="message", user_confirmed=True))["sends_left"] == CAPACITY - 1


async def test_an_upload_running_on_one_replica_keeps_the_link_busy_on_the_other(pods, fake, session):
    a, b = pods.replica(), pods.replica()
    up = a.rt.links.announce_uploads(session, GROUP, ["a.txt"])[0]
    lid = up["url"].rsplit("/", 1)[1]
    claim = await a.rt.links.claim_upload(lid)  # an upload that is still running on replica one
    assert claim is not None
    fake.on("GET", "/api/v139/files/init", {"status": {"code": 200}, "result": {"host": BIN_HOST, "url": "/up/9"}})
    fake.on(
        "POST",
        "/up/9",
        {"status": {"code": 200}, "result": {"fileid": "F9", "static_url": "https://f/get/F9"}},
        host=BIN_HOST,
    )
    path = up["url"].removeprefix(PUBLIC)
    with b as web:
        busy = await web.put(path, content=b"bytes")
    assert busy.status_code == 403
    await a.rt.links.release_upload(claim[0]["uid"], claim[1])
    with b as web:
        done = await web.put(path, content=b"bytes")
    assert done.status_code == 200, done.text
    assert re.fullmatch(r"[A-Za-z0-9_-]+", done.json()["file"])


async def test_readyz_is_200_while_the_store_answers(pods):
    a = pods.replica()
    with a as web:
        r = await web.get("/readyz")
    assert r.status_code == 200 and r.json() == {"ok": True}
