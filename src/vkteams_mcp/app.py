"""The process: one Starlette app with the MCP endpoint, OAuth, the sign-in page and file links.

VKTEAMS_MCP_PUBLIC_URL=https://vkteams-mcp.example.org VKTEAMS_MCP_SECRET=... vkteams-mcp
"""

from __future__ import annotations

import contextlib
import logging
from urllib.parse import urlsplit

import httpx
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from vkteams_mcp.api.egress import PublicOnlyTransport
from vkteams_mcp.config import Settings
from vkteams_mcp.core import context
from vkteams_mcp.links import routes as link_routes
from vkteams_mcp.links.links import Links
from vkteams_mcp.oauth.provider import Provider
from vkteams_mcp.oauth.store import Store
from vkteams_mcp.server import VKServer
from vkteams_mcp.signin import SignIn, static

log = logging.getLogger("vkteams_mcp")


def build(settings: Settings, http: httpx.AsyncClient | None = None) -> tuple[Starlette, VKServer, Provider]:
    store = Store(settings.database, settings.secret)
    if http is None:
        # Inside a company network the operator allows private addresses; everywhere else every connection is
        # checked when it is made, so a name that rebinds to a private address after sign-in is refused.
        transport = None if settings.policy.allow_private else PublicOnlyTransport()
        http = httpx.AsyncClient(timeout=30, transport=transport)
    rt = context.Runtime(settings=settings, store=store, http=http)
    rt.links = Links(settings.public_url)
    context.install(rt)
    provider = Provider(store, public_url=settings.public_url)
    auth = AuthSettings(
        issuer_url=settings.public_url,
        resource_server_url=f"{settings.public_url}/mcp",
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
        validate_token_resource=False,
    )
    server = VKServer(settings.areas, auth=auth, auth_server_provider=provider)
    signin = SignIn(provider)

    server.custom_route("/signin", methods=["GET"])(signin.page)
    server.custom_route("/signin", methods=["POST"])(signin.submit)
    server.custom_route("/static/{path:path}", methods=["GET"])(static)
    server.custom_route("/files/d/{lid}", methods=["GET"])(link_routes.download)
    server.custom_route("/files/u/{lid}", methods=["PUT", "POST"])(link_routes.upload)

    async def healthz(_: Request) -> Response:
        return JSONResponse({"ok": True})

    server.custom_route("/healthz", methods=["GET"])(healthz)
    host = urlsplit(settings.public_url).netloc
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=[host], allowed_origins=[settings.public_url]
        ),
    )
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):  # the outbound connection pool closes with the app
        async with inner(a) as state:
            try:
                yield state
            finally:
                await http.aclose()

    app.router.lifespan_context = lifespan
    return app, server, provider


def main() -> None:
    import os

    import uvicorn

    os.umask(0o077)  # the database and its WAL files are the operator's alone

    logging.basicConfig(
        level=logging.INFO, format='{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}', style="%"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_env()
    app, _, _ = build(settings)
    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 - a container listens on all interfaces
        port=settings.port,
        log_level="warning",
        access_log=False,  # access logs carry query strings
        log_config=None,
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
