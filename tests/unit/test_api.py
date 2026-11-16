"""The only module that speaks HTTP to VK Teams: paths, error mapping, session renewal."""

from __future__ import annotations

import httpx
import pytest
from conftest import BASE, HOST, ME, ok, rapi_error, wim_error, wim_ok

from vkteams_mcp.api.session import sign
from vkteams_mcp.core.errors import VKError


async def test_rapi_puts_aimsid_in_the_json_body_under_the_versioned_prefix(fake, vk):
    fake.rapi("getUserInfo", ok({"firstName": "A"}))
    res = await vk.rapi("getUserInfo", {"sn": ME})
    assert res == {"firstName": "A"}
    s = fake.calls("getUserInfo")[0]
    assert s.path == f"{BASE}/rapi/getUserInfo"
    assert s.json["aimsid"] == "aimsid-1" and s.json["params"] == {"sn": ME} and s.json["reqId"]
    assert "aimsid" not in s.query


async def test_a_20001_is_success(fake, vk):
    fake.rapi("message/scheduled/history/get", ok({}, code=20001))
    assert await vk.rapi("message/scheduled/history/get", {"sn": ME, "historyVersion": "0"}) == {}


async def test_partial_failures_come_back_to_the_caller(fake, vk):
    fake.rapi("group/members/add", ok({"failures": [{"id": "x", "error": "bot_setjoingroups_false"}]}, code=20070))
    res = await vk.rapi("group/members/add", {"sn": "1@chat.agent", "members": ["x"]})
    assert res["failures"][0]["error"] == "bot_setjoingroups_false"


@pytest.mark.parametrize(
    "code,reason,key",
    [
        (40000, "Invalid chatId", "failed"),
        (40001, "Permission denied", "forbidden"),
        (40400, "Bad target", "not_found"),
    ],
)
async def test_rapi_errors_become_sentences(fake, vk, code, reason, key):
    fake.rapi("getChatInfo", rapi_error(code, reason))
    with pytest.raises(VKError) as e:
        await vk.rapi("getChatInfo", {"sn": "x"})
    assert e.value.key == key


async def test_a_server_error_on_a_write_is_uncertain_and_on_a_read_is_a_failed_read(fake, vk):
    fake.rapi("message/send", rapi_error(50000, "Request timed out"))
    fake.rapi("getHistory", rapi_error(50000, "Request timed out"))
    with pytest.raises(VKError) as w:
        await vk.rapi("message/send", {}, write=True)
    with pytest.raises(VKError) as r:
        await vk.rapi("getHistory", {})
    assert (w.value.key, r.value.key) == ("uncertain", "failed_read")


async def test_an_expired_aimsid_renews_the_session_once_and_retries(fake, vk, session):
    fake.rapi("getUserInfo", rapi_error(40200, "Invalid token"), ok({"firstName": "A"}))
    fake.wim(
        "aim/startSession",
        wim_ok({"aimsid": "aimsid-2", "fetchBaseURL": "https://x/f", "myInfo": {"aimId": ME}}),
        method="POST",
    )
    assert await vk.rapi("getUserInfo", {"sn": ME}) == {"firstName": "A"}
    calls = fake.calls("getUserInfo")
    assert [c.json["aimsid"] for c in calls] == ["aimsid-1", "aimsid-2"]
    assert session.aimsid == "aimsid-2"
    start = [s for s in fake.seen if s.path.endswith("aim/startSession")][0]
    assert start.form["a"] == "token-a" and start.form["k"] == "dev-id" and start.form["sig_sha256"]


async def test_when_renewal_is_refused_the_person_must_sign_in_again(fake, vk):
    fake.rapi("getUserInfo", rapi_error(40200, "Invalid token"))
    fake.wim("aim/startSession", wim_error(401, "Authentication Required"), method="POST")
    with pytest.raises(VKError) as e:
        await vk.rapi("getUserInfo", {"sn": ME})
    assert e.value.key == "reconnect"
    assert len(fake.calls("getUserInfo")) == 1


async def test_wim_puts_aimsid_and_f_json_in_the_query_and_maps_401(fake, vk):
    fake.wim("im/setTyping", wim_ok())
    await vk.wim("im/setTyping", {"t": ME, "typingStatus": "typing"})
    s = [x for x in fake.seen if x.path.endswith("im/setTyping")][0]
    assert s.query["aimsid"] == "aimsid-1" and s.query["f"] == "json"


async def test_http_429_is_throttled_with_retry_after(fake, vk):
    fake.rapi("getHistory", httpx.Response(429, headers={"Retry-After": "7"}))
    with pytest.raises(VKError) as e:
        await vk.rapi("getHistory", {})
    assert e.value.key == "throttled" and "7 seconds" in e.value.text


async def test_the_reason_from_the_server_is_capped_and_one_line(fake, vk):
    fake.rapi("getChatInfo", rapi_error(40000, "x" * 500 + "\nsecond line"))
    with pytest.raises(VKError) as e:
        await vk.rapi("getChatInfo", {})
    assert "\n" not in e.value.text and len(e.value.text) < 400


def test_the_signature_matches_the_desktop_algorithm():
    # Vector from the implementation a live startSession accepted (2026-11-15): HMAC-SHA256(key, "POST&" + pct(url) + "&" + pct(sorted query)), base64.
    sig = sign("POST", "https://h/api/v1/wim/aim/startSession", {"b": "2", "a": "1 x"}, "k")
    assert sig == "l269wzD4K/Mx0oqLEkztEedSZezD88zmbkxmfGmM1pk="


async def test_events_follow_the_next_fetch_url(fake, vk, session):
    path = f"{BASE}/bos/node-1/aim/fetchEvents"
    fake.on(
        "GET",
        path,
        wim_ok(
            {
                "fetchBaseURL": f"https://{HOST}{path}?aimsid=aimsid-1&seqNum=2",
                "events": [{"type": "myInfo", "eventData": {}}],
            }
        ),
    )
    events = await vk.events()
    assert events[0]["type"] == "myInfo"
    assert session.fetch_base_url.endswith("seqNum=2")


async def test_a_passing_server_error_on_a_read_is_tried_once_more(fake, vk):
    fake.rapi("searchAllDialogs", rapi_error(50000, "Server error"), ok({"dialogs": []}))
    assert await vk.rapi("searchAllDialogs", {}) == {"dialogs": []}
    assert len(fake.calls("searchAllDialogs")) == 2


async def test_a_write_is_never_repeated(fake, vk):
    fake.rapi("message/send", rapi_error(50000, "Server error"), ok({"msgId": 1}))
    with pytest.raises(VKError):
        await vk.rapi("message/send", {}, write=True)
    assert len(fake.calls("message/send")) == 1
