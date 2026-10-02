"""Invalidation channel on real Redis: what the decorator publishes on cachekit:py:invalidate:v1, and how
a listening process evicts its L1 on other processes' events (two processes, forks, reconnects)."""

from __future__ import annotations

import ctypes
import logging
import multiprocessing
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import msgpack
import pytest
import redis

from cachekit import cache, invalidation
from cachekit.backends.errors import BackendError
from cachekit.backends.redis import provider as provider_module
from cachekit.backends.redis.provider import PerRequestRedisBackend
from cachekit.l1_cache import L1Cache, get_l1_cache
from tests.integration import _key_registry_worker as worker
from tests.unit.test_l1_memory_bounds import _child_outcome, _report

pytestmark = pytest.mark.integration


@pytest.fixture
def client(redis_test_client: redis.Redis) -> redis.Redis:
    return redis_test_client


@pytest.fixture
def subscriber(client: redis.Redis) -> Iterator[redis.client.PubSub]:
    """A raw subscription to the channel, confirmed before the test publishes anything."""
    pubsub = client.pubsub()
    pubsub.subscribe(invalidation.CHANNEL)
    confirmation = pubsub.get_message(timeout=5)
    assert confirmation is not None and confirmation["type"] == "subscribe"
    yield pubsub
    pubsub.close()


def _received(pubsub: redis.client.PubSub, wait: float = 0.5) -> list[dict[str, str]]:
    """Every event delivered within ``wait`` seconds, decoded."""
    events: list[dict[str, str]] = []
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        message = pubsub.get_message(timeout=0.05)
        if message is not None and message["type"] == "message":
            events.append(msgpack.unpackb(message["data"]))
    return events


def _registry_id(client: redis.Redis) -> str:
    (key,) = client.keys("t:default:ck:reg:*")
    return key.decode().removeprefix("t:default:")


def _client_as(client: redis.Redis, username: str, password: str) -> redis.Redis:
    """A client for the same server, logged in as another ACL user."""
    pool = client.connection_pool
    kwargs: dict[str, Any] = {**pool.connection_kwargs, "username": username, "password": password}
    return redis.Redis(connection_pool=redis.ConnectionPool(connection_class=pool.connection_class, **kwargs))


class TestDrainAnnouncement:
    def test_three_round_drain_publishes_once(
        self, client: redis.Redis, subscriber: redis.client.PubSub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(provider_module, "_DRAIN_CHUNK", 2)
        fn = cache(backend=PerRequestRedisBackend(client, "default"), ttl=300, namespace="chan_rounds")(worker.lookup)
        for x in range(5):
            fn(x)
        rid = _registry_id(client)
        script_calls = 0
        real_register = client.register_script

        def counting_register(script: str) -> Any:
            real = real_register(script)

            def call(*args: Any, **kwargs: Any) -> Any:
                nonlocal script_calls
                script_calls += 1
                return real(*args, **kwargs)

            return call

        monkeypatch.setattr(client, "register_script", counting_register)
        fn.invalidate_cache()

        assert script_calls == 3  # 2 + 2 + 1 members
        assert _received(subscriber) == [{"r": rid}]

    def test_round_guard_return_publishes_once(
        self, client: redis.Redis, subscriber: redis.client.PubSub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard stops the drain with members left and returns: the one deliberate
        announcement of a drain that left keys behind (they wait for the next drain)."""
        monkeypatch.setattr(provider_module, "_DRAIN_CHUNK", 2)
        monkeypatch.setattr(provider_module, "_DRAIN_MAX_ROUNDS", 2)
        fn = cache(backend=PerRequestRedisBackend(client, "default"), ttl=300, namespace="chan_guard")(worker.lookup)
        for x in range(6):
            fn(x)
        rid = _registry_id(client)

        fn.invalidate_cache()

        assert client.scard(f"t:default:{rid}") == 2  # left for the next drain
        assert _received(subscriber) == [{"r": rid}]

    def test_round_guard_that_raises_publishes_nothing(
        self, client: redis.Redis, subscriber: redis.client.PubSub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the guard raises instead of returning, the drain failed: no announcement."""
        monkeypatch.setattr(provider_module, "_DRAIN_CHUNK", 2)
        monkeypatch.setattr(provider_module, "_DRAIN_MAX_ROUNDS", 2)
        real_drain = PerRequestRedisBackend.drain_tracked

        def guard_raises(self: PerRequestRedisBackend, registry_id: str, local_keys: Any) -> set[str]:
            real_drain(self, registry_id, local_keys)
            if self._client.scard(self._scoped_key(registry_id)):
                raise BackendError("drain stopped at the round guard", operation="drain_tracked")
            return set()

        monkeypatch.setattr(PerRequestRedisBackend, "drain_tracked", guard_raises)
        fn = cache(backend=PerRequestRedisBackend(client, "default"), ttl=300, namespace="chan_guard_raise")(worker.lookup)
        for x in range(6):
            fn(x)

        fn.invalidate_cache()
        assert _received(subscriber) == []

    def test_failed_straggler_unlink_publishes_nothing(
        self, client: redis.Redis, subscriber: redis.client.PubSub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(provider_module, "_DRAIN_CHUNK", 2)
        fn = cache(backend=PerRequestRedisBackend(client, "default"), ttl=300, namespace="chan_straggler")(worker.lookup)
        for x in range(5):
            fn(x)
        for reg in client.keys("*ck:reg:*"):
            client.delete(reg)  # every key is a straggler: 3 UNLINK batches of <= 2
        real_unlink = client.unlink
        calls = 0

        def flaky_unlink(*keys: str) -> int:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise redis.ConnectionError("lost")
            return real_unlink(*keys)

        monkeypatch.setattr(client, "unlink", flaky_unlink)
        fn.invalidate_cache()  # falls back to this process's own keys

        assert calls == 2
        assert _received(subscriber) == []

    def test_publish_denied_by_acl_leaves_the_drain_standing(
        self, client: redis.Redis, subscriber: redis.client.PubSub, caplog: pytest.LogCaptureFixture
    ) -> None:
        user, password = "ck-chan-nopub", "ck-chan-nopub-pw"  # pragma: allowlist secret - throwaway ACL user
        client.acl_setuser(
            user, enabled=True, passwords=[f"+{password}"], keys=["*"], channels=["*"], commands=["+@all", "-publish"]
        )
        try:
            restricted = _client_as(client, user, password)
            fn = cache(backend=PerRequestRedisBackend(restricted, "default"), ttl=300, namespace="chan_acl")(worker.lookup)
            for x in range(3):
                fn(x)
            with caplog.at_level(logging.WARNING):
                fn.invalidate_cache()

            assert not client.keys("t:default:*")  # every key and the tracking set are gone
            assert "invalidating local keys only" not in caplog.text
            assert "Invalidation announcement failed" in caplog.text and "NoPermissionError" in caplog.text
            assert _received(subscriber) == []
            restricted.close()
        finally:
            client.acl_deluser(user)


class TestSingleKeyAnnouncement:
    def test_one_event_for_the_current_key(self, client: redis.Redis, subscriber: redis.client.PubSub) -> None:
        fn = cache(backend=PerRequestRedisBackend(client, "default"), ttl=300, namespace="chan_single", serializer="auto")(
            worker.lookup
        )
        fn(1)
        (scoped,) = [k for k in client.keys("t:default:*") if b":ck:reg:" not in k]
        current_key = scoped.decode().removeprefix("t:default:")
        rid = _registry_id(client)

        fn.invalidate_cache(1)

        assert _received(subscriber) == [{"r": rid, "k": current_key}]

    def test_custom_key_announces_the_function(self, client: redis.Redis, subscriber: redis.client.PubSub) -> None:
        fn = cache(
            backend=PerRequestRedisBackend(client, "default"), ttl=300, namespace="chan_custom", key=lambda x: f"user-{x}"
        )(worker.lookup)
        fn(1)
        rid = _registry_id(client)

        fn.invalidate_cache(1)

        assert _received(subscriber) == [{"r": rid}]


# ---- The listener ----


@pytest.fixture
def listening(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """CACHEKIT_INVALIDATION_LISTENER_ENABLED for this process; the listener is stopped afterwards."""
    monkeypatch.setattr(invalidation, "_listener_flag", True)
    monkeypatch.setattr(invalidation, "_start_retry_at", float("-inf"))
    invalidation._stop_listener()
    yield
    invalidation._stop_listener()


def _redis_url(client: redis.Redis) -> str:
    conn = client.connection_pool.connection_kwargs
    db = conn.get("db", 0)
    return f"unix://{conn['path']}?db={db}" if "path" in conn else f"redis://{conn['host']}:{conn['port']}/{db}"


def _run_peer(client: redis.Redis, call: str) -> float:
    """Run ``call`` from the worker module in another process with no listener; returns the
    time.time() at which its last invalidation returned."""
    env = {k: v for k, v in os.environ.items() if k != "CACHEKIT_INVALIDATION_LISTENER_ENABLED"}
    proc = subprocess.run(  # noqa: S603 - trusted: sys.executable + literal code
        [sys.executable, "-c", f"from tests.integration._key_registry_worker import invalidate; {call}"],
        cwd=Path(__file__).resolve().parents[2],
        env={**env, "CK_TEST_REDIS_URL": _redis_url(client)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return float(proc.stdout.strip().splitlines()[-1])


def _subscribers(client: redis.Redis) -> int:
    ((_, count),) = client.pubsub_numsub(invalidation.CHANNEL)
    return count


def _wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _spy_evictions(l1: L1Cache, monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, Any]]:
    """Record (time.time(), keys) for every eviction of ``l1``."""
    seen: list[tuple[float, Any]] = []
    invalidate, invalidate_many = l1.invalidate, l1.invalidate_many

    def one(key: str) -> None:
        seen.append((time.time(), key))
        invalidate(key)

    def many(keys: Any) -> None:
        keys = set(keys)
        seen.append((time.time(), keys))
        invalidate_many(keys)

    monkeypatch.setattr(l1, "invalidate", one)
    monkeypatch.setattr(l1, "invalidate_many", many)
    return seen


def _l1_keys(namespace: str) -> set[str]:
    return set(get_l1_cache(namespace)._state.cache)


class TestListenerAcrossProcesses:
    """The listener in this process evicts what a process without one invalidates."""

    def test_peer_single_key_invalidation_evicts_it_within_a_second(
        self, client: redis.Redis, listening: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ns = "chan_xproc_one"
        fn = worker.cached_lookup(client, namespace=ns)
        fn(1)  # the first cache operation starts the listener
        fn(2)
        assert invalidation._listener_pid == os.getpid()
        assert _wait_for(lambda: _subscribers(client) == 1)
        before = _l1_keys(ns)
        evictions = _spy_evictions(get_l1_cache(ns), monkeypatch)

        announced_at = _run_peer(client, f"invalidate([1], {ns!r})")

        assert _wait_for(lambda: len(evictions) == 1, timeout=5)
        evicted_at, evicted_key = evictions[0]
        assert evicted_at - announced_at <= 1.0
        assert _l1_keys(ns) == before - {evicted_key} and len(_l1_keys(ns)) == 1

    def test_peer_drain_evicts_every_key_within_a_second(
        self, client: redis.Redis, listening: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The peer has no listener flag: it still publishes, and this listening process evicts."""
        ns = "chan_xproc_drain"
        fn = worker.cached_lookup(client, namespace=ns)
        for x in range(3):
            fn(x)
        assert _wait_for(lambda: _subscribers(client) == 1)
        cached = _l1_keys(ns)
        assert len(cached) == 3
        evictions = _spy_evictions(get_l1_cache(ns), monkeypatch)

        announced_at = _run_peer(client, f"invalidate([], {ns!r})")

        assert _wait_for(lambda: len(evictions) == 1, timeout=5)
        evicted_at, evicted_keys = evictions[0]
        assert evicted_at - announced_at <= 1.0
        assert evicted_keys == cached and _l1_keys(ns) == set()


class TestListenerAndFork:
    """A forked child never shares its parent's subscription."""

    def test_multiprocessing_child_runs_its_own_listener_and_leaves_the_parents_alone(
        self, client: redis.Redis, listening: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ns = "chan_fork_mp"
        fn = worker.cached_lookup(client, namespace=ns)
        fn(1)
        assert _wait_for(lambda: _subscribers(client) == 1)
        (parent_conn,) = client.client_list(_type="pubsub")
        parent_listener = invalidation._listener

        ctx = multiprocessing.get_context("fork")
        results, done = ctx.Queue(), ctx.Event()

        def child(q: Any, release: Any) -> None:
            outcome = {"l1_empty": _l1_keys(ns) == set(), "inherited": invalidation._listener_pid == os.getppid()}
            fn(2)  # the child's first cache operation starts the child's own listener
            outcome["own"] = invalidation._listener_pid == os.getpid() and invalidation._listener[1].is_alive()
            q.put(outcome)
            release.wait(20)  # stay subscribed while the parent counts

        process = ctx.Process(target=child, args=(results, done))
        process.start()
        try:
            outcome = results.get(timeout=30)
            assert _wait_for(lambda: _subscribers(client) == 2)  # the child's connection beside the parent's
        finally:
            done.set()
            process.join(timeout=20)
            if process.is_alive():
                process.kill()
        assert outcome == {"l1_empty": True, "inherited": True, "own": True}
        assert process.exitcode == 0

        # The child dropped its inherited copy of the parent's PubSub without touching the socket.
        assert _wait_for(lambda: _subscribers(client) == 1)
        assert [c["id"] for c in client.client_list(_type="pubsub")] == [parent_conn["id"]]
        assert invalidation._listener is parent_listener and invalidation._listener_pid == os.getpid()
        evictions = _spy_evictions(get_l1_cache(ns), monkeypatch)
        _run_peer(client, f"invalidate([1], {ns!r})")
        assert _wait_for(lambda: len(evictions) == 1, timeout=5)

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs glibc fork() via ctypes")
    def test_child_of_a_c_fork_starts_no_listener_and_logs_nothing(
        self, client: redis.Redis, listening: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fork made from C runs no at-fork hook (uWSGI without its fork options): the child runs
        without a listener, starts no thread for one and logs nothing, and the parent keeps its own."""
        ns = "chan_fork_c"
        fn = worker.cached_lookup(client, namespace=ns)
        fn(1)
        assert _wait_for(lambda: _subscribers(client) == 1)
        records: list[logging.LogRecord] = []

        class Recording(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = Recording(level=logging.DEBUG)
        channel_logger = logging.getLogger(invalidation.__name__)
        monkeypatch.setattr(channel_logger, "level", logging.DEBUG)
        channel_logger.addHandler(handler)
        try:
            parent = os.getpid()
            r, w = os.pipe()
            child = ctypes.PyDLL(None).fork()  # PyDLL keeps the GIL through the call
            if child == 0:
                try:
                    signal.alarm(10)  # a hang on a lock a parent thread held ends the child instead
                    os.close(r)
                    started: list[str] = []
                    threading.Thread.start = lambda self: started.append(type(self).__name__)  # type: ignore[method-assign]
                    del records[:]
                    value = fn(2)  # a cache operation that reaches the backend
                    _report(
                        w,
                        {
                            "value": value,
                            "started": started,
                            "records": [rec.getMessage() for rec in records],
                            "listener_pid_is_parents": invalidation._listener_pid == parent,
                        },
                    )
                finally:
                    os._exit(1)
            os.close(w)
            assert _child_outcome(child, r) == {
                "value": 20,
                "started": [],
                "records": [],
                "listener_pid_is_parents": True,
            }
        finally:
            channel_logger.removeHandler(handler)
        assert [c["id"] for c in client.client_list(_type="pubsub")] != []
        evictions = _spy_evictions(get_l1_cache(ns), monkeypatch)
        _run_peer(client, f"invalidate([1], {ns!r})")
        assert _wait_for(lambda: len(evictions) == 1, timeout=5)


class TestListenerResilience:
    def test_forged_events_are_dropped_and_the_listener_keeps_working(
        self, client: redis.Redis, listening: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        ns = "chan_forged"
        fn = worker.cached_lookup(client, namespace=ns)
        fn(1)
        assert _wait_for(lambda: _subscribers(client) == 1)
        rid = _registry_id(client)
        (key,) = _l1_keys(ns)
        evictions = _spy_evictions(get_l1_cache(ns), monkeypatch)
        forged = [
            b"SECRET" * 1000,  # oversize
            b"\x81\xa1a" * 1300 + b"\xc0",  # nested past the decoder's stack
            msgpack.packb(["SECRET-array"]),  # not a map
            msgpack.packb({"r": 7, "k": "SECRET-key"}),  # registry id not a string
        ]
        with caplog.at_level(logging.DEBUG, logger=invalidation.__name__):
            for payload in forged:
                client.publish(invalidation.CHANNEL, payload)
            client.publish(invalidation.CHANNEL, msgpack.packb({"r": "ck:reg:SECRET:unknown", "k": "SECRET-key"}))
            client.publish(invalidation.CHANNEL, invalidation.encode_event(rid, key))  # a real one, last
            assert _wait_for(lambda: len(evictions) == 1, timeout=5)

        assert evictions[0][1] == key and _l1_keys(ns) == set()
        assert invalidation._listener[1].is_alive()
        messages = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == invalidation.__name__]
        assert sum(m.startswith("Invalidation event dropped") for level, m in messages if level == logging.WARNING) == 4
        assert any("does not cache" in m for level, m in messages if level == logging.DEBUG)
        assert "SECRET" not in caplog.text

    def test_listener_reconnects_and_resubscribes(
        self, client: redis.Redis, listening: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ns = "chan_reconnect"
        fn = worker.cached_lookup(client, namespace=ns)
        fn(1)
        assert _wait_for(lambda: _subscribers(client) == 1)
        (before,) = client.client_list(_type="pubsub")

        assert client.client_kill_filter(_type="pubsub") == 1
        assert _wait_for(lambda: [c["id"] for c in client.client_list(_type="pubsub")] not in ([], [before["id"]]), timeout=20)
        assert _wait_for(lambda: _subscribers(client) == 1, timeout=20)

        evictions = _spy_evictions(get_l1_cache(ns), monkeypatch)
        _run_peer(client, f"invalidate([1], {ns!r})")
        assert _wait_for(lambda: len(evictions) == 1, timeout=5)


class TestListenerConfiguration:
    def test_flag_on_with_a_direct_redis_backend_warns_once_and_starts_nothing(
        self, client: redis.Redis, listening: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        from cachekit.backends.redis.backend import RedisBackend
        from tests.fixtures.backend_providers import TestCacheClientProvider

        backend = RedisBackend(client_provider=TestCacheClientProvider(sync_client=client))
        fn = cache(backend=backend, ttl=300, namespace="chan_direct")(worker.lookup)
        with caplog.at_level(logging.WARNING, logger=invalidation.__name__):
            for x in range(3):
                fn(x)
        warnings = [r.getMessage() for r in caplog.records if r.name == invalidation.__name__]
        assert len(warnings) == 1 and "RedisBackend does not carry invalidation events" in warnings[0]
        assert invalidation._listener_pid is None and _subscribers(client) == 0

    def test_flag_unset_opens_no_connection(self, client: redis.Redis) -> None:
        assert invalidation._listener_enabled() is False  # the default
        fn = worker.cached_lookup(client, namespace="chan_flag_off")
        before = len(client.client_list())
        for x in range(3):
            fn(x)
        assert invalidation._listener_pid is None and _subscribers(client) == 0
        assert len(client.client_list()) == before  # no dedicated connection (the shared pool's is reused)
        assert not [t for t in threading.enumerate() if t.name == "cachekit-invalidation-listener"]

    def test_refused_subscription_fails_the_start_and_a_later_grant_takes_effect(
        self, client: redis.Redis, listening: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Redis 7+ gives a new ACL user no channels: SUBSCRIBE is refused. The start waits for the
        confirmation, so the process never believes it listens; granting the channel later takes
        effect on the next start, with no restart."""
        user, password = "ck-chan-nosub", "ck-chan-nosub-pw"  # pragma: allowlist secret - throwaway ACL user
        client.acl_setuser(user, enabled=True, passwords=[f"+{password}"], keys=["*"], commands=["+@all"], reset_channels=True)
        try:
            restricted = _client_as(client, user, password)
            ns = "chan_acl_sub"
            fn = worker.cached_lookup(restricted, namespace=ns)
            with caplog.at_level(logging.WARNING, logger=invalidation.__name__):
                assert fn(1) == 10  # the cache operation is unaffected
            assert "Invalidation listener failed to start" in caplog.text and "NoPermissionError" in caplog.text
            assert invalidation._listener_pid is None and _subscribers(client) == 0

            client.acl_setuser(user, enabled=True, channels=[invalidation.CHANNEL])  # enabled: SETUSER defaults to off
            monkeypatch.setattr(invalidation, "_start_retry_at", float("-inf"))  # the retry window passed
            assert fn(2) == 20  # a cache operation that reaches Redis starts it again
            assert invalidation._listener_pid == os.getpid() and _wait_for(lambda: _subscribers(client) == 1)
            evictions = _spy_evictions(get_l1_cache(ns), monkeypatch)
            _run_peer(client, f"invalidate([1], {ns!r})")
            assert _wait_for(lambda: len(evictions) == 1, timeout=5)
            invalidation._stop_listener()
            restricted.close()
        finally:
            client.acl_deluser(user)

    def test_subscription_revoked_while_running_retires_the_listener(
        self, client: redis.Redis, listening: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        user, password = "ck-chan-revoked", "ck-chan-revoked-pw"  # pragma: allowlist secret - throwaway ACL user
        client.acl_setuser(user, enabled=True, passwords=[f"+{password}"], keys=["*"], channels=["*"], commands=["+@all"])
        try:
            restricted = _client_as(client, user, password)
            fn = worker.cached_lookup(restricted, namespace="chan_acl_revoked")
            fn(1)
            assert invalidation._listener_pid == os.getpid() and _wait_for(lambda: _subscribers(client) == 1)
            with caplog.at_level(logging.WARNING, logger=invalidation.__name__):
                client.acl_setuser(user, enabled=True, reset_channels=True)  # Redis drops the now-forbidden subscription
                assert _wait_for(lambda: "Invalidation listener stopped" in caplog.text, timeout=15)
            assert invalidation._listener_pid is None and _subscribers(client) == 0
            restricted.close()
        finally:
            client.acl_deluser(user)
