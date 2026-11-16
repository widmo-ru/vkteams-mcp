"""Settings, from the environment only. Nothing here names an installation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from vkteams_mcp.api.install import Policy
from vkteams_mcp.registry import parse_areas

# The VK Teams desktop client's public developer id, as VK publishes it in the open-source im-desktop
# (products/myteam/config.json). An operator with an id of their own sets VKTEAMS_MCP_DEV_ID.
DEFAULT_DEV_ID = "ic1DcTU-u6i6jrDP"


class ConfigError(ValueError):
    pass


def _zone(name: str) -> str:
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(name)
    except Exception:
        raise ConfigError(f"VKTEAMS_MCP_TIMEZONE is not a time zone: {name}") from None
    return name


@dataclass(frozen=True)
class Settings:
    public_url: str
    secret: str
    database: str = "vkteams-mcp.sqlite"
    areas: frozenset[str] = field(default_factory=lambda: parse_areas(None))
    policy: Policy = field(default_factory=Policy)
    dev_id: str = DEFAULT_DEV_ID
    timezone: str = "Europe/Moscow"
    port: int = 8000

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        public_url = (e.get("VKTEAMS_MCP_PUBLIC_URL") or "").rstrip("/")
        u = urlsplit(public_url)
        local = u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1") and not u.username
        if u.query or u.fragment or (u.path not in ("", "/")):
            raise ConfigError("VKTEAMS_MCP_PUBLIC_URL is a bare origin: no path, query or fragment")
        if not (u.scheme == "https" and u.hostname and not u.username) and not local:
            raise ConfigError("VKTEAMS_MCP_PUBLIC_URL must be the https address clients reach this server on")
        secret = e.get("VKTEAMS_MCP_SECRET") or ""
        if len(secret) < 32:
            raise ConfigError("VKTEAMS_MCP_SECRET must be at least 32 random characters")
        try:
            areas = parse_areas(e.get("VKTEAMS_MCP_AREAS"))
        except ValueError as err:
            raise ConfigError(str(err)) from None
        pinned = frozenset(
            h.strip().lower() for h in (e.get("VKTEAMS_MCP_INSTALLATIONS") or "").split(",") if h.strip()
        )
        policy = Policy(allow_private=e.get("VKTEAMS_MCP_ALLOW_PRIVATE_NETWORKS") == "1", installations=pinned)
        return cls(
            public_url=public_url,
            secret=secret,
            database=e.get("VKTEAMS_MCP_DATABASE", "vkteams-mcp.sqlite"),
            areas=areas,
            policy=policy,
            dev_id=e.get("VKTEAMS_MCP_DEV_ID", DEFAULT_DEV_ID),
            timezone=_zone(e.get("VKTEAMS_MCP_TIMEZONE", "Europe/Moscow")),
            port=int(e.get("PORT", "8000")),
        )
