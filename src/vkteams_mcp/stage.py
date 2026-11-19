"""The init container: copies secret files from a Kubernetes Secret volume into a memory volume, where the server
reads them, and nothing else can.

    python -m vkteams_mcp.stage /secret/key:/run/vkteams-mcp/key --optional /state/pw:/run/vkteams-mcp/state-password

Each destination is written to a temporary name, mode 0400, owned by the user this runs as (the server's user:
the pod runs both as one), then renamed into place, so a file is never seen half written and a repeated run
replaces it. The Secret is mounted into this container only; the server's own container has no Secret volume and
no variable with a secret in it, so `kubectl exec ... env` shows nothing.

Every line it writes is a JSON object, like the server's. It never writes what it copied.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
import sys

from vkteams_mcp import observability as obs
from vkteams_mcp.config import make_undumpable

log = logging.getLogger("vkteams_mcp.stage")
LIMIT = 8192  # bytes: the largest thing staged here is a key or a password


class StageError(Exception):
    pass


def stage_one(src: str, dst: str, *, optional: bool = False) -> int | None:
    """Copies `src` to `dst` as an owner-only file. Returns the number of bytes, or None when `optional` and there
    is no `src`. Raises StageError."""
    try:
        st = os.stat(src)
        if not stat.S_ISREG(st.st_mode):
            raise StageError(f"{src} is not a regular file")
        with open(src, "rb") as f:
            data = f.read(LIMIT + 1)
    except FileNotFoundError:
        if optional:
            # an earlier try may have staged it, and the Secret has lost it since: a stale password must not stay
            with contextlib.suppress(FileNotFoundError):
                os.unlink(dst)
            return None
        raise StageError(f"{src} does not exist: is the key in the Secret under that name?") from None
    except OSError as err:
        raise StageError(f"{src} could not be read ({err.strerror})") from None
    if not data.strip():
        raise StageError(f"{src} is empty")
    if len(data) > LIMIT:
        raise StageError(f"{src} is longer than {LIMIT} bytes")
    tmp = f"{dst}.staging"
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        try:
            os.fchmod(fd, 0o400)  # whatever the umask said
            with os.fdopen(fd, "wb") as out:
                out.write(data)
        except BaseException:
            os.unlink(tmp)
            raise
        os.replace(tmp, dst)
    except OSError as err:
        raise StageError(f"{dst} could not be written ({err.strerror})") from None
    return len(data)


def main(argv: list[str] | None = None) -> int:
    obs.configure_logging()
    args = sys.argv[1:] if argv is None else argv
    pairs = [a for a in args if a != "--optional"]
    if not pairs or any(a.count(":") != 1 for a in pairs):
        obs.event(
            log,
            logging.ERROR,
            "stage_usage",
            "usage: python -m vkteams_mcp.stage SRC:DST [--optional SRC:DST ...]",
        )
        return 2
    if make_undumpable() is False:
        obs.event(log, logging.ERROR, "not_dumpable", "the process could not be made non-dumpable: not staging")
        return 2
    optional = False
    for arg in args:
        if arg == "--optional":  # the pairs after it may be absent from the Secret
            optional = True
            continue
        src, dst = arg.split(":")
        try:
            n = stage_one(src, dst, optional=optional)
        except StageError as err:
            obs.event(log, logging.ERROR, "stage_failed", str(err))
            return 2
        if n is None:
            obs.event(log, logging.INFO, "stage_skipped", f"{os.path.basename(dst)}: not in the Secret, not staged")
        else:
            obs.event(log, logging.INFO, "staged", f"staged {os.path.basename(dst)} ({n} bytes), mode 0400")
    return 0


if __name__ == "__main__":
    sys.exit(main())
