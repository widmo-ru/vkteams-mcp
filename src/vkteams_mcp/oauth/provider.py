"""OAuth 2.1 authorization server for MCP clients, in front of a VK Teams sign-in.

`authorize` stores the client's parameters and sends the browser to our sign-in page; the page
proves the person by the code VK Teams e-mails (`api.login`) and calls `complete_sign_in`, which
issues a one-time authorization code back to the client. PKCE is verified by the SDK's token
handler. Access tokens live one hour; refresh tokens rotate, and a refresh retires the whole
previous pair (one "family" per sign-in).
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from vkteams_mcp.api.session import VKSession
from vkteams_mcp.oauth.store import Store

ACCESS_TTL = 3600
REFRESH_TTL = 90 * 24 * 3600
CODE_TTL = 300
SIGN_IN_TTL = 15 * 60


class Provider:
    def __init__(self, store: Store, *, public_url: str, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.public_url = public_url.rstrip("/")
        self._clock = clock

    # ---------------------------------------------------------------- clients
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = self.store.client(client_id)
        return OAuthClientInformationFull.model_validate_json(info) if info else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.save_client(client_info.client_id, client_info.model_dump_json(), self._clock())

    # ---------------------------------------------------------------- authorize → sign-in page
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self.store.purge(self._clock())  # expired requests, codes, tokens and drafts do not pile up
        request_id = secrets.token_urlsafe(32)
        self.store.save_request(
            request_id,
            client.client_id,
            {
                "state": params.state,
                "scopes": params.scopes or [],
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": params.resource,
            },
            self._clock() + SIGN_IN_TTL,
        )
        return f"{self.public_url}/signin?request={request_id}"

    async def sign_in_context(self, request_id: str) -> dict | None:
        """What the sign-in page shows and allows: the client's name and where it redirects."""
        info = self.store.request_info(request_id, self._clock())
        if info is None:
            return None
        client_id, p = info
        client = await self.get_client(client_id)
        u = urlsplit(p["redirect_uri"])
        return {
            "client_name": (client.client_name if client else None) or "An AI app",
            "redirect_origin": f"{u.scheme}://{u.netloc}",
        }

    def sign_in_pending(self, request_id: str) -> bool:
        return self.store.peek_request(request_id, self._clock())

    async def complete_sign_in(self, request_id: str, session: VKSession) -> str:
        """The person proved themselves: seal their session and hand the client a one-time code."""
        taken = self.store.take_request(request_id, self._clock())
        if taken is None:
            raise LookupError("sign-in request unknown, used or expired")
        client_id, p = taken
        person = self.store.save_session(session)
        code = secrets.token_urlsafe(32)
        self.store.save_code(code, client_id, person, p, self._clock() + CODE_TTL)
        return construct_redirect_uri(p["redirect_uri"], code=code, state=p["state"])

    # ---------------------------------------------------------------- codes
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = self.store.code(authorization_code)
        if row is None:
            return None
        client_id, person, p, expires = row
        if client_id != client.client_id or expires < self._clock():
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=p["scopes"],
            expires_at=expires,
            client_id=client_id,
            code_challenge=p["code_challenge"],
            redirect_uri=AnyUrl(p["redirect_uri"]),
            redirect_uri_provided_explicitly=p["redirect_uri_provided_explicitly"],
            resource=p["resource"],
            subject=person,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self.store.delete_code(authorization_code.code)
        return self._issue(
            client.client_id,
            authorization_code.subject or "",
            authorization_code.scopes,
            authorization_code.resource,
            family=secrets.token_hex(16),
        )

    # ---------------------------------------------------------------- tokens
    def _issue(self, client_id: str, person: str, scopes: list[str], resource: str | None, family: str) -> OAuthToken:
        now = self._clock()
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.store.save_token(access, "access", client_id, person, family, scopes, resource, now + ACCESS_TTL)
        self.store.save_token(refresh, "refresh", client_id, person, family, scopes, resource, now + REFRESH_TTL)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=refresh,
            scope=" ".join(scopes) or None,
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        used = self.store.token(refresh_token, "used_refresh")
        if used is not None and used["client_id"] == client.client_id:
            # A refresh token used twice was stolen, or its client is broken: either way the whole chain
            # goes, so a thief who refreshed first loses what they got (OAuth 2.1 refresh token reuse).
            self.store.revoke_family(used["family"])
            return None
        row = self.store.token(refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id or row["expires"] < self._clock():
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=int(row["expires"]),
            resource=row["resource"],
            subject=row["person_id"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        row = self.store.token(refresh_token.token, "refresh")
        if row is None:
            raise LookupError("refresh token unknown")
        # The old pair dies, the chain keeps its family, and the used refresh token is remembered so that a
        # second use of it revokes the chain.
        self.store.revoke_family(row["family"])
        self.store.save_token(
            refresh_token.token,
            "used_refresh",
            row["client_id"],
            row["person_id"],
            row["family"],
            row["scopes"],
            row["resource"],
            self._clock() + REFRESH_TTL,
        )
        return self._issue(
            client.client_id, row["person_id"], scopes or row["scopes"], row["resource"], family=row["family"]
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self.store.token(token, "access")
        if row is None or row["expires"] < self._clock():
            return None
        if self.store.load_session(row["person_id"]) is None:
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=int(row["expires"]),
            resource=row["resource"],
            subject=row["person_id"],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        for kind in ("access", "refresh"):
            row = self.store.token(token.token, kind)
            if row is not None:
                self.store.revoke_family(row["family"])
