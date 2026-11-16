"""Defects found by the QA run on a live server on 2026-11-16, each held by a test that fails without its fix.

B1  message ids above 2^53 were JSON numbers: a JavaScript client rounds them, so every action on a message
    by id failed. Ids are strings both ways.
B2  a bot's id from list_chats did not resolve as a chat.
B3  a download link ignored Range.
B5  a cursor was not bound to the list it came from.
B6  bots were listed as people.
B7  get_chat showed `live` as "join_by_link". In the apps `live` is the live-chat mode (some actions only for
    admins), the server refuses it in modChat, and joining is governed by `controlled` (approval).
B8  a bot's buttons were not shown in the history, so its message looked cut off.
B9  creating a group, which only its creator sees, asked for confirmation.
B10 one message in full passed reaction/get through raw: its msgId came out as a JSON number above 2^53, so a
    JavaScript client rounded it (an id past 2^53 came out changed in its last digits).
"""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import (
    EVENTS_PATH,
    GROUP,
    GROUP2,
    call_tool,
    dialogs,
    history_like_vk,
    ok,
)

BIG = 7700000000000000301  # shaped like a live id: 2^53 < BIG, so a float cannot hold it
BIG2 = BIG + 1  # its neighbour: a float maps both to the same number
BOT = "70001"


def test_the_ids_really_are_beyond_a_float():
    assert float(BIG) == float(BIG2)  # what a JavaScript client sees if ids are numbers


@pytest.fixture
def big(chats):
    msgs = [
        {"msgId": BIG, "time": 1_800_000_000, "text": "first", "chat": {"sender": "ann@example.com"}},
        {
            "msgId": BIG2,
            "time": 1_800_000_100,
            "outgoing": True,
            "parts": [{"mediaType": "quote", "msgId": BIG, "text": "first"}, {"mediaType": "text", "text": "mine"}],
        },
    ]
    chats.rapi("getHistory", history_like_vk(msgs, persons=[{"sn": "ann@example.com", "friendly": "Ann Smith"}]))
    return chats


# ---------------------------------------------------------------- B1


async def test_message_ids_come_out_as_exact_strings(server, big):
    r = await call_tool(server, "list_messages", {"chat": GROUP})
    items = r.structured_content["items"]
    assert [m["id"] for m in items] == [str(BIG2), str(BIG)]
    assert items[0]["reply_to"] == str(BIG)
    # what crosses the wire: no message id is a JSON number a client could round
    wire = json.loads(json.dumps(r.structured_content), parse_int=float)
    assert wire["items"][0]["id"] == str(BIG2)


async def test_one_message_is_fetched_by_its_exact_string_id(server, big):
    r = await call_tool(server, "list_messages", {"chat": GROUP, "message": str(BIG)})
    assert not r.is_error, r.content
    assert r.structured_content["id"] == str(BIG)
    assert big.calls("getHistory")[-1].params["fromMsgId"] == BIG + 1  # exact, not rounded


def _unsafe_ints(value):
    """Every integer in a result that a JavaScript client cannot hold exactly."""
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value] if abs(value) > 2**53 else []
    if isinstance(value, dict):
        return [n for v in value.values() for n in _unsafe_ints(v)]
    if isinstance(value, list):
        return [n for v in value for n in _unsafe_ints(v)]
    return []


async def test_reactions_on_one_message_carry_no_number_a_client_could_round(server, chats):
    msgs = [
        {
            "msgId": BIG,
            "time": 1_800_000_000,
            "outgoing": True,
            "parts": [{"mediaType": "text", "text": "mine"}],
            "reactions": {"exist": True},
        }
    ]
    chats.rapi("getHistory", history_like_vk(msgs))
    chats.rapi(
        "reaction/get",
        ok({"reactions": [{"msgId": BIG, "chatId": GROUP, "reactions": [{"reaction": "👍", "counter": 1}]}]}),
    )
    r = await call_tool(server, "list_messages", {"chat": GROUP, "message": str(BIG)})
    assert not r.is_error, r.content
    assert "👍" in json.dumps(r.structured_content, ensure_ascii=False)
    assert _unsafe_ints(r.structured_content) == []


async def test_a_message_id_given_as_a_number_is_refused(server, big):
    r = await call_tool(server, "list_messages", {"chat": GROUP, "message": BIG})
    assert r.is_error


async def test_actions_reach_vk_teams_with_the_exact_id(server, big):
    big.rapi("reaction/add", ok())
    big.rapi("delMsgBatch", ok())
    r = await call_tool(
        server,
        "update_message",
        {"chat": GROUP, "message": str(BIG2), "action": "react", "reaction": "👍", "user_confirmed": True},
    )
    assert not r.is_error, r.content
    assert big.calls("reaction/add")[0].params["msgId"] == BIG2
    r = await call_tool(
        server, "update_message", {"chat": GROUP, "message": str(BIG2), "action": "delete", "user_confirmed": True}
    )
    assert not r.is_error, r.content
    assert big.calls("delMsgBatch")[0].params["msgIds"] == [BIG2]


async def test_a_reply_quotes_the_exact_message(server, big):
    r = await call_tool(server, "save_draft", {"chat": GROUP, "text": "re", "reply_to": str(BIG)})
    assert not r.is_error, r.content
    assert r.structured_content["reply_to"] == str(BIG)


async def test_a_sent_message_id_is_a_string(server, chats):
    chats.rapi("message/send", ok({"msgId": BIG, "time": 1_800_000_000}))
    await call_tool(server, "save_draft", {"chat": GROUP, "text": "hello"})
    r = await call_tool(server, "send_draft", {"chat": GROUP, "user_confirmed": True})
    assert not r.is_error, r.content
    assert r.structured_content["message"] == str(BIG)


# ---------------------------------------------------------------- B2, B6


@pytest.fixture
def with_bot(fake):
    fake.on(
        "GET",
        EVENTS_PATH,
        dialogs(
            (GROUP, "Project Alpha", 0, 1_800_000_300, "x"),
            (BOT, "Metabot", 1, 1_800_000_200, "Hello"),
            ("ann@example.com", "Ann Smith", 0, 1_800_000_100, "hi"),
        ),
    )
    return fake


async def test_a_bot_is_listed_as_a_bot_not_a_person(server, with_bot):
    r = await call_tool(server, "list_chats", {})
    kinds = {i["chat"]: i["kind"] for i in r.structured_content["items"]}
    assert kinds == {GROUP: "group", BOT: "bot", "ann@example.com": "person"}
    people = await call_tool(server, "list_chats", {"filter": "people"})
    assert [i["chat"] for i in people.structured_content["items"]] == ["ann@example.com"]


async def test_a_bot_id_from_list_chats_is_a_chat(server, with_bot):
    with_bot.rapi("getUserInfo", ok({"friendly": "Metabot"}))
    r = await call_tool(server, "get_chat", {"chat": BOT})
    assert not r.is_error, r.content
    assert r.structured_content["chat"] == BOT and r.structured_content["kind"] == "bot"
    r = await call_tool(server, "save_draft", {"chat": BOT, "text": "/help"})
    assert not r.is_error, r.content
    assert r.structured_content["chat"] == BOT


async def test_an_unknown_id_without_an_at_sign_is_still_refused(server, with_bot):
    r = await call_tool(server, "save_draft", {"chat": "99999", "text": "x"})
    assert r.is_error  # a bot id must be one the person has a chat with


# ---------------------------------------------------------------- B5


async def test_a_list_chats_cursor_is_bound_to_its_filter(server, chats):
    first = await call_tool(server, "list_chats", {"filter": "all", "limit": 1})
    cursor = first.structured_content["cursor"]
    again = await call_tool(server, "list_chats", {"filter": "all", "limit": 1, "cursor": cursor})
    assert not again.is_error
    other = await call_tool(server, "list_chats", {"filter": "unread", "limit": 1, "cursor": cursor})
    assert other.is_error and "cursor" in other.content[0].text


async def test_a_history_cursor_is_bound_to_its_chat(server, chats):
    msgs = [{"msgId": i, "time": 1_800_000_000 + i, "text": f"m{i}"} for i in range(1, 6)]
    chats.rapi("getHistory", history_like_vk(msgs))
    first = await call_tool(server, "list_messages", {"chat": GROUP, "limit": 2})
    cursor = first.structured_content["cursor"]
    other = await call_tool(server, "list_messages", {"chat": GROUP2, "limit": 2, "cursor": cursor})
    assert other.is_error


# ---------------------------------------------------------------- B8


async def test_a_bots_buttons_are_shown_in_the_history(server, with_bot):
    msg = {
        "msgId": 5,
        "time": 1_800_000_000,
        "text": "Choose one of the commands:",
        "inlineKeyboardMarkup": [
            [{"text": "Help", "callbackData": "help", "style": "primary"}],
            [{"text": "Docs", "url": "https://example.com/docs"}, {"text": "Ping", "callbackData": "ping"}],
        ],
    }
    with_bot.rapi("getHistory", history_like_vk([msg]))
    r = await call_tool(server, "list_messages", {"chat": BOT})
    item = r.structured_content["items"][0]
    assert [b["button"].split(".")[0] for b in item["buttons"]] == ["1", "2", "3"]  # the callback data stays here
    assert "Help" in item["buttons"][0]["text"] and "https://example.com/docs" in item["buttons"][1]["url"]
    assert "help" not in str([b["button"] for b in item["buttons"]])


# ---------------------------------------------------------------- B7, B9


@pytest.fixture
def admin(make_server):
    return make_server("account,chats,messages,people,chat_admin")[0]


async def test_a_new_group_is_private_and_needs_no_confirmation(admin, chats):
    chats.rapi("createChat", ok({"sn": "5555@chat.agent"}))
    r = await call_tool(admin, "save_chat", {"title": "New team"})
    assert not r.is_error, r.content
    params = chats.calls("createChat")[0].params
    assert params["live"] is False and params["public"] is False  # the desktop client's private defaults
    assert "sends_left" in r.structured_content  # still counted against the person's allowance


async def test_changing_a_group_still_needs_confirmation(admin, chats):
    r = await call_tool(admin, "save_chat", {"chat": GROUP, "title": "Renamed"})
    assert r.is_error and chats.writes() == []


async def test_creating_groups_is_rate_limited(admin, chats):
    chats.rapi("createChat", *[ok({"sn": f"{i}@chat.agent"}) for i in range(12)])
    results = [await call_tool(admin, "save_chat", {"title": f"T{i}"}) for i in range(12)]
    assert sum(r.is_error for r in results) >= 1 and len(chats.calls("createChat")) <= 10


# ---------------------------------------------------------------- B3


async def test_a_download_passes_range_through(fake, tmp_path, session):
    from conftest import BIN_HOST, SECRET
    from test_files import PUBLIC, urlpath

    from vkteams_mcp.app import build
    from vkteams_mcp.config import Settings
    from vkteams_mcp.core import context

    app, _, _ = build(
        Settings(public_url=PUBLIC, secret=SECRET, database=str(tmp_path / "db.sqlite")),
        http=httpx.AsyncClient(transport=fake.transport()),
    )
    rt = context.runtime()
    person = rt.store.save_session(session)
    context.person_override.set(person)

    def answer(seen):
        assert seen.headers.get("range") == "bytes=0-3"
        return httpx.Response(
            206, content=b"PDFD", headers={"content-range": "bytes 0-3/1000", "content-type": "application/pdf"}
        )

    fake.on("GET", "/api/v139/files/get/signed", answer, host=BIN_HOST)
    link = rt.links.download(person, f"https://{BIN_HOST}/api/v139/files/get/signed", "a.pdf")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as c:
        r = await c.get(urlpath(link["url"]), headers={"Range": "bytes=0-3"})
    assert r.status_code == 206 and r.content == b"PDFD" and r.headers["content-range"] == "bytes 0-3/1000"


async def test_a_malformed_range_is_not_passed_on(fake, tmp_path, session):
    from conftest import BIN_HOST, SECRET
    from test_files import PUBLIC, urlpath

    from vkteams_mcp.app import build
    from vkteams_mcp.config import Settings
    from vkteams_mcp.core import context

    app, _, _ = build(
        Settings(public_url=PUBLIC, secret=SECRET, database=str(tmp_path / "db.sqlite")),
        http=httpx.AsyncClient(transport=fake.transport()),
    )
    rt = context.runtime()
    person = rt.store.save_session(session)
    context.person_override.set(person)
    seen_range = []

    def answer(seen):
        seen_range.append(seen.headers.get("range"))
        return httpx.Response(200, content=b"whole")

    fake.on("GET", "/api/v139/files/get/signed", answer, host=BIN_HOST)
    link = rt.links.download(person, f"https://{BIN_HOST}/api/v139/files/get/signed", "a.pdf")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as c:
        r = await c.get(urlpath(link["url"]), headers={"Range": "bytes=0-3\r\nX-Evil: 1"})
    assert r.status_code == 200 and seen_range == [None]


async def test_get_chat_does_not_call_live_chat_mode_join_by_link(server, chats):
    chats.rapi("getChatInfo", ok({"name": "Alpha", "live": True, "controlled": True, "you": {"role": "admin"}}))
    r = await call_tool(server, "get_chat", {"chat": GROUP})
    assert "join_by_link" not in r.structured_content and r.structured_content["approval_to_join"] is True


@pytest.fixture
def web(fake, tmp_path, session):
    from conftest import BIN_HOST, SECRET
    from test_files import PUBLIC

    from vkteams_mcp.app import build
    from vkteams_mcp.config import Settings
    from vkteams_mcp.core import context

    app, _, _ = build(
        Settings(public_url=PUBLIC, secret=SECRET, database=str(tmp_path / "db.sqlite")),
        http=httpx.AsyncClient(transport=fake.transport()),
    )
    rt = context.runtime()
    person = rt.store.save_session(session)
    context.person_override.set(person)

    async def get(answer, **kw):
        from test_files import urlpath

        fake.on("GET", "/api/v139/files/get/signed", answer, host=BIN_HOST)
        link = rt.links.download(person, f"https://{BIN_HOST}/api/v139/files/get/signed", "a.pdf")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as c:
            return await c.get(urlpath(link["url"]), **kw)

    return get


async def test_a_range_past_the_end_says_so_with_the_length(web):
    r = await web(httpx.Response(416, headers={"content-range": "bytes */1000"}), headers={"Range": "bytes=1000-"})
    assert r.status_code == 416 and r.headers["content-range"] == "bytes */1000"


async def test_a_download_asks_for_identity_and_never_claims_a_wrong_length(web):
    import gzip

    seen = []

    def answer(req):
        seen.append(req.headers.get("accept-encoding"))
        packed = gzip.compress(b"x" * 5000)
        return httpx.Response(
            200, content=packed, headers={"content-encoding": "gzip", "content-length": str(len(packed))}
        )

    r = await web(answer)
    assert seen == ["identity"]
    assert r.status_code == 200 and r.content == b"x" * 5000
    assert r.headers.get("content-length") in (None, "5000")


async def test_reactions_are_counts_without_ids_and_zero_counts_drop(server, chats):
    msgs = [{"msgId": 7, "time": 1_800_000_000, "outgoing": True, "text": "mine", "reactions": {"exist": True}}]
    chats.rapi("getHistory", history_like_vk(msgs))
    chats.rapi(
        "reaction/get",
        ok(
            {
                "reactions": [
                    {
                        "msgId": 7,
                        "chatId": GROUP,
                        "reactions": [{"reaction": "👍", "counter": 2}, {"reaction": "🔥", "counter": 0}],
                    }
                ]
            }
        ),
    )
    r = (await call_tool(server, "list_messages", {"chat": GROUP, "message": "7"})).structured_content
    assert r["reactions"] == [{"emoji": "👍", "count": 2}]


async def test_a_message_whose_reactions_are_all_taken_back_has_none(server, chats):
    msgs = [{"msgId": 7, "time": 1_800_000_000, "outgoing": True, "text": "mine", "reactions": {"exist": True}}]
    chats.rapi("getHistory", history_like_vk(msgs))
    chats.rapi(
        "reaction/get",
        ok({"reactions": [{"msgId": 7, "chatId": GROUP, "reactions": [{"reaction": "👍", "counter": 0}]}]}),
    )
    r = (await call_tool(server, "list_messages", {"chat": GROUP, "message": "7"})).structured_content
    assert "reactions" not in r and "has_reactions" not in r


async def test_a_reply_draft_shows_what_it_quotes(server, big):
    r = await call_tool(server, "save_draft", {"chat": GROUP, "text": "re", "reply_to": str(BIG)})
    quote = r.structured_content["quote"]
    assert "first" in quote and "<<<" in quote  # the quoted text, marked as someone else's
