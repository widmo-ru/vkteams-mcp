"""What the tools share: the chat index, name resolution, projections, local time.

Upstream facts (docs/live-findings.md): the chat list is not a rapi method; it arrives as the
`buddylist` and `histDlgState` events of the event stream. A person is an e-mail address; a group or
channel is `<n>@chat.agent`; a thread is a chat of its own.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from vkteams_mcp.core import context
from vkteams_mcp.core.content import content_block, content_line, name
from vkteams_mcp.core.context import Call
from vkteams_mcp.core.errors import VKError, invalid
from vkteams_mcp.core.project import compact

INDEX_TTL = 60.0
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CHAT_SN = re.compile(r"^\d+@chat\.agent$")
# Message ids run past 2^53 (live ids look like 7700000000000000301). A JSON number that large is rounded by
# any JavaScript client, so ids travel as strings of digits both ways and become ints only towards VK Teams.
MSG_ID_PATTERN = r"^\d{1,20}$"


MAX_MSG_ID = 2**64 - 1


def msg_id(value: str) -> int:
    n = int(value)
    if n > MAX_MSG_ID:  # the pattern allows 20 digits; VK Teams answers "Invalid json" past 64 bits
        raise invalid("That is not a message id: ids come from list_messages. Nothing was done.")
    return n


def id_text(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


def is_chat_sn(value: str) -> bool:
    return bool(_CHAT_SN.match(value))


def is_address(value: str) -> bool:
    return bool(_EMAIL.match(value)) and not is_chat_sn(value)


def is_bot_id(value: str) -> bool:
    """People are e-mail addresses and groups are `<n>@chat.agent`; a bot has an id with no @ (live: `70001`)."""
    return bool(value) and "@" not in value and bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value))


def kind_of(sn: str, chat_type: str | None = None) -> str:
    if is_bot_id(sn):
        return "bot"
    if not is_chat_sn(sn):
        return "person"
    return "channel" if chat_type == "channel" else "group"


# ---------------------------------------------------------------- chat index


async def chat_index(c: Call) -> dict[str, dict[str, Any]]:
    """sn -> {title, kind, unread, last_id, last_at, last_text, last_from}, from one snapshot of the
    event stream. Cached per person for a minute."""
    cache: dict = c.rt.__dict__.setdefault("_chat_index", {})
    hit = cache.get(c.person)
    if hit and hit[0] > time.time():
        return hit[1]
    events = await c.vk.snapshot()
    if not any(e.get("type") in ("buddylist", "histDlgState") for e in events):
        await c.vk.renew()
        events = await c.vk.snapshot()
    index: dict[str, dict[str, Any]] = {}
    names: dict[str, str] = {}
    for e in events:
        d = e.get("eventData") or {}
        if e.get("type") == "buddylist":
            for g in d.get("groups") or []:
                for b in g.get("buddies") or []:
                    sn = b.get("aimId")
                    if sn:
                        index.setdefault(sn, {})
                        index[sn].update(title=b.get("friendly") or sn, kind=kind_of(sn, b.get("chatType")))
        elif e.get("type") == "histDlgState":
            sn = d.get("sn")
            if not sn:
                continue
            for p in d.get("persons") or []:
                if p.get("sn") and p.get("friendly"):
                    names[p["sn"]] = p["friendly"]
            msgs = d.get("messages") or []
            last = msgs[-1] if msgs else {}
            entry = index.setdefault(sn, {"kind": kind_of(sn)})
            entry.update(
                unread=d.get("unreadCnt") or 0,
                last_id=d.get("lastMsgId"),
                last_at=last.get("time"),
                last_text=message_text(last),
                last_from=None if last.get("outgoing") else (last.get("chat") or {}).get("sender", sn),
            )
    for sn, entry in index.items():
        entry.setdefault("title", names.get(sn, sn))
        entry["names"] = names
    cache[c.person] = (time.time() + INDEX_TTL, index)
    return index


def forget_index(c: Call) -> None:
    c.rt.__dict__.setdefault("_chat_index", {}).pop(c.person, None)


async def resolve_chat(c: Call, chat: str) -> str:
    """A chat named by id, address or title → its sn. Ambiguity lists candidates (rule 7)."""
    chat = (chat or "").strip()
    if not chat:
        raise invalid("chat is required: a title, an e-mail address or a chat id from list_chats.")
    if is_chat_sn(chat) or is_address(chat):
        return chat
    index = await chat_index(c)
    if is_bot_id(chat) and chat in index:
        return chat  # a bot's id as list_chats shows it
    q = chat.casefold()
    exact = [sn for sn, e in index.items() if str(e.get("title", "")).casefold() == q]
    if len(exact) == 1:
        return exact[0]
    found = exact or [sn for sn, e in index.items() if q in str(e.get("title", "")).casefold()]
    if len(found) == 1:
        return found[0]
    if not found:
        raise VKError("unresolved", kind="chat", query=chat)
    return _ambiguous("chat", chat, [(sn, index[sn].get("title", sn)) for sn in found])


def _ambiguous(kind: str, query: str, pairs: list[tuple[str, str]]):
    shown = "; ".join(f"{content_line('VK Teams', title)} = {sn}" for sn, title in pairs[:6])
    more = f" and {len(pairs) - 6} more" if len(pairs) > 6 else ""
    raise VKError("ambiguous", kind=kind, query=query, candidates=shown + more)


async def resolve_person(c: Call, who: str) -> str:
    """A person named by address or name → their address. The directory search answers names."""
    who = (who or "").strip().lstrip("@")
    if not who:
        raise invalid("A person is required: a name or an e-mail address.")
    if is_address(who):
        return who
    res = await c.vk.rapi("search", {"keyword": who})
    people = [
        (d.get("sn"), (d.get("anketa") or {}).get("friendly") or d.get("sn"))
        for d in res.get("data") or []
        if d.get("sn") and not is_chat_sn(d["sn"])
    ]
    exact = [p for p in people if str(p[1]).casefold() == who.casefold()]
    if len(exact) == 1:
        return exact[0][0]
    if len(people) == 1:
        return people[0][0]
    if not people:
        raise VKError("unresolved", kind="person", query=who)
    return _ambiguous("person", who, people)


async def fetch_message(c: Call, sn: str, msg_id: int) -> tuple[dict, list[dict]]:
    """One message by id, with the persons of its page. getHistory's fromMsgId is EXCLUSIVE (live
    2026-11-15: count 1 gives the next newer message, count -1 the previous one), so the message X is
    asked for as fromMsgId X+1, count -1, and checked."""
    h = await c.vk.rapi("getHistory", {"sn": sn, "fromMsgId": msg_id + 1, "count": -1, "patchVersion": "init"})
    m = next((x for x in h.get("messages") or [] if x.get("msgId") == msg_id), None)
    if m is None:
        raise VKError("not_found")
    return m, h.get("persons") or []


# ---------------------------------------------------------------- projections


def message_text(m: dict) -> str:
    if m.get("text"):
        return str(m["text"])
    out = []
    for p in m.get("parts") or []:
        if p.get("mediaType") in (None, "text") and p.get("text"):
            out.append(str(p["text"]))
        elif (p.get("captionedContent") or {}).get("caption"):
            out.append(str(p["captionedContent"]["caption"]))
    return "\n".join(out)


def project_message(m: dict, chat: str, names: dict[str, str], tz: ZoneInfo) -> dict[str, Any]:
    """One message as the person reads it. Service events (joined, renamed) are marked as such."""
    sender = (m.get("chat") or {}).get("sender") or (None if m.get("outgoing") else chat)
    author = "you" if m.get("outgoing") else name(names.get(sender or "", sender))
    text = message_text(m)
    quote = next((p for p in m.get("parts") or [] if p.get("mediaType") in ("quote", "forward")), None)
    poll = next((p.get("poll") for p in m.get("parts") or [] if p.get("poll")), None)
    # filesharing (live 2026-11-15): [{id, name, size (string), mime, original_url, antivirus_check, ...}]
    files = [
        compact(
            {
                "id": f.get("id"),
                "name": content_line("file name", str(f.get("name") or "")),
                "size": f.get("size"),
                "mime": f.get("mime"),
            }
        )
        for f in (m.get("filesharing") or [])
        if isinstance(f, dict) and f.get("id")
    ]
    out = {
        "id": id_text(m.get("msgId")),
        "at": local(m.get("time"), tz),
        "from": author,
        "from_id": None if m.get("outgoing") else sender,
        "event": bool(m.get("class") or m.get("eventTypeId")),
        "text": content_block(str(author or "someone"), text) if text else None,
        "mentions": [name(names.get(x, x)) for x in (m.get("mentions") or [])]
        if isinstance(m.get("mentions"), list)
        else None,
        "reply_to": id_text(quote.get("msgId")) if quote else None,
        "forwarded": bool(quote and quote.get("mediaType") == "forward"),
        "poll": poll.get("id") if isinstance(poll, dict) else None,
        "has_reactions": bool((m.get("reactions") or {}).get("exist")),
        "thread": (m.get("thread") or {}).get("threadId") if isinstance(m.get("thread"), dict) else None,
        "files": files or None,
        "buttons": buttons(m) or None,
        "read_by": m.get("readsCount"),
    }
    return compact(out)


def keyboard(m: dict) -> list[dict]:
    """A bot's inline keyboard, flattened in reading order: the order `button` numbers count in."""
    return [
        b for row in m.get("inlineKeyboardMarkup") or [] if isinstance(row, list) for b in row if isinstance(b, dict)
    ][:40]


BUTTON_PATTERN = r"^\d{1,2}\.[0-9a-f]{6}$"


def button_token(i: int, b: dict) -> str:
    """A button's number and a short print of what it is. A bot can edit its keyboard after the agent read it;
    the print makes the same number on a changed keyboard a refusal instead of a press of another button.
    The print is keyed by the server's secret: the bot chooses the text and data, and an unkeyed 24-bit print
    let it find a different button with the same print in under a minute (QA D25)."""
    # each field encoded on its own: joined by newlines, a line moved from the label into the data kept the print
    seen = json.dumps([str(b.get(k, "")) for k in ("text", "callbackData", "url")], ensure_ascii=False).encode()
    key = hashlib.sha256(b"button-key\n" + context.runtime().settings.secret.encode()).digest()
    return f"{i}.{hmac.new(key, seen, hashlib.sha256).hexdigest()[:6]}"


def buttons(m: dict) -> list[dict[str, Any]]:
    """A bot's inline keyboard: often the bot's whole answer (live: Metabot lists its commands as buttons).
    Each button is shown by number: its callback data is the bot's text and could carry instructions, so it
    stays on this server, and respond_to_bot looks it up again by the number. A url button opens a page."""
    return [
        compact(
            {
                "button": button_token(i, b),
                "text": content_line("bot", str(b.get("text", ""))),
                "url": content_line("link", str(b["url"])) if b.get("url") else None,
            }
        )
        for i, b in enumerate(keyboard(m), 1)
    ]


# ---------------------------------------------------------------- time


def local(ts: Any, tz: ZoneInfo) -> str | None:
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M")


def parse_local(value: str, tz: ZoneInfo) -> int:
    """'2026-11-16 15:00' in the person's zone (or with an explicit offset) → unix seconds."""
    v = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M%z", "%Y-%m-%dT%H:%M%z"):
        try:
            dt = datetime.strptime(v, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return int(dt.astimezone(UTC).timestamp())
    raise invalid(f"Time must look like 2026-11-16 15:00 (local time, {tz.key}).")
