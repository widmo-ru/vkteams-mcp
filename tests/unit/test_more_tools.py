"""people, calls, bots, files, account."""

from __future__ import annotations

from conftest import BIN_HOST, ME, call_tool, ok, text_of


async def test_find_people_searches_the_directory(server, chats):
    chats.rapi(
        "search",
        ok(
            {
                "data": [
                    {"sn": "ann@example.com", "anketa": {"friendly": "Ann Smith"}},
                    {"sn": "9@chat.agent", "anketa": {"friendly": "a chat"}},
                ]
            }
        ),
    )
    r = (await call_tool(server, "find_people", {"query": "Ann"})).structured_content
    assert [i["person"] for i in r["items"]] == ["ann@example.com"]
    assert chats.calls("search")[0].params == {"keyword": "Ann"}


async def test_find_people_by_address_returns_a_profile(server, chats):
    chats.rapi("getUserInfo", ok({"friendly": "Ann Smith", "userState": {"lastseen": 1_800_000_000}}))
    chats.rapi("getCommonChats", ok({"chats": [{"sn": "1001@chat.agent"}]}))
    item = (await call_tool(server, "find_people", {"query": "ann@example.com"})).structured_content["items"][0]
    assert item["common_chats"] == ["1001@chat.agent"] and item["last_seen"]


async def test_a_call_link_alone_needs_no_confirmation(server, chats):
    chats.rapi("conference/create", ok({"conferenceUrl": "https://call.example.com/x", "conferencePin": "1234"}))
    r = (await call_tool(server, "save_call_link", {"title": "Sync"})).structured_content
    assert r["link"] == "https://call.example.com/x" and r["pin"] == "1234"
    assert chats.calls("conference/create")[0].params == {
        "name": "Sync",
        "type": "equitable",
        "participants": [],
        "callParticipants": False,
    }


async def test_inviting_to_a_call_needs_confirmation(server, chats):
    r = await call_tool(server, "save_call_link", {"title": "Sync", "invite": ["ann@example.com"]})
    assert r.is_error and chats.writes() == []


async def test_bots_need_confirmation_and_press_needs_a_button(server, chats):
    r = await call_tool(server, "respond_to_bot", {"bot": "70001", "action": "start"})
    assert r.is_error
    r = await call_tool(server, "respond_to_bot", {"bot": "70001", "action": "press", "user_confirmed": True})
    assert r.is_error and "button" in text_of(r)
    chats.rapi("startBot", ok())
    r = await call_tool(server, "respond_to_bot", {"bot": "70001", "action": "start", "user_confirmed": True})
    assert not r.is_error and chats.calls("startBot")[0].params == {"bot": "70001", "startParameter": ""}


async def test_download_gives_a_signed_link_not_bytes(server, chats):
    chats.on(
        "GET",
        "/api/v139/files/info/abc123/",
        {
            "status": {"code": 20000},
            "result": {
                "info": {
                    "file_name": "a.pdf",
                    "file_size": 10,
                    "dlink": f"https://{BIN_HOST}/api/v139/files/get/signed",
                    "antivirus": {"check_result": "unchecked"},
                }
            },
        },
    )
    r = (await call_tool(server, "download_file", {"file": "https://files.example.com/get/abc123"})).structured_content
    assert r["url"].startswith("https://mcp.example.org/files/d/") and r["command"].startswith("curl -fsS --globoff -o")
    assert "signed" not in str(r)  # the upstream link stays on the server


async def test_a_non_file_is_refused(server, chats):
    r = await call_tool(server, "download_file", {"file": "../../etc/passwd"})
    assert r.is_error and chats.seen == []


async def test_get_connection_names_the_person_and_server(server, chats):
    chats.rapi("getUserInfo", ok({"friendly": "Test Person"}))
    r = (await call_tool(server, "get_connection", {})).structured_content
    assert r["signed_in_as"] == f"Test Person <{ME}>" and r["server"] == "teams.example.com"
    assert "chat_admin" in r["areas_off"]


async def test_a_link_that_is_not_a_vk_file_is_refused(server, chats):
    for bad in ("https://unrelated.example/page/F1", "ftp://x.example/get/F1", "https://[broken/get/F1"):
        r = await call_tool(server, "download_file", {"file": bad})
        assert r.is_error, bad
    assert chats.seen == []
