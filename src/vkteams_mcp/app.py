"""The process: one Starlette app with the MCP endpoint, OAuth, the sign-in page and file links. It keeps
no state between requests (`core.seal`), so any number of replicas sharing one sealing key serve it.

VKTEAMS_MCP_PUBLIC_URL=https://vkteams-mcp.example.org VKTEAMS_MCP_KEY_FILE=/run/vkteams-mcp/key vkteams-mcp

In Kubernetes an init container puts the key file there (`config`); the process makes itself non-dumpable.
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

from vkteams_mcp.api.client import VK
from vkteams_mcp.api.egress import PublicOnlyTransport
from vkteams_mcp.api.session import VKSession
from vkteams_mcp.config import ConfigError, Settings, make_undumpable
from vkteams_mcp.core import context
from vkteams_mcp.links import routes as link_routes
from vkteams_mcp.links.links import Links
from vkteams_mcp.oauth.provider import Provider
from vkteams_mcp.server import VKServer
from vkteams_mcp.signin import AuthorizeErrorPage, SignIn, static

log = logging.getLogger("vkteams_mcp")


def build(settings: Settings, http: httpx.AsyncClient | None = None) -> tuple[Starlette, VKServer, Provider]:
    if http is None:
        # Inside a company network the operator allows private addresses; everywhere else every connection is
        # checked when it is made, so a name that rebinds to a private address after sign-in is refused.
        transport = None if settings.policy.allow_private else PublicOnlyTransport()
        http = httpx.AsyncClient(timeout=30, transport=transport)
    rt = context.Runtime(settings=settings, http=http)
    rt.links = Links(settings.public_url, rt.sealer)
    context.install(rt)

    async def end_session(session: VKSession) -> None:
        await VK(session, rt.http).end()

    provider = Provider(
        rt.sealer, settings.secret, public_url=settings.public_url, latest=rt.latest, end_session=end_session
    )
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
    app.add_middleware(AuthorizeErrorPage)
    return app, server, provider


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO, format='{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}', style="%"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
    except ConfigError as err:  # one line that says what to do, not a traceback
        log.error("%s", err)
        raise SystemExit(2) from None
    if not make_undumpable():
        log.warning("the process could not be made non-dumpable: its memory is readable to its own user")
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
