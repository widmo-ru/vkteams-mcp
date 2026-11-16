"""More message and chat features, live, inside the test group or the self chat:
edit, quote-reply, forward, poll, typing, read marks, mute, folders/archive membership,
scheduled message, avatars, link preview, tasks. Shapes from im-desktop where it has them.
"""

from __future__ import annotations

import json
import time

import pytest

from test_features import BOT, GROUP_STATE, rapi, send_text, wim  # noqa: F401  (reuse helpers and group)
from test_features import ctx, group  # noqa: F401  pytest fixtures


def history(live, sn, n=5):
    return live.rapi("getHistory", {"sn": sn, "fromMsgId": -1, "count": -n, "patchVersion": "init"})["results"]


def test_edit_message(live, recorder, validator, ctx, group):
    sn = group["sn"]
    mid = send_text(live, recorder, validator, ctx, sn, "mcp edit: before")
    group["msgs"].append(mid)
    ok, j = wim(live, recorder, validator, ctx, "im/sendIM", {"t": sn, "message": "mcp edit: after", "updateMsgId": mid},
                method="POST", note="edit = sendIM with updateMsgId=<histMsgId>")
    assert ok, j
    m = next(x for x in history(live, sn)["messages"] if x["msgId"] == mid)
    assert m.get("text") == "mcp edit: after", m


@pytest.mark.parametrize("kind", ["quote", "forward"])
def test_quote_and_forward(live, recorder, validator, ctx, group, kind):
    sn, me = group["sn"], ctx["me"]
    src = send_text(live, recorder, validator, ctx, sn, f"mcp {kind} source")
    group["msgs"].append(src)
    m = next(x for x in history(live, sn)["messages"] if x["msgId"] == src)
    parts = [{"mediaType": kind, "text": m["text"], "sn": me, "msgId": src, "time": m["time"],
              "chat": {"sn": sn}}]
    if kind == "quote":
        parts.append({"mediaType": "text", "text": "mcp reply to the quote"})
    target = sn if kind == "quote" else me
    ok, j = wim(live, recorder, validator, ctx, "im/sendIM", {"t": target, "parts": json.dumps(parts, ensure_ascii=False)},
                method="POST", note=f"parts=[{{mediaType:{kind}, text, sn, msgId, time, chat:{{sn}}}}]"
                + (", then {mediaType:text}" if kind == "quote" else ""))
    assert ok, j
    new = j["response"]["data"]["histMsgId"]
    got = next(x for x in history(live, target)["messages"] if x["msgId"] == new)
    recorder.add(path="/api/v/wim/im/sendIM", verdict="works", note=f"{kind}: history parts mediaTypes = "
                 f"{[p.get('mediaType') for p in got.get('parts', [])]}")
    if target == sn:
        group["msgs"].append(new)
    else:
        rapi(live, recorder, validator, ctx, "delMsgBatch", {"sn": me, "msgIds": [new], "shared": False}, note="cleanup")
    assert any(p.get("mediaType") == kind for p in got.get("parts", [])), got


def test_poll(live, recorder, validator, ctx, group):
    sn = group["sn"]
    parts = [{"mediaType": "text", "text": "mcp poll?", "poll": {"type": "anon", "responses": ["yes", "no"]}}]
    ok, j = wim(live, recorder, validator, ctx, "im/sendIM", {"t": sn, "parts": json.dumps(parts)}, method="POST",
                note="poll = parts[{mediaType:text, text, poll:{type:anon, responses:[...]}}]")
    assert ok, j
    mid = j["response"]["data"]["histMsgId"]
    group["msgs"].append(mid)
    m = next(x for x in history(live, sn)["messages"] if x["msgId"] == mid)
    poll = next(p["poll"] for p in m.get("parts", []) if p.get("poll"))
    pid = poll["id"]
    ok, j = rapi(live, recorder, validator, ctx, "poll/get", {"id": pid, "subscribe": True})
    assert ok, j
    answers = j["results"].get("answers") or []
    aid = answers[0]["answerId"] if answers else None  # answers[{answerId:"1", text, votes}]
    assert aid, j["results"]
    for method, p in (("poll/vote", {"id": pid, "answerId": aid}), ("poll/revoke", {"id": pid}),
                      ("poll/stop", {"id": pid})):
        ok, j = rapi(live, recorder, validator, ctx, method, p)
        assert ok, (method, j)


def test_typing_and_read_mark(live, recorder, validator, ctx, group):
    sn = group["sn"]
    for st in ("typing", "none"):
        ok, j = wim(live, recorder, validator, ctx, "im/setTyping", {"t": sn, "typingStatus": st})
        assert ok, j
    last = history(live, sn, 1).get("lastMsgId")
    ok, j = rapi(live, recorder, validator, ctx, "setDlgState", {"sn": sn, "lastRead": last})
    assert ok, j


def test_mute(live, recorder, validator, ctx, group):
    sn = group["sn"]
    for eternal in (1, 0):
        ok, j = wim(live, recorder, validator, ctx, "buddylist/Mute", {"buddy": sn, "eternal": eternal})
        assert ok, j
    ok, j = rapi(live, recorder, validator, ctx, "contacts/mute", {"sn": sn, "duration": 3600, "tieCounter": True})
    assert ok, j
    ok, j = rapi(live, recorder, validator, ctx, "contacts/mute", {"sn": sn, "duration": 0, "tieCounter": False},
                 note="duration 0 = unmute")
    assert ok, j


def test_folder_with_chat(live, recorder, validator, ctx, group):
    sn = group["sn"]
    ok, j = rapi(live, recorder, validator, ctx, "folders/create", {"title": "mcp-f", "chats": [sn]})
    assert ok, j
    fid = j["results"]["folderId"]
    try:
        for method, p in (("folders/getContent", {"folderId": fid}),
                          ("folders/chatPin", {"folderId": fid, "chat": sn, "pin": True}),
                          ("folders/chatPin", {"folderId": fid, "chat": sn, "pin": False}),
                          ("folders/chatsModify", {"folderId": fid, "delChats": [sn]}),
                          ("folders/chatsModify", {"folderId": fid, "addChats": [sn]})):
            ok, j = rapi(live, recorder, validator, ctx, method, p)
            assert ok, (method, p, j)
    finally:
        rapi(live, recorder, validator, ctx, "folders/delete", {"folderId": fid}, note="cleanup")


def test_archive(live, recorder, validator, ctx, group):
    sn = group["sn"]
    ok, j = rapi(live, recorder, validator, ctx, "archive/chatsModify", {"addChats": [sn]})
    assert ok, j
    ok, j = rapi(live, recorder, validator, ctx, "archive/getContent", {})
    assert ok, j
    ok, j = rapi(live, recorder, validator, ctx, "archive/chatsModify", {"delChats": [sn]})
    assert ok, j


def test_scheduled_message(live, recorder, validator, ctx):
    me = ctx["me"]
    at = int(time.time()) + 3600
    for schedule in ({"time": at}, at):
        ok, j = wim(live, recorder, validator, ctx, "im/sendIM",
                    {"t": me, "message": "mcp scheduled", "schedule": json.dumps(schedule) if isinstance(schedule, dict) else at},
                    method="POST", note=f"schedule={'json {time}' if isinstance(schedule, dict) else 'unix ts'}")
        if ok:
            break
    if not ok:
        pytest.xfail(f"schedule format unknown: {j['response']}")
    ok, h = rapi(live, recorder, validator, ctx, "message/scheduled/history/get", {"sn": me, "historyVersion": "0"})
    ids = [m.get("msgId") or m.get("scheduledMsgId") for m in (h.get("results") or {}).get("messages", [])]
    recorder.add(path="/api/v/rapi/message/scheduled/history/get", verdict="works" if ids else "fails",
                 note=f"after scheduling: {len(ids)} scheduled; result keys {sorted((h.get('results') or {}))}")
    ok, j = rapi(live, recorder, validator, ctx, "message/scheduled/cancel", {"sn": me, "cancelAll": True})
    assert ok, j


def test_avatar_and_link_preview(live, recorder, validator, ctx):
    bin_host = live.base.replace("://u.", "://ub.")
    a = live.http.get(f"{bin_host}/files/avatar/get", params={"aimsid": live.s.aimsid, "targetSn": ctx["me"], "size": "128"})  # 128|256|1024
    recorder.add(path="/files/avatar/get", verdict="works" if a.status_code in (200, 304, 404) else "fails",
                 note=f"GET <main-binary-api>/api/v<N>/files/avatar/get?targetSn&size=128|256|1024 -> HTTP {a.status_code} "
                      f"{a.headers.get('content-type')}")
    p = live.http.get(f"{bin_host}/misc/preview/getURLContent",
                      params={"aimsid": live.s.aimsid, "url": "https://example.com/", "f": "json"})
    recorder.add(path="/misc/preview/getURLContent", verdict="works" if p.status_code == 200 else "fails",
                 note=f"GET <main-binary-api>/api/v<N>/misc/preview/getURLContent?url -> HTTP {p.status_code} "
                      f"{p.headers.get('content-type')}")
    assert a.status_code in (200, 304, 404)


def test_tasks_list(live, recorder, ctx):
    r = live.http.post(f"{live.base}/tasks/list", json={"reqId": "1", "aimsid": live.s.aimsid, "params": {}})
    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text[:200]}
    code = (j.get("status") or {}).get("code")
    recorder.add(path="*tasks*", verdict="observed",
                 note=f"tasks live outside rapi: POST /api/v<N>/tasks/list -> HTTP {r.status_code} status {code}; "
                      "rapi/tasks/* -> Unknown method")
    assert r.status_code == 200


# ------------------------------------------------------------------ request shapes, measured live

def _send(live, recorder, validator, ctx, target, parts, note, **extra):
    import uuid
    p = {"target": target, "localMsgId": str(uuid.uuid4()), "parts": parts, **extra}
    ok, j = rapi(live, recorder, validator, ctx, "message/send", p, note=note)
    return ok, j


def test_rapi_message_send_text_quote_edit(live, recorder, validator, ctx, group):
    """parts is an OBJECT {mainPart, quoteParts, forwardParts}, not an array."""
    sn, me = group["sn"], ctx["me"]
    ok, j = _send(live, recorder, validator, ctx, sn, {"mainPart": {"text": {"plain": "mcp rapi send"}}},
                  "parts={mainPart:{text:{plain}}} -> results.msgId, prevMsgId, state, time")
    assert ok, j
    mid = j["results"]["msgId"]
    group["msgs"].append(mid)
    m = next(x for x in history(live, sn)["messages"] if x["msgId"] == mid)
    ok, j = _send(live, recorder, validator, ctx, sn,
                  {"mainPart": {"text": {"plain": "mcp rapi reply"}},
                   "quoteParts": [{"sn": me, "msgId": mid, "time": m["time"], "chat": {"sn": sn},
                                   "text": {"plain": "mcp rapi send"}}]},
                  "quoteParts[{sn, msgId, time, chat:{sn}, text:{plain (minLength 1)}}]")
    assert ok, j
    group["msgs"].append(j["results"]["msgId"])
    ok, j = _send(live, recorder, validator, ctx, sn, {"mainPart": {"text": {"plain": "mcp rapi send (edited)"}}},
                  "edit: updateMsgId", updateMsgId=mid)
    assert ok, j
    m = next(x for x in history(live, sn)["messages"] if x["msgId"] == mid)
    texts = [m.get("text")] + [p.get("text") for p in m.get("parts", []) or []]  # rapi messages keep text in parts
    assert "mcp rapi send (edited)" in texts, m


def test_rapi_scheduled_lifecycle(live, recorder, validator, ctx):
    me = ctx["me"]
    ok, j = _send(live, recorder, validator, ctx, me, {"mainPart": {"text": {"plain": "mcp scheduled"}}},
                  "schedule:{scheduledTime: unix seconds} -> 20020 'Scheduled message stored to queue'",
                  schedule={"scheduledTime": int(time.time()) + 7200})
    assert ok and j["status"]["code"] == 20020, j
    sid = j["results"]["scheduled"]["scheduledMsgId"]
    ok, h = rapi(live, recorder, validator, ctx, "message/scheduled/history/get", {"sn": me, "historyVersion": "0"},
                 note="results.messages is an object {new:[{scheduledMsgId, scheduledTime, parts}]}")
    assert ok and any(x["scheduledMsgId"] == sid for x in h["results"]["messages"].get("new", [])), h
    ok, j = rapi(live, recorder, validator, ctx, "message/scheduled/cancel", {"sn": me, "scheduledMsgIds": [sid]})
    assert ok, j
    ok, h = rapi(live, recorder, validator, ctx, "message/scheduled/history/get", {"sn": me, "historyVersion": "0"})
    assert h["results"].get("queuedMessagesCount", 0) == 0 or sid not in [
        x["scheduledMsgId"] for x in h["results"]["messages"].get("new", [])], h


def test_thread_subscribers_search(live, recorder, validator, ctx, group):
    sn = group["sn"]
    mid = send_text(live, recorder, validator, ctx, sn, "mcp thread search root")
    group["msgs"].append(mid)
    ok, j = rapi(live, recorder, validator, ctx, "thread/add", {"parentTopic": {"type": "message", "chatId": sn, "messageId": mid}})
    assert ok, j
    ok, j = rapi(live, recorder, validator, ctx, "thread/subscribers/search",
                 {"threadId": j["results"]["threadId"], "filter": {"keyword": "a"}, "pageSize": 10},
                 note="keyword goes inside filter")
    assert ok, j


def other_group(exclude):
    from vkclient import Client, Session
    ev = Client(Session.load()).fetch_events(500)["response"]["data"]["events"]
    return next(b["aimId"] for e in ev if e["type"] == "buddylist" for g in e["eventData"]["groups"]
                for b in g.get("buddies", []) if b.get("chatType") == "group" and b["aimId"] != exclude)


def test_folders_reorder_and_pins_order(live, recorder, validator, ctx, group):
    """Folders are per-user; putting an existing chat into a temporary folder changes nothing for others.
    The self chat is not allowed in folders: 40003 'addChats has invalid items'."""
    sn = group["sn"]
    sn2 = other_group(sn)
    ids = []
    try:
        for t in ("mcp-a", "mcp-b"):
            ok, j = rapi(live, recorder, validator, ctx, "folders/create", {"title": t, "chats": [sn, sn2]})
            assert ok, j
            ids.append(j["results"]["folderId"])
        ok, j = rapi(live, recorder, validator, ctx, "folders/reorder", {"newFolderIdsOrder": list(reversed(ids))},
                     note="all of the user's folder ids in the new order")
        recorder.add(path="/api/v/rapi/folders/reorder", verdict="observed", note=f"partial id list -> {j['status']}")
        for chat in (sn, sn2):
            rapi(live, recorder, validator, ctx, "folders/chatPin", {"folderId": ids[0], "chat": chat, "pin": True})
        ok, j = rapi(live, recorder, validator, ctx, "folders/changePinsOrder", {"folderId": ids[0], "pins": [sn2, sn]},
                     note="pins: all pinned chats in the new order, minItems 2")
        assert ok, j
    finally:
        for fid in ids:
            rapi(live, recorder, validator, ctx, "folders/delete", {"folderId": fid}, note="cleanup")


def test_invite_blacklist_roundtrip(live, recorder, validator, ctx):
    ok, j = rapi(live, recorder, validator, ctx, "privacy/groups/inviteBlacklist/add", {"users": [BOT]})
    assert ok, j
    ok, j = rapi(live, recorder, validator, ctx, "privacy/groups/inviteBlacklist/get", {"pageSize": 10})
    assert ok, j
    ok, j = rapi(live, recorder, validator, ctx, "privacy/groups/inviteBlacklist/del", {"users": [BOT]},
                 note="{users:[sn]} or {everyone:true}")
    assert ok, j
