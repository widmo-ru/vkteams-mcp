"""The server's own refusals, before any tool runs, and the contract every tool keeps.

Parametrised over the registry: a new tool is checked without a new test.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest
from conftest import call_tool, text_of

from vkteams_mcp.registry import ALLOWED_VERBS, AREAS, all_tools
from vkteams_mcp.server import annotations

TOOLS = all_tools()
SRC = Path(__file__).resolve().parents[2] / "src" / "vkteams_mcp"


def test_the_catalogue_is_not_empty():
    assert len(TOOLS) >= 12  # a broken import would make every parametrised test below vacuous


# ---------------------------------------------------------------- refusals before the tool runs


async def test_confirmation_must_be_the_boolean_true(server, fake):
    for word in ("yes", 1, "true", None):
        r = await call_tool(server, "send_draft", {"chat": "a@example.com", "user_confirmed": word})
        assert r.is_error and "must be the boolean true" in text_of(r)
    assert fake.seen == []


async def test_an_unknown_argument_is_refused_before_upstream(server, fake):
    r = await call_tool(server, "list_chats", {"query": "x", "recursive": True})
    assert r.is_error and "has no argument recursive" in text_of(r) and "Nothing was done" in text_of(r)
    assert fake.seen == []


async def test_an_off_area_is_not_published_and_cannot_be_called(make_server, fake):
    from mcp import Client

    server, _ = make_server("account,messages")
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        r = await client.call_tool("list_chats", {})
    assert "list_chats" not in names and "save_chat" not in names and "get_connection" in names
    assert r.is_error
    assert fake.seen == []


async def test_chat_admin_is_off_by_default(server):
    from mcp import Client

    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"save_chat", "update_chat_members"}.isdisjoint(names)
    assert len(names) == len([s for s in TOOLS.values() if AREAS[s.area]])


async def test_a_gated_parameter_disappears_with_its_area(make_server):
    from mcp import Client

    server, _ = make_server("account,chats,messages")
    async with Client(server) as client:
        draft = next(t for t in (await client.list_tools()).tools if t.name == "save_draft")
        r = await client.call_tool("save_draft", {"chat": "a@example.com", "text": "x", "attach": ["a.pdf"]})
    assert "attach" not in draft.input_schema["properties"]
    assert r.is_error and "files" in text_of(r)


async def test_invalid_arguments_name_the_field_never_the_value(server, fake):
    r = await call_tool(
        server, "update_message", {"chat": "a@example.com", "message": "1", "action": "purge", "user_confirmed": True}
    )
    assert r.is_error and "action must be one of" in text_of(r) and "purge" not in text_of(r)
    assert fake.seen == []


# ---------------------------------------------------------------- the contract, per tool


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_name_uses_an_allowed_verb_and_no_prefix(name):
    assert name.startswith(ALLOWED_VERBS)
    assert not name.startswith(("vkteams_", "vk_", "mcp_"))


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_description_reads_like_a_man_page(name):
    d = TOOLS[name].description
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", d) if s.strip()]
    assert len(sentences) >= 3, name
    first = set(re.findall(r"[a-z]+", sentences[0].lower())) - set(name.split("_"))
    assert len(first) >= 5, f"{name}: the first sentence restates the name"
    assert not re.search(r"\b[A-Z]{4,}\b", d.replace("VK", "")), f"{name}: no shouting"
    assert "Call it" in d, f"{name}: say when to call it"


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_irreversible_or_outward_tools_say_so(name):
    s = TOOLS[name]
    if s.notifies_others:
        assert re.search(r"see[sn]? (it|this|these|the message|changes)|notified|seen by|see it|told", s.description), (
            name
        )


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_annotations_follow_behaviour(name):
    s = TOOLS[name]
    a = annotations(s)
    assert a.read_only_hint == s.read_only and a.open_world_hint == s.notifies_others
    if s.read_only:
        assert not s.notifies_others and not s.irreversible
    if s.notifies_others or s.irreversible:
        assert not s.idempotent


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_notifying_tools_take_confirmation_and_call_the_guard(name):
    s = TOOLS[name]
    src = inspect.getsource(s.fn)
    if s.notifies_others:
        assert "user_confirmed" in inspect.signature(s.fn).parameters, name
        assert "c.guard(" in src, name
    else:
        assert "c.guard(" not in src, f"{name} guards but is not marked as notifying others"


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_read_only_tools_do_not_write(name):
    s = TOOLS[name]
    if s.read_only:
        assert "write=True" not in inspect.getsource(s.fn), name


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_arguments_are_flat_and_described(name):
    sig = inspect.signature(TOOLS[name].fn)
    hints = TOOLS[name].fn.__annotations__
    for p in sig.parameters.values():
        ann = str(hints.get(p.name, ""))
        assert "dict" not in ann, f"{name}.{p.name} is nested"
        if p.name not in ("chat", "limit", "cursor", "query"):
            assert "Field(" in inspect.getsource(TOOLS[name].fn).split(p.name, 1)[1][:300] or p.name in ("text",), (
                f"{name}.{p.name} has no description"
            )


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_lists_take_a_capped_limit(name):
    s = TOOLS[name]
    if s.returns_list:
        assert "limit" in inspect.signature(s.fn).parameters
        assert "clamp_limit(" in inspect.getsource(s.fn)


def test_actions_are_closed_enums():
    for s in TOOLS.values():
        p = inspect.signature(s.fn).parameters.get("action")
        if p is not None:
            assert "Literal[" in str(p.annotation), s.name


# ---------------------------------------------------------------- structural rules


def test_only_the_api_package_speaks_http():
    offenders = []
    for f in SRC.rglob("*.py"):
        rel = f.relative_to(SRC).as_posix()
        if rel.startswith(("api/", "links/routes", "app.py", "signin/")) or rel == "core/context.py":
            continue
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in ("get", "post", "request", "stream")
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "http"
            ):
                offenders.append(rel)
    assert not offenders


def test_forbidden_capabilities_are_absent():
    banned = [
        "profile/update",
        "setNick",
        "status/set",
        "session/reset",
        "reportAbuse",
        "sendNotifySms",
        "delHistory",
        "inviteBlacklist",
        "attachPhone",
        "updatePrivacySettings",
        "misc/support",
    ]
    text = "\n".join(f.read_text() for f in (SRC / "tools").rglob("*.py"))
    assert [b for b in banned if b in text] == []


def test_no_tool_takes_a_server_user_or_session_argument():
    for s in TOOLS.values():
        for p in inspect.signature(s.fn).parameters:
            assert p not in ("host", "server", "aimsid", "session", "token", "user", "account"), (s.name, p)
