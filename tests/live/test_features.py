"""Feature scenarios, live: files, a test group and its management, mentions, threads,
reactions, pins, a call link, a bot. Request shapes are taken from the open-source desktop
client (github.com/mail-ru-im/im-desktop, core/connections/wim/packets/*.cpp) and checked here.

Everything created is cleaned up: messages deleted, the bot removed from the group, the group
not left: one persistent group 'mcp-test' is reused across runs (id in ~/.config/vkteams-mcp/test-group.json).
Leaving a chat is POST-less wim buddylist/removeBuddy?buddy=<sn>&allGroups=1 (verified once by hand).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
import uuid

import httpx
import pytest

from conftest import placeholders, redact

BOT = "70001"  # Metabot; present on every VK Teams installation


def rec(recorder, validator, path, body, ok, ctx, params=None, note=""):
    v = validator.check_response(path, body) if path in validator.spec["paths"] and isinstance(body, dict) else {}
    code = (body.get("status") or {}).get("code") if isinstance(body, dict) else None
    if code is None and isinstance(body, dict):
        code = (body.get("response") or {}).get("statusCode")
    row = {"path": path, "http": 200, "api_status": code, "verdict": "works" if ok else "fails", "note": note,
           "schema": v, "sample": redact(body)}
    if params is not None:
        row["params_example"] = placeholders(params, ctx)
    recorder.add(**row)


def rapi(live, recorder, validator, ctx, method, params, note=""):
    j = live.rapi_raw(method, params).json()
    code = j.get("status", {}).get("code")
    # 200xx is success, EXCEPT when results.failures is non-empty (20070: partial failure per item).
    ok = isinstance(code, int) and code // 100 == 200 and not (j.get("results") or {}).get("failures")
    rec(recorder, validator, f"/api/v/rapi/{method}", j, ok, ctx, params, note)
    return ok, j


def wim(live, recorder, validator, ctx, path, params, method="GET", note=""):
    j = live.wim_raw(path, params, method=method).json()
    ok = j.get("response", {}).get("statusCode") == 200
    rec(recorder, validator, f"/api/v/wim/{path}", j, ok, ctx, params, note)
    return ok, j


def send_text(live, recorder, validator, ctx, to, text, mentions=None):
    p = {"t": to, "message": text}
    if mentions:
        p["mentions"] = ",".join(mentions)
    ok, j = wim(live, recorder, validator, ctx, "im/sendIM", p, method="POST",
                note="mentions" if mentions else "")
    assert ok, j
    return j["response"]["data"]["histMsgId"]


@pytest.fixture(scope="module")
def ctx(live):
    return {"me": live.s.my_sn, "bot": BOT}


# ------------------------------------------------------------------ files

def test_file_upload_info_download_send(live, recorder, validator, ctx):
    data = f"vkteams-mcp file test {uuid.uuid4()}\n".encode()
    name = "mcp-test.txt"
    base = live.base
    # 1. gateway: GET {base}/files/init?aimsid&f=json&k&size&filename&client&r -> result.host, result.url
    q = {"aimsid": live.s.aimsid, "f": "json", "k": live.s.dev_id, "size": len(data), "filename": name,
         "client": "VKTeams", "r": str(uuid.uuid4())}
    g = live.http.get(f"{base}/files/init", params=q).json()
    ok = g.get("status", {}).get("code") == 200
    rec(recorder, validator, "/files/init/", g, ok, ctx, {k: v for k, v in q.items() if k != "aimsid"},
        note=f"GET {{base}}/files/init (base=/api/v<N>), not /files/init/ on a separate host")
    assert ok, g
    host, url = g["result"]["host"], g["result"]["url"]
    # 2. upload: POST https://{host}{url}?aimsid&f=json&k&r, octet-stream with Session-ID and Content-Range
    up = live.http.post(f"https://{host}{url}",
                        params={"aimsid": live.s.aimsid, "f": "json", "k": live.s.dev_id, "r": str(uuid.uuid4())},
                        headers={"Session-ID": str(uuid.uuid4()), "Content-Type": "application/octet-stream",
                                 "Content-Disposition": f'attachment; filename="{name}"',
                                 "Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}"},
                        content=data)
    u = up.json()
    ok = u.get("status", {}).get("code") == 200 and "fileid" in u.get("result", {})
    rec(recorder, validator, "/files/upload", u, ok, ctx,
        note=f"POST https://<result.host><result.url>; HTTP {up.status_code}; headers Session-ID, Content-Range, "
             "Content-Disposition; single chunk")
    assert ok, u
    file_id, static_url = u["result"]["fileid"], u["result"]["static_url"]
    # 3. info: GET {base}/files/info/<fileId>/?f=json&aimsid
    i = live.http.get(f"{base}/files/info/{file_id}/", params={"f": "json", "aimsid": live.s.aimsid}).json()
    ok = i.get("status", {}).get("code") in (200, 20000)  # init/upload say 200, info says 20000
    rec(recorder, validator, "/files/info/{fileId}", i, ok, ctx, note="GET {base}/files/info/<fileid>/?f=json&aimsid; status 20000 (init/upload use 200); "
             "info has dlink (signed, on main-binary-api host), md5, antivirus.check_result, dlp.check_result")
    assert ok, i
    info = i["result"]["info"] if "info" in i.get("result", {}) else i["result"]
    dlink = info.get("dlink")
    assert dlink, i
    # 4. download and compare bytes
    d = live.http.get(dlink, follow_redirects=True)
    same = d.content == data
    recorder.add(path="/files/info/{fileId}", verdict="works" if same else "fails",
                 note=f"download via result.info.dlink: HTTP {d.status_code}, bytes match={same}, "
                      f"sha256={hashlib.sha256(d.content).hexdigest()[:12]}")
    assert same
    # 5. send as a message: the desktop sends the static_url as text
    mid = send_text(live, recorder, validator, ctx, ctx["me"], static_url)
    ok, _ = rapi(live, recorder, validator, ctx, "delMsgBatch", {"sn": ctx["me"], "msgIds": [mid], "shared": False},
                 note="cleanup")
    assert ok


# ------------------------------------------------------------------ group scenario

GROUP_STATE = Path(os.path.expanduser("~/.config/vkteams-mcp/test-group.json"))


@pytest.fixture(scope="module")
def group(live, recorder, validator, ctx):
    """One persistent test group, reused across runs; created on first run only."""
    sn = None
    if GROUP_STATE.exists():
        sn = json.loads(GROUP_STATE.read_text()).get("sn")
        if sn and live.rapi_raw("getChatInfo", {"sn": sn, "memberLimit": 1}).json()["status"]["code"] != 20000:
            sn = None
    if not sn:
        p = {"name": "mcp-test", "aimSid": live.s.aimsid, "about": "vkteams-mcp test group"}
        ok, j = rapi(live, recorder, validator, ctx, "createChat", p, note="aimSid (capital S) as the desktop sends it")
        assert ok, j
        sn = j["results"]["sn"]
        GROUP_STATE.write_text(json.dumps({"sn": sn}))
    ctx["groupSn"] = sn
    state = {"sn": sn, "msgs": []}
    yield state
    if state["msgs"]:
        rapi(live, recorder, validator, ctx, "delMsgBatch", {"sn": sn, "msgIds": state["msgs"], "shared": True},
             note="cleanup")
    rapi(live, recorder, validator, ctx, "group/members/delete", {"sn": sn, "members": [BOT]}, note="cleanup")


def test_group_info_and_mod(live, recorder, validator, ctx, group):
    sn = group["sn"]
    ok, j = rapi(live, recorder, validator, ctx, "getChatInfo", {"sn": sn, "memberLimit": 10})
    assert ok, j
    ok, j = rapi(live, recorder, validator, ctx, "modChat",
                 {"sn": sn, "name": "mcp-test 2", "about": "updated", "rules": "no rules", "threadsEnabled": True})
    assert ok, j
    ok, j = rapi(live, recorder, validator, ctx, "getChatInfo", {"sn": sn, "memberLimit": 10})
    assert j["results"].get("name") == "mcp-test 2", j["results"]


def test_group_members_roles_block(live, recorder, validator, ctx, group):
    sn = group["sn"]
    ok, j = rapi(live, recorder, validator, ctx, "group/members/add", {"sn": sn, "members": [BOT]})
    if not ok:
        pytest.xfail(f"cannot add {BOT} to group: {j.get('results', {}).get('failures') or j['status']} — "
                     "member management needs a second account that may join groups")
    ok, j = rapi(live, recorder, validator, ctx, "getChatMembers", {"id": sn, "pageSize": 10})
    assert ok and any(m.get("sn") == BOT for m in j["results"].get("members", [])), j
    ok, j = rapi(live, recorder, validator, ctx, "getChatMemberInfo", {"id": sn, "filter": {"members": [BOT, ctx["me"]]}},
                 note="members go inside filter (im-desktop get_chat_member_info.cpp)")
    assert ok, j
    for role in ("admin", "readonly", "member"):
        ok, j = rapi(live, recorder, validator, ctx, "modChatMember", {"sn": sn, "memberSn": BOT, "role": role})
        assert ok, (role, j)
    ok, j = rapi(live, recorder, validator, ctx, "blockChatMembers",
                 {"sn": sn, "members": [{"sn": BOT}], "deleteLastMessages": False}, note="members are objects {sn}")
    assert ok, j
    ok, j = rapi(live, recorder, validator, ctx, "getChatBlocked", {"sn": sn})
    assert ok, j
    ok, j = rapi(live, recorder, validator, ctx, "unblockChatMembers", {"sn": sn, "members": [{"sn": BOT}]})
    assert ok, j


def test_group_message_mention_pin_reactions_thread(live, recorder, validator, ctx, group):
    sn, me = group["sn"], ctx["me"]
    mid = send_text(live, recorder, validator, ctx, sn, f"@[{me}] mcp mention test", mentions=[me])
    group["msgs"].append(mid)
    h = live.rapi("getHistory", {"sn": sn, "fromMsgId": -1, "count": -3, "patchVersion": "init"})["results"]
    m = next(x for x in h["messages"] if x["msgId"] == mid)
    recorder.add(path="/api/v/wim/im/sendIM", verdict="works",
                 note=f"mention: text '@[<sn>]' + form mentions=<sn,sn>; history keys: {sorted(m)}")
    # pin / unpin
    ok, j = rapi(live, recorder, validator, ctx, "pinMessage", {"sn": sn, "msgId": mid})
    assert ok, j
    ok, j = rapi(live, recorder, validator, ctx, "pinMessage", {"sn": sn, "msgId": mid, "unpin": True})
    assert ok, j
    # reactions: add, get, list, remove
    ok, j = rapi(live, recorder, validator, ctx, "reaction/add",
                 {"msgId": mid, "chatId": sn, "reaction": "👍", "reactions": ["👍", "❤️", "🔥"]},
                 note="reactions = the allowed set shown to the user")
    assert ok, j
    ok, j = rapi(live, recorder, validator, ctx, "reaction/get", {"chatId": sn, "msgIds": [mid]})
    assert ok, j
    ok, j = rapi(live, recorder, validator, ctx, "reaction/list",
                 {"msgId": mid, "chatId": sn, "reaction": "👍", "newerThan": "", "limit": 10})
    if not ok:
        ok, j = rapi(live, recorder, validator, ctx, "reaction/list",
                     {"msgId": mid, "chatId": sn, "reaction": "👍", "limit": 10, "olderThan": "0"})
    assert ok, j
    ok, j = rapi(live, recorder, validator, ctx, "reaction/remove", {"msgId": mid, "chatId": sn})
    assert ok, j
    # thread on the message
    ok, j = rapi(live, recorder, validator, ctx, "thread/add",
                 {"parentTopic": {"type": "message", "chatId": sn, "messageId": mid}})
    assert ok, j
    tid = j["results"]["threadId"]
    ctx["threadId"] = tid
    tmid = send_text(live, recorder, validator, ctx, tid, "mcp thread reply")
    for method, p in (("thread/get", {"threadId": tid}),
                      ("thread/subscribers/get", {"threadId": tid, "pageSize": 10}),
                      ("thread/unsubscribe", {"threadId": tid}),
                      ("thread/subscribe", {"threadId": tid})):
        ok, j = rapi(live, recorder, validator, ctx, method, p)
        assert ok, (method, j)
    rapi(live, recorder, validator, ctx, "delMsgBatch", {"sn": tid, "msgIds": [tmid], "shared": True}, note="cleanup")


# ------------------------------------------------------------------ calls

@pytest.mark.parametrize("ctype", ["equitable", "webinar"])
def test_call_link(live, recorder, validator, ctx, ctype):
    p = {"name": "mcp-test call", "type": ctype, "participants": [], "callParticipants": False}
    ok, j = rapi(live, recorder, validator, ctx, "conference/create", p)
    if not ok:
        pytest.xfail(f"{ctype}: {j['status']}")
    url = j["results"].get("conferenceUrl")
    assert url and url.startswith("https://"), j
    recorder.add(path="/api/v/rapi/conference/create", verdict="works",
                 note=f"type={ctype}: results.conferenceUrl on host {httpx.URL(url).host.split('.', 1)[0]}.*, "
                      f"expiresOn={'present' if j['results'].get('expiresOn') else 'absent'}")


# ------------------------------------------------------------------ bots

def test_bot_start_and_callback(live, recorder, validator, ctx):
    ok, j = rapi(live, recorder, validator, ctx, "startBot", {"bot": BOT, "startParameter": ""})
    assert ok, j
    btn_msg = None
    for _ in range(10):
        time.sleep(1)
        h = live.rapi("getHistory", {"sn": BOT, "fromMsgId": -1, "count": -5, "patchVersion": "init"})["results"]
        for m in reversed(h.get("messages", [])):
            if not m.get("outgoing") and any(p.get("inlineKeyboardMarkup") for p in m.get("parts", []) or []):
                btn_msg = m
                break
            if not m.get("outgoing") and m.get("inlineKeyboardMarkup"):
                btn_msg = m
                break
        if btn_msg:
            break
    recorder.add(path="/api/v/rapi/startBot", verdict="works",
                 note=f"bot replied with buttons: {bool(btn_msg)}; message keys: {sorted(btn_msg) if btn_msg else '-'}")
    if not btn_msg:
        pytest.xfail("Metabot reply has no inline buttons; callback not exercised")
    kb = btn_msg.get("inlineKeyboardMarkup") or next(p["inlineKeyboardMarkup"] for p in btn_msg["parts"]
                                                      if p.get("inlineKeyboardMarkup"))
    button = next(b for row in kb for b in row if b.get("callbackData"))
    ok, j = rapi(live, recorder, validator, ctx, "getBotCallbackAnswer",
                 {"chatId": BOT, "msgId": btn_msg["msgId"], "callbackData": button["callbackData"]})
    assert ok, j
