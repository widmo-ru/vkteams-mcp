"""account: who is connected, to which installation, and what is switched on."""

from __future__ import annotations

from typing import Any

from vkteams_mcp.core.context import call
from vkteams_mcp.registry import AREAS, tool


@tool(area="account", title="Connection", read_only=True, idempotent=True)
async def get_connection() -> dict[str, Any]:
    """Shows who is signed in to VK Teams through this connector, which VK Teams server it is, and which
    tool areas are switched on. Call it first when a tool fails with a sign-in or permission error, or when
    the person asks which account the assistant uses. It changes nothing and refuses nothing; when the
    session has ended it says to sign in again."""
    async with call("get_connection") as c:
        info = await c.vk.rapi("getUserInfo", {"sn": c.me})
        on = sorted(c.rt.settings.areas)
        return {
            "signed_in_as": f"{info.get('friendly') or c.vk.s.my_name or c.me} <{c.me}>",
            "server": c.vk.s.host,
            "api_version": c.vk.s.api_version,
            "areas_on": on,
            "areas_off": sorted(set(AREAS) - set(on)),
            "time_zone": c.rt.settings.timezone,
        }
