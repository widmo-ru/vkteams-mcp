"""Outbound requests that cannot be pointed at a private network after the fact.

Checking a host once at sign-in is not enough: the name is resolved again for every connection, and
a domain its owner controls can answer a public address to the check and a private one to the
connection (DNS rebinding). This transport resolves each request's host itself, refuses non-public
addresses, and connects to the checked address, keeping the name for TLS (SNI and certificate) and
the Host header.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpx


class PrivateAddress(httpx.ConnectError):
    pass


async def resolve_public(host: str) -> str:
    """The first address of `host`, if every address it resolves to is public; else PrivateAddress."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise httpx.ConnectError(f"cannot resolve: {type(e).__name__}") from None
    addrs = sorted({i[4][0] for i in infos})
    for a in addrs:
        ip = ipaddress.ip_address(a)
        if not ip.is_global or ip.is_multicast:
            raise PrivateAddress("refused: the name resolves to a private network")
    if not addrs:
        raise httpx.ConnectError("cannot resolve")
    return addrs[0]


class PublicOnlyTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport | None = None, resolve=resolve_public) -> None:
        self._fixed = inner  # tests pass one; otherwise one pool per host name, so a TLS connection made
        self._pools: dict[str, httpx.AsyncBaseTransport] = {}  # for one name is never reused for another
        self._resolve = resolve

    def _inner_for(self, host: str) -> httpx.AsyncBaseTransport:
        if self._fixed is not None:
            return self._fixed
        if host not in self._pools:
            self._pools[host] = httpx.AsyncHTTPTransport()
        return self._pools[host]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        try:
            ipaddress.ip_address(host)
            raise PrivateAddress("refused: an address, not a name")
        except ValueError:
            pass
        ip = await self._resolve(host)
        request.url = request.url.copy_with(host=ip)
        request.headers["Host"] = host if request.url.port in (None, 443) else f"{host}:{request.url.port}"
        request.extensions = {**request.extensions, "sni_hostname": host}
        return await self._inner_for(host).handle_async_request(request)

    async def aclose(self) -> None:
        for t in [self._fixed, *self._pools.values()]:
            if t is not None:
                await t.aclose()
