"""Short-lived signed links that move file bytes between the agent's sandbox and VK Teams, so the
bytes never enter the model's context (manifesto rule 10).

A link carries what it may do inside its own URL, sealed (`core.seal`): a download link names the
file's signed address and the installation it may come from; an upload link names the chat, the file
name and the person's VK Teams session, for ten minutes. Nothing is kept on the server, so any replica
serves any link. A finished upload answers with a sealed `file` the agent passes to `send_draft`.
"""

from __future__ import annotations

import secrets
import shlex
import time
from collections.abc import Callable

from vkteams_mcp.api.session import VKSession
from vkteams_mcp.core.seal import Sealer

DOWNLOAD_TTL = 300
UPLOAD_TTL = 600
FILE_TTL = 24 * 3600  # an uploaded file waits for send_draft as long as its draft lives
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
BUSY_MAX = 10_000


class Links:
    def __init__(self, public_url: str, sealer: Sealer, clock: Callable[[], float] = time.time) -> None:
        self.public_url = public_url.rstrip("/")
        self.sealer = sealer
        self._clock = clock
        # One upload at a time per link on this replica: claimed before the first await, released when it ends.
        # Uploading again within the link's ten minutes (a lost response, another replica) stores the file
        # again under the same upload id; send_draft takes one file per upload id.
        self._busy: dict[str, float] = {}

    # downloads
    def download(self, session: VKSession, dlink: str, name: str, size: int | None = None) -> dict:
        lid = self.sealer.seal(
            "download",
            {"dlink": dlink, "name": name, "size": size, "hosts": [session.binary_host, session.host]},
            DOWNLOAD_TTL,
        )
        url = f"{self.public_url}/files/d/{lid}"
        # --globoff: curl expands {a,b} and [1-9] in its own arguments, whatever the shell quoting
        return {"url": url, "command": f"curl -fsS --globoff -o '{_safe(name)}' '{url}'", "expires_in": DOWNLOAD_TTL}

    def opened_download(self, lid: str) -> dict | None:
        return self.sealer.open("download", lid)

    # uploads announced by save_draft, finished by the upload route
    def announce_uploads(self, session: VKSession, chat: str, names: list[str]) -> list[dict]:
        out = []
        for name in names:
            uid = secrets.token_urlsafe(12)
            body = {"uid": uid, "chat": chat, "name": name, "s": session.to_json()}
            url = f"{self.public_url}/files/u/{self.sealer.seal('upload', body, UPLOAD_TTL)}"
            out.append(
                {
                    "id": uid,
                    "name": name,
                    "url": url,
                    "expires_in": UPLOAD_TTL,
                    # the person's own local path, quoted as it is; download names are sanitised instead
                    "command": f"curl -fsS --globoff -T {shlex.quote(name)} '{url}'",
                }
            )
        return out

    def claim_upload(self, lid: str) -> dict | None:
        """The upload link's payload while no other upload of it runs here: no await between check and claim."""
        body = self.sealer.open("upload", lid)
        now = self._clock()
        if body is None or self._busy.get(body["uid"], 0) > now:
            return None
        if len(self._busy) >= BUSY_MAX:
            for k in [k for k, exp in self._busy.items() if exp < now]:
                del self._busy[k]
        self._busy[body["uid"]] = now + UPLOAD_TTL
        return body

    def release_upload(self, uid: str) -> None:
        self._busy.pop(uid, None)

    def finished(self, body: dict, result: dict) -> str:
        """The sealed file the agent hands to send_draft."""
        return self.sealer.seal("file", {"uid": body["uid"], "chat": body["chat"], **result}, FILE_TTL)

    def files(self, tokens: list[str]) -> list[dict] | None:
        out = []
        for t in tokens:
            body = self.sealer.open("file", t)
            if body is None:
                return None
            out.append(body)
        return out


def _safe(name: str) -> str:
    """A bare file name for a shell command: no directories (a name from VK Teams is someone else's text,
    and `../../.bashrc` must not become a path), no quotes, no control characters."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch not in "'\"" and ch.isprintable())[:200].strip()
    return base if base not in ("", ".", "..") and not base.startswith("-") else "file"
