"""The signed-link routes: a download streams only from the person's own installation; an upload is
capped, bound to its announcement, and lands in VK Teams for send_draft."""

from __future__ import annotations

import re

import httpx
import pytest
from conftest import BIN_HOST, GROUP, HOST, SECRET, call_tool, ok, session  # noqa: F401

PUBLIC = "https://mcp.example.org"


@pytest.fixture
def app(fake, tmp_path, session):  # noqa: F811
    from vkteams_mcp.app import build
    from vkteams_mcp.config import Settings
    from vkteams_mcp.core import context

    app, server, _ = build(
        Settings(public_url=PUBLIC, secret=SECRET),
        http=httpx.AsyncClient(transport=fake.transport()),
    )
    rt = context.runtime()
    context.session_override.set(session)
    person = session
    return app, server, rt, person


async def get(app, path, method="GET", **kw):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as c:
        return await c.request(method, path, **kw)


async def test_a_download_streams_the_file_from_the_installation(app, fake):
    a, _, rt, person = app
    fake.on(
        "GET",
        "/api/v139/files/get/signed",
        httpx.Response(200, content=b"PDFDATA", headers={"content-type": "application/pdf"}),
        host=BIN_HOST,
    )
    link = rt.links.download(person, f"https://{BIN_HOST}/api/v139/files/get/signed", "a.pdf")
    r = await get(a, urlpath(link["url"]))
    assert r.status_code == 200 and r.content == b"PDFDATA" and 'filename="a.pdf"' in r.headers["content-disposition"]


async def test_a_download_link_pointing_elsewhere_is_refused(app, fake):
    a, _, rt, person = app
    link = rt.links.download(person, "https://elsewhere.example.net/steal", "a.pdf")
    r = await get(a, urlpath(link["url"]))
    assert r.status_code == 403 and fake.seen == []


async def test_an_unknown_or_wrong_kind_link_is_refused(app, fake):
    a, _, rt, person = app
    up = rt.links.announce_uploads(person, GROUP, ["a.txt"])[0]
    assert (await get(a, "/files/d/nope")).status_code == 403
    assert (await get(a, urlpath(up["url"]).replace("/u/", "/d/"))).status_code == 403  # an upload id cannot download


async def test_an_upload_lands_in_vk_teams_and_send_draft_posts_it(app, fake):
    a, server, rt, person = app
    fake.on("GET", "/api/v139/files/init", {"status": {"code": 200}, "result": {"host": BIN_HOST, "url": "/up/1"}})
    fake.on(
        "POST",
        "/up/1",
        {"status": {"code": 200}, "result": {"fileid": "F1", "static_url": "https://f.example.com/get/F1"}},
        host=BIN_HOST,
    )
    fake.on("GET", "/api/v139/bos/node-1/aim/fetchEvents", {"response": {"statusCode": 200, "data": {"events": []}}})
    fake.rapi("message/send", ok({"msgId": 7}))
    r = await call_tool(server, "save_draft", {"chat": GROUP, "text": "see attached", "attach": ["a.txt"]})
    url = re.search(r"'(https://[^']+)'", r.structured_content["upload"][0]["command"]).group(1)
    put = await get(a, urlpath(url), method="PUT", content=b"hello file")
    assert put.status_code == 200, put.text
    upload = [s for s in fake.seen if s.path == "/up/1"][0]
    assert upload.content == b"hello file" and upload.headers["content-range"] == "bytes 0-9/10"
    # the response was lost on the way back: uploading again within the link's life recovers the file
    again = await get(a, urlpath(url), method="PUT", content=b"hello file")
    assert again.status_code == 200, again.text
    file = again.json()["file"]
    d = r.structured_content["draft"]
    sent = await call_tool(server, "send_draft", {"chat": GROUP, "draft": d, "files": [file], "user_confirmed": True})
    assert not sent.is_error, sent.content
    texts = [s.params["parts"]["mainPart"]["text"]["plain"] for s in fake.calls("message/send")]
    assert texts == ["https://f.example.com/get/F1", "see attached"]


async def test_an_oversized_upload_is_refused_before_it_is_read(app, fake, monkeypatch):
    a, _, rt, person = app
    monkeypatch.setattr("vkteams_mcp.links.routes.MAX_UPLOAD_BYTES", 5)
    up = rt.links.announce_uploads(person, GROUP, ["big.bin"])[0]
    r = await get(a, urlpath(up["url"]), method="PUT", content=b"0123456789")
    assert r.status_code == 413 and fake.seen == []


def urlpath(url: str) -> str:
    return url.replace(PUBLIC, "")


def test_a_download_name_cannot_become_a_path():
    from vkteams_mcp.core.seal import Sealer
    from vkteams_mcp.links.links import Links

    for evil in ("../../.bashrc", "/etc/passwd", "..\\..\\x", "a'; rm -rf ~; '", "-rf", ".."):
        cmd = Links("https://mcp.example.org", Sealer("x" * 40)).download(_session(), "https://x/y", evil)["command"]
        target = cmd.split("-o '")[1].split("'")[0]
        assert "/" not in target and "\\" not in target and not target.startswith("-") and target not in ("", "..")


async def test_a_cyrillic_file_name_downloads(app, fake):
    a, _, rt, person = app
    fake.on("GET", "/api/v139/files/get/signed", httpx.Response(200, content=b"x"), host=BIN_HOST)
    link = rt.links.download(person, f"https://{BIN_HOST}/api/v139/files/get/signed", "отчёт.pdf")
    r = await get(a, urlpath(link["url"]))
    assert (
        r.status_code == 200
        and "filename*=UTF-8''%D0%BE%D1%82%D1%87%D1%91%D1%82.pdf" in r.headers["content-disposition"]
    )


async def test_a_cyrillic_file_name_uploads(app, fake):
    a, _, rt, person = app
    fake.on("GET", "/api/v139/files/init", {"status": {"code": 200}, "result": {"host": BIN_HOST, "url": "/up/2"}})
    fake.on(
        "POST",
        "/up/2",
        {"status": {"code": 200}, "result": {"fileid": "F2", "static_url": "https://f/get/F2"}},
        host=BIN_HOST,
    )
    up = rt.links.announce_uploads(person, GROUP, ["отчёт.pdf"])[0]
    r = await get(a, urlpath(up["url"]), method="PUT", content=b"data")
    assert r.status_code == 200, r.text
    sent = [s for s in fake.seen if s.path == "/up/2"][0]
    assert "отчёт.pdf" in sent.headers["content-disposition"]


def test_an_upload_command_quotes_the_local_name_as_it_is():
    from vkteams_mcp.core.seal import Sealer
    from vkteams_mcp.links.links import Links

    cmd = Links("https://mcp.example.org", Sealer("x" * 40)).announce_uploads(_session(), "c", ["my report's v2.pdf"])[
        0
    ]["command"]
    assert cmd.startswith("curl -fsS --globoff -T 'my report'\"'\"'s v2.pdf' ")


def _session():
    from vkteams_mcp.api.session import VKSession

    return VKSession("teams.example.com", "files.example.com", 139, "d", "me@example.com", "a", "k", 0, "s", "u", "me")
