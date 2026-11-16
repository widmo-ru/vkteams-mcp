"""chat_admin: groups and members, off by default."""

from __future__ import annotations

import pytest
from conftest import GROUP, call_tool, ok, text_of


@pytest.fixture
def admin(make_server):
    return make_server("account,chats,messages,people,chat_admin")[0]


async def test_more_than_twenty_people_is_refused(admin, chats):
    people = [f"p{i}@example.com" for i in range(21)]
    r = await call_tool(
        admin, "update_chat_members", {"chat": GROUP, "action": "add", "people": people, "user_confirmed": True}
    )
    assert r.is_error and "At most 20 people" in text_of(r)
    assert chats.writes() == []


async def test_members_need_confirmation(admin, chats):
    r = await call_tool(admin, "update_chat_members", {"chat": GROUP, "action": "add", "people": ["a@example.com"]})
    assert r.is_error and chats.writes() == []


async def test_partial_failures_are_reported_per_person(admin, chats):
    chats.rapi("group/members/add", ok({"failures": [{"id": "70001", "error": "bot_setjoingroups_false"}]}, code=20070))
    r = await call_tool(
        admin,
        "update_chat_members",
        {"chat": GROUP, "action": "add", "user_confirmed": True, "people": ["a@example.com", "70001@example.com"]},
    )
    out = r.structured_content
    assert out["changed"] == 1 and out["failed"] == [{"person": "70001", "reason": "bot_setjoingroups_false"}]


async def test_block_sends_member_objects(admin, chats):
    chats.rapi("blockChatMembers", ok())
    await call_tool(
        admin,
        "update_chat_members",
        {"chat": GROUP, "action": "block", "people": ["a@example.com"], "user_confirmed": True},
    )
    assert chats.calls("blockChatMembers")[0].params == {
        "sn": GROUP,
        "members": [{"sn": "a@example.com"}],
        "deleteLastMessages": False,
    }


async def test_roles_go_one_by_one(admin, chats):
    chats.rapi("modChatMember", ok())
    await call_tool(
        admin,
        "update_chat_members",
        {
            "chat": GROUP,
            "action": "make_readonly",
            "user_confirmed": True,
            "people": ["a@example.com", "b@example.com"],
        },
    )
    assert [c.params["role"] for c in chats.calls("modChatMember")] == ["readonly", "readonly"]


async def test_create_a_group_with_the_session_aimsid(admin, chats):
    chats.rapi("createChat", ok({"sn": "5555@chat.agent"}))
    r = await call_tool(admin, "save_chat", {"title": "New team", "user_confirmed": True})
    assert r.structured_content["chat"] == "5555@chat.agent"
    assert chats.calls("createChat")[0].params == {
        "name": "New team",
        "aimSid": "aimsid-1",
        "live": False,
        "public": False,
    }


async def test_a_change_with_nothing_to_change_is_refused(admin, chats):
    r = await call_tool(admin, "save_chat", {"chat": GROUP, "user_confirmed": True})
    assert r.is_error and chats.writes() == []


async def test_the_same_person_twice_is_one_change(admin, chats):
    chats.rapi("modChatMember", ok())
    r = await call_tool(
        admin,
        "update_chat_members",
        {"chat": GROUP, "action": "make_admin", "user_confirmed": True, "people": ["a@example.com", "a@example.com"]},
    )
    assert len(chats.calls("modChatMember")) == 1 and r.structured_content["changed"] == 1
