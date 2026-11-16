"""Every outbound connection is checked when it is made, not only at sign-in (DNS rebinding)."""

from __future__ import annotations

import httpx
import pytest

from vkteams_mcp.api.egress import PrivateAddress, PublicOnlyTransport


class Recorder(httpx.AsyncBaseTransport):
    def __init__(self):
        self.seen = []

    async def handle_async_request(self, request):
        self.seen.append(request)
        return httpx.Response(200, json={"ok": True})


async def test_a_name_that_now_resolves_privately_is_refused_at_connect_time():
    answers = iter(["93.184.215.14", "10.0.0.5"])  # public at sign-in, private later: rebinding

    async def resolve(host):
        ip = next(answers)
        if ip.startswith("10."):
            raise PrivateAddress("private")
        return ip

    inner = Recorder()
    async with httpx.AsyncClient(transport=PublicOnlyTransport(inner, resolve=resolve)) as c:
        assert (await c.get("https://teams.example.com/x")).status_code == 200
        with pytest.raises(httpx.ConnectError):
            await c.get("https://teams.example.com/x")
    assert len(inner.seen) == 1


async def test_the_connection_goes_to_the_checked_address_with_the_name_for_tls():
    async def resolve(host):
        return "93.184.215.14"

    inner = Recorder()
    async with httpx.AsyncClient(transport=PublicOnlyTransport(inner, resolve=resolve)) as c:
        await c.get("https://teams.example.com/api/v139/x?y=1")
    r = inner.seen[0]
    assert r.url.host == "93.184.215.14" and r.url.path == "/api/v139/x" and r.url.query == b"y=1"
    assert r.headers["Host"] == "teams.example.com" and r.extensions["sni_hostname"] == "teams.example.com"


async def test_an_ip_literal_is_refused():
    inner = Recorder()
    async with httpx.AsyncClient(transport=PublicOnlyTransport(inner)) as c:
        with pytest.raises(httpx.ConnectError):
            await c.get("https://10.0.0.1/")
    assert inner.seen == []


async def test_a_name_resolving_to_loopback_is_refused():
    from vkteams_mcp.api.egress import resolve_public

    with pytest.raises(PrivateAddress):
        await resolve_public("localhost")
