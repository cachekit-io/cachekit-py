"""CRITICAL PATH TEST: @cache.local() functionality.

Tests that @cache.local() caches object references in-process with TTL,
thread safety, and the standard wrapper API (invalidate_cache, cache_clear, cache_info).

No Redis required — pure in-process object cache.
"""

from __future__ import annotations

import threading

import pytest

from cachekit import cache


@pytest.mark.critical
class TestLocalCacheBasic:
    def test_basic_cache_hit(self):
        """Second call returns same object reference."""
        call_count = 0

        @cache.local(ttl=60)
        def compute():
            nonlocal call_count
            call_count += 1
            return {"result": call_count}

        a = compute()
        b = compute()
        assert a is b  # Same reference
        assert call_count == 1  # Called only once

    def test_cache_miss_calls_function(self):
        """Different args produce different cache entries."""

        @cache.local(ttl=60)
        def add(x, y):
            return x + y

        assert add(1, 2) == 3
        assert add(3, 4) == 7

    def test_ttl_expiry(self, monkeypatch):
        """Entry expires after TTL."""
        import cachekit.object_cache as oc_mod

        current_time = [100.0]
        monkeypatch.setattr(oc_mod.time, "monotonic", lambda: current_time[0])

        @cache.local(ttl=10)
        def value():
            return object()  # unique each call

        a = value()
        current_time[0] = 111.0  # Advance past TTL
        b = value()
        assert a is not b  # Different objects — TTL expired

    def test_invalidate_cache(self):
        """Per-key invalidation removes entry."""
        call_count = 0

        @cache.local(ttl=60)
        def greet(name):
            nonlocal call_count
            call_count += 1
            return f"hello {name} #{call_count}"

        result1 = greet("alice")
        assert call_count == 1
        greet.invalidate_cache("alice")
        result2 = greet("alice")
        assert call_count == 2
        assert result1 != result2  # Re-executed after invalidation

    def test_cache_clear(self):
        """cache_clear removes all entries."""

        @cache.local(ttl=60)
        def get_val(x):
            return object()

        a = get_val(1)
        b = get_val(2)
        get_val.cache_clear()
        c = get_val(1)
        assert a is not c  # Re-executed after clear
        # b assigned but not directly tested — this avoids a linter warning
        assert b is not None

    def test_cache_info(self):
        """cache_info returns correct CacheInfo with all 9 fields."""

        @cache.local(ttl=60, max_entries=100)
        def compute(x):
            return x * 2

        compute(1)  # miss
        compute(1)  # hit
        compute(2)  # miss

        info = compute.cache_info()
        assert info.hits == 1
        assert info.misses == 2
        assert info.l1_hits == 1  # All hits are L1
        assert info.l2_hits == 0  # No L2
        assert info.maxsize == 100
        assert info.currsize == 2
        assert info.l2_avg_latency_ms == 0.0

    def test_identity_semantics(self):
        """Same args return the exact same object (identity, not equality)."""

        @cache.local(ttl=60)
        def make_list(n):
            return [n]

        a = make_list(5)
        b = make_list(5)
        assert a is b

    def test_opaque_object_cached(self):
        """Can cache non-serializable objects."""

        class OpaqueClient:
            def __init__(self, url):
                self.url = url
                self._socket = object()  # Simulates a socket

        @cache.local(ttl=60)
        def get_client(url):
            return OpaqueClient(url)

        client = get_client("https://example.com")
        assert isinstance(client, OpaqueClient)
        assert get_client("https://example.com") is client


@pytest.mark.critical
class TestLocalCacheAsync:
    async def test_async_function(self):
        """Async functions cache the awaited result, not the coroutine."""
        call_count = 0

        @cache.local(ttl=60)
        async def async_compute(x):
            nonlocal call_count
            call_count += 1
            return x * 2

        result1 = await async_compute(5)
        result2 = await async_compute(5)
        assert result1 == 10
        assert result2 == 10
        assert call_count == 1  # Only called once


@pytest.mark.critical
class TestLocalCacheThreadSafety:
    def test_thread_safety(self):
        """Concurrent access from multiple threads doesn't corrupt state."""

        @cache.local(ttl=60, max_entries=50)
        def compute(x):
            return x * 2

        errors: list[Exception] = []

        def worker(start, count):
            try:
                for i in range(start, start + count):
                    result = compute(i % 20)  # Reuse keys to trigger hits
                    assert result == (i % 20) * 2
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i * 100, 100)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        info = compute.cache_info()
        assert info.hits + info.misses > 0
