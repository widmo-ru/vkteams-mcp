"""The process: one Starlette app with the MCP endpoint, OAuth, the sign-in page and file links. Authorization
is stateless (`core.seal`); the few counters that must agree between replicas (a used code, sign-in limits,
the send allowance, an upload lock) live in the shared state store (`core.state`), so any number of replicas
sharing one sealing key and one store serve it.

VKTEAMS_MCP_PUBLIC_URL=https://vkteams-mcp.example.org VKTEAMS_MCP_KEY_FILE=/run/vkteams-mcp/key \
VKTEAMS_MCP_STATE_URL=redis://valkey:6379/0 vkteams-mcp

In Kubernetes an init container puts the key file (and the store's passwords) there; the process makes itself
non-dumpable. Logs are JSON, one object per line; metrics are on VKTEAMS_MCP_METRICS_PORT, never the public port.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from urllib.parse import urlsplit

import httpx
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from vkteams_mcp import __version__
from vkteams_mcp import observability as obs
from vkteams_mcp.api.client import VK
from vkteams_mcp.api.egress import PublicOnlyTransport
from vkteams_mcp.api.session import VKSession
from vkteams_mcp.config import ConfigError, Settings, make_undumpable
from vkteams_mcp.core import context
from vkteams_mcp.core.state import StateStore, StateUnavailable, build_store
from vkteams_mcp.instrument import Instrument, metrics_server
from vkteams_mcp.links import routes as link_routes
from vkteams_mcp.links.links import Links
from vkteams_mcp.oauth.provider import Provider
from vkteams_mcp.server import VKServer
from vkteams_mcp.signin import AuthorizeErrorPage, SignIn, static

log = logging.getLogger("vkteams_mcp")
PROBE_EVERY = 5  # seconds between the state store's own heartbeats, so `mcp_state_store_up` is true to the minute


def build(
    settings: Settings, http: httpx.AsyncClient | None = None, store: StateStore | None = None
) -> tuple[Starlette, VKServer, Provider]:
    """The app. `store` is for tests that need to bring the store down and up; a deployment's comes from settings."""
    if http is None:
        # Inside a company network the operator allows private addresses; everywhere else every connection is
        # checked when it is made, so a name that rebinds to a private address after sign-in is refused.
        verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=settings.ca_bundle) if settings.ca_bundle else True
        )
        transport = None if settings.policy.allow_private else PublicOnlyTransport(verify=verify)
        http = httpx.AsyncClient(timeout=30, transport=transport, verify=verify)
    store = store or build_store(settings.state, settings.secret)
    rt = context.Runtime(settings=settings, http=http, store=store)
    rt.links = Links(settings.public_url, rt.sealer, store=store)
    context.install(rt)

    async def end_session(session: VKSession) -> None:
        await VK(session, rt.http).end()

    provider = Provider(
        rt.sealer,
        settings.secret,
        public_url=settings.public_url,
        latest=rt.latest,
        end_session=end_session,
        store=store,
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
        """Liveness: the process answers."""
        return JSONResponse({"ok": True})

    async def readyz(_: Request) -> Response:
        """Readiness: the key and the configuration are loaded, which is true of any process that got this far. It
        does not ask the state store: with the store down, sign-in and sending refuse in code, `mcp_state_store_up` is 0
        and an alert fires, while reading goes on. A readiness that followed the store would take every pod out of
        the Service at once, for every person, for a failure that only some tools feel."""
        return JSONResponse({"ok": True})

    server.custom_route("/healthz", methods=["GET"])(healthz)
    server.custom_route("/readyz", methods=["GET"])(readyz)
    host = urlsplit(settings.public_url).netloc
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=[host], allowed_origins=[settings.public_url]
        ),
    )
    inner = app.router.lifespan_context

    async def heartbeat() -> None:
        """Asks the store every few seconds, so the gauge and the pod's readiness follow it with no traffic."""
        while True:
            with contextlib.suppress(StateUnavailable, TimeoutError):
                await asyncio.wait_for(store.ping(), 3)
            await asyncio.sleep(PROBE_EVERY)

    @contextlib.asynccontextmanager
    async def lifespan(a):  # the outbound connection pool and the store's connections close with the app
        beat = asyncio.create_task(heartbeat())
        try:
            async with inner(a) as state:
                yield state
        finally:
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
            await http.aclose()
            await store.close()

    async def store_down(_: Request, __: Exception) -> Response:
        """The store was needed (a code to spend, a limit to count) and did not answer: ask again shortly."""
        return JSONResponse(
            {"error": "temporarily_unavailable", "error_description": "Try again shortly."},
            status_code=503,
            headers={"Retry-After": "5"},
        )

    app.router.lifespan_context = lifespan
    app.add_exception_handler(StateUnavailable, store_down)
    app.add_middleware(AuthorizeErrorPage)
    app.add_middleware(Instrument)  # outermost: it sees every request, including the ones the SDK answers
    return app, server, provider


async def serve(app: Starlette, settings: Settings) -> None:
    """The public server and the metrics server, in one event loop. Signals stop the public one; the metrics
    server follows it, so a scrape works until the last request is done."""
    import uvicorn

    public = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",  # noqa: S104 - a container listens on all interfaces
            port=settings.port,
            log_level=obs.log_level().lower(),
            access_log=False,  # access logs carry query strings
            log_config=None,
            # only the listed proxies may say who the client is (X-Forwarded-For); anyone else's header is ignored
            proxy_headers=bool(settings.trusted_proxies),
            forwarded_allow_ips=",".join(settings.trusted_proxies) or None,
        )
    )
    metrics = metrics_server(settings.metrics_port)
    asyncio.get_running_loop().set_exception_handler(obs.asyncio_handler)
    scraper = asyncio.create_task(metrics.serve())
    try:
        await public.serve()
    finally:
        metrics.should_exit = True
        await asyncio.gather(scraper, return_exceptions=True)


def main() -> None:
    obs.configure_logging()
    try:
        settings = Settings.from_env()
    except ConfigError as err:  # one JSON line that says what to do, not a traceback
        obs.event(log, logging.ERROR, "config_error", str(err))
        raise SystemExit(2) from None
    dumpable = make_undumpable()
    if dumpable is False:  # on Linux the protection is promised, so no protection is no start
        obs.event(
            log,
            logging.ERROR,
            "not_dumpable",
            "the process could not be made non-dumpable (prctl PR_SET_DUMPABLE refused): not starting",
        )
        raise SystemExit(2)
    if dumpable is None:
        obs.event(
            log,
            logging.WARNING,
            "dumpable",
            "not Linux: the process is not made non-dumpable, its memory is readable to its own user",
        )
    if not settings.trusted_proxies:
        obs.event(
            log,
            logging.WARNING,
            "network_limit_off",
            "no VKTEAMS_MCP_TRUSTED_PROXIES: the sign-in limit per client network is OFF, because behind an ingress "
            "every request comes from the ingress's address and would share one bucket. The limit per address "
            "stays. List the ingress controller's CIDRs to turn the limit per network on",
        )
    if not settings.state.shared:
        obs.event(
            log,
            logging.WARNING,
            "state_memory",
            "no VKTEAMS_MCP_STATE_URL or VKTEAMS_MCP_STATE_SENTINELS: used codes, sign-in limits and the send "
            "allowance are kept in this process, which is right for ONE replica only. With more, a code can be "
            "redeemed twice and every limit is multiplied by the number of replicas",
            mode="memory",
        )
    app, _, _ = build(settings)
    obs.event(
        log,
        logging.INFO,
        "start",
        f"vkteams-mcp {__version__} serving {settings.public_url}",
        mode=settings.state.mode,
        version=__version__,
    )
    asyncio.run(serve(app, settings))


if __name__ == "__main__":
    main()
