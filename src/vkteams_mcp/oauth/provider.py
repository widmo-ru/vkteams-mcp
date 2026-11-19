"""OAuth 2.1 authorization server for MCP clients, in front of a VK Teams sign-in. It stores nothing,
except one mark in the shared state store (`core.state`): that an authorization code was redeemed.

Every artefact of the flow is a sealed blob (`core.seal`) that carries what the next step needs:

| Artefact | Carries | Lives |
|---|---|---|
| `client_id` | the client's registration | as long as the client keeps it |
| sign-in request | the client and its authorization parameters | 15 minutes, in the sign-in URL |
| authorization code | the parameters and the person's VK Teams session | 5 minutes |
| access token | the session | 1 hour |
| refresh token | the session | 30 days |

The client refreshes its tokens itself, by the standard; a refresh hands back new tokens with the
freshest session this replica knows. Any number of replicas serve the same tokens if they share the
secret, and a code redeemed on one replica is refused on every other.
Revoking a token ends the VK Teams session itself, which kills every token that carries it.
"""

from __future__ import annotations

import os
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from vkteams_mcp import observability
from vkteams_mcp.api.session import VKSession
from vkteams_mcp.core.seal import Sealer, person_id
from vkteams_mcp.core.state import MemoryStore, StateStore

ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
CODE_TTL = 300
SIGN_IN_TTL = 15 * 60
CLIENT_TTL = 10 * 365 * 24 * 3600
CODE_MARK_SLACK = 5  # seconds the mark outlives the code: replicas' clocks are not the same clock


class Provider:
    def __init__(
        self,
        sealer: Sealer,
        secret: str,
        *,
        public_url: str,
        latest: Callable[[str, VKSession], VKSession] = lambda _p, s: s,
        end_session: Callable[[VKSession], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.time,
        store: StateStore | None = None,
    ) -> None:
        self.sealer = sealer
        self._secret = secret
        self.public_url = public_url.rstrip("/")
        self._latest = latest  # the freshest session this replica has seen for a person
        self._end_session = end_session
        self._clock = clock
        # A code is single-use across every replica: the first exchange marks it in the shared store.
        self.store: StateStore = store or MemoryStore(os.urandom(32))

    def person(self, session: VKSession) -> str:
        return person_id(session.host, session.login, self._secret)

    # ---------------------------------------------------------------- clients
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        body = self.sealer.open("client", client_id)
        if body is None:
            return None
        return OAuthClientInformationFull.model_validate({**body["info"], "client_id": client_id})

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """The registration becomes the client id: the SDK answers with this same object."""
        info = client_info.model_dump(mode="json", exclude={"client_id"}, exclude_none=True)
        client_info.client_id = self.sealer.seal("client", {"info": info}, CLIENT_TTL)

    # ---------------------------------------------------------------- authorize → sign-in page
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        request = self.sealer.seal(
            "signin",
            {
                "client_id": client.client_id,
                "state": params.state,
                "scopes": params.scopes or [],
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": params.resource,
            },
            SIGN_IN_TTL,
        )
        return f"{self.public_url}/signin?request={request}"

    def _request(self, request_id: str) -> dict | None:
        return self.sealer.open("signin", request_id)

    async def sign_in_context(self, request_id: str) -> dict | None:
        """What the sign-in page shows and allows: where the browser goes back to."""
        p = self._request(request_id)
        if p is None:
            return None
        u = urlsplit(p["redirect_uri"])
        return {"redirect_origin": f"{u.scheme}://{u.netloc}"}

    async def complete_sign_in(self, request_id: str, session: VKSession) -> str:
        """The person proved themselves: hand the client a one-time code that carries their session."""
        p = self._request(request_id)
        if p is None:
            raise LookupError("sign-in request unknown or expired")
        code = self.sealer.seal("code", {**p, "s": session.to_json()}, CODE_TTL)
        return construct_redirect_uri(p["redirect_uri"], code=code, state=p["state"])

    # ---------------------------------------------------------------- codes
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        p = self.sealer.open("code", authorization_code)
        if p is None or p["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=p["scopes"],
            expires_at=p["exp"],
            client_id=client.client_id,
            code_challenge=p["code_challenge"],
            redirect_uri=AnyUrl(p["redirect_uri"]),
            redirect_uri_provided_explicitly=p["redirect_uri_provided_explicitly"],
            resource=p["resource"],
            subject=p["s"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """The SDK has checked the code and the PKCE verifier; this is where the code is spent. The mark is set
        before anything is issued, so two exchanges of one code, on one replica or on two, cannot both win. A
        store that cannot be asked raises StateUnavailable: the code stays unspent and the client may try again."""
        left = max(1.0, authorization_code.expires_at - self._clock()) + CODE_MARK_SLACK
        if not await self.store.once("code", authorization_code.code, left):
            observability.inc(observability.AUTH_REFUSALS, "code_reused")
            raise TokenError("invalid_grant", "authorization code was already used")
        session = VKSession.from_json(authorization_code.subject or "")
        return self._issue(client.client_id, session, authorization_code.scopes, authorization_code.resource)

    # ---------------------------------------------------------------- tokens
    def _issue(self, client_id: str, session: VKSession, scopes: list[str], resource: str | None) -> OAuthToken:
        body = {"client_id": client_id, "scopes": scopes, "resource": resource, "s": session.to_json()}
        return OAuthToken(
            access_token=self.sealer.seal("access", body, ACCESS_TTL),
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=self.sealer.seal("refresh", body, REFRESH_TTL),
            scope=" ".join(scopes) or None,
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        p = self.sealer.open("refresh", refresh_token)
        if p is None or p["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=p["scopes"],
            expires_at=p["exp"],
            resource=p["resource"],
            subject=p["s"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        session = VKSession.from_json(refresh_token.subject or "")
        session = self._latest(self.person(session), session)
        return self._issue(client.client_id, session, scopes or refresh_token.scopes, refresh_token.resource)

    def session_of(self, access_token: str) -> VKSession | None:
        """The VK Teams session an access token carries, or None for anything not a live access token."""
        p = self.sealer.open("access", access_token)
        return VKSession.from_json(p["s"]) if p else None

    async def load_access_token(self, token: str) -> AccessToken | None:
        p = self.sealer.open("access", token)
        if p is None:
            return None
        session = VKSession.from_json(p["s"])
        return AccessToken(
            token=token,
            client_id=p["client_id"],
            scopes=p["scopes"],
            expires_at=p["exp"],
            resource=p["resource"],
            subject=self.person(session),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Disconnect: end the VK Teams session the token carries, so no token carrying it works again."""
        p = self.sealer.open("access", token.token) or self.sealer.open("refresh", token.token)
        if p is not None and self._end_session is not None:
            await self._end_session(VKSession.from_json(p["s"]))
