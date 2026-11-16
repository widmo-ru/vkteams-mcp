"""The OAuth 2.1 server in front of VK Teams sessions: what is stored, how tokens live and die,
and that a token only ever reaches its own person's session."""

from __future__ import annotations

import sqlite3

import pytest
from conftest import ME
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from vkteams_mcp.oauth.provider import ACCESS_TTL, CODE_TTL, Provider
from vkteams_mcp.oauth.store import Store

SECRET = "s" * 40


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "db.sqlite", SECRET)


@pytest.fixture
def provider(store):
    return Provider(store, public_url="https://mcp.example.org", clock=lambda: 1_000_000.0)


CLIENT = OAuthClientInformationFull(
    client_id="c1",
    redirect_uris=[AnyUrl("https://client.example.org/cb")],
    token_endpoint_auth_method="none",
    grant_types=["authorization_code", "refresh_token"],
    response_types=["code"],
)


def params(**kw) -> AuthorizationParams:
    base = dict(
        state="st",
        scopes=None,
        code_challenge="chal",
        redirect_uri=AnyUrl("https://client.example.org/cb"),
        redirect_uri_provided_explicitly=True,
        resource="https://mcp.example.org/mcp",
    )
    base.update(kw)
    return AuthorizationParams(**base)


async def signed_in(provider, session, client=CLIENT):
    await provider.register_client(client)
    url = await provider.authorize(client, params())
    assert url.startswith("https://mcp.example.org/signin?request=")
    request_id = url.split("request=")[1]
    redirect = await provider.complete_sign_in(request_id, session)
    assert redirect.startswith("https://client.example.org/cb?") and "state=st" in redirect
    code = redirect.split("code=")[1].split("&")[0]
    ac = await provider.load_authorization_code(client, code)
    return await provider.exchange_authorization_code(client, ac)


# ---------------------------------------------------------------- storage


def test_vk_credentials_are_sealed_at_rest(store, session, tmp_path):
    pid = store.save_session(session)
    raw = sqlite3.connect(tmp_path / "db.sqlite").execute("select sealed from persons").fetchone()[0]
    for secret in (session.token_a, session.session_key, session.aimsid, ME):
        assert secret.encode() not in raw
    assert store.load_session(pid).token_a == session.token_a


def test_a_sealed_session_cannot_be_moved_to_another_person(store, session, tmp_path):
    pid = store.save_session(session)
    db = sqlite3.connect(tmp_path / "db.sqlite")
    db.execute(
        "insert into persons(person_id, host, sealed, updated) values('other','h',"
        "(select sealed from persons where person_id=?),0)",
        (pid,),
    )
    db.commit()
    assert store.load_session("other") is None


def test_another_secret_cannot_open_the_store(store, session, tmp_path):
    pid = store.save_session(session)
    assert Store(tmp_path / "db.sqlite", "t" * 40).load_session(pid) is None


def test_a_short_secret_is_refused(tmp_path):
    with pytest.raises(ValueError):
        Store(tmp_path / "db.sqlite", "short")


# ---------------------------------------------------------------- flow


async def test_tokens_are_stored_only_as_hashes(provider, session, tmp_path):
    tok = await signed_in(provider, session)
    dump = "\n".join(sqlite3.connect(tmp_path / "db.sqlite").iterdump())
    assert tok.access_token not in dump and tok.refresh_token not in dump


async def test_an_access_token_names_its_person(provider, session, store):
    tok = await signed_in(provider, session)
    at = await provider.load_access_token(tok.access_token)
    assert at.subject == store.person_id(session) and at.client_id == "c1"
    assert at.resource == "https://mcp.example.org/mcp"
    assert tok.expires_in == ACCESS_TTL


async def test_a_token_reaches_only_its_own_session(provider, store, session):
    tok_a = await signed_in(provider, session)
    other = type(session)(
        **{**session.__dict__, "login": "other@example.com", "my_sn": "other@example.com", "aimsid": "aimsid-other"}
    )
    tok_b = await signed_in(provider, other)
    a = await provider.load_access_token(tok_a.access_token)
    b = await provider.load_access_token(tok_b.access_token)
    assert a.subject != b.subject
    assert store.load_session(a.subject).aimsid == "aimsid-1"
    assert store.load_session(b.subject).aimsid == "aimsid-other"


async def test_a_code_is_used_once(provider, session):
    await provider.register_client(CLIENT)
    url = await provider.authorize(CLIENT, params())
    redirect = await provider.complete_sign_in(url.split("request=")[1], session)
    code = redirect.split("code=")[1].split("&")[0]
    ac = await provider.load_authorization_code(CLIENT, code)
    await provider.exchange_authorization_code(CLIENT, ac)
    assert await provider.load_authorization_code(CLIENT, code) is None


async def test_a_code_belongs_to_its_client(provider, session):
    await provider.register_client(CLIENT)
    other = CLIENT.model_copy(update={"client_id": "c2"})
    await provider.register_client(other)
    url = await provider.authorize(CLIENT, params())
    code = (await provider.complete_sign_in(url.split("request=")[1], session)).split("code=")[1].split("&")[0]
    assert await provider.load_authorization_code(other, code) is None


async def test_a_sign_in_request_is_used_once_and_expires(provider, session, store):
    await provider.register_client(CLIENT)
    url = await provider.authorize(CLIENT, params())
    rid = url.split("request=")[1]
    await provider.complete_sign_in(rid, session)
    with pytest.raises(LookupError):
        await provider.complete_sign_in(rid, session)
    url2 = await provider.authorize(CLIENT, params())
    late = Provider(store, public_url="https://mcp.example.org", clock=lambda: 1_000_000.0 + 3600)
    with pytest.raises(LookupError):
        await late.complete_sign_in(url2.split("request=")[1], session)


async def test_expired_and_revoked_tokens_are_refused(provider, store, session):
    tok = await signed_in(provider, session)
    later = Provider(store, public_url="https://mcp.example.org", clock=lambda: 1_000_000.0 + ACCESS_TTL + 1)
    assert await later.load_access_token(tok.access_token) is None
    at = await provider.load_access_token(tok.access_token)
    await provider.revoke_token(at)
    assert await provider.load_access_token(tok.access_token) is None


async def test_refresh_rotates_and_the_old_refresh_token_dies(provider, session):
    tok = await signed_in(provider, session)
    rt = await provider.load_refresh_token(CLIENT, tok.refresh_token)
    new = await provider.exchange_refresh_token(CLIENT, rt, [])
    assert new.access_token != tok.access_token and new.refresh_token != tok.refresh_token
    assert (await provider.load_access_token(new.access_token)).subject
    assert await provider.load_access_token(tok.access_token) is None  # the old access token goes too
    assert await provider.load_refresh_token(CLIENT, tok.refresh_token) is None  # and reusing the old refresh
    assert await provider.load_access_token(new.access_token) is None  # ...revokes the chain


async def test_a_refresh_token_belongs_to_its_client(provider, session):
    tok = await signed_in(provider, session)
    other = CLIENT.model_copy(update={"client_id": "c2"})
    assert await provider.load_refresh_token(other, tok.refresh_token) is None


async def test_signing_out_revokes_every_token_of_the_person(provider, session, store):
    tok = await signed_in(provider, session)
    at = await provider.load_access_token(tok.access_token)
    store.forget_person(at.subject)
    assert await provider.load_access_token(tok.access_token) is None
    assert store.load_session(at.subject) is None


def test_the_code_lifetime_is_short():
    assert CODE_TTL <= 600 and ACCESS_TTL <= 3600


async def test_a_reused_refresh_token_revokes_the_whole_chain(provider, session):
    tok = await signed_in(provider, session)
    rt = await provider.load_refresh_token(CLIENT, tok.refresh_token)
    thief = await provider.exchange_refresh_token(CLIENT, rt, [])  # the thief refreshes first
    assert await provider.load_refresh_token(CLIENT, tok.refresh_token) is None  # the owner tries the old one
    assert await provider.load_access_token(thief.access_token) is None
    assert await provider.load_refresh_token(CLIENT, thief.refresh_token) is None


def test_drafts_are_sealed_at_rest(store, tmp_path):
    store.save_draft("p1", "1001@chat.agent", {"text": "secret plans"}, expires=10**10)
    rows = sqlite3.connect(tmp_path / "db.sqlite").execute("select chat, data from drafts").fetchall()
    raw = b"".join(bytes(c, "utf-8") + bytes(d) for c, d in rows)
    assert b"secret plans" not in raw and b"1001@chat.agent" not in raw
    assert store.draft("p1", "1001@chat.agent", 0)["text"] == "secret plans"
    assert store.draft("p2", "1001@chat.agent", 0) is None


async def test_a_replay_after_several_rotations_still_revokes_the_chain(provider, session):
    t0 = await signed_in(provider, session)
    t1 = await provider.exchange_refresh_token(CLIENT, await provider.load_refresh_token(CLIENT, t0.refresh_token), [])
    t2 = await provider.exchange_refresh_token(CLIENT, await provider.load_refresh_token(CLIENT, t1.refresh_token), [])
    assert await provider.load_refresh_token(CLIENT, t0.refresh_token) is None  # R0 replayed after R1 -> R2
    assert await provider.load_access_token(t2.access_token) is None


async def test_a_replay_from_another_client_does_not_revoke_the_owners_chain(provider, session):
    t0 = await signed_in(provider, session)
    t1 = await provider.exchange_refresh_token(CLIENT, await provider.load_refresh_token(CLIENT, t0.refresh_token), [])
    other = CLIENT.model_copy(update={"client_id": "c2"})
    assert await provider.load_refresh_token(other, t0.refresh_token) is None
    assert await provider.load_access_token(t1.access_token) is not None
