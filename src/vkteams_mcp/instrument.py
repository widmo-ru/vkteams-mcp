"""What the HTTP side reports: requests in flight, by route group, and the separate port metrics are served on.

`Instrument` wraps the whole app, so it sees every request, including the ones the MCP SDK answers itself
(`/token`, `/authorize`, a 401 on `/mcp`). The metrics are served by a second, tiny ASGI app on
`VKTEAMS_MCP_METRICS_PORT`: the public port has no `/metrics`, so a scrape needs the pod's address and an
ingress cannot publish it by accident.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator

import uvicorn

from vkteams_mcp import observability as obs

_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"})
_OAUTH = ("/authorize", "/token", "/register", "/revoke", "/.well-known/")


def route_group(path: str) -> str:
    if path == "/mcp" or path.startswith("/mcp/"):
        return "mcp"
    if path in ("/healthz", "/readyz"):
        return "health"
    if path == "/signin" or path.startswith("/static/"):
        return "signin"
    if path.startswith("/files/"):
        return "links"
    if path.startswith(_OAUTH):
        return "oauth"
    return "other"


class Instrument:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        group = route_group(scope["path"])
        method = scope["method"] if scope["method"] in _METHODS else "OTHER"
        gauge = obs.HTTP_IN_FLIGHT.labels(obs.SERVER, group)
        status = 500  # what a request that raised before answering counts as
        gauge.inc()
        t0 = time.monotonic()

        async def counted(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, counted)
        finally:
            gauge.dec()
            obs.HTTP_REQUESTS.labels(obs.SERVER, group, method, f"{status // 100}xx").inc()
            obs.HTTP_SECONDS.labels(obs.SERVER, group).observe(time.monotonic() - t0)
            if status == 401:
                obs.inc(obs.AUTH_REFUSALS, "invalid_token" if group == "mcp" else "invalid_client")


async def metrics_app(scope, receive, send) -> None:
    """`/metrics` and the probes `/healthz` and `/readyz`: this port is the one
    the kubelet and Prometheus use, and no Service or Ingress publishes it. No lifespan: the server is started with
    `lifespan="off"`. Readiness is "the key and the configuration are loaded", which is true of any process that got
    this far; it does NOT ask the state store, so a store outage cannot empty the Service."""
    if scope["type"] != "http":
        return
    path = scope["path"]
    if path == "/metrics" and scope["method"] in ("GET", "HEAD"):
        body, ctype = obs.scrape()
        status = 200
    elif path in ("/healthz", "/readyz") and scope["method"] in ("GET", "HEAD"):
        body, ctype, status = b'{"ok":true}', "application/json", 200
    else:
        body, ctype, status = b"Not found\n", "text/plain; charset=utf-8", 404
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", ctype.encode()), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body if scope["method"] != "HEAD" else b""})


class _Follower(uvicorn.Server):
    """A server that leaves signals to the main one: it stops when told to (`should_exit`)."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def metrics_server(port: int) -> uvicorn.Server:
    return _Follower(
        uvicorn.Config(
            metrics_app,
            host="0.0.0.0",  # noqa: S104 - a container listens on all interfaces
            port=port,
            log_level="warning",
            access_log=False,
            log_config=None,
            lifespan="off",
        )
    )
