"""Unit tests for ObjectCache — TTL, LRU eviction, byte bounds, SWR, stats, and thread safety.

All tests are isolated; no Redis or external services required.
"""

from __future__ import annotations

import threading
import types

import pytest

from cachekit.object_cache import ObjectCache, _estimate_object_size


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

    @pytest.mark.parametrize("bad_ttl", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_ttl_raises(self, bad_ttl: float) -> None:
        """Non-finite TTL (NaN/inf) must be rejected, not stored as an immortal entry (#158)."""
        oc = ObjectCache()
        with pytest.raises(ValueError, match="finite"):
            oc.put("k", "value", ttl=bad_ttl)

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


@pytest.mark.unit
class TestObjectCacheByteBound:
    """max_size_bytes bounds estimated bytes, independent of entry count (#207)."""

    def test_requires_at_least_one_bound(self) -> None:
        with pytest.raises(ValueError, match="at least one bound"):
            ObjectCache(max_entries=None, max_size_bytes=None)

    def test_invalid_bounds_raise(self) -> None:
        with pytest.raises(ValueError, match="max_size_bytes"):
            ObjectCache(max_size_bytes=0)
        with pytest.raises(ValueError, match="swr_threshold_ratio"):
            ObjectCache(swr_threshold_ratio=0.0)
        with pytest.raises(ValueError, match="swr_threshold_ratio"):
            ObjectCache(swr_threshold_ratio=1.5)

    def test_byte_pressure_evicts_lru(self) -> None:
        """Third same-sized value over a ~2.5x budget evicts the LRU entry."""
        value = "x" * 1000
        budget = int(_estimate_object_size(value) * 2.5)
        oc = ObjectCache(max_entries=None, max_size_bytes=budget)

        oc.put("a", value, ttl=60)
        oc.put("b", value, ttl=60)
        assert oc.size == 2

        oc.put("c", value, ttl=60)  # over budget -> "a" (LRU) evicted

        assert oc.get("a")[0] is False
        assert oc.get("b")[0] is True
        assert oc.get("c")[0] is True
        assert oc.size_bytes <= budget

    def test_oversized_value_declined_and_stale_entry_dropped(self) -> None:
        """A value bigger than the whole budget is never stored; a smaller stale
        entry under the same key is dropped so it stops being served."""
        small = "x" * 100
        budget = _estimate_object_size(small) * 3
        oc = ObjectCache(max_entries=None, max_size_bytes=budget)

        oc.put("k", small, ttl=60)
        assert oc.get("k")[0] is True

        oc.put("k", "x" * 100_000, ttl=60)  # far over budget -> declined
        assert oc.get("k")[0] is False  # stale small value no longer served
        assert oc.size == 0
        assert oc.size_bytes == 0

    def test_replacing_entry_updates_byte_accounting(self) -> None:
        value = "x" * 1000
        oc = ObjectCache(max_entries=None, max_size_bytes=_estimate_object_size(value) * 10)

        oc.put("k", value, ttl=60)
        first_bytes = oc.size_bytes
        oc.put("k", value, ttl=60)  # replace with same-sized value
        assert oc.size_bytes == first_bytes
        assert oc.size == 1

    def test_estimator_counts_container_contents(self) -> None:
        """A list of large strings must weigh (roughly) its contents, not pointer size."""
        big_list = ["x" * 10_000 for _ in range(10)]
        assert _estimate_object_size(big_list) > 10 * 10_000

    def test_estimator_handles_cycles(self) -> None:
        cyclic: list[object] = []
        cyclic.append(cyclic)
        assert _estimate_object_size(cyclic) > 0  # terminates


@pytest.mark.unit
class TestObjectCacheSWR:
    """Stale-while-revalidate: threshold flagging, refresh completion, anti-resurrection.

    Time is monkeypatched. Elapsed times are chosen with margin around the ±10%
    jitter window (threshold in [0.9, 1.1] * ttl * ratio) so tests stay deterministic.
    """

    @staticmethod
    def _fake_clock(monkeypatch: pytest.MonkeyPatch, start: float = 1000.0) -> types.SimpleNamespace:
        fake_time = types.SimpleNamespace(monotonic=lambda: start)
        monkeypatch.setattr("cachekit.object_cache.time", fake_time)
        return fake_time

    def test_fresh_entry_no_refresh_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5)
        oc.put("k", "v1", ttl=10)

        fake.monotonic = lambda: 1004.0  # elapsed 4.0 < 4.5 (min jittered threshold)
        hit, value, needs_refresh, _ = oc.get_with_swr("k", ttl=10)

        assert hit is True
        assert value == "v1"
        assert needs_refresh is False

    def test_stale_entry_flags_refresh_exactly_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5)
        oc.put("k", "v1", ttl=10)

        fake.monotonic = lambda: 1006.0  # elapsed 6.0 > 5.5 (max jittered threshold)
        hit, value, needs_refresh, _ = oc.get_with_swr("k", ttl=10)
        assert hit is True
        assert value == "v1"
        assert needs_refresh is True

        # Concurrent readers must not be told to refresh again while one is in flight
        hit2, _, needs_refresh2, _ = oc.get_with_swr("k", ttl=10)
        assert hit2 is True
        assert needs_refresh2 is False

    def test_hard_expired_entry_is_miss_not_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache()
        oc.put("k", "v1", ttl=10)

        fake.monotonic = lambda: 1011.0  # past hard expiry
        hit, value, needs_refresh, _ = oc.get_with_swr("k", ttl=10)

        assert hit is False
        assert value is None
        assert needs_refresh is False
        assert oc.size == 0

    def test_complete_refresh_updates_value_and_extends_expiry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """L1-only has no L2 source of truth — a refresh restarts the TTL clock."""
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5)
        oc.put("k", "v1", ttl=10)  # expires at 1010

        fake.monotonic = lambda: 1006.0
        hit, _, needs_refresh, version = oc.get_with_swr("k", ttl=10)
        assert hit and needs_refresh

        assert oc.complete_refresh("k", version, "v2", ttl=10) is True  # now expires at 1016

        fake.monotonic = lambda: 1012.0  # past the ORIGINAL expiry, inside the extended one
        hit, value = oc.get("k")
        assert hit is True
        assert value == "v2"

    def test_complete_refresh_after_delete_does_not_resurrect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A refresh landing after invalidation must not bring stale data back (#207)."""
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5)
        oc.put("k", "v1", ttl=10)

        fake.monotonic = lambda: 1006.0
        _, _, needs_refresh, version = oc.get_with_swr("k", ttl=10)
        assert needs_refresh

        oc.delete("k")  # invalidated while the refresh is "in flight"

        assert oc.complete_refresh("k", version, "v2", ttl=10) is False
        assert oc.get("k")[0] is False

    def test_complete_refresh_after_clear_does_not_resurrect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5)
        oc.put("k", "v1", ttl=10)

        fake.monotonic = lambda: 1006.0
        _, _, needs_refresh, version = oc.get_with_swr("k", ttl=10)
        assert needs_refresh

        oc.clear()

        assert oc.complete_refresh("k", version, "v2", ttl=10) is False
        assert oc.get("k")[0] is False

    def test_cancel_refresh_allows_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """After a failed refresh is cancelled, the next stale hit flags again."""
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5)
        oc.put("k", "v1", ttl=10)

        fake.monotonic = lambda: 1006.0
        _, _, needs_refresh, _ = oc.get_with_swr("k", ttl=10)
        assert needs_refresh

        oc.cancel_refresh("k")

        _, _, needs_refresh_retry, _ = oc.get_with_swr("k", ttl=10)
        assert needs_refresh_retry is True

    def test_oversized_refresh_result_drops_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the refreshed value no longer fits the byte budget, the stale entry
        is dropped rather than served forever."""
        fake = self._fake_clock(monkeypatch)
        small = "x" * 100
        oc = ObjectCache(
            max_entries=None,
            max_size_bytes=_estimate_object_size(small) * 3,
            swr_threshold_ratio=0.5,
        )
        oc.put("k", small, ttl=10)

        fake.monotonic = lambda: 1006.0
        _, _, needs_refresh, version = oc.get_with_swr("k", ttl=10)
        assert needs_refresh

        assert oc.complete_refresh("k", version, "x" * 100_000, ttl=10) is False
        assert oc.get("k")[0] is False
        assert oc.size_bytes == 0
