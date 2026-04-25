"""Unit tests for ObjectCache — TTL, LRU eviction, stats, and thread safety.

All tests are isolated; no Redis or external services required.
"""

from __future__ import annotations

import threading
import types

import pytest

from cachekit.object_cache import ObjectCache


@pytest.mark.unit
class TestObjectCacheBasic:
    """Fundamental get/put/delete/clear behaviour."""

    def test_get_miss_empty(self) -> None:
        oc = ObjectCache()
        found, value = oc.get("missing")
        assert found is False
        assert value is None

    def test_put_then_get_hit(self) -> None:
        oc = ObjectCache()
        oc.put("k", "hello", ttl=60)
        found, value = oc.get("k")
        assert found is True
        assert value == "hello"

    def test_delete_existing(self) -> None:
        oc = ObjectCache()
        oc.put("k", 42, ttl=60)
        removed = oc.delete("k")
        assert removed is True
        found, _ = oc.get("k")
        assert found is False

    def test_delete_nonexistent(self) -> None:
        oc = ObjectCache()
        removed = oc.delete("ghost")
        assert removed is False

    def test_clear(self) -> None:
        oc = ObjectCache()
        oc.put("a", 1, ttl=60)
        oc.put("b", 2, ttl=60)
        oc.clear()
        assert oc.size == 0
        found, _ = oc.get("a")
        assert found is False


@pytest.mark.unit
class TestObjectCacheTTL:
    """TTL expiry behaviour — time is monkeypatched, never slept."""

    def test_expired_entry_returns_miss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Entry past its TTL must be treated as a miss and removed."""
        fake_time = types.SimpleNamespace(monotonic=lambda: 1000.0)
        monkeypatch.setattr("cachekit.object_cache.time", fake_time)

        oc = ObjectCache()
        oc.put("k", "value", ttl=10)  # expires_at = 1010.0

        # Advance past expiry
        fake_time.monotonic = lambda: 1011.0
        found, value = oc.get("k")

        assert found is False
        assert value is None
        assert oc.size == 0  # lazy removal happened

    def test_put_evicts_expired_before_lru(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When full, expired entries are evicted before the LRU fresh entry."""
        fake_time = types.SimpleNamespace(monotonic=lambda: 1000.0)
        monkeypatch.setattr("cachekit.object_cache.time", fake_time)

        oc = ObjectCache(max_entries=3)
        oc.put("a", "A", ttl=10)  # expires_at = 1010.0 (will expire)
        oc.put("b", "B", ttl=10)  # expires_at = 1010.0 (will expire)
        oc.put("c", "C", ttl=100)  # expires_at = 1100.0 (fresh)

        # Advance time so "a" and "b" have expired
        fake_time.monotonic = lambda: 1015.0

        # Insert "d" — cache is full; expired entries should be swept first
        oc.put("d", "D", ttl=100)

        # "c" (oldest fresh, LRU) must still be present — expired were swept first
        found_c, _ = oc.get("c")
        assert found_c is True

        # "d" must be present
        found_d, _ = oc.get("d")
        assert found_d is True

        # "a" and "b" are gone (expired, swept)
        found_a, _ = oc.get("a")
        found_b, _ = oc.get("b")
        assert found_a is False
        assert found_b is False


@pytest.mark.unit
class TestObjectCacheLRU:
    """LRU eviction ordering when the cache is at capacity."""

    def test_lru_eviction_order(self) -> None:
        """The oldest entry (first inserted, never accessed since) is evicted first."""
        oc = ObjectCache(max_entries=3)
        oc.put("first", 1, ttl=600)
        oc.put("second", 2, ttl=600)
        oc.put("third", 3, ttl=600)

        # Insert a fourth entry — "first" is the LRU and must be evicted
        oc.put("fourth", 4, ttl=600)

        found_first, _ = oc.get("first")
        assert found_first is False

        found_fourth, val = oc.get("fourth")
        assert found_fourth is True
        assert val == 4

    def test_get_refreshes_lru_order(self) -> None:
        """A get() call moves the entry to MRU; the actual oldest is evicted instead."""
        oc = ObjectCache(max_entries=3)
        oc.put("a", "A", ttl=600)
        oc.put("b", "B", ttl=600)
        oc.put("c", "C", ttl=600)

        # "a" was inserted first, but we access it now — making it MRU
        oc.get("a")

        # "b" is now the LRU; inserting a new entry should evict it
        oc.put("d", "D", ttl=600)

        found_b, _ = oc.get("b")
        assert found_b is False

        found_a, _ = oc.get("a")
        assert found_a is True

    def test_max_entries_1(self) -> None:
        """A cache with max_entries=1 only ever holds one entry."""
        oc = ObjectCache(max_entries=1)
        oc.put("x", 10, ttl=600)
        oc.put("y", 20, ttl=600)

        found_x, _ = oc.get("x")
        found_y, val_y = oc.get("y")

        assert found_x is False
        assert found_y is True
        assert val_y == 20
        assert oc.size == 1


@pytest.mark.unit
class TestObjectCacheStats:
    """Hit/miss counters and the size property."""

    def test_hit_miss_counters(self) -> None:
        """Hits and misses are correctly tracked across a mixed workload."""
        oc = ObjectCache()
        oc.put("a", 1, ttl=60)
        oc.put("b", 2, ttl=60)

        oc.get("a")  # hit
        oc.get("b")  # hit
        oc.get("c")  # miss
        oc.get("d")  # miss
        oc.get("a")  # hit

        assert oc.hits == 3
        assert oc.misses == 2

    def test_size_property(self) -> None:
        """size reflects the actual number of live entries."""
        oc = ObjectCache()
        assert oc.size == 0

        oc.put("a", 1, ttl=60)
        assert oc.size == 1

        oc.put("b", 2, ttl=60)
        assert oc.size == 2

        oc.delete("a")
        assert oc.size == 1

        oc.clear()
        assert oc.size == 0


@pytest.mark.unit
class TestObjectCacheThreadSafety:
    """Concurrent access must not raise exceptions and stats must be consistent."""

    def test_concurrent_put_get(self) -> None:
        """10 threads × 100 put+get pairs — no exceptions, consistent stats."""
        oc = ObjectCache(max_entries=50)
        errors: list[Exception] = []
        total_gets = 0
        lock = threading.Lock()

        def worker(thread_id: int) -> None:
            nonlocal total_gets
            local_gets = 0
            try:
                for i in range(100):
                    key = f"t{thread_id}-{i}"
                    oc.put(key, i, ttl=60)
                    oc.get(key)
                    local_gets += 1
            except Exception as exc:
                with lock:
                    errors.append(exc)
            finally:
                with lock:
                    total_gets += local_gets

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Exceptions in worker threads: {errors}"

        # Every get either hit (entry still present) or missed (evicted under LRU)
        # but hits + misses must equal the total number of get() calls
        assert oc.hits + oc.misses == total_gets
