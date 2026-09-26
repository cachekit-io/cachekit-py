"""Key registry on real Redis: PerRequestRedisBackend.track_key / drain_tracked and the
decorator's whole-function invalidate_cache() across processes and tenants."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import redis

from cachekit import cache
from cachekit.backends.errors import BackendError
from cachekit.backends.redis import provider as provider_module
from cachekit.backends.redis.provider import PerRequestRedisBackend, tenant_context
from tests.integration import _key_registry_worker as worker

pytestmark = pytest.mark.integration

REG = "ck:reg:ns:0123456789abcdef"


@pytest.fixture
def client(redis_test_client: redis.Redis) -> redis.Redis:
    return redis_test_client


def _cache_keys(client: redis.Redis, tenant: str) -> set[bytes]:
    """Every key under the tenant prefix except tracking sets."""
    return {k for k in client.keys(f"t:{tenant}:*") if b":ck:reg:" not in k}


def _registry_sets(client: redis.Redis) -> list[bytes]:
    return list(client.keys("*ck:reg:*"))


class TestTrackKey:
    def test_tracking_set_stores_raw_keys(self, client: redis.Redis) -> None:
        backend = PerRequestRedisBackend(client, "self")
        backend.track_key(REG, "ns:x:func:m.f:args:abc:1")
        assert client.smembers(f"t:self:{REG}") == {b"ns:x:func:m.f:args:abc:1"}
        assert 604_000 < client.ttl(f"t:self:{REG}") <= 604_800

    def test_track_key_failure_is_classified(self, client: redis.Redis) -> None:
        broken = MagicMock()
        broken.pipeline.side_effect = redis.ConnectionError("down")
        with pytest.raises(BackendError):
            PerRequestRedisBackend(broken, "self").track_key(REG, "k")


class TestDrainTracked:
    def test_drain_deletes_actual_l2_data(self, client: redis.Redis) -> None:
        backend = PerRequestRedisBackend(client, "self")
        for k in ("a", "b"):
            backend.set(k, b"v")
            backend.track_key(REG, k)
        assert backend.drain_tracked(REG, set()) == {"a", "b"}
        assert _cache_keys(client, "self") == set()
        assert not client.exists(f"t:self:{REG}")

    def test_drain_returns_raw_keys_for_l1(self, client: redis.Redis) -> None:
        backend = PerRequestRedisBackend(client, "org:1")
        backend.set("user:9", b"v")
        backend.track_key(REG, "user:9")
        assert backend.drain_tracked(REG, {"local-only"}) == {"user:9", "local-only"}

    def test_drain_stays_inside_tenant_prefix(self, client: redis.Redis) -> None:
        """A planted member naming another tenant's key unlinks only
        {own prefix}{member}; the other tenant's key survives."""
        client.set("t:other:secret", b"theirs")
        client.set("t:self:t:other:secret", b"ours")
        client.sadd(f"t:self:{REG}", "t:other:secret")

        PerRequestRedisBackend(client, "self").drain_tracked(REG, set())
        assert client.get("t:other:secret") == b"theirs"
        assert not client.exists("t:self:t:other:secret")

    def test_drain_is_chunked(self, client: redis.Redis) -> None:
        """25 000 members drain in three script calls; every key and the set are gone."""
        backend = PerRequestRedisBackend(client, "self")
        members = [f"k{i}" for i in range(25_000)]
        pipe = client.pipeline(transaction=False)
        for i in range(0, len(members), 5_000):
            pipe.sadd(f"t:self:{REG}", *members[i : i + 5_000])
            pipe.mset({f"t:self:{m}": b"v" for m in members[i : i + 5_000]})
        pipe.execute()

        script = MagicMock(wraps=client.register_script(provider_module._DRAIN_SCRIPT))
        backend._drain_script = script
        out = backend.drain_tracked(REG, set())
        assert script.call_count == 3
        assert out == set(members)
        assert _cache_keys(client, "self") == set()
        assert not client.exists(f"t:self:{REG}")

    def test_stragglers_unlinked_in_batches(self, client: redis.Redis, monkeypatch: pytest.MonkeyPatch) -> None:
        """25 000 untracked local keys: three UNLINK commands, all gone, all returned."""
        backend = PerRequestRedisBackend(client, "self")
        local = {f"s{i}" for i in range(25_000)}
        client.mset({f"t:self:{k}": b"v" for k in local})
        unlink = MagicMock(wraps=client.unlink)
        monkeypatch.setattr(client, "unlink", unlink)

        assert backend.drain_tracked(REG, local) == local
        assert unlink.call_count == 3
        assert _cache_keys(client, "self") == set()

    def test_straggler_batch_failure_propagates(self, client: redis.Redis, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = PerRequestRedisBackend(client, "self")
        local = {f"s{i}" for i in range(25_000)}
        real_unlink = client.unlink
        calls = 0

        def flaky_unlink(*keys: str) -> int:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise redis.ConnectionError("lost")
            return real_unlink(*keys)

        monkeypatch.setattr(client, "unlink", flaky_unlink)
        with pytest.raises(BackendError):
            backend.drain_tracked(REG, local)
        assert calls == 2

    def test_drain_skips_undecodable_members(self, client: redis.Redis, caplog: pytest.LogCaptureFixture) -> None:
        client.sadd(f"t:self:{REG}", b"\xff\xfe", b"\xc3\x28", "good")
        client.set("t:self:good", b"v")
        with caplog.at_level(logging.WARNING, logger=provider_module.__name__):
            out = PerRequestRedisBackend(client, "self").drain_tracked(REG, set())
        assert out == {"good"}
        warnings = [r for r in caplog.records if "undecodable" in r.getMessage()]
        assert len(warnings) == 1
        assert "unlinked 2 undecodable members" in warnings[0].getMessage()
        assert "\\xff" not in caplog.text and REG not in caplog.text
        assert not client.exists(f"t:self:{REG}")

    def test_drain_logs_count_at_info(self, client: redis.Redis, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger=provider_module.__name__):
            PerRequestRedisBackend(client, "self").drain_tracked(REG, set())
        assert "Key registry drained 0 keys" in caplog.text
        assert REG not in caplog.text  # registry id is redacted

    def test_round_guard_stops_and_returns_popped(
        self, client: redis.Redis, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(provider_module, "_DRAIN_CHUNK", 2)
        monkeypatch.setattr(provider_module, "_DRAIN_MAX_ROUNDS", 2)
        client.sadd(f"t:self:{REG}", *[f"k{i}" for i in range(6)])
        with caplog.at_level(logging.WARNING, logger=provider_module.__name__):
            out = PerRequestRedisBackend(client, "self").drain_tracked(REG, {"local"})
        assert len(out) == 5 and "local" in out  # 2 rounds x 2 members + the local key
        assert client.scard(f"t:self:{REG}") == 2  # the rest waits for the next drain
        assert "stopped after 2 rounds" in caplog.text


class TestDecoratorOnRedis:
    def test_distributed_invalidation_golden_path(self, client: redis.Redis) -> None:
        """Write via wrapper A, invalidate via wrapper B that never wrote → L2 and set gone."""
        a = worker.cached_lookup(client)
        b = worker.cached_lookup(client)
        for x in range(5):
            a(x)
        assert len(_cache_keys(client, "default")) == 5

        b.invalidate_cache()
        assert _cache_keys(client, "default") == set()
        assert _registry_sets(client) == []

    def test_cross_process_invalidation(self, client: redis.Redis) -> None:
        """Process A writes keys and exits; this process's no-args invalidate_cache() deletes
        them from Redis and the tracking set is gone."""
        conn = client.connection_pool.connection_kwargs
        db = conn.get("db", 0)
        url = f"unix://{conn['path']}?db={db}" if "path" in conn else f"redis://{conn['host']}:{conn['port']}/{db}"
        repo_root = Path(__file__).resolve().parents[2]
        proc = subprocess.run(  # noqa: S603 - trusted: sys.executable + literal code
            [sys.executable, "-c", "from tests.integration._key_registry_worker import write; write([1, 2, 3])"],
            cwd=repo_root,
            env={**os.environ, "CK_TEST_REDIS_URL": url},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert len(_cache_keys(client, "default")) == 3
        assert len(_registry_sets(client)) == 1

        worker.cached_lookup(client).invalidate_cache()
        assert _cache_keys(client, "default") == set()
        assert _registry_sets(client) == []

    @pytest.mark.asyncio
    async def test_async_cross_wrapper_invalidation(self, client: redis.Redis) -> None:
        backend = PerRequestRedisBackend(client, "default")

        async def alookup(x: int) -> int:
            return x

        a = cache(backend=backend, ttl=300, namespace="key_registry_async")(alookup)
        b = cache(backend=PerRequestRedisBackend(client, "default"), ttl=300, namespace="key_registry_async")(alookup)
        for x in range(3):
            await a(x)
        await b.ainvalidate_cache()
        assert _cache_keys(client, "default") == set()
        assert _registry_sets(client) == []

    def test_untracked_key_removed_by_next_drain(self, client: redis.Redis) -> None:
        """The tracking set lost the key (a failed SADD, a lapsed
        set); the process that wrote it still deletes it from L2 and L1 on its next drain."""
        calls = 0

        def f(x: int) -> int:
            nonlocal calls
            calls += 1
            return x

        fn = cache(backend=PerRequestRedisBackend(client, "default"), ttl=300, namespace="key_registry_r7")(f)
        fn(1)
        for reg in _registry_sets(client):
            client.delete(reg)  # the set never saw the key

        fn.invalidate_cache()
        assert _cache_keys(client, "default") == set()
        fn(1)
        assert calls == 2  # evicted from L1 too

    def test_straggler_failure_falls_back_to_local(self, client: redis.Redis, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed straggler batch propagates out of drain_tracked; the wrapper then runs the
        local per-key loop, which still deletes this process's keys."""
        monkeypatch.setattr(provider_module, "_DRAIN_CHUNK", 2)
        fn = cache(backend=PerRequestRedisBackend(client, "default"), ttl=300, namespace="key_registry_fallback")(worker.lookup)
        for x in range(5):
            fn(x)
        for reg in _registry_sets(client):
            client.delete(reg)  # every key is a straggler: 3 batches of <= 2

        real_unlink = client.unlink
        calls = 0

        def flaky_unlink(*keys: str) -> int:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise redis.ConnectionError("lost")
            return real_unlink(*keys)

        monkeypatch.setattr(client, "unlink", flaky_unlink)
        fn.invalidate_cache()
        assert calls == 2
        assert _cache_keys(client, "default") == set()

    def test_writes_during_drain_are_never_orphaned(self, client: redis.Redis, monkeypatch: pytest.MonkeyPatch) -> None:
        """With set-then-track ordering, every key left in L2 after concurrent
        writes and a multi-round drain is still in the tracking set for the next drain."""
        monkeypatch.setattr(provider_module, "_DRAIN_CHUNK", 25)
        writer_fn = cache(backend=PerRequestRedisBackend(client, "default"), ttl=300, namespace="key_registry_race")(
            worker.lookup
        )
        drainer = cache(backend=PerRequestRedisBackend(client, "default"), ttl=300, namespace="key_registry_race")(worker.lookup)
        for x in range(200):
            writer_fn(x)

        stop = threading.Event()

        def write_more() -> None:
            x = 1_000
            while not stop.is_set() and x < 3_000:
                writer_fn(x)
                x += 1

        t = threading.Thread(target=write_more)
        t.start()
        for _ in range(5):
            drainer.invalidate_cache()
        stop.set()
        t.join()

        (reg,) = _registry_sets(client) or [None]
        tracked = {b"t:default:" + m for m in client.smembers(reg)} if reg else set()
        assert _cache_keys(client, "default") <= tracked

    def test_multi_tenant_drain_is_tenant_scoped(self, client: redis.Redis) -> None:
        """Tenant is pinned at the wrapper's first call (pre-existing), so each tenant's
        wrapper sets tenant_context before its first call."""

        def f(x: int) -> int:
            return x

        token = tenant_context.set("tenant-a")
        try:
            a = cache(ttl=300, namespace="key_registry_tenants")(f)
            a(1)
            a(2)
        finally:
            tenant_context.reset(token)
        token = tenant_context.set("tenant-b")
        try:
            b = cache(ttl=300, namespace="key_registry_tenants")(f)
            b(3)  # not b(1): L1 is tenant-blind (pre-existing), so b(1) would hit a's L1 entry
        finally:
            tenant_context.reset(token)

        a.invalidate_cache()
        assert _cache_keys(client, "tenant-a") == set()
        assert not client.keys("t:tenant-a:ck:reg:*")
        assert len(_cache_keys(client, "tenant-b")) == 1
        assert len(client.keys("t:tenant-b:ck:reg:*")) == 1

    def test_direct_redis_backend_keeps_process_local_semantics(self, client: redis.Redis) -> None:
        """Direct RedisBackend is not a KeyTrackableBackend: no tracking set, and a wrapper
        only invalidates the keys it knows."""
        from cachekit.backends.redis.backend import RedisBackend
        from tests.fixtures.backend_providers import TestCacheClientProvider

        backend = RedisBackend(client_provider=TestCacheClientProvider(sync_client=client))
        a = cache(backend=backend, ttl=300, namespace="key_registry_direct")(worker.lookup)
        b = cache(backend=backend, ttl=300, namespace="key_registry_direct")(worker.lookup)
        a(1)
        b(2)
        assert _registry_sets(client) == []

        b.invalidate_cache()
        assert len(client.keys("*key_registry_direct*")) == 1  # a's key survives: b never saw it
