"""Settings, from the environment only. Nothing here names an installation."""

from __future__ import annotations

import logging
import os
import stat
import sys
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from vkteams_mcp.api.install import Policy
from vkteams_mcp.core.num import as_int
from vkteams_mcp.core.state import StateConfig
from vkteams_mcp.registry import parse_areas

# The VK Teams desktop client's public developer id, as VK publishes it in the open-source im-desktop
# (products/myteam/config.json). An operator with an id of their own sets VKTEAMS_MCP_DEV_ID.
DEFAULT_DEV_ID = "ic1DcTU-u6i6jrDP"

# Where the init container leaves the sealing key: a file in an emptyDir with `medium: Memory`, mode 0400,
# owned by the server's user. The server reads it at start; it is in no environment variable and no log.
DEFAULT_KEY_FILE = "/run/vkteams-mcp/key"
MAX_KEY = 4096
# The state store's passwords are staged next to the key by the same init container, with the same checks.
DEFAULT_STATE_PASSWORD_FILE = "/run/vkteams-mcp/state-password"
DEFAULT_STATE_SENTINEL_PASSWORD_FILE = "/run/vkteams-mcp/state-sentinel-password"
MAX_PASSWORD = 1024

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


def _read_owner_only(path: str, what: str, limit: int) -> bytes | None:
    """The bytes of a secret file the init container staged, or None if there is no such file. It must be a
    regular file readable by its owner only and owned by this server's user, as `install -m 0400` leaves it:
    a FIFO would block the start, and a file the group or anyone else can read is not a secret any more."""
    try:
        st = os.stat(path)  # before opening: a FIFO would block the start
        if not stat.S_ISREG(st.st_mode):
            raise ConfigError(f"the {what} in {path} is not a regular file")
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            raw = f.read(limit + 1)
    except FileNotFoundError:
        return None
    except OSError as err:  # a directory, no permission: say so without a traceback
        raise ConfigError(f"the {what} in {path} could not be read ({err.strerror})") from None
    if st.st_mode & 0o077:
        raise ConfigError(
            f"the {what} in {path} is readable by others (mode {stat.S_IMODE(st.st_mode):04o}): it must be 0400, "
            "owned by the server's user, as `install -m 0400` in the init container leaves it"
        )
    if st.st_uid != os.geteuid():
        raise ConfigError(
            f"the {what} in {path} belongs to uid {st.st_uid}, not to this server's uid {os.geteuid()}: "
            "run the init container as the server's user"
        )
    if len(raw) > limit:
        raise ConfigError(f"the {what} in {path} is longer than {limit} bytes")
    return raw


def _take_key(e: dict[str, str] | os._Environ[str]) -> str:
    """The sealing key, read from the file the init container wrote.

    The file stays, so a container restarted inside the same pod finds the same key and keeps everyone
    signed in. It must be readable by its owner only: a key the group or anyone else can read is refused.

    VKTEAMS_MCP_SECRET, a key in a variable, was deprecated in 0.3.0 and is refused from 0.4.0: the server does not
    start while it is set, whether or not there is a key file. It is dropped from this process's environment first, so
    nothing it starts inherits it."""
    path = e.get("VKTEAMS_MCP_KEY_FILE") or DEFAULT_KEY_FILE
    legacy = e.get("VKTEAMS_MCP_SECRET") or ""
    if e is os.environ:
        os.environ.pop("VKTEAMS_MCP_SECRET", None)
    if legacy:
        raise ConfigError(
            "VKTEAMS_MCP_SECRET is no longer accepted (refused since 0.4.0): a key in a variable is visible to anyone "
            "who can run `kubectl exec ... env`. Put the same key in a Secret under the field `key`, deliver it as a "
            f"file through the init container ({DEFAULT_KEY_FILE}), and remove the variable: deploy/README.en.md, "
            "'Moving from 0.3.x'"
        )
    raw = _read_owner_only(path, "sealing key", MAX_KEY)
    if raw is None:
        raise ConfigError(
            f"no sealing key: {path} does not exist (VKTEAMS_MCP_KEY_FILE). In Kubernetes an init container "
            "copies it there from the Secret before the server starts"
        )
    try:
        key: str | None = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        key = None
    if key is None:
        raise ConfigError(f"the sealing key in {path} is not text: write it as base64, as `openssl rand -base64 48`")
    if len(key) < 32:
        raise ConfigError("the sealing key must be at least 32 random characters")
    return key


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _hostport(item: str, default_port: int) -> tuple[str, int]:
    """host, host:port, [v6], [v6]:port. A bare IPv6 address has no way to say where the port starts: brackets."""
    text = item.strip()
    bad = ConfigError(f"VKTEAMS_MCP_STATE_SENTINELS: {text!r} is not host:port (an IPv6 address goes in [brackets])")
    if text.startswith("["):
        host, close, rest = text[1:].partition("]")
        if not close or (rest and not rest.startswith(":")):
            raise bad
        port = rest[1:] if rest else str(default_port)
    elif text.count(":") > 1:
        raise bad
    else:
        host, sep, port = text.partition(":")
        port = port if sep else str(default_port)
    n = as_int(port)
    if not host or n is None or not 0 < n < 65536:
        raise bad
    return host, n


def _take_state(e: dict[str, str] | os._Environ[str]) -> StateConfig:
    """Where the shared state lives (core.state). Nothing set: memory, which is right for one replica only.
    Passwords come from files, never from the environment, so `kubectl exec ... env` shows none."""
    url = (e.get("VKTEAMS_MCP_STATE_URL") or "").strip()
    sentinels_raw = (e.get("VKTEAMS_MCP_STATE_SENTINELS") or "").strip()
    pw_path = e.get("VKTEAMS_MCP_STATE_PASSWORD_FILE") or ""
    spw_path = e.get("VKTEAMS_MCP_STATE_SENTINEL_PASSWORD_FILE") or ""
    if url and sentinels_raw:
        raise ConfigError(
            "VKTEAMS_MCP_STATE_URL and VKTEAMS_MCP_STATE_SENTINELS are both set: use the URL for one Redis or "
            "Valkey, the sentinels for a Sentinel setup, not both"
        )
    sentinels = tuple(_hostport(x, 26379) for x in sentinels_raw.split(",") if x.strip())
    if sentinels_raw and not sentinels:
        raise ConfigError("VKTEAMS_MCP_STATE_SENTINELS has no host:port in it")
    if not url and not sentinels:
        for name, v in (
            ("VKTEAMS_MCP_STATE_PASSWORD_FILE", pw_path),
            ("VKTEAMS_MCP_STATE_SENTINEL_PASSWORD_FILE", spw_path),
            ("VKTEAMS_MCP_STATE_USERNAME", e.get("VKTEAMS_MCP_STATE_USERNAME") or ""),
        ):
            if v:
                raise ConfigError(f"{name} is set but there is no VKTEAMS_MCP_STATE_URL or VKTEAMS_MCP_STATE_SENTINELS")
        return StateConfig(prefix=e.get("VKTEAMS_MCP_STATE_PREFIX") or "vkteams:")
    if url:
        try:
            u = urlsplit(url)
            u.port  # noqa: B018 - a port that is not a number raises here
        except ValueError:
            raise ConfigError(
                "VKTEAMS_MCP_STATE_URL is not a URL: use redis://host:6379/0 or rediss://host:6379/0"
            ) from None
        if u.scheme not in ("redis", "rediss") or not u.hostname:
            raise ConfigError("VKTEAMS_MCP_STATE_URL must be redis://host:6379/0 or rediss://host:6379/0")
        if u.path.strip("/") and (as_int(u.path.strip("/")) is None or as_int(u.path.strip("/")) < 0):
            raise ConfigError("VKTEAMS_MCP_STATE_URL: the path is a database number, as in redis://host:6379/0")
        if u.username or u.password:
            raise ConfigError(
                "VKTEAMS_MCP_STATE_URL carries a user or password: a password in a variable is visible to "
                "`kubectl exec ... env`. Use VKTEAMS_MCP_STATE_USERNAME and VKTEAMS_MCP_STATE_PASSWORD_FILE"
            )
        if u.query or u.fragment:
            raise ConfigError("VKTEAMS_MCP_STATE_URL has no query or fragment: the other settings are variables")
    db_raw = (e.get("VKTEAMS_MCP_STATE_DB") or "").strip()
    if db_raw and (as_int(db_raw) is None or as_int(db_raw) < 0):
        raise ConfigError("VKTEAMS_MCP_STATE_DB is a database number")

    def password(path: str, default: str, what: str) -> str:
        raw = _read_owner_only(path or default, what, MAX_PASSWORD)
        if raw is None:
            if path:  # asked for by name: its absence is an error, not "no password"
                raise ConfigError(f"the {what} in {path} does not exist")
            return ""
        return raw.decode("utf-8", "replace").rstrip("\r\n")

    return StateConfig(
        url=url,
        sentinels=sentinels,
        master=(e.get("VKTEAMS_MCP_STATE_SENTINEL_MASTER") or "mymaster").strip(),
        db=as_int(db_raw) if db_raw else None,
        tls=_truthy(e.get("VKTEAMS_MCP_STATE_TLS")),
        username=(e.get("VKTEAMS_MCP_STATE_USERNAME") or "").strip(),
        password=password(pw_path, DEFAULT_STATE_PASSWORD_FILE, "state store password"),
        sentinel_password=password(spw_path, DEFAULT_STATE_SENTINEL_PASSWORD_FILE, "state store Sentinel password")
        if sentinels
        else "",
        prefix=e.get("VKTEAMS_MCP_STATE_PREFIX") or "vkteams:",
    )


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


def _take_proxies(e: dict[str, str] | os._Environ[str]) -> tuple[str, ...]:
    """VKTEAMS_MCP_TRUSTED_PROXIES: comma-separated addresses or CIDRs."""
    import ipaddress

    out = []
    for item in (e.get("VKTEAMS_MCP_TRUSTED_PROXIES") or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ipaddress.ip_network(item, strict=False)
        except ValueError:
            raise ConfigError(f"VKTEAMS_MCP_TRUSTED_PROXIES: {item!r} is not an address or a CIDR") from None
        out.append(item)
    return tuple(out)


def _take_ca(e: dict[str, str] | os._Environ[str]) -> str:
    """VKTEAMS_MCP_CA_BUNDLE: a PEM file. It replaces the roots the server trusts for VK Teams."""
    path = (e.get("VKTEAMS_MCP_CA_BUNDLE") or "").strip()
    if not path:
        return ""
    if not os.path.isfile(path):
        raise ConfigError(f"VKTEAMS_MCP_CA_BUNDLE: {path} is not a file")
    try:
        import ssl

        ssl.create_default_context(cafile=path)
    except (ssl.SSLError, OSError):
        raise ConfigError(f"VKTEAMS_MCP_CA_BUNDLE: {path} holds no readable PEM certificates") from None
    return path


def _port(e: dict[str, str] | os._Environ[str], name: str, default: int) -> int:
    raw = (e.get(name) or str(default)).strip()
    n = as_int(raw)
    if n is None or not 0 < n < 65536:
        raise ConfigError(f"{name} is a port number")
    return n


@dataclass(frozen=True)
class Settings:
    public_url: str
    secret: str = field(repr=False)
    areas: frozenset[str] = field(default_factory=lambda: parse_areas(None))
    policy: Policy = field(default_factory=Policy)
    dev_id: str = DEFAULT_DEV_ID
    timezone: str = "Europe/Moscow"
    port: int = 8000
    metrics_port: int = 9090
    trusted_proxies: tuple[str, ...] = ()  # CIDRs that may set X-Forwarded-For; empty: no per-network limit
    ca_bundle: str = ""  # a CA file for VK Teams hosts behind an internal CA; empty: the default roots
    state: StateConfig = field(default_factory=StateConfig)

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
        port = _port(e, "PORT", 8000)
        metrics_port = _port(e, "VKTEAMS_MCP_METRICS_PORT", 9090)
        if metrics_port == port:
            raise ConfigError("VKTEAMS_MCP_METRICS_PORT is the public port: metrics are never served there")
        return cls(
            public_url=public_url,
            secret=secret,
            areas=areas,
            policy=policy,
            dev_id=e.get("VKTEAMS_MCP_DEV_ID", DEFAULT_DEV_ID),
            timezone=_zone(e.get("VKTEAMS_MCP_TIMEZONE", "Europe/Moscow")),
            port=port,
            metrics_port=metrics_port,
            trusted_proxies=_take_proxies(e),
            ca_bundle=_take_ca(e),
            state=_take_state(e),
        )
