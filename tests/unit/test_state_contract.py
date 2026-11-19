"""The state store's contract: one suite, every implementation.

`MemoryStore` always. `RedisStore` against a REAL Valkey, standalone and behind Sentinel, when
VKTEAMS_TEST_VALKEY / VKTEAMS_TEST_SENTINELS name one (scripts/state-tests.sh starts them; CI does). With
VKTEAMS_TEST_REQUIRE_VALKEY=1 a missing Valkey fails the suite instead of skipping it: a contract suite that
skipped its real half would pass having tested nothing. No fakeredis, no mocks.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest

from vkteams_mcp.core.state import (
    MemoryStore,
    Observed,
    RedisStore,
    StateConfig,
    StateUnavailable,
    build_store,
    derive_key,
)

SECRET = "k" * 40
REQUIRE = os.environ.get("VKTEAMS_TEST_REQUIRE_VALKEY") == "1"


def _hostport(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host, int(port)


def _need(var: str) -> str:
    value = os.environ.get(var)
    if not value:
        assert not REQUIRE, f"VKTEAMS_TEST_REQUIRE_VALKEY=1 but {var} is not set"
        pytest.skip(f"no {var}")
    return value


def config_for(backend: str, prefix: str) -> StateConfig:
    """memory; valkey and sentinel (Valkey standalone, and behind three Sentinels); redis and redis-sentinel (Redis
    standalone, and behind one Sentinel, as the platform runs it)."""
    if backend == "memory":
        return StateConfig(prefix=prefix)
    if backend in ("valkey", "redis"):
        host = _need("VKTEAMS_TEST_VALKEY" if backend == "valkey" else "VKTEAMS_TEST_REDIS")
        return StateConfig(url=f"redis://{host}/0", prefix=prefix)
    var = "VKTEAMS_TEST_SENTINELS" if backend == "sentinel" else "VKTEAMS_TEST_REDIS_SENTINELS"
    return StateConfig(
        sentinels=tuple(_hostport(x) for x in _need(var).split(",")),
        master=os.environ.get("VKTEAMS_TEST_SENTINEL_MASTER", "mymaster"),
        prefix=prefix,
    )


BACKENDS = ["memory", "valkey", "sentinel", "redis", "redis-sentinel"]


@pytest.fixture(params=BACKENDS)
def backend(request):
    return request.param


@pytest.fixture
async def store(backend):
    s = build_store(config_for(backend, f"t{uuid.uuid4().hex[:8]}:"), SECRET)
    await s.ping()  # a Valkey that is not there fails here, loudly, not three tests later
    yield s
    await s.close()


# ---------------------------------------------------------------- once


async def test_once_is_true_the_first_time_only(store):
    assert await store.once("code", "a", 30) is True
    assert await store.once("code", "a", 30) is False
    assert await store.once("code", "b", 30) is True


async def test_once_is_per_namespace(store):
    assert await store.once("one", "x", 30) is True
    assert await store.once("two", "x", 30) is True


async def test_once_forgets_after_its_ttl(store):
    assert await store.once("code", "a", 0.3) is True
    await asyncio.sleep(0.45)
    assert await store.once("code", "a", 30) is True


async def test_once_has_one_winner_among_concurrent_callers(store):
    wins = await asyncio.gather(*[store.once("code", "race", 30) for _ in range(60)])
    assert sum(wins) == 1


# ---------------------------------------------------------------- counter


async def test_a_counter_counts_and_expires_from_its_first_increment(store):
    assert [await store.counter("tries", "n", 0.5) for _ in range(3)] == [1, 2, 3]
    await asyncio.sleep(0.65)
    assert await store.counter("tries", "n", 30) == 1


async def test_a_counter_is_exact_under_concurrency(store):
    got = await asyncio.gather(*[store.counter("tries", "c", 30) for _ in range(40)])
    assert sorted(got) == list(range(1, 41))


# ---------------------------------------------------------------- window


async def test_a_window_allows_its_limit_then_refuses(store):
    for _ in range(3):
        assert await store.window_take("codes", [("a", 3)], 30) is True
    assert await store.window_take("codes", [("a", 3)], 30) is False
    assert await store.window_take("codes", [("b", 3)], 30) is True  # another key has its own window


async def test_a_window_slides(store):
    assert await store.window_take("codes", [("a", 2)], 0.5) is True
    await asyncio.sleep(0.3)
    assert await store.window_take("codes", [("a", 2)], 0.5) is True
    assert await store.window_take("codes", [("a", 2)], 0.5) is False
    await asyncio.sleep(0.3)  # the first hit is 0.6 s old: out of the window; the second is still in
    assert await store.window_take("codes", [("a", 2)], 0.5) is True
    assert await store.window_take("codes", [("a", 2)], 0.5) is False


async def test_a_refusal_records_nothing_on_any_key(store):
    """Two keys, all or none: the address that was refused must not use up the network's allowance."""
    for _ in range(2):
        assert await store.window_take("codes", [("addr", 2)], 30) is True
    for _ in range(5):
        assert await store.window_take("codes", [("addr", 2), ("net", 3)], 30) is False
    # the network was asked 5 times in refused calls and recorded none of them: its 3 are all still there
    for _ in range(3):
        assert await store.window_take("codes", [("net", 3)], 30) is True
    assert await store.window_take("codes", [("net", 3)], 30) is False


async def test_a_window_is_exact_under_concurrency(store):
    got = await asyncio.gather(*[store.window_take("codes", [("race", 5)], 30) for _ in range(40)])
    assert sum(got) == 5


# ---------------------------------------------------------------- bucket


async def test_a_bucket_gives_its_capacity_then_says_how_long_to_wait(store):
    for i in range(10):
        b = await store.bucket_take("bucket", "p", 10, 10 / 60)
        assert b.ok and b.left == 9 - i
    b = await store.bucket_take("bucket", "p", 10, 10 / 60)
    assert not b.ok and b.left == 0 and 1 <= b.wait <= 6
    assert (await store.bucket_take("bucket", "q", 10, 10 / 60)).ok  # per key


async def test_a_bucket_refills(store):
    for _ in range(3):
        assert (await store.bucket_take("bucket", "p", 3, 20.0)).ok
    assert not (await store.bucket_take("bucket", "p", 3, 20.0)).ok
    await asyncio.sleep(0.2)  # 20 a second: four tokens' worth, capped at the capacity of 3
    got = [(await store.bucket_take("bucket", "p", 3, 20.0)).ok for _ in range(4)]
    assert got[:3] == [True, True, True] and got[3] is False


async def test_a_bucket_is_exact_under_concurrency(store):
    got = await asyncio.gather(*[store.bucket_take("bucket", "race", 10, 0.001) for _ in range(40)])
    assert sum(b.ok for b in got) == 10


# ---------------------------------------------------------------- lock


async def test_a_lock_is_held_until_released_by_its_holder(store):
    token = await store.acquire("upload", "u", 30)
    assert token
    assert await store.acquire("upload", "u", 30) is None
    await store.release("upload", "u", "not-the-token")  # someone else's token frees nothing
    assert await store.acquire("upload", "u", 30) is None
    await store.release("upload", "u", token)
    assert await store.acquire("upload", "u", 30)


async def test_a_lock_expires_by_itself(store):
    assert await store.acquire("upload", "u", 0.3)
    await asyncio.sleep(0.45)
    assert await store.acquire("upload", "u", 30)


async def test_a_late_release_does_not_free_the_next_holders_lock(store):
    first = await store.acquire("upload", "u", 0.3)
    await asyncio.sleep(0.45)
    second = await store.acquire("upload", "u", 30)
    assert second
    await store.release("upload", "u", first)  # the first holder, long expired
    assert await store.acquire("upload", "u", 30) is None


async def test_a_lock_has_one_holder_among_concurrent_callers(store):
    got = await asyncio.gather(*[store.acquire("upload", "race", 30) for _ in range(40)])
    assert len([t for t in got if t]) == 1


# ---------------------------------------------------------------- what the store holds


async def test_no_key_carries_a_login_an_address_or_an_ip(backend):
    prefix = f"t{uuid.uuid4().hex[:8]}:"
    s = build_store(config_for(backend, prefix), SECRET)
    secrets_ = ["person@example.com", "198.51.100.7", "someone.else"]
    for v in secrets_:
        await s.once("code", v, 30)
        await s.counter("tries", v, 30)
        await s.window_take("codes", [("a:" + v, 3), ("n:" + v, 3)], 30)
        await s.bucket_take("bucket", v, 10, 1.0)
        await s.acquire("upload", v, 30)
    inner = s._inner
    if isinstance(inner, RedisStore):
        keys = [k async for k in inner._r.scan_iter(match=f"{prefix}*", count=100)]
    else:
        keys = inner.keys()
    assert len(keys) >= 5 * len(secrets_)  # the store did hold them: a search through nothing proves nothing
    assert all(k.startswith(prefix) for k in keys)
    for k in keys:
        for v in secrets_:
            assert v not in k and v.replace(".", "") not in k
    await s.close()


async def test_prefixes_keep_two_deployments_apart(backend):
    a = build_store(config_for(backend, f"a{uuid.uuid4().hex[:6]}:"), SECRET)
    b = build_store(config_for(backend, f"b{uuid.uuid4().hex[:6]}:"), SECRET)
    if backend == "memory":
        pytest.skip("two memory stores share nothing, with or without a prefix")
    assert await a.once("code", "x", 30) is True
    assert await b.once("code", "x", 30) is True
    await a.close()
    await b.close()


async def test_a_different_sealing_key_is_a_different_keyspace(backend):
    if backend == "memory":
        pytest.skip("memory shares nothing between stores")
    prefix = f"t{uuid.uuid4().hex[:8]}:"
    a = build_store(config_for(backend, prefix), "a" * 40)
    b = build_store(config_for(backend, prefix), "b" * 40)
    assert await a.once("code", "x", 30) is True
    assert await b.once("code", "x", 30) is True  # a new deploy's key: nothing of the old one counts
    await a.close()
    await b.close()


# ---------------------------------------------------------------- two replicas, one store


async def test_two_clients_of_one_store_agree(backend):
    """What the replicas rely on: the second one sees what the first one did."""
    if backend == "memory":
        pytest.skip("memory is one replica's")
    prefix = f"t{uuid.uuid4().hex[:8]}:"
    one, two, three = (build_store(config_for(backend, prefix), SECRET) for _ in range(3))
    assert await one.once("code", "c", 30) is True
    assert await two.once("code", "c", 30) is False
    assert await three.once("code", "c", 30) is False
    for i in range(3):  # three codes, one through each replica; the fourth, through any, is refused
        assert await [one, two, three][i].window_take("codes", [("a", 3)], 30) is True
    assert await two.window_take("codes", [("a", 3)], 30) is False
    for s in (one, two, three):
        for _ in range(3):
            await s.bucket_take("bucket", "p", 10, 10 / 60)
    assert (await one.bucket_take("bucket", "p", 10, 10 / 60)).left == 0  # 9 taken so far, this is the tenth
    assert not (await three.bucket_take("bucket", "p", 10, 10 / 60)).ok
    token = await one.acquire("upload", "u", 30)
    assert await two.acquire("upload", "u", 30) is None
    await three.release("upload", "u", token)
    assert await two.acquire("upload", "u", 30)
    for s in (one, two, three):
        await s.close()


# ---------------------------------------------------------------- the store is gone


@pytest.mark.parametrize("mode", ["standalone", "sentinel"])
async def test_an_unreachable_store_raises_state_unavailable_quickly(mode):
    """Nothing listens on port 1 of the loopback: every operation fails closed, within the timeouts."""
    cfg = (
        StateConfig(url="redis://127.0.0.1:1/0", prefix="t:")
        if mode == "standalone"
        else StateConfig(sentinels=(("127.0.0.1", 1),), prefix="t:")
    )
    s = build_store(cfg, SECRET)
    started = time.monotonic()
    for call in (
        s.once("code", "a", 30),
        s.counter("tries", "a", 30),
        s.window_take("codes", [("a", 3)], 30),
        s.bucket_take("bucket", "a", 10, 1.0),
        s.acquire("upload", "a", 30),
        s.ping(),
    ):
        with pytest.raises(StateUnavailable):
            await call
    assert time.monotonic() - started < 10
    await s.close()


async def test_the_error_names_the_class_and_nothing_else():
    s = build_store(StateConfig(url="redis://127.0.0.1:1/0"), SECRET)
    with pytest.raises(StateUnavailable) as e:
        await s.ping()
    assert "127.0.0.1" not in str(e.value)
    await s.close()


# ---------------------------------------------------------------- the memory store on its own clock


async def test_the_memory_store_follows_its_clock():
    now = [0.0]
    m = MemoryStore(derive_key(SECRET), clock=lambda: now[0])
    assert await m.once("code", "a", 10) is True
    assert await m.once("code", "a", 10) is False
    now[0] = 10.0
    assert await m.once("code", "a", 10) is True
    assert await m.window_take("codes", [("a", 1)], 60) is True
    assert await m.window_take("codes", [("a", 1)], 60) is False
    now[0] = 70.0
    assert await m.window_take("codes", [("a", 1)], 60) is True


async def test_a_full_memory_store_refuses_newcomers_and_forgets_nothing_live(monkeypatch):
    """Full of live entries, the store fails closed for a NEW key. It never forgets a live one: that would lift a limit,
    free a held lock or let a spent code be spent again."""
    from vkteams_mcp.core import state

    monkeypatch.setattr(state, "MEMORY_MAX", 3)
    now = [0.0]
    m = MemoryStore(derive_key(SECRET), clock=lambda: now[0])
    assert await m.once("code", "spent", 300) is True
    assert await m.counter("tries", "n", 300) == 1
    lock = await m.acquire("upload", "u", 300)
    assert lock
    for call in (m.once("code", "new", 300), m.counter("tries", "new", 300), m.acquire("upload", "new", 300)):
        with pytest.raises(StateUnavailable):
            await call
    # nothing live was forgotten
    assert await m.once("code", "spent", 300) is False
    assert await m.counter("tries", "n", 300) == 2
    assert await m.acquire("upload", "u", 300) is None
    # windows and buckets are bounded in the same way
    for i in range(3):
        assert await m.window_take("codes", [(f"a{i}", 5)], 300) is True
        assert (await m.bucket_take("bucket", f"p{i}", 10, 1.0)).ok
    with pytest.raises(StateUnavailable):
        await m.window_take("codes", [("a-new", 5)], 300)
    with pytest.raises(StateUnavailable):
        await m.bucket_take("bucket", "p-new", 10, 1.0)
    assert await m.window_take("codes", [("a0", 5)], 300) is True  # an existing key still works
    # once entries expire (and a second has passed), newcomers are admitted again
    now[0] = 400.0
    assert await m.once("code", "new", 300) is True
    assert await m.window_take("codes", [("a-new", 5)], 300) is True
    assert (await m.bucket_take("bucket", "p-new", 10, 1.0)).ok


async def test_the_observed_wrapper_counts_and_follows_the_store(backend):
    from vkteams_mcp import observability as obs

    s = build_store(config_for(backend, f"t{uuid.uuid4().hex[:8]}:"), SECRET)
    assert isinstance(s, Observed)
    before = obs.STATE_SECONDS.labels(obs.SERVER, "once")._sum.get()
    await s.once("code", "a", 30)
    assert obs.STATE_SECONDS.labels(obs.SERVER, "once")._sum.get() > before
    assert obs.STATE_UP.labels(obs.SERVER)._value.get() == 1
    await s.close()


async def test_a_dead_store_is_counted_and_the_gauge_goes_down_then_up():
    from vkteams_mcp import observability as obs

    s = build_store(StateConfig(url="redis://127.0.0.1:1/0"), SECRET)
    errors = obs.STATE_ERRORS.labels(obs.SERVER, "once")._value.get()
    with pytest.raises(StateUnavailable):
        await s.once("code", "a", 30)
    assert obs.STATE_ERRORS.labels(obs.SERVER, "once")._value.get() == errors + 1
    assert obs.STATE_UP.labels(obs.SERVER)._value.get() == 0
    m = build_store(StateConfig(), SECRET)
    obs.STATE_UP.labels(obs.SERVER).set(0)  # the constructor starts it at 1: put it down, then let an answer raise it
    await m.ping()
    assert obs.STATE_UP.labels(obs.SERVER)._value.get() == 1
    await s.close()


async def test_a_call_with_several_new_keys_needs_room_for_all_of_them(monkeypatch):
    """CodeLimiter counts an address and a network in one call: with room for one of the two, neither is counted."""
    from vkteams_mcp.core import state

    monkeypatch.setattr(state, "MEMORY_MAX", 3)
    m = MemoryStore(derive_key(SECRET))
    assert await m.window_take("codes", [("a", 5), ("b", 5)], 300) is True
    with pytest.raises(StateUnavailable):
        await m.window_take("codes", [("c", 5), ("d", 5)], 300)  # one free slot, two newcomers
    assert len(m.keys()) == 2  # nothing of the refused call was recorded
    assert await m.window_take("codes", [("c", 5)], 300) is True
