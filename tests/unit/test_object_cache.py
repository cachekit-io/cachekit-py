"""Unit tests for ObjectCache — TTL, LRU eviction, byte bounds, SWR, stats, and thread safety.

All tests are isolated; no Redis or external services required.
"""

from __future__ import annotations

import gc
import os
import signal
import threading
import time
import types
import weakref
from collections.abc import Callable
from typing import Any

import pytest

from cachekit import cache, object_cache
from cachekit.config import L1CacheConfig
from cachekit.object_cache import ObjectCache, _estimate_object_size
from tests.utils.fork_helpers import child_outcome, on_new_thread, report
from tests.utils.timing_helper import TimingHelper


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

    def test_complete_refresh_after_put_replacement_does_not_overwrite(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """put() replacing an entry mid-refresh invalidates the in-flight refresh.

        Regression: put() used a bare pop() that kept the old refresh valid, so
        an older in-flight refresh could overwrite the newer value.
        """
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5)
        oc.put("k", "v1", ttl=10)

        fake.monotonic = lambda: 1006.0
        _, _, needs_refresh, version = oc.get_with_swr("k", ttl=10)
        assert needs_refresh

        oc.put("k", "v2-newer", ttl=10)  # replaced while the refresh is "in flight"

        assert oc.complete_refresh("k", version, "v1-stale-refresh", ttl=10) is False
        hit, value = oc.get("k")
        assert hit is True
        assert value == "v2-newer"  # the newer value survived

    def test_put_replacement_clears_refreshing_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """After put() replaces mid-refresh, a later stale hit can flag a new refresh."""
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5)
        oc.put("k", "v1", ttl=10)

        fake.monotonic = lambda: 1006.0
        _, _, needs_refresh, _ = oc.get_with_swr("k", ttl=10)
        assert needs_refresh  # marker now set

        oc.put("k", "v2", ttl=10)  # replacement clears the in-flight marker

        fake.monotonic = lambda: 1012.0  # new entry (cached at 1006) is stale again
        _, _, needs_refresh_again, _ = oc.get_with_swr("k", ttl=10)
        assert needs_refresh_again is True

    def test_complete_refresh_after_delete_and_reput_does_not_overwrite(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """delete() + a fresh put() of the same key must still reject the old refresh."""
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5)
        oc.put("k", "v1", ttl=10)

        fake.monotonic = lambda: 1006.0
        _, _, needs_refresh, version = oc.get_with_swr("k", ttl=10)
        assert needs_refresh

        oc.delete("k")
        oc.put("k", "v2-new-entry", ttl=10)

        assert oc.complete_refresh("k", version, "v1-stale-refresh", ttl=10) is False
        assert oc.get("k")[1] == "v2-new-entry"

    def test_cancel_refresh_allows_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """After a failed refresh is cancelled, the next stale hit flags again."""
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5)
        oc.put("k", "v1", ttl=10)

        fake.monotonic = lambda: 1006.0
        _, _, needs_refresh, version = oc.get_with_swr("k", ttl=10)
        assert needs_refresh

        oc.cancel_refresh("k", version)

        _, _, needs_refresh_retry, _ = oc.get_with_swr("k", ttl=10)
        assert needs_refresh_retry is True

    def test_stale_refresh_cannot_clear_newer_refresh_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An old refresh finishing after a replacement's refresh started must not
        release the newer refresh's marker (that would allow duplicate concurrent
        refreshes racing last-write-wins) nor overwrite its result.
        """
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5)
        oc.put("k", "v1", ttl=10)

        fake.monotonic = lambda: 1006.0
        _, _, needs_refresh_a, version_a = oc.get_with_swr("k", ttl=10)
        assert needs_refresh_a  # refresh A in flight

        oc.put("k", "v2", ttl=10)  # replacement clears A's marker, new generation

        fake.monotonic = lambda: 1012.0  # replacement entry (cached at 1006) stale again
        _, _, needs_refresh_b, version_b = oc.get_with_swr("k", ttl=10)
        assert needs_refresh_b  # refresh B in flight
        assert version_b != version_a

        # A finishes late: must neither land nor release B's marker
        assert oc.complete_refresh("k", version_a, "vA-stale", ttl=10) is False
        _, _, needs_refresh_dup, _ = oc.get_with_swr("k", ttl=10)
        assert needs_refresh_dup is False, "stale refresh released the in-flight marker"

        # B still owns the cycle and lands normally
        assert oc.complete_refresh("k", version_b, "vB-new", ttl=10) is True
        assert oc.get("k") == (True, "vB-new")

    def test_stale_cancel_cannot_clear_newer_refresh_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed old refresh cancelling late must not release a newer refresh's marker."""
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5)
        oc.put("k", "v1", ttl=10)

        fake.monotonic = lambda: 1006.0
        _, _, needs_refresh_a, version_a = oc.get_with_swr("k", ttl=10)
        assert needs_refresh_a

        oc.put("k", "v2", ttl=10)

        fake.monotonic = lambda: 1012.0
        _, _, needs_refresh_b, _ = oc.get_with_swr("k", ttl=10)
        assert needs_refresh_b

        oc.cancel_refresh("k", version_a)  # A failed and cancels late

        _, _, needs_refresh_dup, _ = oc.get_with_swr("k", ttl=10)
        assert needs_refresh_dup is False, "stale cancel released the in-flight marker"

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


@pytest.mark.unit
class TestObjectCacheRefreshRetryBackoff:
    """A failed refresh is not retried until swr_retry_interval has passed.

    Clock is faked. With ttl=100 and ratio=0.5 the max jittered threshold is 55 s,
    so every read from t=1060 on is in the refresh band until hard expiry at t=1100.
    """

    @staticmethod
    def _fake_clock(monkeypatch: pytest.MonkeyPatch, start: float = 1000.0) -> types.SimpleNamespace:
        fake_time = types.SimpleNamespace(monotonic=lambda: start)
        monkeypatch.setattr("cachekit.object_cache.time", fake_time)
        return fake_time

    @staticmethod
    def _fail_once(oc: ObjectCache, key: str) -> None:
        _, _, needs_refresh, version = oc.get_with_swr(key, ttl=100)
        assert needs_refresh
        oc.fail_refresh(key, version)

    def test_no_refresh_flagged_inside_interval_and_value_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5, swr_retry_interval=20)
        oc.put("k", "held", ttl=100)

        fake.monotonic = lambda: 1060.0
        self._fail_once(oc, "k")

        for i in range(12):
            fake.monotonic = lambda i=i: 1060.0 + i  # up to 1071, all inside the 20 s back-off
            hit, value, needs_refresh, _ = oc.get_with_swr("k", ttl=100)
            assert (hit, value, needs_refresh) == (True, "held", False)

    def test_first_read_after_interval_flags_exactly_one_refresh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5, swr_retry_interval=20)
        oc.put("k", "held", ttl=100)

        fake.monotonic = lambda: 1060.0
        self._fail_once(oc, "k")

        fake.monotonic = lambda: 1080.0
        flags = [oc.get_with_swr("k", ttl=100)[2] for _ in range(5)]
        assert flags == [True, False, False, False, False]

    def test_success_clears_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5, swr_retry_interval=20)
        oc.put("k", "held", ttl=100)

        fake.monotonic = lambda: 1060.0
        self._fail_once(oc, "k")
        assert oc._state.store["k"].refresh_failed_at == 1060.0

        fake.monotonic = lambda: 1080.0
        _, _, needs_refresh, version = oc.get_with_swr("k", ttl=100)
        assert needs_refresh
        assert oc.complete_refresh("k", version, "new", ttl=100) is True
        assert oc._state.store["k"].refresh_failed_at is None

    def test_backoff_is_per_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5, swr_retry_interval=20)
        oc.put("a", 1, ttl=100)
        oc.put("b", 2, ttl=100)

        fake.monotonic = lambda: 1060.0
        self._fail_once(oc, "a")

        assert oc.get_with_swr("a", ttl=100)[2] is False
        assert oc.get_with_swr("b", ttl=100)[2] is True

    def test_invalidated_entry_takes_its_backoff_with_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5, swr_retry_interval=1000)
        oc.put("k", "held", ttl=100)
        fake.monotonic = lambda: 1060.0
        self._fail_once(oc, "k")

        assert oc.delete("k") is True
        oc.put("k", "recached", ttl=100)
        fake.monotonic = lambda: 1120.0  # stale again, still inside the old 1000 s back-off
        assert oc.get_with_swr("k", ttl=100)[2] is True

    def test_evicted_entry_takes_its_backoff_with_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(max_entries=1, swr_threshold_ratio=0.5, swr_retry_interval=1000)
        oc.put("k", "held", ttl=100)
        fake.monotonic = lambda: 1060.0
        self._fail_once(oc, "k")

        oc.put("other", "x", ttl=100)  # LRU-evicts k
        assert oc.get("k")[0] is False
        oc.put("k", "recached", ttl=100)
        fake.monotonic = lambda: 1120.0
        assert oc.get_with_swr("k", ttl=100)[2] is True

    def test_zero_interval_retries_on_next_stale_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5, swr_retry_interval=0)
        oc.put("k", "held", ttl=100)
        fake.monotonic = lambda: 1060.0
        self._fail_once(oc, "k")

        assert oc.get_with_swr("k", ttl=100)[2] is True

    def test_cancel_does_not_back_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A refresh that never ran (capacity, uncopyable args) made no upstream call."""
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5, swr_retry_interval=20)
        oc.put("k", "held", ttl=100)
        fake.monotonic = lambda: 1060.0
        _, _, needs_refresh, version = oc.get_with_swr("k", ttl=100)
        assert needs_refresh
        oc.cancel_refresh("k", version)

        assert oc.get_with_swr("k", ttl=100)[2] is True

    def test_stale_fail_cannot_back_off_newer_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5, swr_retry_interval=1000)
        oc.put("k", "v1", ttl=100)
        fake.monotonic = lambda: 1060.0
        _, _, _, old_version = oc.get_with_swr("k", ttl=100)

        oc.put("k", "v2", ttl=100)  # replaced while the old refresh ran
        oc.fail_refresh("k", old_version)
        assert oc._state.store["k"].refresh_failed_at is None

        fake.monotonic = lambda: 1120.0
        assert oc.get_with_swr("k", ttl=100)[2] is True

    def test_hard_expiry_still_misses_during_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(swr_threshold_ratio=0.5, swr_retry_interval=1000)
        oc.put("k", "held", ttl=100)
        fake.monotonic = lambda: 1060.0
        self._fail_once(oc, "k")

        fake.monotonic = lambda: 1100.0
        assert oc.get_with_swr("k", ttl=100) == (False, None, False, 0)

    @pytest.mark.parametrize("bad", [-1, -0.001, float("nan")])
    def test_rejects_negative_or_nan_interval(self, bad: float) -> None:
        with pytest.raises(ValueError, match="swr_retry_interval"):
            ObjectCache(swr_retry_interval=bad)


def _consistent(oc: ObjectCache) -> bool:
    """Whether the byte total and the in-flight refresh markers agree with the entries."""
    s = oc._state
    sized = s.size_bytes == sum(entry.size_bytes for entry in s.store.values())
    return sized and all(key in s.store and s.store[key].generation == g for key, g in s.refreshing.items())


@pytest.mark.unit
@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() not available on this platform")
class TestObjectCacheFork:
    """A forked child inherits each ObjectCache as its parent's threads left it at fork().

    Those threads do not survive the fork: a lock one of them held never comes free, and a refresh
    one of them was running never clears its in-flight marker. Each child reports over a pipe and
    os._exit()s, so it never returns into pytest.
    """

    @staticmethod
    def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
        fake = types.SimpleNamespace(monotonic=lambda: 1000.0)
        monkeypatch.setattr("cachekit.object_cache.time", fake)
        return fake

    @staticmethod
    def _decorate(make: Callable[[], Any], fn: Callable[[int], int]) -> tuple[Callable[[int], int], ObjectCache]:
        """fn decorated by make(), and the ObjectCache the decorator built for it, found in the hook's registry."""
        before = set(object_cache._caches)
        wrapped = make()(fn)
        (oc,) = set(object_cache._caches) - before
        return wrapped, oc

    @pytest.mark.parametrize(
        "make",
        [
            pytest.param(lambda: cache(ttl=60, backend=None, l1=L1CacheConfig(swr_enabled=False)), id="get"),
            pytest.param(lambda: cache(ttl=60, backend=None, l1=L1CacheConfig(swr_enabled=True)), id="get_with_swr"),
            pytest.param(lambda: cache.local(ttl=60), id="local"),
        ],
    )
    def test_forked_child_survives_a_lock_a_parent_thread_held(self, make) -> None:
        calls: list[int] = []

        def double(n: int) -> int:
            calls.append(n)
            return n * 2

        cached, oc = self._decorate(make, double)
        assert cached(1) == 2
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            with oc._state.lock:
                held.set()
                release.wait()

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert held.wait(5)
        r, w = os.pipe()
        try:
            child = os.fork()
            if child == 0:
                signal.alarm(5)  # without the repair the first call never returns: end the child instead
                try:
                    outcome: object = {"first": cached(1), "again": cached(1), "calls": len(calls)}
                except BaseException as e:
                    outcome = {"error": repr(e)}
                report(w, outcome)
        finally:
            release.set()
            holder.join(5)
        os.close(w)

        # The holder may have left the entries half-updated, so the child dropped them and recomputed once.
        assert child_outcome(child, r) == {"first": 2, "again": 2, "calls": 2}

    def test_forked_child_refreshes_a_key_whose_refresh_was_in_flight_at_fork(self, monkeypatch) -> None:
        fake = self._fake_clock(monkeypatch)
        gate = threading.Event()
        calls: list[int] = []
        parent_pid = os.getpid()

        @cache(ttl=100, backend=None, l1=L1CacheConfig(swr_threshold_ratio=0.5))
        def compute(x: int) -> int:
            calls.append(os.getpid())
            # Only the parent parks. The child must never touch the gate: the parent's refresh thread
            # can be forked while it holds the gate's lock on its way into wait().
            if len(calls) > 1 and os.getpid() == parent_pid:
                gate.wait(timeout=30)
            return x + 1

        r, w = os.pipe()
        try:
            assert compute(1) == 2  # miss -> store
            fake.monotonic = lambda: 1060.0  # past ttl * ratio, even with +10% jitter
            assert compute(1) == 2  # stale hit: the refresh thread parks with the key marked in flight
            TimingHelper.wait_for_condition(lambda: len(calls) == 2, message="the parent's refresh never started")
            child = os.fork()
            if child == 0:
                signal.alarm(5)
                try:
                    before = len(calls)
                    result = compute(1)  # stale hit on the key the parent was still refreshing
                    TimingHelper.wait_for_condition(lambda: len(calls) > before, message="the child never refreshed")
                    outcome: object = {"result": result, "refreshes": len(calls) - before}
                except BaseException as e:
                    outcome = {"error": repr(e)}
                report(w, outcome)
        finally:
            gate.set()  # release the parent's parked refresh
        os.close(w)

        assert child_outcome(child, r) == {"result": 2, "refreshes": 1}

    def test_forked_child_keeps_entries_and_restarts_refreshes(self, monkeypatch) -> None:
        """With the lock free at fork nothing was mid-update: entries stay, in-flight markers go."""
        fake = self._fake_clock(monkeypatch)
        oc = ObjectCache(max_entries=None, max_size_bytes=1 << 20, swr_threshold_ratio=0.5)
        oc.put("k", "v", ttl=100)
        fake.monotonic = lambda: 1060.0
        assert oc.get_with_swr("k", ttl=100)[2] is True  # a refresh is now in flight, never to finish
        r, w = os.pipe()
        child = os.fork()
        if child == 0:
            try:
                outcome: object = {
                    "consistent": _consistent(oc),
                    "read": oc.get_with_swr("k", ttl=100)[:3],
                    "new_thread": on_new_thread(lambda: oc.get("k")),
                }
            except BaseException as e:
                outcome = {"error": repr(e)}
            report(w, outcome)
        os.close(w)

        assert child_outcome(child, r) == {"consistent": True, "read": (True, "v", True), "new_thread": (True, "v")}
        assert oc.get_with_swr("k", ttl=100)[2] is False  # the parent's own marker is untouched

    def test_fork_inside_a_critical_section_lets_the_child_finish_it(self, monkeypatch) -> None:
        """A fork from a signal handler or finalizer inside get(): the child returns into it."""
        oc = ObjectCache(max_entries=None, max_size_bytes=1 << 20)
        oc.put("k", "v", ttl=60)
        parent, (r, w) = os.getpid(), os.pipe()
        child = 0

        def fork_here() -> float:  # get() reads the clock inside its critical section
            nonlocal child
            monkeypatch.setattr("cachekit.object_cache.time", time)
            child = os.fork()
            return time.monotonic()

        monkeypatch.setattr("cachekit.object_cache.time", types.SimpleNamespace(monotonic=fork_here))
        try:
            found = oc.get("k")
        except BaseException as e:
            if os.getpid() != parent:
                report(w, {"error": repr(e)})  # the repair changed the state under the in-flight get()
            raise
        if os.getpid() != parent:
            try:
                outcome: object = {
                    "found": found,
                    "consistent": _consistent(oc),
                    "new_thread": on_new_thread(lambda: (oc.put("n", "v", ttl=60), oc.get("n"))[1]),
                }
            except BaseException as e:
                outcome = {"error": repr(e)}
            report(w, outcome)
        os.close(w)

        assert child, "get() no longer reads the clock"
        assert child_outcome(child, r) == {"found": (True, "v"), "consistent": True, "new_thread": (True, "v")}

    def test_fork_inside_a_critical_section_the_child_never_leaves(self, monkeypatch) -> None:
        """multiprocessing's child runs its target, then os._exit()s without unwinding to the critical section."""
        oc = ObjectCache()
        oc.put("pre-fork", "v", ttl=60)
        r, w = os.pipe()
        child = 0

        def fork_here() -> float:
            nonlocal child
            monkeypatch.setattr("cachekit.object_cache.time", time)
            child = os.fork()
            if child == 0:
                try:  # this thread holds the old lock for good, so new threads must not need it
                    outcome: object = {
                        "pre_fork": on_new_thread(lambda: oc.get("pre-fork")),
                        "new_thread": on_new_thread(lambda: (oc.put("n", "v", ttl=60), oc.get("n"))[1]),
                    }
                except BaseException as e:
                    outcome = {"error": repr(e)}
                report(w, outcome)
            return time.monotonic()

        monkeypatch.setattr("cachekit.object_cache.time", types.SimpleNamespace(monotonic=fork_here))
        assert oc.get("pre-fork") == (True, "v")
        os.close(w)
        assert child, "get() no longer reads the clock"

        # The forking thread may have been mid-update, so the child dropped the entries.
        assert child_outcome(child, r) == {"pre_fork": (False, None), "new_thread": (True, "v")}

    def test_registry_keeps_no_cache_alive(self) -> None:
        ref = weakref.ref(ObjectCache())
        gc.collect()
        assert ref() is None
