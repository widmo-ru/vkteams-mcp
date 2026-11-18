"""Settings, from the environment only. Nothing here names an installation."""

from __future__ import annotations

import logging
import os
import stat
import sys
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from vkteams_mcp.api.install import Policy
from vkteams_mcp.registry import parse_areas

# The VK Teams desktop client's public developer id, as VK publishes it in the open-source im-desktop
# (products/myteam/config.json). An operator with an id of their own sets VKTEAMS_MCP_DEV_ID.
DEFAULT_DEV_ID = "ic1DcTU-u6i6jrDP"

# Where the init container leaves the sealing key: a file in an emptyDir with `medium: Memory`, mode 0400,
# owned by the server's user. The server reads it at start; it is in no environment variable and no log.
DEFAULT_KEY_FILE = "/run/vkteams-mcp/key"
MAX_KEY = 4096

log = logging.getLogger("vkteams_mcp")


class ConfigError(ValueError):
    pass


def _zone(name: str) -> str:
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(name)
    except Exception:
        raise ConfigError(f"VKTEAMS_MCP_TIMEZONE is not a time zone: {name}") from None
    return name


def _take_key(e: dict[str, str] | os._Environ[str]) -> str:
    """The sealing key, read from the file the init container wrote.

    The file stays, so a container restarted inside the same pod finds the same key and keeps everyone
    signed in. It must be readable by its owner only: a key the group or anyone else can read is refused.

    For one release a leftover VKTEAMS_MCP_SECRET still works, with a warning, so a deployment upgraded
    without its manifests changed keeps running and keeps its sessions. Either way the variable is dropped
    from this process's environment, so nothing it starts inherits it."""
    path = e.get("VKTEAMS_MCP_KEY_FILE") or DEFAULT_KEY_FILE
    legacy = e.get("VKTEAMS_MCP_SECRET") or ""
    if e is os.environ:
        os.environ.pop("VKTEAMS_MCP_SECRET", None)
    try:
        st = os.stat(path)  # before opening: a FIFO would block the start
        if not stat.S_ISREG(st.st_mode):
            raise ConfigError(f"the sealing key in {path} is not a regular file")
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            raw = f.read(MAX_KEY + 1)
    except FileNotFoundError:
        if not legacy:
            raise ConfigError(
                f"no sealing key: {path} does not exist (VKTEAMS_MCP_KEY_FILE). In Kubernetes an init container "
                "copies it there from the Secret before the server starts"
            ) from None
        log.warning(
            "VKTEAMS_MCP_SECRET is deprecated and the next release refuses it: it is visible to anyone who "
            "can run `kubectl exec ... env`. Deliver the key as a file through an init container (README, "
            "Kubernetes). Using it for now; no file at %s",
            path,
        )
        key: str | None = legacy
    except OSError as err:  # a directory, no permission: say so without a traceback
        raise ConfigError(f"the sealing key in {path} could not be read ({err.strerror})") from None
    else:
        mode = st.st_mode
        if mode & 0o077:
            raise ConfigError(
                f"the sealing key in {path} is readable by others (mode {stat.S_IMODE(mode):04o}): it must be 0400, "
                "owned by the server's user, as `install -m 0400` in the init container leaves it"
            )
        if st.st_uid != os.geteuid():
            raise ConfigError(
                f"the sealing key in {path} belongs to uid {st.st_uid}, not to this server's uid {os.geteuid()}: "
                "run the init container as the server's user"
            )
        if len(raw) > MAX_KEY:
            raise ConfigError(f"the sealing key in {path} is longer than {MAX_KEY} bytes")
        try:
            key = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            key = None
        if legacy:
            log.warning("VKTEAMS_MCP_SECRET is ignored: the key comes from %s. Remove the variable", path)
    if key is None:
        raise ConfigError(f"the sealing key in {path} is not text: write it as base64, as `openssl rand -base64 48`")
    if len(key) < 32:
        raise ConfigError("the sealing key must be at least 32 random characters")
    return key


def make_undumpable() -> bool | None:
    """Linux: mark this process non-dumpable, so another process of the same user cannot read its memory
    through /proc/<pid>/mem or ptrace, nor its environment through /proc/<pid>/environ. True when it is set,
    False when Linux refused, None on a system without prctl (a developer's machine)."""
    if not sys.platform.startswith("linux"):
        return None
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    pr_get_dumpable, pr_set_dumpable = 3, 4
    if libc.prctl(pr_set_dumpable, 0, 0, 0, 0) != 0:
        return False
    return libc.prctl(pr_get_dumpable, 0, 0, 0, 0) == 0


@dataclass(frozen=True)
class Settings:
    public_url: str
    secret: str = field(repr=False)
    areas: frozenset[str] = field(default_factory=lambda: parse_areas(None))
    policy: Policy = field(default_factory=Policy)
    dev_id: str = DEFAULT_DEV_ID
    timezone: str = "Europe/Moscow"
    port: int = 8000

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        public_url = (e.get("VKTEAMS_MCP_PUBLIC_URL") or "").rstrip("/")
        u = urlsplit(public_url)
        local = u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1") and not u.username
        if u.query or u.fragment or (u.path not in ("", "/")):
            raise ConfigError("VKTEAMS_MCP_PUBLIC_URL is a bare origin: no path, query or fragment")
        if not (u.scheme == "https" and u.hostname and not u.username) and not local:
            raise ConfigError("VKTEAMS_MCP_PUBLIC_URL must be the https address clients reach this server on")
        secret = _take_key(e)
        try:
            areas = parse_areas(e.get("VKTEAMS_MCP_AREAS"))
        except ValueError as err:
            raise ConfigError(str(err)) from None
        pinned = frozenset(
            h.strip().lower() for h in (e.get("VKTEAMS_MCP_INSTALLATIONS") or "").split(",") if h.strip()
        )
        policy = Policy(allow_private=e.get("VKTEAMS_MCP_ALLOW_PRIVATE_NETWORKS") == "1", installations=pinned)
        return cls(
            public_url=public_url,
            secret=secret,
            areas=areas,
            policy=policy,
            dev_id=e.get("VKTEAMS_MCP_DEV_ID", DEFAULT_DEV_ID),
            timezone=_zone(e.get("VKTEAMS_MCP_TIMEZONE", "Europe/Moscow")),
            port=int(e.get("PORT", "8000")),
        )
