"""The metrics every server of the family exports: names, labels, and what they must never carry."""

from __future__ import annotations

import asyncio
import re

import httpx
import pytest
from conftest import GROUP, SECRET, call_tool, public_resolver, text_of
from prometheus_client import REGISTRY
from test_http import ME, PUBLIC, authorize, pending_of, register, vk_sign_in

from vkteams_mcp.app import build
from vkteams_mcp.config import Settings
from vkteams_mcp.core import context
from vkteams_mcp.instrument import Instrument, metrics_app, route_group


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, {"server": "vkteams", **labels}) or 0.0


def app_for(fake):
    app, _, _ = build(Settings(public_url=PUBLIC, secret=SECRET), http=httpx.AsyncClient(transport=fake.transport()))
    context.runtime().resolve = public_resolver
    context.session_override.set(None)
    return app


def web_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC)


# ---------------------------------------------------------------- where metrics are served


async def test_the_public_port_has_no_metrics(fake):
    async with web_for(app_for(fake)) as web:
        assert (await web.get("/metrics")).status_code == 404


async def test_the_metrics_port_serves_metrics_the_probes_and_nothing_else():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=metrics_app), base_url="http://pod") as c:
        r = await c.get("/metrics")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
        for name in (
            "mcp_http_requests_in_flight",
            "mcp_http_requests_total",
            "mcp_http_request_duration_seconds",
            "mcp_tool_calls_total",
            "mcp_tool_duration_seconds",
            "mcp_upstream_requests_total",
            "mcp_upstream_duration_seconds",
            "mcp_signin_total",
            "mcp_auth_refusals_total",
            "mcp_guard_refusals_total",
            "mcp_link_transfers_total",
            "mcp_state_store_up",
            "mcp_state_store_errors_total",
            "mcp_state_store_duration_seconds",
            "mcp_log_records_dropped_total",
            "mcp_build_info",
            "python_info",
        ):
            assert f"# TYPE {name}" in r.text, name
        assert re.search(r'^mcp_state_store_up\{server="vkteams"\} 1.0$', r.text, re.M)
        assert "mcp_build_info" in r.text and re.search(
            r'^mcp_build_info\{server="vkteams",version="[^"]+"\} 1.0$', r.text, re.M
        )
        for probe in ("/healthz", "/readyz"):
            p = await c.get(probe)
            assert p.status_code == 200 and p.json() == {"ok": True}
        assert (await c.get("/")).status_code == 404
        assert (await c.post("/metrics")).status_code == 404
        assert (await c.post("/readyz")).status_code == 404
        assert (await c.get("/metrics/other")).status_code == 404


# ---------------------------------------------------------------- HTTP


@pytest.mark.parametrize(
    ("path", "group"),
    [
        ("/mcp", "mcp"),
        ("/mcp/", "mcp"),
        ("/healthz", "health"),
        ("/readyz", "health"),
        ("/signin", "signin"),
        ("/static/fonts/fonts.css", "signin"),
        ("/files/d/abc", "links"),
        ("/files/u/abc", "links"),
        ("/authorize", "oauth"),
        ("/token", "oauth"),
        ("/register", "oauth"),
        ("/revoke", "oauth"),
        ("/.well-known/oauth-authorization-server", "oauth"),
        ("/wp-login.php", "other"),
        ("/mcpx", "other"),
    ],
)
def test_every_path_is_in_one_route_group(path, group):
    assert route_group(path) == group


async def test_requests_are_counted_by_group_method_and_status_class(fake):
    ok = lambda: sample("mcp_http_requests_total", route_group="health", method="GET", status_class="2xx")  # noqa: E731
    nf = lambda: sample("mcp_http_requests_total", route_group="other", method="GET", status_class="4xx")  # noqa: E731
    h0, n0 = ok(), nf()
    d0 = sample("mcp_http_request_duration_seconds_count", route_group="health")
    async with web_for(app_for(fake)) as web:
        await web.get("/healthz")
        await web.get("/healthz")
        await web.get("/wp-login.php")
    assert ok() == h0 + 2 and nf() == n0 + 1
    assert sample("mcp_http_request_duration_seconds_count", route_group="health") == d0 + 2


async def test_in_flight_goes_up_while_a_request_runs_and_back_down_after():
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow(scope, receive, send):
        started.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = Instrument(slow)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pod") as c:
        base = sample("mcp_http_requests_in_flight", route_group="mcp")
        t = [asyncio.create_task(c.get("/mcp")) for _ in range(3)]
        await started.wait()
        await asyncio.sleep(0.05)
        assert sample("mcp_http_requests_in_flight", route_group="mcp") == base + 3
        release.set()
        await asyncio.gather(*t)
    assert sample("mcp_http_requests_in_flight", route_group="mcp") == base


async def test_a_request_that_raises_is_counted_as_a_5xx_and_leaves_nothing_in_flight():
    async def broken(scope, receive, send):
        raise RuntimeError("boom")

    before = sample("mcp_http_requests_total", route_group="other", method="GET", status_class="5xx")
    base = sample("mcp_http_requests_in_flight", route_group="other")
    with pytest.raises(RuntimeError):
        await Instrument(broken)({"type": "http", "path": "/x", "method": "GET"}, None, None)
    assert sample("mcp_http_requests_total", route_group="other", method="GET", status_class="5xx") == before + 1
    assert sample("mcp_http_requests_in_flight", route_group="other") == base


async def test_a_refused_token_is_counted(fake):
    tok = lambda: sample("mcp_auth_refusals_total", reason="invalid_token")  # noqa: E731
    before = tok()
    async with web_for(app_for(fake)) as web:
        r = await web.post("/mcp", json={}, headers={"Authorization": "Bearer forged"})
    assert r.status_code == 401 and tok() == before + 1


# ---------------------------------------------------------------- tools and upstream


async def test_tool_calls_are_counted_by_result_and_unknown_names_are_not_labels(make_server, chats):
    server, _ = make_server()
    c = lambda tool, result: sample("mcp_tool_calls_total", tool=tool, result=result)  # noqa: E731
    ok0, refused0, unk0 = c("list_chats", "ok"), c("send_draft", "refused"), c("unknown", "refused")
    up0 = sample("mcp_upstream_requests_total", status_class="2xx")
    await call_tool(server, "list_chats", {})
    r = await call_tool(server, "save_draft", {"chat": GROUP, "text": "hi"})
    await call_tool(server, "send_draft", {"chat": GROUP, "draft": r.structured_content["draft"]})  # no confirmation
    await call_tool(server, "no_such_tool_zz9", {})
    assert c("list_chats", "ok") == ok0 + 1
    assert c("send_draft", "refused") == refused0 + 1
    assert c("unknown", "refused") == unk0 + 1
    assert sample("mcp_upstream_requests_total", status_class="2xx") > up0
    assert sample("mcp_tool_duration_seconds_count", tool="list_chats") >= 1
    assert not any("no_such_tool_zz9" in str(x.labels) for m in REGISTRY.collect() for x in m.samples)


async def test_a_tool_that_crashes_is_a_crash_not_a_refusal(make_server, chats, monkeypatch):
    from vkteams_mcp.api.client import VK

    async def boom(self, *a, **kw):
        raise RuntimeError("not a VKError")

    monkeypatch.setattr(VK, "snapshot", boom)
    server, _ = make_server()
    crash0 = sample("mcp_tool_calls_total", tool="list_chats", result="crash")
    r = await call_tool(server, "list_chats", {})
    assert r.is_error
    assert sample("mcp_tool_calls_total", tool="list_chats", result="crash") == crash0 + 1


async def test_a_failed_upstream_call_is_counted_by_class(make_server, fake):
    server, _ = make_server()
    fake.rapi("getUserInfo", httpx.Response(503, text="down"))
    before = sample("mcp_upstream_requests_total", status_class="5xx")
    await call_tool(server, "get_connection", {})
    assert sample("mcp_upstream_requests_total", status_class="5xx") > before


# ---------------------------------------------------------------- sign-in and guard


async def test_sign_in_outcomes_are_counted(fake):
    n = lambda r: sample("mcp_signin_total", result=r)  # noqa: E731
    ok0, wrong0, limited0, error0 = n("ok"), n("wrong"), n("limited"), n("error")
    async with web_for(app_for(fake)) as web:
        rid = await authorize(web, await register(web))
        vk_sign_in(fake)
        step2 = await web.post(
            "/signin", data={"request": rid, "step": "address", "server": "teams.example.com", "email": ME}
        )
        form = {"request": rid, "step": "code", "pending": pending_of(step2)}
        fake.routes[("POST", "/api/v139/wim/auth/clientLogin")].insert(0, lambda s: {"response": {"statusCode": 330}})
        await web.post("/signin", data={**form, "code": "WRONG1"})
        done = await web.post("/signin", data={**form, "code": "ab12cd"})
        assert done.status_code == 302
        for _ in range(4):  # the fourth request for one address in an hour is limited
            await web.post(
                "/signin", data={"request": rid, "step": "address", "server": "teams.example.com", "email": ME}
            )
        await web.post("/signin", data={"request": rid, "step": "address", "server": "x", "email": "not-an-address"})
        await web.post(
            "/signin", data={"request": rid, "step": "address", "server": "1.2.3.4", "email": "a@example.com"}
        )
    assert n("ok") == ok0 + 1 and n("wrong") == wrong0 + 1
    assert n("limited") >= limited0 + 1 and n("error") >= error0 + 1


async def test_guard_refusals_are_counted_by_reason(make_server, chats):
    from vkteams_mcp.core.errors import VKError

    server, rt = make_server()
    g = lambda r: sample("mcp_guard_refusals_total", reason=r)  # noqa: E731
    confirm0, rate0 = g("confirm"), g("rate")
    with pytest.raises(VKError):
        await rt.guard.check("p", kind="message", user_confirmed=None)
    for _ in range(10):
        await rt.guard.check("p", kind="message", user_confirmed=True)
    with pytest.raises(VKError):
        await rt.guard.check("p", kind="message", user_confirmed=True)
    assert g("confirm") == confirm0 + 1 and g("rate") == rate0 + 1


async def test_link_transfers_are_counted(fake, session):
    app = app_for(fake)
    rt = context.runtime()
    denied0 = sample("mcp_link_transfers_total", op="download", result="denied")
    async with web_for(app) as web:
        assert (await web.get("/files/d/forged")).status_code == 403
        up = rt.links.announce_uploads(session, GROUP, ["a.txt"])[0]
        fake.on(
            "GET", "/api/v139/files/init", {"status": {"code": 200}, "result": {"host": "x.example.com", "url": "/u"}}
        )
        assert (await web.put(up["url"].removeprefix(PUBLIC), content=b"")).status_code == 400
    assert sample("mcp_link_transfers_total", op="download", result="denied") == denied0 + 1
    assert sample("mcp_link_transfers_total", op="upload", result="error") >= 1


# ---------------------------------------------------------------- what labels never carry


async def test_no_label_carries_a_person_an_address_a_token_or_an_argument(fake, make_server, chats):
    async with web_for(app_for(fake)) as web:
        rid = await authorize(web, await register(web))
        vk_sign_in(fake)
        step2 = await web.post(
            "/signin", data={"request": rid, "step": "address", "server": "teams.example.com", "email": ME}
        )
        await web.post("/signin", data={"request": rid, "step": "code", "pending": pending_of(step2), "code": "ab12cd"})
    server, _ = make_server()
    await call_tool(server, "get_chat", {"chat": "Project Alpha"})
    await call_tool(server, "list_chats", {"query": "confidential"})
    exposition = "\n".join(
        f"{s.name}{s.labels}" for m in REGISTRY.collect() if m.name.startswith("mcp_") for s in m.samples
    )
    assert "mcp_http_requests_total" in exposition  # the scan read something
    for secret in (
        ME,
        "Test Person",
        "Project Alpha",
        "confidential",
        "teams.example.com",
        "token-a",
        "aimsid",
        "127.0.0.1",
    ):
        assert secret not in exposition, secret
    assert text_of(await call_tool(server, "list_chats", {}))  # the harness itself works
