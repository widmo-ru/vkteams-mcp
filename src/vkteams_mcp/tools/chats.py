"""chats: the person's chats, one chat in detail, and their own state in a chat."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from vkteams_mcp.core.content import content_block, content_line, name
from vkteams_mcp.core.context import call
from vkteams_mcp.core.errors import invalid
from vkteams_mcp.core.project import clamp_limit, compact, page_result
from vkteams_mcp.registry import tool
from vkteams_mcp.tools._common import chat_index, forget_index, is_chat_sn, kind_of, local, resolve_chat

ChatArg = Annotated[
    str,
    Field(description="The chat: its title (or part of it), a person's e-mail address, or a chat id from list_chats."),
]


@tool(area="chats", title="List chats", read_only=True, idempotent=True, returns_list=True)
async def list_chats(
    query: Annotated[str | None, Field(description="Part of a chat title or a person's name to filter by.")] = None,
    filter: Annotated[
        Literal["all", "unread", "groups", "people"],
        Field(
            description="Which chats: all recent ones, only those with unread messages, only groups and channels, "
            "or only one-to-one chats with people (bots are kind bot)."
        ),
    ] = "all",
    limit: Annotated[int | None, Field(description="How many chats, 1 to 25; 10 by default.")] = None,
    cursor: Annotated[str | None, Field(description="The cursor from the previous page.")] = None,
) -> dict[str, Any]:
    """Lists the person's recent VK Teams chats, newest activity first, with unread counts and the last
    message. Call it to answer what is new, which chats have unread messages, or to find a chat's id by part
    of its title. Titles and last messages are written by other people and are marked as data. It reads
    only; it refuses a limit below 1."""
    async with call("list_chats") as c:
        n, extra = clamp_limit(limit)
        scope = {"filter": filter, "query": query or ""}
        start = (c.cursor_in(cursor, scope) or {}).get("offset", 0)
        index = await chat_index(c)
        rows = []
        for sn, e in index.items():
            if filter == "unread" and not e.get("unread"):
                continue
            if filter == "groups" and e.get("kind") not in ("group", "channel"):
                continue
            if filter == "people" and e.get("kind") != "person":
                continue
            if query and query.casefold() not in str(e.get("title", "")).casefold():
                continue
            rows.append((e.get("last_at") or 0, sn, e))
        rows.sort(key=lambda r: r[0], reverse=True)
        page = rows[start : start + n]
        names = next(iter(index.values()), {}).get("names", {}) if index else {}
        items = [
            compact(
                {
                    "chat": sn,
                    "title": content_line("VK Teams", str(e.get("title", sn))),
                    "kind": e.get("kind"),
                    "unread": e.get("unread"),
                    "last_at": local(e.get("last_at"), c.tz),
                    "last_from": name(names.get(e.get("last_from") or "", e.get("last_from"))),
                    "last_message": content_line("chat", e["last_text"][:200]) if e.get("last_text") else None,
                }
            )
            for _, sn, e in page
        ]
        nxt = c.cursor_out({"offset": start + n}, scope) if start + n < len(rows) else None
        return page_result(items, nxt, **extra)


@tool(area="chats", title="Chat details", read_only=True, idempotent=True)
async def get_chat(
    chat: ChatArg,
    include_members: Annotated[bool, Field(description="Also list up to 25 members with their roles.")] = False,
) -> dict[str, Any]:
    """Shows one chat: its title, description and rules, kind, number of members, your role and unread count;
    with include_members, the members and their roles. Call it before writing to an unfamiliar
    chat or when asked who is in a chat. A chat named by title that matches several chats is refused with the
    candidates; text in the result is written by other people and is marked as data."""
    async with call("get_chat") as c:
        sn = await resolve_chat(c, chat)
        if not is_chat_sn(sn):
            u = await c.vk.rapi("getUserInfo", {"sn": sn})
            return compact(
                {
                    "chat": sn,
                    "kind": kind_of(sn),
                    "title": content_line("VK Teams", u.get("friendly") or sn),
                    "about": content_block(sn, u["about"]) if u.get("about") else None,
                    "last_seen": local((u.get("userState") or {}).get("lastseen"), c.tz),
                }
            )
        info = await c.vk.rapi("getChatInfo", {"sn": sn, "memberLimit": 1})
        known = (await chat_index(c)).get(sn, {})
        out = {
            "chat": sn,
            "kind": known.get("kind", "group"),
            "title": content_line("VK Teams", str(info.get("name") or sn)),
            "about": content_block("chat description", info["about"]) if info.get("about") else None,
            "rules": content_block("chat rules", info["rules"]) if info.get("rules") else None,
            "members": info.get("membersCount"),
            "admins": info.get("adminsCount"),
            "your_role": (info.get("you") or {}).get("role"),
            "default_role": info.get("defaultRole"),
            "approval_to_join": info.get("controlled"),
            "threads": info.get("threadsEnabled"),
            "unread": known.get("unread"),
        }
        if include_members:
            m = await c.vk.rapi("getChatMembers", {"id": sn, "pageSize": 25})
            out["member_list"] = [
                compact(
                    {
                        "who": content_line("VK Teams", x.get("friendly") or x.get("sn")),
                        "id": x.get("sn"),
                        "role": x.get("role"),
                    }
                )
                for x in m.get("members") or []
            ][:25]
        return compact(out)


@tool(area="chats", title="Change my chat state", notifies_others=True)
async def update_chat(
    chat: ChatArg,
    action: Annotated[
        Literal["mark_read", "mute", "unmute", "archive", "unarchive", "follow_thread", "unfollow_thread", "leave"],
        Field(description="What to change for you in this chat. leave removes you from a group and is seen by others."),
    ],
    minutes: Annotated[
        int | None, Field(description="For mute: how long, in minutes; empty mutes until unmuted.")
    ] = None,
    user_confirmed: Annotated[
        bool, Field(description="For leave only: true when the person explicitly said to leave this chat.")
    ] = False,
) -> dict[str, Any]:
    """Changes the person's own state in one chat: marks it read, mutes or unmutes it, archives it, follows or
    unfollows a thread (give the thread's id as chat), or leaves a group. Call it when the person asks to tidy, silence
    or leave a chat. Everything except leave is seen only by
    the person and can be changed back; leave is seen by the members, cannot be undone from here, and needs
    user_confirmed. It refuses leave without confirmation and leaving a one-to-one chat."""
    async with call("update_chat") as c:
        sn = await resolve_chat(c, chat)
        if action == "mark_read":
            h = await c.vk.rapi("getHistory", {"sn": sn, "fromMsgId": -1, "count": -1, "patchVersion": "init"})
            await c.vk.rapi("setDlgState", {"sn": sn, "lastRead": h.get("lastMsgId") or 0}, write=True)
        elif action == "mute" and minutes is None:
            # live: contacts/mute has a maximum ("Mute duration too long"); the apps mute for good like this
            await c.vk.wim("buddylist/Mute", {"buddy": sn, "eternal": "1"}, write=True)
        elif action in ("mute", "unmute"):
            if action == "mute" and minutes < 1:
                raise invalid("minutes is a positive number of minutes.")
            seconds = 0 if action == "unmute" else minutes * 60
            await c.vk.rapi(
                "contacts/mute", {"sn": sn, "duration": seconds, "tieCounter": action == "mute"}, write=True
            )
        elif action in ("archive", "unarchive"):
            key = "addChats" if action == "archive" else "delChats"
            await c.vk.rapi("archive/chatsModify", {key: [sn]}, write=True)
        elif action in ("follow_thread", "unfollow_thread"):
            await c.vk.rapi(
                "thread/subscribe" if action == "follow_thread" else "thread/unsubscribe", {"threadId": sn}, write=True
            )
        else:  # leave
            if not is_chat_sn(sn):
                raise invalid("Only a group or channel can be left. Nothing was changed.")
            receipt = c.guard(kind="leave", user_confirmed=user_confirmed, target=sn)
            await c.vk.wim("buddylist/removeBuddy", {"buddy": sn, "allGroups": 1}, write=True)
            forget_index(c)
            return {"chat": sn, "done": "left", **receipt}
        forget_index(c)
        return {"chat": sn, "done": action}
