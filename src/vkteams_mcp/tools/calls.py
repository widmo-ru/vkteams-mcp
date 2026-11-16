"""calls: a link to a video meeting or a webinar."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from vkteams_mcp.core.context import call
from vkteams_mcp.registry import tool
from vkteams_mcp.tools._common import resolve_person


@tool(area="calls", title="Create a call link", notifies_others=True)
async def save_call_link(
    title: Annotated[str, Field(description="The meeting's name, shown to whoever opens the link.")],
    kind: Annotated[
        Literal["meeting", "webinar"], Field(description="meeting: everyone can speak; webinar: only hosts speak.")
    ] = "meeting",
    invite: Annotated[
        list[str] | None,
        Field(
            description="People to invite by name or address; they are notified at once. Leave empty for a link only."
        ),
    ] = None,
    user_confirmed: Annotated[
        bool, Field(description="Needed only with invite: true when the person explicitly said to invite these people.")
    ] = False,
) -> dict[str, Any]:
    """Creates a VK Teams call link for a meeting or a webinar and returns the link and its PIN. Call it when the
    person wants a call link to share or to start a call with named people. Without invite nobody is notified and
    no confirmation is needed; with invite the people are told at once, so it needs user_confirmed and refuses
    more than 20 people."""
    async with call("save_call_link") as c:
        people = [await resolve_person(c, p) for p in invite or []]
        receipt = c.guard(kind="call", user_confirmed=user_confirmed, people=len(people)) if people else {}
        r = await c.vk.rapi(
            "conference/create",
            {
                "name": title,
                "type": "webinar" if kind == "webinar" else "equitable",
                "participants": people,
                "callParticipants": bool(people),
            },
            write=True,
        )
        return {
            "link": r.get("conferenceUrl"),
            "pin": r.get("conferencePin"),
            "kind": kind,
            "invited": len(people),
            **receipt,
        }
