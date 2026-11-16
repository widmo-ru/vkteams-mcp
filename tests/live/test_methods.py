"""Does each method behave the way the spec says? Live calls, response checked against the spec's schema.

Read-only calls first. Then writes that only touch the caller's own account:
a message to yourself (deleted afterwards), a reaction on it, a draft, a folder
created, renamed and deleted. Nobody else sees any of it.
"""

from __future__ import annotations

import time

import pytest

from conftest import placeholders, redact
from vkclient import ApiError


def _record(recorder, validator, path, body, *, ok: bool, note: str = "", params=None, ctx=None):
    v = validator.check_response(path, body) if isinstance(body, dict) else {}
    code = body.get("status", {}).get("code") if isinstance(body, dict) and "status" in body else \
        (body.get("response", {}).get("statusCode") if isinstance(body, dict) else None)
    extra = {"params_example": placeholders(params, ctx or {})} if params is not None else {}
    recorder.add(path=path, http=200, api_status=code, verdict="works" if ok else "fails",
                 note=note, schema=v, sample=redact(body), **extra)
    return v


@pytest.fixture(scope="module")
def ctx(live):
    """Real ids to feed read-only calls: me, one group chat, one folder, one message in that chat."""
    me = live.s.my_sn
    # fetchBaseURL is a cursor. A fresh client replays from the saved one, so the initial
    # buddylist event is there; a client that already polled has moved past it.
    from vkclient import Client, Session
    ev = Client(Session.load()).fetch_events(500)["response"]["data"]["events"]
    buddies = [b for e in ev if e["type"] == "buddylist" for g in e["eventData"]["groups"] for b in g.get("buddies", [])]
    groups = [b["aimId"] for b in buddies if b.get("chatType") == "group"]
    assert groups, "no group chat in buddylist; chat-scoped tests need one"
    chat = groups[0]
    # requestFolders answers {"status":20000} with no results; the list is not in the response.
    folders = (live.rapi("folders/requestFolders").get("results") or {}).get("folders", [])
    folder_id = (folders[0].get("id") or folders[0].get("folderId")) if folders else None
    h = live.rapi("getHistory", {"sn": chat, "fromMsgId": -1, "count": -3, "patchVersion": "init"})["results"]
    msg_id = h["messages"][-1]["msgId"] if h.get("messages") else None
    return {"me": me, "chat": chat, "folder": folder_id, "msg": msg_id}


# (rapi method, params builder over ctx). Only methods that read; no state changes.
READ_ONLY = [
    ("getUserInfo", lambda c: {"sn": c["me"]}),
    ("session/list", lambda c: {}),
    ("folders/requestFolders", lambda c: {}),
        ("archive/getContent", lambda c: {}),
    ("getPrivacySettings", lambda c: {}),
    ("getHistory", lambda c: {"sn": c["me"], "fromMsgId": -1, "count": -5, "patchVersion": "init"}),
    ("getHistoryBatch", lambda c: {"sn": c["chat"], "patchVersion": "init",
                                   "subreqs": [{"fromMsgId": -1, "count": -2, "subreqId": "1"}]}),
    ("getIdInfo", lambda c: {"id": c["me"]}),
    ("getChatInfo", lambda c: {"sn": c["chat"], "memberLimit": 5}),
    ("getChatMembers", lambda c: {"id": c["chat"], "pageSize": 5}),
    ("searchChatMembers", lambda c: {"id": c["chat"], "pageSize": 5, "keyword": "a"}),
    ("getChatMemberInfo", lambda c: {"id": c["chat"], "members": [c["me"]]}),
    ("getChatContacts", lambda c: {"id": c["chat"], "pageSize": 5}),
    ("getChatBlocked", lambda c: {"sn": c["chat"]}),
    ("getPendingList", lambda c: {"sn": c["chat"]}),
    ("getRecentWriters", lambda c: {"sn": c["chat"]}),
    ("getCommonChats", lambda c: {"sn": c["me"]}),
    ("getMsgGallery", lambda c: {"sn": c["chat"],
                                 "subreqs": [{"subreqId": "1", "urlType": "image", "fromMsgId": -1, "msgCount": -5}]}),
    ("reaction/get", lambda c: {"chatId": c["chat"], "msgIds": [c["msg"]]}),
    ("reaction/list", lambda c: {"chatId": c["chat"], "msgId": c["msg"], "reaction": "👍", "limit": 5, "newerThan": ""}),
    ("searchAllDialogs", lambda c: {"pagesize": 5, "filter": {"keyword": "test"}}),
    ("searchOneDialog", lambda c: {"pagesize": 5, "sn": c["chat"], "filter": {"keyword": "test"}}),
    ("findChat", lambda c: {"query": "test"}),
    ("checkNick", lambda c: {"nick": "vkteams_mcp_probe_nick"}),
    ("message/scheduled/history/get", lambda c: {"sn": c["me"], "historyVersion": "0"}),
    ("privacy/groups/inviteBlacklist/get", lambda c: {"pageSize": 5}),
    ("privacy/groups/inviteBlacklist/getBlacklistedContacts", lambda c: {"pageSize": 5}),
    ("privacy/groups/inviteBlacklist/search", lambda c: {"keyword": "a", "pageSize": 5}),
    ("getChatHome", lambda c: {}),
    ("searchThreadsFeed", lambda c: {"filter": {"keyword": "test"}, "pagesize": 5}),
    ("searchChatThreads", lambda c: {"sn": c["chat"], "filter": {"keyword": "test"}}),
]


# Known gaps: the server accepts the call shape but these params are not yet right.
UNRESOLVED = {
    "getChatMemberInfo": "members format unknown: list of sn / list of {sn} / string all -> 'Required parameter not found'",
    "reaction/list": "needs a message that already has reactions; otherwise 'no offset'",
}


@pytest.mark.parametrize("method,params", READ_ONLY, ids=[m for m, _ in READ_ONLY])
def test_read_only(live, validator, recorder, ctx, method, params):
    path = f"/api/v/rapi/{method}"
    p = params(ctx)
    j = live.rapi_raw(method, p).json()
    code = j.get("status", {}).get("code")
    ok = isinstance(code, int) and code // 100 == 200 and not (j.get("results") or {}).get("failures")
    v = _record(recorder, validator, path, j, ok=ok, note=f"params={sorted(p)}", params=p,
                ctx={"me": ctx["me"], "groupChatSn": ctx["chat"], "msgId": ctx["msg"]})
    if not ok and method in UNRESOLVED:
        pytest.xfail(f"{UNRESOLVED[method]}: {j.get('status')}")
    assert ok, j.get("status")
    # Schema drift is recorded, not fatal: the spec is a hypothesis being tested.
    if v.get("envelope_errors") or v.get("results_errors"):
        pytest.xfail(f"spec schema mismatch: {v}")


def test_wim_get_permit_deny(live, validator, recorder):
    j = live.wim_raw("preference/getPermitDeny").json()
    ok = j["response"]["statusCode"] == 200
    _record(recorder, validator, "/api/v/wim/preference/getPermitDeny", j, ok=ok, note="GET works (spec says POST)")
    assert ok


# ---------------------------------------------------------------- writes to self

@pytest.fixture(scope="module")
def own_message(live, validator, recorder):
    """Send a message to yourself via WIM sendIM, yield its msgId, delete it afterwards."""
    me = live.s.my_sn
    text = f"vkteams-mcp spec test {time.strftime('%H:%M:%S')} — удалится автоматически"
    j = live.wim_raw("im/sendIM", {"t": me, "message": text}, method="POST").json()
    ok = j["response"]["statusCode"] == 200
    _record(recorder, validator, "/api/v/wim/im/sendIM", j, ok=ok, note="POST form t=<self>")
    assert ok, j
    # data.msgId is the client request UUID (it comes back as reqId in history); histMsgId is the real id.
    msg_id = j["response"]["data"]["histMsgId"]
    yield {"msgId": msg_id, "reqId": j["response"]["data"]["msgId"], "sn": me, "text": text}
    d = live.rapi_raw("delMsgBatch", {"sn": me, "msgIds": [int(msg_id)], "shared": False}).json()
    _record(recorder, validator, "/api/v/rapi/delMsgBatch", d, ok=d["status"]["code"] == 20000,
            note="cleanup of the test message")


def test_message_appears_in_history(live, own_message, recorder):
    for _ in range(5):
        j = live.rapi("getHistory", {"sn": own_message["sn"], "fromMsgId": -1, "count": -10, "patchVersion": "init"})
        ids = {str(m.get("msgId")) for m in j["results"].get("messages", [])}
        if str(own_message["msgId"]) in ids:
            return
        time.sleep(1)
    pytest.fail("sent message not found in getHistory")


def test_rapi_message_send_to_self(live, validator, recorder):
    """message/send is the rapi way. Config says message-send-api-enabled=false; record what happens."""
    me = live.s.my_sn
    j = live.rapi_raw("message/send", {"target": me, "parts": [{"mediaType": "text", "text": "vkteams-mcp rapi send test"}]}).json()
    code = j["status"]["code"]
    _record(recorder, validator, "/api/v/rapi/message/send", j, ok=code == 20000, note="target=<self>, parts=[text]")
    if code == 20000:
        mid = j["results"].get("msgId")
        if mid:
            live.rapi_raw("delMsgBatch", {"sn": me, "msgIds": [int(mid)], "shared": False})
    else:
        pytest.xfail(f"message/send: {j['status']}")


@pytest.mark.parametrize("variant", ["reaction"])  # reactions/v2/* answer Unknown method on api v139
def test_reaction_roundtrip(live, validator, recorder, own_message, variant):
    me, mid = own_message["sn"], int(own_message["msgId"])
    if variant == "reactions/v2":
        add = {"msgId": mid, "chatSn": me, "reaction": "👍"}
        rem = {"msgId": mid, "chatSn": me}
    else:
        add = {"msgId": mid, "chatId": me, "reaction": "👍", "reactions": ["👍"]}
        rem = {"msgId": mid, "chatId": me}
    a = live.rapi_raw(f"{variant}/add", add).json()
    _record(recorder, validator, f"/api/v/rapi/{variant}/add", a, ok=a["status"]["code"] == 20000, note=f"params={sorted(add)}")
    r = live.rapi_raw(f"{variant}/remove", rem).json()
    _record(recorder, validator, f"/api/v/rapi/{variant}/remove", r, ok=r["status"]["code"] == 20000, note=f"params={sorted(rem)}")
    if a["status"]["code"] != 20000:
        pytest.xfail(f"{variant}/add: {a['status']}")
    assert r["status"]["code"] == 20000


def test_draft_set_and_clear(live, validator, recorder):
    me = live.s.my_sn
    now = int(time.time())
    s = live.rapi_raw("draft/set", {"sn": me, "time": now, "parts": [{"mediaType": "text", "text": "vkteams-mcp draft"}]}).json()
    _record(recorder, validator, "/api/v/rapi/draft/set", s, ok=s["status"]["code"] == 20000, note="set")
    c = live.rapi_raw("draft/set", {"sn": me, "time": now + 1, "parts": []}).json()
    _record(recorder, validator, "/api/v/rapi/draft/set", c, ok=c["status"]["code"] == 20000, note="clear (parts=[])")
    assert s["status"]["code"] == 20000, s["status"]


def test_folder_lifecycle(live, validator, recorder):
    c = live.rapi_raw("folders/create", {"title": "mcp-test", "chats": []}).json()
    _record(recorder, validator, "/api/v/rapi/folders/create", c, ok=c["status"]["code"] == 20000, note="title,chats=[] (type is rejected: additionalProperties false)")
    assert c["status"]["code"] == 20000, c["status"]
    fid = c["results"].get("folderId") or c["results"].get("id") or c["results"].get("folder", {}).get("id")
    if not fid:  # find it by title
        fl = live.rapi("folders/requestFolders")["results"]
        fid = next((f.get("id") or f.get("folderId") for f in fl.get("folders", []) if f.get("title") == "mcp-test"), None)
    assert fid, c["results"]
    try:
        r = live.rapi_raw("folders/rename", {"folderId": fid, "title": "mcp-test-2"}).json()
        _record(recorder, validator, "/api/v/rapi/folders/rename", r, ok=r["status"]["code"] == 20000, note="folderId,title")
    finally:
        d = live.rapi_raw("folders/delete", {"folderId": fid}).json()
        _record(recorder, validator, "/api/v/rapi/folders/delete", d, ok=d["status"]["code"] == 20000, note="folderId")
    assert d["status"]["code"] == 20000, d["status"]
