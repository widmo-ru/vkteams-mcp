"""Defects found by the per-parameter live QA run on 2026-11-16 (docs/qa-scenarios.md), each held by a test.

D2  an address written as @someone@example.com right before a full stop is listed in `mentions`, but stays
    plain text in the message: nobody is mentioned. The replacement refuses a following "." (and "-", "@"),
    while the finder stops before it; other punctuation works and is held here so it stays working.
D15 a history page loses one message at every page boundary. Live, `olderMsgId` is the next older message
    that the page did NOT include, and `fromMsgId` is exclusive, so the next page starts below it.
    Seen live twice, in a bot chat and in a group: the message at the page boundary never came back.
"""

from __future__ import annotations

import pytest
from conftest import GROUP, call_tool, ok


def history_with_live_older(messages: list[dict]):
    """getHistory as the live server pages it: fromMsgId is exclusive, and olderMsgId is the id of the next
    older message that is NOT in the page (absent when there is none)."""

    def answer(seen):
        p = seen.params
        frm, count = p.get("fromMsgId", -1), p.get("count", -1)
        ids = sorted(m["msgId"] for m in messages)
        below = [i for i in ids if frm == -1 or i < frm]
        pool = below[count:] if count < 0 else []
        rest = [i for i in below if pool and i < min(pool)]
        page = [m for m in messages if m["msgId"] in pool]
        body = {"messages": page, "lastMsgId": ids[-1] if ids else 0}
        if rest:
            body["olderMsgId"] = rest[-1]
        return ok(body)

    return answer


# ---------------------------------------------------------------- D15


async def test_paging_through_history_returns_every_message_once(server, chats):
    msgs = [
        {"msgId": i, "time": 1_800_000_000 + i, "outgoing": True, "parts": [{"mediaType": "text", "text": f"m{i}"}]}
        for i in range(101, 108)
    ]
    chats.rapi("getHistory", history_with_live_older(msgs))
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        args = {"chat": GROUP, "limit": 2, **({"cursor": cursor} if cursor else {})}
        r = (await call_tool(server, "list_messages", args)).structured_content
        seen += [m["id"] for m in r["items"]]
        cursor = r.get("cursor")
        if not cursor:
            break
    assert seen == [str(i) for i in range(107, 100, -1)]


# ---------------------------------------------------------------- D2


@pytest.mark.parametrize("tail", [".", ",", "!", "?", ")", ":", ";"])
async def test_an_address_before_punctuation_becomes_a_mention(server, chats, tail):
    r = await call_tool(server, "save_draft", {"chat": GROUP, "text": f"ping @ann@example.com{tail}"})
    assert not r.is_error, r.content
    assert f"@[ann@example.com]{tail}" in r.structured_content["draft"]


async def test_an_address_listed_in_mentions_is_a_mention_in_the_text(server, chats):
    r = (await call_tool(server, "save_draft", {"chat": GROUP, "text": "see @ann@example.com."})).structured_content
    for addr in r.get("mentions") or []:
        assert f"@[{addr}]" in r["draft"]


# ---------------------------------------------------------------- limits not covered elsewhere


async def test_a_text_over_ten_thousand_characters_is_refused_and_saves_nothing(server, chats):
    r = await call_tool(server, "save_draft", {"chat": GROUP, "text": "a" * 10_001})
    assert r.is_error
    sent = await call_tool(server, "send_draft", {"chat": GROUP, "user_confirmed": True})
    assert sent.is_error and chats.writes() == []


async def test_a_text_of_exactly_ten_thousand_characters_is_kept(server, chats):
    r = await call_tool(server, "save_draft", {"chat": GROUP, "text": "a" * 10_000})
    assert not r.is_error, r.content


# ---------------------------------------------------------------- D7


@pytest.mark.parametrize(
    "name", ["../../etc/passwd", "/etc/passwd", "~/.ssh/id_rsa", "sub/dir.txt", "a\\b.txt", ".env", "-rf", ""]
)
async def test_an_attachment_is_a_file_name_in_the_current_folder_not_a_path(make_server, chats, name):
    server = make_server("account,chats,messages,people,files")[0]
    r = await call_tool(server, "save_draft", {"chat": GROUP, "text": "x", "attach": [name]})
    assert r.is_error, name
    assert chats.writes() == []


async def test_a_plain_file_name_is_attached(make_server, chats):
    server = make_server("account,chats,messages,people,files")[0]
    r = await call_tool(server, "save_draft", {"chat": GROUP, "text": "x", "attach": ["отчёт 2026.pdf"]})
    assert not r.is_error, r.content
    assert "отчёт 2026.pdf" in r.structured_content["upload"][0]["command"]


# ---------------------------------------------------------------- D8


async def test_cancelling_a_scheduled_message_that_is_not_there_is_refused(server, chats):
    chats.rapi(
        "message/scheduled/history/get",
        ok({"messages": {"new": [{"scheduledMsgId": 4, "scheduledTime": 4_000_000_000, "parts": []}]}}),
    )
    chats.rapi("message/scheduled/cancel", ok())
    r = await call_tool(server, "update_message", {"chat": GROUP, "message": "999999", "action": "cancel_scheduled"})
    assert r.is_error and chats.calls("message/scheduled/cancel") == []
    r = await call_tool(server, "update_message", {"chat": GROUP, "message": "4", "action": "cancel_scheduled"})
    assert not r.is_error and chats.calls("message/scheduled/cancel")[0].params["scheduledMsgIds"] == [4]


# ---------------------------------------------------------------- D9


async def test_mute_without_minutes_is_an_eternal_mute(server, chats):
    chats.wim("buddylist/Mute", {"response": {"statusCode": 200, "data": {"result": "OK"}}})
    r = await call_tool(server, "update_chat", {"chat": GROUP, "action": "mute"})
    assert not r.is_error, r.content
    sent = [s for s in chats.seen if s.path.endswith("/buddylist/Mute")]
    assert sent and sent[0].query.get("eternal") == "1" and sent[0].query.get("buddy") == GROUP
    assert chats.calls("contacts/mute") == []


# ---------------------------------------------------------------- D4


@pytest.mark.parametrize("reaction", ["not-an-emoji", "ok", "👍👍👍👍👍👍👍👍👍", " "])
async def test_a_reaction_that_is_not_one_emoji_is_refused_before_vk(server, chats, reaction):
    r = await call_tool(
        server,
        "update_message",
        {"chat": GROUP, "message": "1", "action": "react", "reaction": reaction, "user_confirmed": True},
    )
    assert r.is_error and chats.writes() == []


@pytest.mark.parametrize("reaction", ["👍", "❤️", "👍🏽", "🤷‍♂️"])
async def test_one_emoji_is_a_reaction(server, chats, reaction):
    chats.rapi("reaction/add", ok())
    r = await call_tool(
        server,
        "update_message",
        {"chat": GROUP, "message": "1", "action": "react", "reaction": reaction, "user_confirmed": True},
    )
    assert not r.is_error, r.content


# ---------------------------------------------------------------- D5


async def test_history_shows_reaction_counts_and_nothing_once_they_are_taken_back(server, chats):
    from conftest import history_like_vk

    msgs = [
        {"msgId": 1, "time": 1_800_000_000, "outgoing": True, "text": "a", "reactions": {"exist": True}},
        {"msgId": 2, "time": 1_800_000_001, "outgoing": True, "text": "b", "reactions": {"exist": True}},
        {"msgId": 3, "time": 1_800_000_002, "outgoing": True, "text": "c"},
    ]
    chats.rapi("getHistory", history_like_vk(msgs))
    chats.rapi(
        "reaction/get",
        ok(
            {
                "reactions": [
                    {"msgId": 1, "reactions": [{"reaction": "👍", "counter": 0}]},
                    {"msgId": 2, "reactions": [{"reaction": "🔥", "counter": 3}]},
                ]
            }
        ),
    )
    items = {
        m["id"]: m for m in (await call_tool(server, "list_messages", {"chat": GROUP})).structured_content["items"]
    }
    assert "reactions" not in items["1"] and "has_reactions" not in items["1"]
    assert items["2"]["reactions"] == [{"emoji": "🔥", "count": 3}]
    assert len(chats.calls("reaction/get")) == 1  # one call for the page, not one per message


# ---------------------------------------------------------------- D3


async def test_a_range_past_the_end_names_the_length_even_when_vk_does_not(fake, tmp_path, session):
    import httpx
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
    fake.on("GET", "/api/v139/files/get/signed", httpx.Response(416), host=BIN_HOST)
    link = rt.links.download(person, f"https://{BIN_HOST}/api/v139/files/get/signed", "a.bin", size=65536)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as c:
        r = await c.get(urlpath(link["url"]), headers={"Range": "bytes=70000-70100"})
    assert r.status_code == 416 and r.headers["content-range"] == "bytes */65536"


async def test_an_upload_command_turns_curl_globbing_off(make_server, chats):
    """curl expands {a,b} and [1-9] in -T itself, whatever the shell quoting: `{.env,x}` would upload .env."""
    import shlex

    server = make_server("account,chats,messages,people,files")[0]
    r = await call_tool(server, "save_draft", {"chat": GROUP, "text": "x", "attach": ["{.env,report.txt}"]})
    if r.is_error:
        return  # refusing the name is as good as not globbing it
    argv = shlex.split(r.structured_content["upload"][0]["command"])
    assert "--globoff" in argv or "-g" in argv


def test_a_download_command_turns_curl_globbing_off():
    import shlex

    from vkteams_mcp.links.links import Links

    argv = shlex.split(Links("https://mcp.example.org").download("p", "https://x/y", "a[1-9].txt")["command"])
    assert "--globoff" in argv or "-g" in argv


@pytest.mark.parametrize("reaction", ["1️⃣", "#️⃣", "🇷🇺", "☺️", "✅"])
async def test_keycaps_flags_and_older_emoji_are_reactions(server, chats, reaction):
    chats.rapi("reaction/add", ok())
    r = await call_tool(
        server,
        "update_message",
        {"chat": GROUP, "message": "1", "action": "react", "reaction": reaction, "user_confirmed": True},
    )
    assert not r.is_error, (reaction, r.content)


@pytest.mark.parametrize("reaction", ["␀", "©", "1", "→", "★★"])
async def test_symbols_that_are_not_emoji_are_not_reactions(server, chats, reaction):
    r = await call_tool(
        server,
        "update_message",
        {"chat": GROUP, "message": "1", "action": "react", "reaction": reaction, "user_confirmed": True},
    )
    assert r.is_error and chats.writes() == [], reaction  # refused here, not by the server


# ---------------------------------------------------------------- gaps the QA run listed


@pytest.mark.parametrize(("action", "key"), [("archive", "addChats"), ("unarchive", "delChats")])
async def test_archive_and_unarchive_change_only_the_persons_list(server, chats, action, key):
    chats.rapi("archive/chatsModify", ok())
    r = await call_tool(server, "update_chat", {"chat": GROUP, "action": action})
    assert not r.is_error, r.content
    assert chats.calls("archive/chatsModify")[0].params == {key: [GROUP]}


@pytest.mark.parametrize(
    ("action", "method"), [("follow_thread", "thread/subscribe"), ("unfollow_thread", "thread/unsubscribe")]
)
async def test_following_a_thread_subscribes_by_its_id(server, chats, action, method):
    chats.rapi(method, ok())
    r = await call_tool(server, "update_chat", {"chat": GROUP, "action": action})
    assert not r.is_error, r.content
    assert chats.calls(method)[0].params == {"threadId": GROUP}


async def test_a_call_inviting_more_than_twenty_people_is_refused(server, chats):
    people = [f"p{i}@example.com" for i in range(21)]
    r = await call_tool(server, "save_call_link", {"title": "sync", "invite": people, "user_confirmed": True})
    assert r.is_error and chats.writes() == []
