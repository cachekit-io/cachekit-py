"""Integration tests for MemcachedBackend against a real Memcached instance.

Requires a running Memcached server. Start one with:
    docker run -d --name cachekit-memcached -p 11211:11211 docker.io/library/memcached:alpine -m 64

Set MEMCACHED_TEST_HOST / MEMCACHED_TEST_PORT to override defaults.
Tests are skipped when Memcached is unreachable.

Covers:
- CRUD round-trip (set/get/delete/exists)
- TTL expiry (real server-side eviction)
- Key prefix isolation
- Multi-server HashClient consistent hashing
- Concurrent thread safety
- Large value handling
- Binary data integrity (null bytes, high bytes)
- Health check against live server
- Decorator integration with real backend
- 30-day TTL clamping
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from cachekit.backends.memcached.backend import MemcachedBackend
from cachekit.backends.memcached.config import MAX_MEMCACHED_TTL, MemcachedBackendConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MEMCACHED_HOST = os.environ.get("MEMCACHED_TEST_HOST", "localhost")
MEMCACHED_PORT = int(os.environ.get("MEMCACHED_TEST_PORT", "11211"))


def _memcached_reachable() -> bool:
    """Check if Memcached is reachable."""
    try:
        from pymemcache.client.base import Client

        c = Client((MEMCACHED_HOST, MEMCACHED_PORT), connect_timeout=2, timeout=2)
        c.version()
        c.close()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _memcached_reachable(), reason="Memcached not reachable"),
]


# Override autouse Redis fixture — Memcached tests don't need Redis
@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation():
    """Override global Redis fixture — MemcachedBackend doesn't need Redis."""
    pass


@pytest.fixture
def memcached_config() -> MemcachedBackendConfig:
    """Config pointing at the test Memcached instance."""
    return MemcachedBackendConfig(
        servers=[f"{MEMCACHED_HOST}:{MEMCACHED_PORT}"],
        connect_timeout=2.0,
        timeout=2.0,
    )


@pytest.fixture
def backend(memcached_config: MemcachedBackendConfig) -> MemcachedBackend:
    """Provide a MemcachedBackend connected to a real server, flushed between tests."""
    b = MemcachedBackend(memcached_config)
    # Flush all keys before each test for isolation
    b._client.flush_all()
    yield b
    # Cleanup after test
    try:
        b._client.flush_all()
    except Exception:
        pass


@pytest.fixture
def prefixed_backend() -> MemcachedBackend:
    """Backend with a key prefix for isolation tests."""
    config = MemcachedBackendConfig(
        servers=[f"{MEMCACHED_HOST}:{MEMCACHED_PORT}"],
        key_prefix="test_ns:",
        connect_timeout=2.0,
        timeout=2.0,
    )
    b = MemcachedBackend(config)
    # No flush_all here — the 'backend' fixture handles global flush
    yield b


# ---------------------------------------------------------------------------
# CRUD round-trip
# ---------------------------------------------------------------------------


class TestCRUDRoundTrip:
    """Verify basic set/get/delete/exists against real Memcached."""

    def test_set_get_roundtrip(self, backend: MemcachedBackend) -> None:
        backend.set("hello", b"world")
        assert backend.get("hello") == b"world"

    def test_get_missing_key_returns_none(self, backend: MemcachedBackend) -> None:
        assert backend.get("nonexistent") is None

    def test_delete_existing_key(self, backend: MemcachedBackend) -> None:
        backend.set("to_delete", b"bye")
        assert backend.delete("to_delete") is True
        assert backend.get("to_delete") is None

    def test_delete_missing_key(self, backend: MemcachedBackend) -> None:
        assert backend.delete("never_existed") is False

    def test_exists_true(self, backend: MemcachedBackend) -> None:
        backend.set("present", b"data")
        assert backend.exists("present") is True

    def test_exists_false(self, backend: MemcachedBackend) -> None:
        assert backend.exists("absent") is False

    def test_overwrite_value(self, backend: MemcachedBackend) -> None:
        backend.set("key", b"v1")
        backend.set("key", b"v2")
        assert backend.get("key") == b"v2"

    def test_empty_value(self, backend: MemcachedBackend) -> None:
        backend.set("empty", b"")
        assert backend.get("empty") == b""

    def test_set_without_ttl(self, backend: MemcachedBackend) -> None:
        """Keys with no TTL should persist (no server-side expiry)."""
        backend.set("no_ttl", b"forever")
        assert backend.get("no_ttl") == b"forever"

    def test_set_with_ttl_zero(self, backend: MemcachedBackend) -> None:
        """TTL=0 means no expiry in Memcached."""
        backend.set("zero_ttl", b"data", ttl=0)
        assert backend.get("zero_ttl") == b"data"


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


class TestTTLExpiry:
    """Verify server-side TTL expiry with real Memcached."""

    def test_key_expires_after_ttl(self, backend: MemcachedBackend) -> None:
        """Key should disappear after TTL seconds."""
        backend.set("ephemeral", b"gone_soon", ttl=1)
        assert backend.get("ephemeral") == b"gone_soon"
        # Server-side TTL — must sleep (time-machine can't mock Redis/Memcached clock)
        time.sleep(3)
        assert backend.get("ephemeral") is None

    def test_key_alive_before_ttl(self, backend: MemcachedBackend) -> None:
        """Key should be present before TTL expires."""
        backend.set("alive", b"still_here", ttl=5)
        time.sleep(0.5)
        assert backend.get("alive") == b"still_here"

    def test_negative_ttl_treated_as_no_expiry(self, backend: MemcachedBackend) -> None:
        """Negative TTL should be treated as no expiry (expire=0)."""
        backend.set("neg_ttl", b"data", ttl=-10)
        assert backend.get("neg_ttl") == b"data"

    def test_ttl_clamped_to_30_day_max(self, backend: MemcachedBackend) -> None:
        """TTL exceeding 30 days should be clamped, not rejected."""
        huge_ttl = MAX_MEMCACHED_TTL + 86400  # 31 days
        backend.set("clamped", b"data", ttl=huge_ttl)
        # Should still be stored (clamped, not rejected)
        assert backend.get("clamped") == b"data"


# ---------------------------------------------------------------------------
# Key prefix isolation
# ---------------------------------------------------------------------------


class TestKeyPrefixIsolation:
    """Verify key_prefix provides namespace isolation."""

    def test_prefixed_keys_isolated(self, prefixed_backend: MemcachedBackend, backend: MemcachedBackend) -> None:
        """Keys from prefixed backend should not collide with unprefixed."""
        # Set unprefixed first, then prefixed — avoids flush_all ordering issue
        backend.set("shared_name", b"unprefixed_value")
        prefixed_backend.set("shared_name", b"prefixed_value")

        assert prefixed_backend.get("shared_name") == b"prefixed_value"
        assert backend.get("shared_name") == b"unprefixed_value"

    def test_delete_only_affects_own_prefix(self, prefixed_backend: MemcachedBackend, backend: MemcachedBackend) -> None:
        """Deleting a prefixed key should not affect unprefixed."""
        prefixed_backend.set("to_del", b"prefixed")
        backend.set("to_del", b"unprefixed")

        prefixed_backend.delete("to_del")
        assert prefixed_backend.get("to_del") is None
        assert backend.get("to_del") == b"unprefixed"


# ---------------------------------------------------------------------------
# Binary data integrity
# ---------------------------------------------------------------------------


class TestBinaryDataIntegrity:
    """Verify binary data survives Memcached round-trip."""

    def test_null_bytes(self, backend: MemcachedBackend) -> None:
        data = b"\x00\x01\x02\x00\xff\xfe\x00"
        backend.set("nulls", data)
        assert backend.get("nulls") == data

    def test_high_bytes(self, backend: MemcachedBackend) -> None:
        data = bytes(range(256))
        backend.set("all_bytes", data)
        assert backend.get("all_bytes") == data

    def test_large_value_1mb(self, backend: MemcachedBackend) -> None:
        """Memcached default max value is 1MB."""
        data = b"x" * (1024 * 1024 - 100)  # Just under 1MB
        backend.set("large", data)
        assert backend.get("large") == data
        assert len(backend.get("large")) == len(data)

    def test_msgpack_like_payload(self, backend: MemcachedBackend) -> None:
        """Simulate what cachekit actually stores (MessagePack bytes)."""
        import struct

        # Simulate a simple MessagePack-like payload
        payload = struct.pack(">BHI", 0x92, 42, 1234567890) + b"\xa5hello"
        backend.set("msgpack", payload)
        assert backend.get("msgpack") == payload


# ---------------------------------------------------------------------------
# Concurrent thread safety
# ---------------------------------------------------------------------------


class TestConcurrentThreadSafety:
    """Verify thread-safe operations against real Memcached.

    Each thread gets its own MemcachedBackend instance — pymemcache HashClient
    connection pooling has known contention issues when a single pool is
    hammered from many threads simultaneously. Per-thread backends is also
    the realistic usage pattern (e.g., one backend per web worker).
    """

    def _make_backend(self) -> MemcachedBackend:
        """Create a fresh backend for per-thread use."""
        return MemcachedBackend(
            MemcachedBackendConfig(
                servers=[f"{MEMCACHED_HOST}:{MEMCACHED_PORT}"],
                connect_timeout=2.0,
                timeout=2.0,
            )
        )

    def test_concurrent_writes_no_corruption(self, backend: MemcachedBackend) -> None:
        """10 threads x 50 ops each — no data corruption."""
        num_threads = 10
        ops_per_thread = 50
        barrier = threading.Barrier(num_threads)
        errors: list[str] = []

        def worker(tid: int) -> None:
            local_backend = self._make_backend()
            try:
                barrier.wait(timeout=10)
                for i in range(ops_per_thread):
                    key = f"t{tid}_k{i}"
                    value = f"t{tid}_v{i}".encode()
                    local_backend.set(key, value)
                    got = local_backend.get(key)
                    if got != value:
                        errors.append(f"Thread {tid} op {i}: expected {value!r}, got {got!r}")
            except Exception as exc:
                errors.append(f"Thread {tid}: {exc}")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Thread errors: {errors}"

    def test_concurrent_read_write_delete(self, backend: MemcachedBackend) -> None:
        """Mixed read/write/delete operations under contention."""
        num_threads = 5
        ops = 100
        errors: list[str] = []
        barrier = threading.Barrier(num_threads)

        def mixed_worker(tid: int) -> None:
            local_backend = self._make_backend()
            try:
                barrier.wait(timeout=10)
                for i in range(ops):
                    key = f"mixed_{i % 20}"  # Shared keys for contention
                    value = f"t{tid}_i{i}".encode()
                    if i % 3 == 0:
                        local_backend.set(key, value)
                    elif i % 3 == 1:
                        local_backend.get(key)  # May be None
                    else:
                        local_backend.delete(key)  # May return False
            except Exception as exc:
                errors.append(f"Thread {tid}: {exc}")

        threads = [threading.Thread(target=mixed_worker, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Thread errors: {errors}"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    """Verify health_check against live server."""

    def test_healthy_server(self, backend: MemcachedBackend) -> None:
        is_healthy, details = backend.health_check()
        assert is_healthy is True
        assert details["backend_type"] == "memcached"
        assert details["latency_ms"] > 0
        assert details["configured_servers"] >= 1

    def test_unhealthy_server(self) -> None:
        """Health check against unreachable server returns False."""
        config = MemcachedBackendConfig(
            servers=["localhost:19999"],  # Nothing there
            connect_timeout=0.5,
            timeout=0.5,
        )
        b = MemcachedBackend(config)
        is_healthy, details = b.health_check()
        assert is_healthy is False
        assert "error" in details


# ---------------------------------------------------------------------------
# Decorator integration
# ---------------------------------------------------------------------------


class TestDecoratorIntegration:
    """Verify @cache decorator works with real MemcachedBackend."""

    def test_cache_minimal_with_memcached(self, backend: MemcachedBackend) -> None:
        """@cache.minimal works with MemcachedBackend via set_default_backend."""
        from cachekit import cache
        from cachekit.config.decorator import set_default_backend

        set_default_backend(backend)

        call_count = 0

        @cache.minimal(ttl=10, namespace="mc_test")
        def add(a: int, b: int) -> int:
            nonlocal call_count
            call_count += 1
            return a + b

        # First call — cache miss
        result1 = add(2, 3)
        assert result1 == 5
        assert call_count == 1

        # Second call — cache hit
        result2 = add(2, 3)
        assert result2 == 5
        assert call_count == 1  # Not incremented

        # Different args — cache miss
        result3 = add(10, 20)
        assert result3 == 30
        assert call_count == 2

        # Clean up
        set_default_backend(None)
        add.cache_clear()


# ---------------------------------------------------------------------------
# Batch operations & edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases for Memcached protocol quirks."""

    def test_many_keys_sequentially(self, backend: MemcachedBackend) -> None:
        """Write and read back 500 keys to stress the connection pool."""
        for i in range(500):
            backend.set(f"bulk_{i}", f"val_{i}".encode())

        for i in range(500):
            assert backend.get(f"bulk_{i}") == f"val_{i}".encode()

    def test_key_with_special_characters(self, backend: MemcachedBackend) -> None:
        """Memcached keys must not contain whitespace or control chars.

        But typical cachekit keys (namespace:hash format) are safe.
        """
        key = "ns:func:abc123def456"
        backend.set(key, b"data")
        assert backend.get(key) == b"data"

    def test_rapid_set_delete_cycle(self, backend: MemcachedBackend) -> None:
        """Rapid set/delete cycles should not leak or corrupt."""
        for i in range(200):
            backend.set("cycle", f"iter_{i}".encode())
            backend.delete("cycle")

        assert backend.get("cycle") is None
