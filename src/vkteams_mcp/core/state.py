"""The shared state store: the few counters and marks that must be the same on every replica.

Authorization stays stateless (`core.seal`): the person's session, a draft, a file link all travel sealed
inside what the client holds. What cannot travel is what must be refused the second time, wherever the
second time lands:

| What | Operation |
|---|---|
| an authorization code, redeemed once | `once` |
| code requests per address and per network, wrong codes per sign-in | `window_take`, `counter` |
| the one bucket of actions other people see, per person | `bucket_take` |
| an upload link, busy while one upload of it runs | `acquire`, `release` |

Two implementations pass the same contract suite (`tests/unit/test_state_contract.py`): `MemoryStore`
(one replica, tests) and `RedisStore` (redis-py, against Redis or Valkey, standalone or behind Sentinel).

Nothing here keeps a session, a draft or a credential. A key is `<prefix><kind>:<hex>` where the hex is an
HMAC of the value under a key derived from the sealing key, so a login, an address or an IP never appears in
the store, not even to someone who can read it. When the store cannot be reached every operation raises
`StateUnavailable`, and the callers refuse (sign-in, sending) instead of letting the person through without
the limit.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar
from urllib.parse import urlsplit

from vkteams_mcp import observability

T = TypeVar("T")

CONNECT_TIMEOUT = 1.0  # seconds: connecting and each reply; a store that is slower than this is down
OP_TIMEOUT = 3.0  # seconds: a whole operation, however many Sentinels have to be asked
MEMORY_MAX = 50_000  # entries of each kind a memory store holds before it forgets the oldest


class StateUnavailable(Exception):
    """The store cannot be reached or did not answer in time. Callers refuse; they never carry on without it."""


@dataclass(frozen=True)
class Bucket:
    ok: bool  # a token was taken
    left: int  # tokens left after this call
    wait: int  # seconds until one token is back, when none was taken


class StateStore(Protocol):
    async def once(self, ns: str, ident: str, ttl: float) -> bool:
        """True the first time `ident` is seen in `ns` within `ttl` seconds, False every time after."""

    async def counter(self, ns: str, ident: str, ttl: float) -> int:
        """Adds one and returns the new count. The count lives `ttl` seconds from its first increment."""

    async def window_take(self, ns: str, checks: Sequence[tuple[str, int]], window: float) -> bool:
        """Sliding window: records one hit on every key of `checks` and returns True, unless some key already
        has its `limit` hits within the last `window` seconds. All keys or none: a refusal records nothing."""

    async def bucket_take(self, ns: str, ident: str, capacity: int, refill_per_second: float) -> Bucket:
        """Token bucket: takes one token if there is one."""

    async def acquire(self, ns: str, ident: str, ttl: float) -> str | None:
        """A lock on `ident` for at most `ttl` seconds: its token, or None while someone holds it."""

    async def release(self, ns: str, ident: str, token: str) -> None:
        """Frees the lock, only if `token` still holds it."""

    async def ping(self) -> None:
        """Returns when the store answers; raises StateUnavailable when it does not."""

    async def close(self) -> None: ...


# ---------------------------------------------------------------- keys


def derive_key(secret: str) -> bytes:
    """The key every state key is hashed under: derived from the sealing key, domain-separated from the keys
    the seals and the cursors derive from it."""
    return hashlib.sha256(b"vkteams-mcp state\n" + secret.encode()).digest()


def _hashed(key: bytes, ns: str, ident: str) -> str:
    return hmac.new(key, f"{ns}\n{ident}".encode(), hashlib.sha256).hexdigest()[:32]


# ---------------------------------------------------------------- memory


@dataclass
class _Bkt:
    tokens: float
    at: float


@dataclass
class MemoryStore:
    """Today's behaviour, for one replica and for tests. Every method runs to its end without an await, so
    a call is atomic with respect to the event loop, as a Lua script is to Redis."""

    key: bytes
    prefix: str = ""
    clock: Callable[[], float] = time.monotonic
    _kv: dict[str, tuple[float, Any]] = field(default_factory=dict)
    _windows: dict[str, deque[float]] = field(default_factory=dict)
    _buckets: dict[str, _Bkt] = field(default_factory=dict)
    _pruned_at: dict[int, float] = field(default_factory=dict)  # per table: when it was last swept

    def _k(self, ns: str, ident: str) -> str:
        return f"{self.prefix}{ns}:{_hashed(self.key, ns, ident)}"

    def _live(self, k: str, now: float) -> tuple[float, Any] | None:
        hit = self._kv.get(k)
        if hit is None:
            return None
        if hit[0] <= now:
            del self._kv[k]
            return None
        return hit

    def _admit(self, table: dict, forget: Callable[[float], None], keys: Sequence[str] | None = None) -> None:
        """Called before NEW keys go into `table`: `keys` are the ones this call needs (default: one new one). When
        there is no room for those that are not already there, the expired entries go (at most once a second: a full
        store must not be scanned on every request), and if there is still no room because the table is full of LIVE
        entries the call fails closed. Forgetting a live entry would lift a limit, free a held lock or let a spent
        code be spent again; refusing the newcomers only refuses the newcomers."""

        def missing() -> int:
            return 1 if keys is None else len({k for k in keys if k not in table})

        if len(table) + missing() <= MEMORY_MAX:
            return
        now = self.clock()
        if now - self._pruned_at.get(id(table), float("-inf")) >= 1.0:
            self._pruned_at[id(table)] = now
            forget(now)
        if len(table) + missing() > MEMORY_MAX:  # counted again: the sweep may have removed a key this call needs
            raise StateUnavailable("MemoryStoreFull")

    def _forget_kv(self, now: float) -> None:
        for k in [k for k, (exp, _) in self._kv.items() if exp <= now]:
            del self._kv[k]

    async def once(self, ns: str, ident: str, ttl: float) -> bool:
        k, now = self._k(ns, ident), self.clock()
        if self._live(k, now):
            return False
        self._admit(self._kv, self._forget_kv)
        self._kv[k] = (now + ttl, 1)
        return True

    async def counter(self, ns: str, ident: str, ttl: float) -> int:
        k, now = self._k(ns, ident), self.clock()
        hit = self._live(k, now)
        if hit is None:
            self._admit(self._kv, self._forget_kv)
            self._kv[k] = (now + ttl, 1)
            return 1
        self._kv[k] = (hit[0], hit[1] + 1)
        return hit[1] + 1

    async def window_take(self, ns: str, checks: Sequence[tuple[str, int]], window: float) -> bool:
        now = self.clock()
        keyed = [(self._k(ns, ident), limit) for ident, limit in checks]

        def forget(at: float) -> None:
            for k in [k for k, q in self._windows.items() if not q or at - q[-1] >= window]:
                del self._windows[k]

        self._admit(self._windows, forget, [k for k, _ in keyed])  # room for ALL the new keys of this call, or none
        for k, limit in keyed:
            q = self._windows.setdefault(k, deque())
            while q and now - q[0] >= window:
                q.popleft()
            if len(q) >= limit:
                return False
        for k, _ in keyed:
            self._windows[k].append(now)
        return True

    async def bucket_take(self, ns: str, ident: str, capacity: int, refill_per_second: float) -> Bucket:
        k, now = self._k(ns, ident), self.clock()
        b = self._buckets.get(k)
        if b is None:
            full_after = capacity / refill_per_second

            def forget(at: float) -> None:  # a bucket that has refilled to full is the same as no bucket
                for key in [key for key, x in self._buckets.items() if at - x.at >= full_after]:
                    del self._buckets[key]

            self._admit(self._buckets, forget)
            b = self._buckets[k] = _Bkt(capacity, now)
        b.tokens = min(capacity, b.tokens + max(0.0, now - b.at) * refill_per_second)
        b.at = now
        if b.tokens < 1:
            return Bucket(False, 0, math.ceil((1 - b.tokens) / refill_per_second))
        b.tokens -= 1
        return Bucket(True, int(b.tokens), 0)

    async def acquire(self, ns: str, ident: str, ttl: float) -> str | None:
        k, now = self._k(ns, ident), self.clock()
        if self._live(k, now):
            return None
        self._admit(self._kv, self._forget_kv)
        token = uuid.uuid4().hex
        self._kv[k] = (now + ttl, token)
        return token

    async def release(self, ns: str, ident: str, token: str) -> None:
        k = self._k(ns, ident)
        hit = self._kv.get(k)
        if hit is not None and hit[1] == token:
            del self._kv[k]

    async def ping(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def keys(self) -> list[str]:
        """Every key held, for the tests that look for a login or an address in them."""
        return [*self._kv, *self._windows, *self._buckets]


# ---------------------------------------------------------------- redis / valkey

# All of these run inside the server, one round trip each, so two replicas never interleave. Time is the
# server's own (TIME), so replicas with different clocks agree. Each starts with `#!lua`: a script with a shebang is
# held to the server's memory limit like any write command (it fails with OOM), while one without it is run in a
# compatibility mode that lets a script's writes through when the server is out of memory, and a limit would fail open.
# Needs Redis or Valkey 7 or later.
_COUNTER = """#!lua
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('PEXPIRE', KEYS[1], ARGV[1]) end
return n
"""

_WINDOW = """#!lua
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local window = tonumber(ARGV[1])
local member = ARGV[2]
for i = 1, #KEYS do
  redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now - window)
  if redis.call('ZCARD', KEYS[i]) >= tonumber(ARGV[2 + i]) then return 0 end
end
for i = 1, #KEYS do
  redis.call('ZADD', KEYS[i], now, member)
  redis.call('PEXPIRE', KEYS[i], window)
end
return 1
"""

_BUCKET = """#!lua
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local h = redis.call('HMGET', KEYS[1], 'tokens', 'at')
local tokens = tonumber(h[1])
local at = tonumber(h[2])
if tokens == nil or at == nil then tokens = capacity; at = now end
tokens = math.min(capacity, tokens + math.max(0, now - at) * rate)
local ok = 0
local wait = 0
if tokens < 1 then
  wait = math.ceil((1 - tokens) / rate)
else
  tokens = tokens - 1
  ok = 1
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'at', string.format('%.6f', now))
redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / rate * 1000) + 1000)
return {ok, math.floor(tokens), wait}
"""

_RELEASE = """#!lua
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


def _ms(seconds: float) -> int:
    return max(1, math.ceil(seconds * 1000))


class RedisStore:
    """redis-py against Redis or Valkey, standalone or behind Sentinel. `client` is a `redis.asyncio.Redis`;
    with Sentinel it is the one `master_for` returns, which asks the Sentinels for the master again whenever
    a connection fails, so a failover is followed without a restart."""

    def __init__(self, client: Any, key: bytes, prefix: str = "") -> None:
        self._r = client
        self.key = key
        self.prefix = prefix
        self._counter = client.register_script(_COUNTER)
        self._window = client.register_script(_WINDOW)
        self._bucket = client.register_script(_BUCKET)
        self._release = client.register_script(_RELEASE)

    def _k(self, ns: str, ident: str) -> str:
        return f"{self.prefix}{ns}:{_hashed(self.key, ns, ident)}"

    async def _run(self, call: Awaitable[T]) -> T:
        from redis.exceptions import RedisError

        try:
            return await asyncio.wait_for(call, OP_TIMEOUT)
        except (RedisError, OSError, TimeoutError) as err:
            # only the class goes up: the message can name a host
            raise StateUnavailable(type(err).__name__) from None

    async def once(self, ns: str, ident: str, ttl: float) -> bool:
        return bool(await self._run(self._r.set(self._k(ns, ident), 1, nx=True, px=_ms(ttl))))

    async def counter(self, ns: str, ident: str, ttl: float) -> int:
        return int(await self._run(self._counter(keys=[self._k(ns, ident)], args=[_ms(ttl)])))

    async def window_take(self, ns: str, checks: Sequence[tuple[str, int]], window: float) -> bool:
        keys = [self._k(ns, ident) for ident, _ in checks]
        args = [_ms(window), uuid.uuid4().hex, *[limit for _, limit in checks]]
        return bool(await self._run(self._window(keys=keys, args=args)))

    async def bucket_take(self, ns: str, ident: str, capacity: int, refill_per_second: float) -> Bucket:
        ok, left, wait = await self._run(
            self._bucket(keys=[self._k(ns, ident)], args=[capacity, repr(float(refill_per_second))])
        )
        return Bucket(bool(ok), int(left), int(wait))

    async def acquire(self, ns: str, ident: str, ttl: float) -> str | None:
        token = uuid.uuid4().hex
        won = await self._run(self._r.set(self._k(ns, ident), token, nx=True, px=_ms(ttl)))
        return token if won else None

    async def release(self, ns: str, ident: str, token: str) -> None:
        await self._run(self._release(keys=[self._k(ns, ident)], args=[token]))

    async def ping(self) -> None:
        await self._run(self._r.ping())

    async def close(self) -> None:
        try:
            await self._r.aclose()
        except Exception:  # noqa: BLE001 - closing a store that is already gone is not an error
            return None


# ---------------------------------------------------------------- what the store holds, observed


class Observed:
    """Wraps a store so every operation is counted and timed, and `mcp_state_store_up` follows the last answer."""

    def __init__(self, inner: StateStore) -> None:
        self._inner = inner
        observability.STATE_UP.labels(observability.SERVER).set(1)

    async def _op(self, op: str, call: Awaitable[T]) -> T:
        t0 = time.monotonic()
        try:
            out = await call
        except StateUnavailable:
            observability.STATE_ERRORS.labels(observability.SERVER, op).inc()
            observability.STATE_UP.labels(observability.SERVER).set(0)
            raise
        finally:
            observability.STATE_SECONDS.labels(observability.SERVER, op).observe(time.monotonic() - t0)
        observability.STATE_UP.labels(observability.SERVER).set(1)
        return out

    async def once(self, ns: str, ident: str, ttl: float) -> bool:
        return await self._op("once", self._inner.once(ns, ident, ttl))

    async def counter(self, ns: str, ident: str, ttl: float) -> int:
        return await self._op("counter", self._inner.counter(ns, ident, ttl))

    async def window_take(self, ns: str, checks: Sequence[tuple[str, int]], window: float) -> bool:
        return await self._op("window", self._inner.window_take(ns, checks, window))

    async def bucket_take(self, ns: str, ident: str, capacity: int, refill_per_second: float) -> Bucket:
        return await self._op("bucket", self._inner.bucket_take(ns, ident, capacity, refill_per_second))

    async def acquire(self, ns: str, ident: str, ttl: float) -> str | None:
        return await self._op("acquire", self._inner.acquire(ns, ident, ttl))

    async def release(self, ns: str, ident: str, token: str) -> None:
        await self._op("release", self._inner.release(ns, ident, token))

    async def ping(self) -> None:
        await self._op("ping", self._inner.ping())

    async def close(self) -> None:
        await self._inner.close()


# ---------------------------------------------------------------- configuration


@dataclass(frozen=True)
class StateConfig:
    """Where the state lives. All empty: memory, for one replica."""

    url: str = ""  # redis:// or rediss://, standalone
    sentinels: tuple[tuple[str, int], ...] = ()
    master: str = "mymaster"
    db: int | None = None
    tls: bool = False
    username: str = ""
    password: str = field(default="", repr=False)
    sentinel_password: str = field(default="", repr=False)
    prefix: str = "vkteams:"

    @property
    def shared(self) -> bool:
        return bool(self.url or self.sentinels)

    @property
    def mode(self) -> str:
        return "sentinel" if self.sentinels else "standalone" if self.url else "memory"


def build_store(cfg: StateConfig, secret: str) -> StateStore:
    key = derive_key(secret)
    if not cfg.shared:
        return Observed(MemoryStore(key, cfg.prefix))
    from redis.asyncio import Redis
    from redis.asyncio.retry import Retry
    from redis.asyncio.sentinel import Sentinel
    from redis.backoff import NoBackoff

    common: dict[str, Any] = {
        "socket_timeout": CONNECT_TIMEOUT,
        "socket_connect_timeout": CONNECT_TIMEOUT,
        "retry": Retry(NoBackoff(), 1),  # a pooled connection that went stale is replaced once
        "health_check_interval": 10,
        "decode_responses": True,
    }
    if cfg.sentinels:
        sentinel_kwargs: dict[str, Any] = {
            "socket_timeout": CONNECT_TIMEOUT,
            "socket_connect_timeout": CONNECT_TIMEOUT,
            "retry": Retry(NoBackoff(), 0),  # a Sentinel that fails is skipped for the next one, not retried
            "decode_responses": True,
        }
        if cfg.sentinel_password:
            sentinel_kwargs["password"] = cfg.sentinel_password
        if cfg.tls:
            sentinel_kwargs["ssl"] = True
        sentinel = Sentinel(list(cfg.sentinels), sentinel_kwargs=sentinel_kwargs, **common)
        client = sentinel.master_for(
            cfg.master,
            db=cfg.db or 0,
            username=cfg.username or None,
            password=cfg.password or None,
            ssl=cfg.tls,
        )
    else:
        u = urlsplit(cfg.url)
        db = cfg.db if cfg.db is not None else int(u.path.strip("/") or 0)
        client = Redis(
            host=u.hostname or "localhost",
            port=u.port or 6379,
            db=db,
            username=cfg.username or None,
            password=cfg.password or None,
            ssl=u.scheme == "rediss" or cfg.tls,
            **common,
        )
    return Observed(RedisStore(client, key, cfg.prefix))
