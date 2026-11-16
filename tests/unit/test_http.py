"""The whole public path over HTTP: client registration, sign-in by e-mailed code, PKCE token
exchange, and an MCP call with the token — against the fake VK Teams."""

from __future__ import annotations

import base64
import hashlib
import re
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from conftest import BASE, HOST, ME, SECRET, public_resolver, wim_error, wim_ok

PUBLIC = "https://mcp.example.org"
CB = "https://client.example.org/cb"
LOGIN = f"{BASE}/wim/auth/clientLogin"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


@pytest.fixture
def app(fake, tmp_path):
    from vkteams_mcp.app import build
    from vkteams_mcp.config import Settings
    from vkteams_mcp.core import context

    settings = Settings(public_url=PUBLIC, secret=SECRET)
    app, _, provider = build(settings, http=httpx.AsyncClient(transport=fake.transport()))
    context.runtime().resolve = public_resolver
    context.session_override.set(None)
    return app


@asynccontextmanager
async def serve(app):
    """The app with its lifespan running (the MCP session manager starts there), in this task."""
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as c:
            yield c


def vk_sign_in(fake, code_ok=True):
    def login(seen):
        if seen.form.get("tokenType") == "otp_via_email":
            return wim_ok({"token": {"a": ""}})
        if not code_ok or seen.form.get("pwd") != "AB12CD":
            return wim_error(330, "Password/LoginId Required/Invalid")
        return wim_ok(
            {"hostTime": 1_800_000_000, "token": {"a": "tok", "expiresIn": 0}, "sessionSecret": "sec", "loginId": ME}
        )

    fake.on("POST", LOGIN, login)
    fake.on(
        "POST",
        f"{BASE}/wim/aim/startSession",
        wim_ok(
            {
                "aimsid": "aimsid-http",
                "fetchBaseURL": f"https://{HOST}{BASE}/bos/n/aim/fetchEvents?aimsid=aimsid-http",
                "myInfo": {"aimId": ME, "friendly": "Test Person"},
            }
        ),
    )


async def register(web) -> str:
    r = await web.post(
        "/register",
        json={
            "redirect_uris": [CB],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "test client",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


async def authorize(web, client_id) -> str:
    r = await web.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CB,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "state": "xyz",
            "resource": f"{PUBLIC}/mcp",
        },
    )
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    assert loc.startswith(f"{PUBLIC}/signin?request=")
    return parse_qs(urlsplit(loc).query)["request"][0]


async def sign_in(web, fake, rid, code="ab12cd") -> httpx.Response:
    vk_sign_in(fake)
    page = await web.get("/signin", params={"request": rid})
    assert page.status_code == 200 and "VK Teams server" in page.text
    step2 = await web.post("/signin", data={"request": rid, "step": "address", "server": HOST, "email": ME})
    assert step2.status_code == 200 and 'name="code"' in step2.text, step2.text
    return await web.post("/signin", data={"request": rid, "step": "code", "pending": pending_of(step2), "code": code})


def pending_of(page: httpx.Response) -> str:
    """The sealed code step the browser carries back, as the form holds it."""
    import html as h

    return h.unescape(re.search(r'name="pending" value="([^"]+)"', page.text).group(1))


async def full_flow(web, fake) -> dict:
    cid = await register(web)
    rid = await authorize(web, cid)
    done = await sign_in(web, fake, rid)
    assert done.status_code == 302
    back = urlsplit(done.headers["location"])
    q = parse_qs(back.query)
    assert f"{back.scheme}://{back.netloc}{back.path}" == CB and q["state"] == ["xyz"]
    tok = await web.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": CB,
            "client_id": cid,
            "code_verifier": VERIFIER,
        },
    )
    assert tok.status_code == 200, tok.text
    return {"client_id": cid, **tok.json()}


async def mcp(web, token: str | None, method: str, params: dict | None = None) -> httpx.Response:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return await web.post(
        "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    )


async def test_metadata_is_published(app):
    async with serve(app) as web:
        r = await web.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 200 and r.json()["registration_endpoint"] == f"{PUBLIC}/register"
        assert "S256" in r.json()["code_challenge_methods_supported"]


async def test_an_unauthenticated_mcp_call_points_to_the_metadata(app, fake):
    async with serve(app) as web:
        r = await mcp(web, None, "tools/list")
        assert r.status_code == 401 and "resource_metadata" in r.headers.get("www-authenticate", "")
        assert fake.seen == []


async def test_the_whole_flow_gives_a_token_that_calls_tools(app, fake):
    async with serve(app) as web:
        tok = await full_flow(web, fake)
        assert tok["token_type"].lower() == "bearer" and tok["refresh_token"]
        sent = [s.form for s in fake.seen if s.path == LOGIN]
        assert sent[0]["tokenType"] == "otp_via_email" and sent[1]["pwd"] == "AB12CD" and "tokenType" not in sent[1]
        r = await mcp(web, tok["access_token"], "tools/list")
        assert r.status_code == 200, r.text
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert "send_draft" in names and "save_chat" not in names


async def test_a_wrong_pkce_verifier_is_refused(app, fake):
    async with serve(app) as web:
        cid = await register(web)
        rid = await authorize(web, cid)
        done = await sign_in(web, fake, rid)
        code = parse_qs(urlsplit(done.headers["location"]).query)["code"][0]
        r = await web.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CB,
                "client_id": cid,
                "code_verifier": "w" * 64,
            },
        )
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


async def test_a_wrong_code_shows_the_form_again_and_resends_nothing(app, fake):
    async with serve(app) as web:
        cid = await register(web)
        rid = await authorize(web, cid)
        r = await sign_in(web, fake, rid, code="WRONG1")
        assert r.status_code == 400 and "not accepted" in r.text and 'name="code"' in r.text
        assert sum(1 for s in fake.seen if s.form.get("tokenType") == "otp_via_email") == 1


async def test_a_private_server_address_is_refused_on_the_page(app, fake):
    async with serve(app) as web:
        from vkteams_mcp.core import context

        context.runtime().resolve = lambda h: ["10.0.0.7"]
        cid = await register(web)
        rid = await authorize(web, cid)
        r = await web.post(
            "/signin", data={"request": rid, "step": "address", "server": "intranet.example.com", "email": ME}
        )
        assert r.status_code == 400 and "private network" in r.text
        assert fake.seen == []


async def test_an_unknown_sign_in_request_is_gone(app):
    async with serve(app) as web:
        r = await web.get("/signin", params={"request": "nope"})
        assert r.status_code == 410


async def test_the_sign_in_page_escapes_what_was_typed(app, fake):
    async with serve(app) as web:
        cid = await register(web)
        rid = await authorize(web, cid)
        r = await web.post(
            "/signin", data={"request": rid, "step": "address", "server": "<script>x</script>", "email": "a@b.co"}
        )
        assert "<script>x</script>" not in r.text and "&lt;script&gt;" in r.text


async def test_the_client_refreshes_its_tokens_over_http(app, fake):
    async with serve(app) as web:
        tok = await full_flow(web, fake)
        r = await web.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": tok["client_id"]},
        )
        assert r.status_code == 200 and r.json()["access_token"] != tok["access_token"]
        fresh = await mcp(web, r.json()["access_token"], "tools/list")
        assert fresh.status_code == 200
        forged = await mcp(web, tok["access_token"][:-4] + "AAAA", "tools/list")
        assert forged.status_code == 401


async def test_a_foreign_host_header_is_refused(app, fake):
    async with serve(app) as web:
        tok = await full_flow(web, fake)
        good = await mcp(web, tok["access_token"], "tools/list")
        assert good.status_code == 200  # the same token works with the right Host...
        bad = await web.post(
            "/mcp",
            headers={
                "Host": "evil.example.net",
                "Authorization": f"Bearer {tok['access_token']}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "MCP-Protocol-Version": "2025-06-18",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    assert bad.status_code in (400, 403, 421), bad.status_code  # ...and is refused with a foreign one


async def test_a_connector_pinned_to_one_installation_asks_only_the_e_mail(fake, tmp_path):
    from vkteams_mcp.api.install import Policy
    from vkteams_mcp.app import build
    from vkteams_mcp.config import Settings
    from vkteams_mcp.core import context

    settings = Settings(
        public_url=PUBLIC,
        secret=SECRET,
        policy=Policy(installations=frozenset({HOST})),
    )
    app, _, _ = build(settings, http=httpx.AsyncClient(transport=fake.transport()))
    context.runtime().resolve = public_resolver
    vk_sign_in(fake)
    async with serve(app) as web:
        cid = await register(web)
        rid = await authorize(web, cid)
        page = await web.get("/signin", params={"request": rid})
        assert 'name="server"' not in page.text and HOST in page.text
        step2 = await web.post(
            "/signin", data={"request": rid, "step": "address", "server": "evil.example.net", "email": ME}
        )
        assert step2.status_code == 200 and 'name="code"' in step2.text
    assert all(s.host == HOST for s in fake.seen)  # a typed server is ignored when pinned


async def test_the_sign_in_page_loads_nothing_from_outside(app):
    async with serve(app) as web:
        cid = await register(web)
        rid = await authorize(web, cid)
        page = await web.get("/signin", params={"request": rid})
        assert "http://" not in page.text and "https://" not in page.text  # no CDN in a closed perimeter
        csp = page.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "font-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "form-action 'self'" in csp
        font = await web.get("/static/fonts/SpaceGrotesk-600-latin.woff2")
        assert font.status_code == 200 and font.content[:4] == b"wOF2"
        css = await web.get("/static/fonts/fonts.css")
        assert css.status_code == 200 and "font-face" in css.text


async def test_the_static_route_serves_only_files_under_static(app):
    async with serve(app) as web:
        for bad in ("/static/../signin.py", "/static/%2e%2e/signin.py", "/static/fonts/../../app.py"):
            r = await web.get(bad)
            assert r.status_code == 404, bad


async def test_the_sign_in_page_speaks_the_browser_language_and_names_no_client(app):
    async with serve(app) as web:
        cid = await register(web)
        rid = await authorize(web, cid)
        en = await web.get("/signin", params={"request": rid}, headers={"Accept-Language": "de-DE,en;q=0.8"})
        ru = await web.get("/signin", params={"request": rid}, headers={"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"})
        assert '<html lang="en">' in en.text and "Get a code" in en.text
        assert '<html lang="ru">' in ru.text and "Получить код" in ru.text and "Get a code" not in ru.text
        assert ru.headers["content-language"] == "ru" and "accept-language" in ru.headers["vary"].lower()
        for page in (en.text, ru.text):
            assert "test client" not in page  # the client names itself at registration: never shown
        refused = await web.post(
            "/signin",
            data={"request": rid, "step": "address", "server": "intranet.example.com", "email": ME},
            headers={"Accept-Language": "ru"},
        )
        assert refused.status_code == 400 and "Введите адрес своего сервера VK Teams" in refused.text


async def test_an_unknown_client_gets_a_page_that_says_to_connect_again(app):
    """A registration the server no longer knows (an old version, a new secret) is a dead end for the
    browser: the client cannot be told, so the person is, in their language, with the way out."""
    q = {
        "response_type": "code",
        "client_id": "75ba0188-bbf9-4a0c-8798-114ed70a33d2",
        "redirect_uri": CB,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "xyz",
    }
    async with serve(app) as web:
        ru = await web.get("/authorize", params=q, headers={"Accept-Language": "ru-RU,ru;q=0.9"})
        en = await web.get("/authorize", params=q, headers={"Accept-Language": "en"})
        bare = await web.post("/authorize", data={**q, "client_id": ""})
    for r in (ru, en, bare):
        assert r.status_code == 400 and r.headers["content-type"].startswith("text/html")
        assert "location" not in r.headers and r.headers["cache-control"] == "no-store"
        assert "75ba0188" not in r.text and "{" not in r.text.split("<style>")[0]
    assert '<html lang="ru">' in ru.text and "Подключитесь заново" in ru.text
    assert '<html lang="en">' in en.text and "connect again" in en.text


async def test_a_known_client_with_a_foreign_redirect_still_gets_no_redirect(app):
    async with serve(app) as web:
        cid = await register(web)
        r = await web.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": cid,
                "redirect_uri": "https://evil.example.net/cb",
                "code_challenge": CHALLENGE,
                "code_challenge_method": "S256",
            },
        )
    assert r.status_code == 400 and "location" not in r.headers and "evil.example.net" not in r.text
