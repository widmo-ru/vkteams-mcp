"""HTTP routes behind the signed links: stream a download from VK Teams, receive an upload for it."""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import quote, urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from vkteams_mcp.api.client import VK
from vkteams_mcp.core.context import runtime
from vkteams_mcp.core.errors import VKError
from vkteams_mcp.links.links import MAX_UPLOAD_BYTES
from vkteams_mcp.links.links import _safe as safe_name

log = logging.getLogger("vkteams_mcp.links")
# At most this many uploads are held in memory at once (each up to MAX_UPLOAD_BYTES), across all people.
_UPLOADS = asyncio.Semaphore(3)
# One byte range, as resumable downloads ask; anything else is not passed on and the whole file comes back.
_RANGE = re.compile(r"bytes=\d{0,19}-\d{0,19}")
_PASSED_BACK = ("content-range", "content-length", "accept-ranges", "last-modified", "etag")


def _deny() -> Response:
    return JSONResponse(
        {"error": "This link has expired or is not valid. Ask the assistant for a new one."}, status_code=403
    )


async def download(request: Request) -> Response:
    rt = runtime()
    link = rt.links.get(request.path_params["lid"], "download")
    session = rt.store.load_session(link.person) if link else None
    if link is None or session is None:
        return _deny()
    url = link.data["dlink"]
    if urlsplit(url).hostname not in (session.binary_host, session.host) or not url.startswith("https://"):
        return _deny()  # only the person's own installation, whatever the upstream said
    asked = request.headers.get("range", "")
    # identity: the bytes passed on must be the bytes Content-Length and Content-Range describe
    headers = {"Accept-Encoding": "identity"}
    if _RANGE.fullmatch(asked) and asked != "bytes=-":
        headers["Range"] = asked
    upstream = await rt.http.send(rt.http.build_request("GET", url, headers=headers), stream=True)
    if upstream.status_code == 416:  # a range past the end: the client learns the length and stops
        await upstream.aclose()
        unsatisfied = {k: upstream.headers[k] for k in ("content-range",) if k in upstream.headers}
        if not unsatisfied and isinstance(link.data.get("size"), int):  # live VK sends none: say the length
            unsatisfied = {"content-range": f"bytes */{link.data['size']}"}
        return Response(status_code=416, headers=unsatisfied)
    if upstream.status_code not in (200, 206):
        await upstream.aclose()
        return JSONResponse({"error": "VK Teams did not return the file. Try again."}, status_code=502)
    name = link.data.get("name", "file")

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    passed = {k: upstream.headers[k] for k in _PASSED_BACK if k in upstream.headers}
    if upstream.headers.get("content-encoding", "identity") != "identity":
        passed.pop("content-length", None)  # the body below is decoded: its length is not the upstream one
    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
        headers={"Content-Disposition": _disposition(name), **passed},
    )


def _disposition(name: str) -> str:
    """ASCII fallback plus RFC 5987 UTF-8 name: Starlette sends headers as Latin-1."""
    ascii_name = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in safe_name(name))
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe_name(name), safe='')}"


async def upload(request: Request) -> Response:
    rt = runtime()
    lid = request.path_params["lid"]
    link = rt.links.claim_upload(lid)  # claimed before any await: a second concurrent PUT is refused
    session = rt.store.load_session(link.person) if link else None
    if link is None or session is None:
        if link is not None:
            rt.links.release_upload(lid)
        return _deny()
    try:
        return await _receive(request, rt, lid, link, session)
    finally:
        rt.links.release_upload(lid)


async def _receive(request: Request, rt, lid: str, link, session) -> Response:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        return JSONResponse({"error": f"The file is larger than {MAX_UPLOAD_BYTES // 2**20} MB."}, status_code=413)
    if _UPLOADS.locked():
        return JSONResponse({"error": "The server is receiving other files. Try again in a minute."}, status_code=503)
    async with _UPLOADS:
        return await _store(request, rt, lid, link, session)


async def _store(request: Request, rt, lid: str, link, session) -> Response:
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_UPLOAD_BYTES:
            return JSONResponse({"error": f"The file is larger than {MAX_UPLOAD_BYTES // 2**20} MB."}, status_code=413)
    if not data:
        return JSONResponse({"error": "The upload was empty."}, status_code=400)
    size = len(data)
    payload = bytes(data)
    del data  # one copy in memory, not two, while VK Teams receives it
    vk = VK(session, rt.http)
    name = link.data["name"]
    try:
        gate = await vk.files_get(
            "init", {"f": "json", "k": session.dev_id, "size": size, "filename": name, "client": "VKTeams"}
        )
        res = await vk.upload(gate["host"], gate["url"], name, payload)
    except (VKError, KeyError):
        log.info("upload refused by VK Teams")
        return JSONResponse({"error": "VK Teams did not accept the file. Try again."}, status_code=502)
    rt.links.finish_upload(
        lid, {"url": res.get("static_url"), "file_id": res.get("fileid"), "name": name, "size": size}
    )
    return JSONResponse({"uploaded": name, "size": size, "next": "Now call send_draft."})
