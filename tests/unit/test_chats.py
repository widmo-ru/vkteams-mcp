"""chats: the list, one chat, the person's own chat state."""

from __future__ import annotations

from conftest import GROUP, call_tool, ok, text_of, wim_ok


async def test_list_chats_is_newest_first_with_unread_and_marked_text(server, chats):
    r = await call_tool(server, "list_chats", {})
    items = r.structured_content["items"]
    assert [i["chat"] for i in items] == [GROUP, "1002@chat.agent", "ann@example.com"]
    assert items[0]["unread"] == 3 and "data" not in items[0]["title"]  # lines are marked with nonces
    assert "<<<text" in items[0]["title"] and "deadline moved" in items[0]["last_message"]
    assert chats.writes() == []


async def test_list_chats_filters_unread_and_people(server, chats):
    unread = (await call_tool(server, "list_chats", {"filter": "unread"})).structured_content["items"]
    people = (await call_tool(server, "list_chats", {"filter": "people"})).structured_content["items"]
    assert {i["chat"] for i in unread} == {GROUP, "ann@example.com"}
    assert [i["chat"] for i in people] == ["ann@example.com"]


async def test_list_chats_pages_with_a_cursor(server, chats):
    first = (await call_tool(server, "list_chats", {"limit": 2})).structured_content
    assert first["has_more"] and len(first["items"]) == 2
    second = (await call_tool(server, "list_chats", {"limit": 2, "cursor": first["cursor"]})).structured_content
    assert [i["chat"] for i in second["items"]] == ["ann@example.com"] and not second["has_more"]


async def test_an_ambiguous_chat_lists_candidates(server, chats):
    r = await call_tool(server, "get_chat", {"chat": "Project"})
    assert r.is_error and "More than one chat" in text_of(r) and GROUP in text_of(r)
    assert chats.calls("getChatInfo") == []


async def test_a_chat_is_found_by_part_of_its_title(server, chats):
    chats.rapi(
        "getChatInfo",
        ok({"name": "Project Alpha", "membersCount": 4, "you": {"role": "admin"}, "threadsEnabled": True}),
    )
    r = await call_tool(server, "get_chat", {"chat": "alpha"})
    assert r.structured_content["chat"] == GROUP and r.structured_content["your_role"] == "admin"
    assert chats.calls("getChatInfo")[0].params == {"sn": GROUP, "memberLimit": 1}


async def test_an_unknown_chat_says_where_to_look(server, chats):
    r = await call_tool(server, "get_chat", {"chat": "Gamma"})
    assert r.is_error and "list_chats" in text_of(r)


async def test_mark_read_sets_the_last_message_as_read(server, chats):
    chats.rapi("getHistory", ok({"lastMsgId": 77, "messages": []}))
    chats.rapi("setDlgState", ok())
    r = await call_tool(server, "update_chat", {"chat": GROUP, "action": "mark_read"})
    assert not r.is_error
    assert chats.calls("setDlgState")[0].params == {"sn": GROUP, "lastRead": 77}


async def test_mute_for_minutes(server, chats):
    chats.rapi("contacts/mute", ok())
    await call_tool(server, "update_chat", {"chat": GROUP, "action": "mute", "minutes": 60})
    assert chats.calls("contacts/mute")[0].params == {"sn": GROUP, "duration": 3600, "tieCounter": True}


async def test_leave_needs_confirmation(server, chats):
    r = await call_tool(server, "update_chat", {"chat": GROUP, "action": "leave"})
    assert r.is_error and "user_confirmed=true" in text_of(r) and "still in the chat" in text_of(r)
    assert chats.writes() == []


async def test_leave_uses_remove_buddy_with_all_groups(server, chats):
    chats.wim("buddylist/removeBuddy", wim_ok())
    r = await call_tool(server, "update_chat", {"chat": GROUP, "action": "leave", "user_confirmed": True})
    assert not r.is_error and r.structured_content["sends_left"] == 9
    s = [x for x in chats.seen if x.path.endswith("removeBuddy")][0]
    assert s.query["buddy"] == GROUP and s.query["allGroups"] == "1"


async def test_a_one_to_one_chat_cannot_be_left(server, chats):
    r = await call_tool(server, "update_chat", {"chat": "ann@example.com", "action": "leave", "user_confirmed": True})
    assert r.is_error and chats.writes() == []
