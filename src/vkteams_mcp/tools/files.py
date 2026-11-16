"""files: download an attachment through a signed link. Uploads ride on save_draft (attach)."""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import Field

from vkteams_mcp.core.content import content_line
from vkteams_mcp.core.context import call
from vkteams_mcp.core.errors import VKError, invalid
from vkteams_mcp.core.project import compact
from vkteams_mcp.registry import tool
from vkteams_mcp.tools._common import resolve_chat


def file_id_from(url_or_id: str) -> str:
    """A VK Teams file link ends in its file id (`https://<files host>/get/<id>`)."""
    v = url_or_id.strip()
    if "://" in v:
        try:
            u = urlsplit(v)
        except ValueError:
            u = None
        # a VK Teams file link is https and ends in /get/<id>; anything else is not one of its files
        parts = u.path.rstrip("/").split("/") if u and u.scheme == "https" else []
        v = parts[-1] if len(parts) >= 2 and parts[-2] == "get" else ""
    if not v or not v.isalnum():
        raise invalid("That is not a VK Teams file: give the file link or id shown in list_messages.")
    return v


@tool(area="files", title="Download a file", read_only=True)
async def download_file(
    file: Annotated[str, Field(description="The file's link or id, as list_messages shows it under files.")],
    chat: Annotated[str | None, Field(description="The chat the file was sent in, for the record.")] = None,
) -> dict[str, Any]:
    """Prepares a file sent in VK Teams for download and returns a curl command with a link that works for five
    minutes, plus the file's name, size and its antivirus and data-loss checks. Call it when the person wants to
    open, read or keep an attachment; run the command in the sandbox, because file bytes never pass through this
    conversation. It refuses something that is not a VK Teams file link or id."""
    async with call("download_file") as c:
        if chat:
            await resolve_chat(c, chat)
        fid = file_id_from(file)
        r = await c.vk.files_get(f"info/{fid}/", {"f": "json"})
        info = r.get("info") or {}
        if not info.get("dlink"):
            raise VKError("not_found")
        size = info.get("file_size")
        size = int(size) if isinstance(size, int) or (isinstance(size, str) and size.isdigit()) else None
        link = c.rt.links.download(c.person, info["dlink"], info.get("file_name") or fid, size)
        return compact(
            {
                "name": content_line("file name", info.get("file_name") or fid),
                "size": info.get("file_size"),
                "mime": info.get("mime"),
                "antivirus": (info.get("antivirus") or {}).get("check_result"),
                "dlp": (info.get("dlp") or {}).get("check_result"),
                **link,
            }
        )
