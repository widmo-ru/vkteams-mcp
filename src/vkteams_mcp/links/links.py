"""Short-lived signed links that move file bytes between the agent's sandbox and VK Teams, so the
bytes never enter the model's context (manifesto rule 10).

A link is a random id in a URL; what it may do (download one file, or receive one announced upload)
and for whom is held here in memory, with an expiry. Ids are 32 random bytes, compared by lookup; an
expired, used or unknown id is a 403. One replica: the table is in memory.
"""

from __future__ import annotations

import secrets
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

DOWNLOAD_TTL = 300
UPLOAD_TTL = 600
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_OPEN = 1000


DONE_TTL = 24 * 3600  # an uploaded file waits for send_draft as long as its draft lives


@dataclass
class Link:
    op: str  # "download" | "upload"
    person: str
    expires: float
    data: dict[str, Any] = field(default_factory=dict)
    done: dict[str, Any] | None = None
    busy: bool = False  # an upload is in progress: claimed before the first await, never swept


class Links:
    def __init__(self, public_url: str, clock: Callable[[], float] = time.time) -> None:
        self.public_url = public_url.rstrip("/")
        self._clock = clock
        self._links: dict[str, Link] = {}

    def _new(self, link: Link) -> str:
        self._sweep()
        if len(self._links) >= MAX_OPEN:
            raise RuntimeError("too many open links")
        lid = secrets.token_urlsafe(32)
        self._links[lid] = link
        return lid

    def _sweep(self) -> None:
        now = self._clock()
        for k in [k for k, v in self._links.items() if v.expires < now and not v.busy]:
            del self._links[k]

    def claim_upload(self, lid: str) -> Link | None:
        """The upload link, once: claimed atomically (no await between check and claim)."""
        link = self.get(lid, "upload")
        if link is None or link.done is not None or link.busy:
            return None
        link.busy = True
        return link

    def release_upload(self, lid: str) -> None:
        link = self._links.get(lid)
        if link is not None:
            link.busy = False

    def get(self, lid: str, op: str) -> Link | None:
        link = self._links.get(lid)
        if link is None or link.op != op or link.expires < self._clock():
            return None
        return link

    # downloads
    def download(self, person: str, dlink: str, name: str, size: int | None = None) -> dict:
        data = {"dlink": dlink, "name": name, "size": size}
        lid = self._new(Link("download", person, self._clock() + DOWNLOAD_TTL, data))
        url = f"{self.public_url}/files/d/{lid}"
        # --globoff: curl expands {a,b} and [1-9] in its own arguments, whatever the shell quoting
        return {"url": url, "command": f"curl -fsS --globoff -o '{_safe(name)}' '{url}'", "expires_in": DOWNLOAD_TTL}

    # uploads announced by save_draft, finished by the upload route
    def announce_uploads(self, person: str, chat: str, names: list[str]) -> list[dict]:
        self._sweep()
        if len(self._links) + len(names) > MAX_OPEN:  # all or none: no orphan link from a half-made batch
            raise RuntimeError("too many open links")
        out = []
        for name in names:
            lid = self._new(Link("upload", person, self._clock() + UPLOAD_TTL, {"chat": chat, "name": name}))
            url = f"{self.public_url}/files/u/{lid}"
            out.append(
                {
                    "id": lid,
                    "name": name,
                    "url": url,
                    "expires_in": UPLOAD_TTL,
                    # the person's own local path, quoted as it is; download names are sanitised instead
                    "command": f"curl -fsS --globoff -T {shlex.quote(name)} '{url}'",
                }
            )
        return out

    def finish_upload(self, lid: str, result: dict) -> None:
        link = self._links.get(lid)
        if link is not None:
            link.done = result
            link.busy = False
            link.expires = self._clock() + DONE_TTL

    def uploaded(self, person: str, ids: list[str]) -> list[dict] | None:
        """The uploaded files for these announcements, or None while any is missing."""
        out = []
        for lid in ids:
            link = self._links.get(lid)
            if link is None or link.person != person or link.done is None:
                return None
            out.append(link.done)
        return out


def _safe(name: str) -> str:
    """A bare file name for a shell command: no directories (a name from VK Teams is someone else's text,
    and `../../.bashrc` must not become a path), no quotes, no control characters."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch not in "'\"" and ch.isprintable())[:200].strip()
    return base if base not in ("", ".", "..") and not base.startswith("-") else "file"
