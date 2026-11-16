"""Finding a VK Teams installation from an address a stranger typed.

The address is untrusted (manifesto rule 19). It becomes a host only if it is a plain DNS name, the
name resolves to public addresses (unless the operator allows private ones), and the host serves a
VK Teams `myteam-config.json` whose API hosts pass the same checks. Redirects are not followed.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from vkteams_mcp.core.errors import invalid

_HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_MAX_CONFIG_BYTES = 1_000_000


@dataclass(frozen=True)
class Policy:
    allow_private: bool = False
    installations: frozenset[str] = field(default_factory=frozenset)  # empty = any public installation


@dataclass(frozen=True)
class Installation:
    host: str
    binary_host: str
    api_version: int


def _refuse(why: str):
    return invalid(f"{why} Type the address of your VK Teams server, for example the host you open VK Teams on.")


def normalise(typed: str) -> str:
    """A typed address → a lower-case DNS host name, or a refusal. https only, no port, no user info."""
    t = (typed or "").strip()
    if not t or len(t) > 260:
        raise _refuse("That is not a server address.")
    if "://" not in t:
        t = "https://" + t
    try:
        u = urlsplit(t)
    except ValueError:
        raise _refuse("That is not a server address.") from None
    if u.scheme != "https":
        raise _refuse("Only https addresses are accepted.")
    if u.username or u.password or "@" in u.netloc:
        raise _refuse("That is not a server address.")
    try:
        if u.port is not None:
            raise _refuse("Give the address without a port.")
    except ValueError:
        raise _refuse("That is not a server address.") from None
    host = (u.hostname or "").rstrip(".").lower()
    try:
        ipaddress.ip_address(host)
        raise _refuse("Give a name, not an IP address.")
    except ValueError:
        pass
    if not _HOSTNAME.match(host):
        raise _refuse("That is not a server address.")
    return host


async def system_resolve(host: str) -> list[str]:
    """DNS off the event loop: a slow resolver must not stall other requests."""
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(loop.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP), timeout=10)
    return sorted({ai[4][0] for ai in infos})


async def _check_public(host: str, policy: Policy, resolve) -> None:
    if policy.allow_private:
        return
    try:
        addrs = resolve(host)
        if inspect.isawaitable(addrs):
            addrs = await addrs
    except (OSError, TimeoutError):
        raise _refuse("That name does not resolve.") from None
    if not addrs:
        raise _refuse("That name does not resolve.")
    for a in addrs:
        ip = ipaddress.ip_address(a)
        if not ip.is_global or ip.is_multicast:
            raise _refuse("That name points to a private network, which this server does not reach.")


async def _api_host(url: object, policy: Policy, resolve) -> str:
    if not isinstance(url, str) or not url.startswith("https://"):
        raise _refuse("The server's configuration does not name an https API address.")
    host = normalise(url)
    await _check_public(host, policy, resolve)
    return host


async def discover(
    typed: str, http: httpx.AsyncClient, policy: Policy, resolve: Callable[[str], list[str]] = system_resolve
) -> Installation:
    host = normalise(typed)
    if policy.installations and host not in policy.installations:
        raise _refuse("This connector serves only the VK Teams installations its operator listed.")
    await _check_public(host, policy, resolve)
    body = bytearray()
    try:
        # Streamed with a cap: the address is a stranger's, and so is the size of its answer.
        async with http.stream("GET", f"https://{host}/myteam-config.json", follow_redirects=False, timeout=10) as r:
            if r.status_code != 200:
                raise _refuse("That address is not a VK Teams server.")
            async for chunk in r.aiter_bytes():
                body.extend(chunk)
                if len(body) > _MAX_CONFIG_BYTES:
                    raise _refuse("That address is not a VK Teams server.")
    except httpx.HTTPError:
        raise _refuse("That server did not answer.") from None
    try:
        cfg = json.loads(body)
    except ValueError:
        raise _refuse("That address is not a VK Teams server.") from None
    version = cfg.get("api-version") if isinstance(cfg, dict) else None
    urls = cfg.get("api-urls") if isinstance(cfg, dict) else None
    if not isinstance(version, int) or not isinstance(urls, dict):
        raise _refuse("That address is not a VK Teams server.")
    main = await _api_host(urls.get("main-api"), policy, resolve)
    binary = await _api_host(urls.get("main-binary-api") or urls.get("main-api"), policy, resolve)
    return Installation(host=main, binary_host=binary, api_version=version)
