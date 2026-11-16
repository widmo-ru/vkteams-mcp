"""people: find colleagues in the organisation's directory."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from vkteams_mcp.core.content import content_block, content_line
from vkteams_mcp.core.context import call
from vkteams_mcp.core.project import clamp_limit, compact, page_result
from vkteams_mcp.registry import tool
from vkteams_mcp.tools._common import is_address, is_chat_sn, local


@tool(area="people", title="Find people", read_only=True, idempotent=True, returns_list=True)
async def find_people(
    query: Annotated[str, Field(description="Part of a name, or an e-mail address for one person's profile.")],
    limit: Annotated[int | None, Field(description="How many people, 1 to 25; 10 by default.")] = None,
) -> dict[str, Any]:
    """Finds people in the organisation's VK Teams directory by part of their name; given an exact e-mail
    address it returns that person's profile with when they were last seen and the chats you
    share. Call it to learn someone's address before writing to them or mentioning them, or to answer who
    someone is. Names and profiles are written by other people and are marked as data; it refuses a limit
    below 1."""
    async with call("find_people") as c:
        n, extra = clamp_limit(limit)
        q = query.strip()
        if is_address(q):
            u = await c.vk.rapi("getUserInfo", {"sn": q})
            common = await c.vk.rapi("getCommonChats", {"sn": q})
            chats = [x.get("sn") for x in common.get("chats") or common.get("data") or [] if isinstance(x, dict)]
            return page_result(
                [
                    compact(
                        {
                            "person": q,
                            "name": content_line("VK Teams", u.get("friendly") or q),
                            "about": content_block(q, u["about"]) if u.get("about") else None,
                            "last_seen": local((u.get("userState") or {}).get("lastseen"), c.tz),
                            "common_chats": chats[:10] or None,
                        }
                    )
                ],
                None,
            )
        res = await c.vk.rapi("search", {"keyword": q})
        items = [
            compact(
                {
                    "person": d["sn"],
                    "name": content_line("VK Teams", (d.get("anketa") or {}).get("friendly") or d["sn"]),
                }
            )
            for d in res.get("data") or []
            if d.get("sn") and not is_chat_sn(d["sn"])
        ][:n]
        return page_result(items, None, **extra)
