"""The OAuth 2.1 server in front of VK Teams sessions. It stores nothing: every artefact is a sealed blob
that carries what the next step needs, and a token only ever reaches the session it carries."""

from __future__ import annotations

import pytest
from conftest import ME
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from vkteams_mcp.core.seal import Sealer
from vkteams_mcp.oauth.provider import ACCESS_TTL, CODE_TTL, REFRESH_TTL, Provider

SECRET = "s" * 40
NOW = 1_000_000.0


@pytest.fixture
def clock():
    return {"now": NOW}


@pytest.fixture
def ended():
    return []


@pytest.fixture
def provider(clock, ended):
    async def end_session(s):
        ended.append(s.token_a)

    now = lambda: clock["now"]  # noqa: E731
    return Provider(
        Sealer(SECRET, now), SECRET, public_url="https://mcp.example.org", end_session=end_session, clock=now
    )


def a_client(redirect="https://client.example.org/cb") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="assigned-by-the-sdk",
        redirect_uris=[AnyUrl(redirect)],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name="test client",
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


async def registered(provider, client=None):
    client = client or a_client()
    await provider.register_client(client)
    return await provider.get_client(client.client_id)


async def code_for(provider, session, client):
    url = await provider.authorize(client, params())
    assert url.startswith("https://mcp.example.org/signin?request=")
    redirect = await provider.complete_sign_in(url.split("request=")[1], session)
    assert redirect.startswith("https://client.example.org/cb?") and "state=st" in redirect
    return redirect.split("code=")[1].split("&")[0]


async def tokens(provider, session, client):
    code = await code_for(provider, session, client)
    ac = await provider.load_authorization_code(client, code)
    assert ac is not None
    return await provider.exchange_authorization_code(client, ac)


# ---------------------------------------------------------------- nothing kept, nothing readable


async def test_a_registration_is_its_own_client_id(provider):
    client = await registered(provider)
    assert client is not None and client.client_name == "test client"
    assert client.client_id != "assigned-by-the-sdk" and "client.example.org" not in client.client_id
    other = Provider(Sealer("t" * 40), "t" * 40, public_url="https://mcp.example.org")
    assert await other.get_client(client.client_id) is None  # another secret opens nothing


async def test_tokens_carry_the_session_unreadably(provider, session):
    client = await registered(provider)
    t = await tokens(provider, session, client)
    for value in (t.access_token, t.refresh_token):
        assert "token-a" not in value and "session-key" not in value and ME not in value
    assert provider.session_of(t.access_token).token_a == "token-a"


async def test_any_replica_with_the_secret_serves_the_token(provider, session, clock):
    client = await registered(provider)
    t = await tokens(provider, session, client)
    replica = Provider(Sealer(SECRET, lambda: clock["now"]), SECRET, public_url="https://mcp.example.org")
    access = await replica.load_access_token(t.access_token)
    assert access is not None and replica.session_of(t.access_token).login == ME


async def test_a_blob_of_one_kind_is_not_another(provider, session):
    client = await registered(provider)
    t = await tokens(provider, session, client)
    assert await provider.load_access_token(t.refresh_token) is None
    assert await provider.load_refresh_token(client, t.access_token) is None
    assert await provider.load_authorization_code(client, t.access_token) is None
    assert await provider.get_client(t.access_token) is None


async def test_an_altered_token_is_refused(provider, session):
    client = await registered(provider)
    t = await tokens(provider, session, client)
    flipped = t.access_token[:-2] + ("A" if t.access_token[-2] != "A" else "B") + t.access_token[-1]
    assert await provider.load_access_token(flipped) is None


def test_a_short_secret_is_refused():
    with pytest.raises(ValueError):
        Sealer("short")


# ---------------------------------------------------------------- the flow


async def test_an_access_token_names_its_person_by_an_opaque_id(provider, session):
    client = await registered(provider)
    t = await tokens(provider, session, client)
    access = await provider.load_access_token(t.access_token)
    assert access.subject == provider.person(session) and ME not in access.subject
    assert access.client_id == client.client_id  # revocation compares it with the client


async def test_a_code_is_used_once(provider, session):
    client = await registered(provider)
    code = await code_for(provider, session, client)
    ac = await provider.load_authorization_code(client, code)
    await provider.exchange_authorization_code(client, ac)
    assert await provider.load_authorization_code(client, code) is None


async def test_a_code_belongs_to_its_client(provider, session):
    client = await registered(provider)
    other = await registered(provider, a_client("https://other.example.org/cb"))
    code = await code_for(provider, session, client)
    assert await provider.load_authorization_code(other, code) is None


async def test_a_code_and_a_sign_in_request_expire(provider, session, clock):
    client = await registered(provider)
    url = await provider.authorize(client, params())
    request = url.split("request=")[1]
    code = await code_for(provider, session, client)
    clock["now"] += CODE_TTL + 1
    assert await provider.load_authorization_code(client, code) is None
    clock["now"] += 15 * 60
    assert await provider.sign_in_context(request) is None
    with pytest.raises(LookupError):
        await provider.complete_sign_in(request, session)


async def test_tokens_expire(provider, session, clock):
    client = await registered(provider)
    t = await tokens(provider, session, client)
    clock["now"] += ACCESS_TTL + 1
    assert await provider.load_access_token(t.access_token) is None
    assert await provider.load_refresh_token(client, t.refresh_token) is not None
    clock["now"] += REFRESH_TTL
    assert await provider.load_refresh_token(client, t.refresh_token) is None


async def test_a_refresh_gives_new_tokens_with_the_freshest_session(provider, session):
    from dataclasses import replace

    client = await registered(provider)
    t = await tokens(provider, session, client)
    renewed = replace(session, aimsid="aimsid-renewed")
    provider._latest = lambda person, s: renewed if person == provider.person(s) else s
    rt = await provider.load_refresh_token(client, t.refresh_token)
    new = await provider.exchange_refresh_token(client, rt, [])
    assert new.access_token != t.access_token and new.refresh_token != t.refresh_token
    assert provider.session_of(new.access_token).aimsid == "aimsid-renewed"


async def test_a_refresh_token_belongs_to_its_client(provider, session):
    client = await registered(provider)
    other = await registered(provider, a_client("https://other.example.org/cb"))
    t = await tokens(provider, session, client)
    assert await provider.load_refresh_token(other, t.refresh_token) is None


async def test_revoking_a_token_ends_the_vk_teams_session(provider, session, ended):
    client = await registered(provider)
    t = await tokens(provider, session, client)
    await provider.revoke_token(await provider.load_access_token(t.access_token))
    assert ended == ["token-a"]  # every token carrying this session now meets a dead session


def test_the_code_lifetime_is_short():
    assert CODE_TTL <= 600 and ACCESS_TTL <= 3600
