"""chat_admin (off by default): create and change groups, manage members."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from vkteams_mcp.core.context import call
from vkteams_mcp.core.errors import VKError, invalid
from vkteams_mcp.core.guard import MAX_PEOPLE
from vkteams_mcp.core.project import compact
from vkteams_mcp.registry import tool
from vkteams_mcp.tools._common import forget_index, is_chat_sn, resolve_chat, resolve_person


@tool(area="chat_admin", title="Create or change a group", notifies_others=True)
async def save_chat(
    user_confirmed: Annotated[
        bool,
        Field(description="To change a group: true only when the person explicitly asked. Creating needs none."),
    ] = False,
    chat: Annotated[str | None, Field(description="The group to change; leave empty to create a new group.")] = None,
    title: Annotated[str | None, Field(description="The group's name; required when creating.")] = None,
    about: Annotated[str | None, Field(description="The description members see.")] = None,
    rules: Annotated[str | None, Field(description="The rules members see.")] = None,
    threads: Annotated[bool | None, Field(description="Allow threads under messages.")] = None,
) -> dict[str, Any]:
    """Creates a private VK Teams group with you as its only member, or changes a group you administer: its
    name, description, rules and whether threads are allowed. Call it when the person asks to set up or rename a
    group; add people afterwards with update_chat_members. A new group is seen by no one else, so creating needs no
    confirmation; members see changes at once, so a change needs user_confirmed. It refuses a new group without a
    title and a change with nothing to change."""
    async with call("save_chat") as c:
        if not chat:
            if not (title or "").strip():
                raise invalid("A new group needs title. Nothing was created.")
            receipt = await c.rt.guard.spend(c.person, kind="chat")
            # the desktop client's defaults for a private group: not public, not a live chat
            params = {"name": title.strip(), "aimSid": c.vk.s.aimsid, "about": about, "live": False, "public": False}
            r = await c.vk.rapi("createChat", {k: v for k, v in params.items() if v is not None}, write=True)
            sn = r.get("sn")
            forget_index(c)
            extra = {k: v for k, v in {"rules": rules, "threadsEnabled": threads}.items() if v is not None}
            if extra:
                try:
                    await c.vk.rapi("modChat", {"sn": sn, **extra}, write=True)
                except VKError as e:
                    # The group exists now: say so, so that a retry changes it instead of creating another.
                    unsure = e.key == "uncertain"
                    return {
                        "chat": sn,
                        "done": "created",
                        ("may_not_be_applied" if unsure else "not_applied"): sorted(extra),
                        "note": "The group was created, but its rules or threads setting was not applied. Call "
                        "save_chat again with this chat to set them.",
                        **receipt,
                    }
            return {"chat": sn, "done": "created", **receipt}
        sn = await resolve_chat(c, chat)
        if not is_chat_sn(sn):
            raise invalid("Only a group can be changed. Nothing was changed.")
        change = {"name": title, "about": about, "rules": rules, "threadsEnabled": threads}
        if all(v is None for v in change.values()):
            raise invalid("Nothing to change: give title, about, rules or threads.")
        receipt = await c.guard(kind="chat", user_confirmed=user_confirmed, target=sn)
        await c.vk.rapi("modChat", {"sn": sn, **{k: v for k, v in change.items() if v is not None}}, write=True)
        forget_index(c)
        return {"chat": sn, "done": "changed", **receipt}


@tool(area="chat_admin", title="Manage group members", notifies_others=True)
async def update_chat_members(
    chat: Annotated[str, Field(description="The group: its title or chat id.")],
    action: Annotated[
        Literal["add", "remove", "make_admin", "make_member", "make_readonly", "block", "unblock"],
        Field(description="What to do with these people in the group."),
    ],
    people: Annotated[list[str], Field(description="Up to 20 people, by name or e-mail address.")],
    user_confirmed: Annotated[bool, Field(description="true only when the person explicitly asked for this.")] = False,
) -> dict[str, Any]:
    """Adds people to a group or removes them, changes their role (admin, member, read-only), or blocks and
    unblocks them. Call it when the person administers a group and asks to change who is in it. The people and the
    group see it at once, so it needs user_confirmed; it refuses more than 20 people, and reports per person anyone
    VK Teams would not take (for example a bot that may not join groups)."""
    async with call("update_chat_members") as c:
        sn = await resolve_chat(c, chat)
        if not is_chat_sn(sn):
            raise invalid("Members belong to a group. Nothing was changed.")
        if not people:
            raise invalid("people is empty. Nothing was changed.")
        if len(people) > MAX_PEOPLE:
            raise VKError("too_many", limit=MAX_PEOPLE, what="people", count=len(people))
        who = list(dict.fromkeys([await resolve_person(c, p) for p in people]))  # a name and its address are one
        receipt = await c.guard(kind="members", user_confirmed=user_confirmed, people=len(who), target=sn)
        failures: list = []
        if action == "add":
            r = await c.vk.rapi("group/members/add", {"sn": sn, "members": who}, write=True)
            failures = r.get("failures") or []
        elif action == "remove":
            r = await c.vk.rapi("group/members/delete", {"sn": sn, "members": who}, write=True)
            failures = r.get("failures") or []
        elif action in ("block", "unblock"):
            method = "blockChatMembers" if action == "block" else "unblockChatMembers"
            p = {"sn": sn, "members": [{"sn": w} for w in who]}
            if action == "block":
                p["deleteLastMessages"] = False
            r = await c.vk.rapi(method, p, write=True)
            failures = r.get("failures") or []
        else:
            role = {"make_admin": "admin", "make_member": "member", "make_readonly": "readonly"}[action]
            for i, w in enumerate(who):
                try:
                    await c.vk.rapi("modChatMember", {"sn": sn, "memberSn": w, "role": role}, write=True)
                except VKError as e:
                    # roles change one by one: report who changed, who failed, and who was not tried
                    failures = [{"id": w, "error": e.key}] + [{"id": x, "error": "not tried"} for x in who[i + 1 :]]
                    break
        failed = [compact({"person": f.get("id"), "reason": f.get("error")}) for f in failures if isinstance(f, dict)]
        forget_index(c)
        return compact(
            {"chat": sn, "done": action, "changed": len(who) - len(failed), "failed": failed or None, **receipt}
        )
