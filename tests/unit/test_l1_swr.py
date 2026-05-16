"""Unit tests for L1Cache stale-while-revalidate (SWR) functionality."""

import threading

import pytest
import time_machine

from cachekit.config.nested import L1CacheConfig
from cachekit.l1_cache import L1Cache


class TestL1CacheSWR:
    """Test stale-while-revalidate behavior."""

    def test_fresh_entry_no_refresh(self):
        """Test that fresh entries (elapsed < threshold) don't trigger refresh."""
        config = L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.5)
        cache = L1Cache(max_memory_mb=10, config=config)

        # Store entry
        key = "test_key"
        value = b"test_value"
        ttl = 100.0
        cache.put(key, value, redis_ttl=ttl)

        # Get immediately (elapsed ~0, threshold = 50s)
        hit, val, needs_refresh, version = cache.get_with_swr(key, ttl)

        assert hit is True
        assert val == value
        assert needs_refresh is False
        assert version == 0

    def test_stale_entry_triggers_refresh(self):
        """Test that stale entries (elapsed > threshold) trigger refresh."""
        config = L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.5)
        cache = L1Cache(max_memory_mb=10, config=config)

        key = "test_key"
        value = b"test_value"
        ttl = 100.0

        with time_machine.travel(0, tick=False) as traveller:
            # Time at put
            traveller.move_to(1000.0)
            cache.put(key, value, redis_ttl=ttl)

            # Time at get (60s later, past threshold of ~50s even with jitter)
            traveller.move_to(1060.0)
            hit, val, needs_refresh, version = cache.get_with_swr(key, ttl)

            assert hit is True
            assert val == value
            assert needs_refresh is True
            assert version == 0

    def test_refreshing_prevents_duplicate_refresh(self):
        """Test that _refreshing_keys prevents duplicate refresh triggers."""
        config = L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.5)
        cache = L1Cache(max_memory_mb=10, config=config)

        key = "test_key"
        value = b"test_value"
        ttl = 100.0

        with time_machine.travel(0, tick=False) as traveller:
            # Put at time 1000
            traveller.move_to(1000.0)
            cache.put(key, value, redis_ttl=ttl)

            # First get at 1060 (triggers refresh)
            traveller.move_to(1060.0)
            hit1, val1, needs_refresh1, version1 = cache.get_with_swr(key, ttl)
            assert needs_refresh1 is True

            # Second get at same time (should NOT trigger duplicate refresh)
            hit2, val2, needs_refresh2, version2 = cache.get_with_swr(key, ttl)
            assert hit2 is True
            assert needs_refresh2 is False  # Already refreshing

    def test_complete_refresh_updates_entry(self):
        """Test that complete_refresh updates entry value and cached_at."""
        config = L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.5)
        cache = L1Cache(max_memory_mb=10, config=config)

        key = "test_key"
        old_value = b"old_value"
        new_value = b"new_value"
        ttl = 100.0

        with time_machine.travel(0, tick=False) as traveller:
            # Put old value
            traveller.move_to(1000.0)
            cache.put(key, old_value, redis_ttl=ttl)

            # Trigger refresh
            traveller.move_to(1060.0)
            hit, val, needs_refresh, version = cache.get_with_swr(key, ttl)
            assert needs_refresh is True
            assert version == 0

            # Complete refresh
            traveller.move_to(1065.0)
            success = cache.complete_refresh(key, version, new_value, 1065.0)
            assert success is True

            # Verify new value is cached
            hit, val, needs_refresh, _ = cache.get_with_swr(key, ttl)
            assert hit is True
            assert val == new_value
            assert needs_refresh is False  # Just refreshed, should be fresh

    def test_jitter_varies_threshold(self):
        """Test that jitter causes threshold to vary by ±10%."""
        config = L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.5)
        cache = L1Cache(max_memory_mb=10, config=config)

        key = "test_key"
        value = b"test_value"
        ttl = 100.0
        base_threshold = ttl * 0.5  # 50s

        thresholds_triggered_refresh = []

        # Run 100 iterations
        for i in range(100):
            cache.clear()
            test_key = f"{key}_{i}"

            with time_machine.travel(0, tick=False) as traveller:
                # Put at time 1000
                traveller.move_to(1000.0)
                cache.put(test_key, value, redis_ttl=ttl)

                # Test at different elapsed times to find threshold
                for elapsed in range(40, 61):  # 40-60s (around 50s ±10%)
                    traveller.move_to(1000.0 + elapsed)
                    hit, val, needs_refresh, version = cache.get_with_swr(test_key, ttl)

                    if needs_refresh:
                        thresholds_triggered_refresh.append(elapsed)
                        break

        # Verify we got variation in thresholds
        assert len(thresholds_triggered_refresh) > 0
        min_threshold = min(thresholds_triggered_refresh)
        max_threshold = max(thresholds_triggered_refresh)

        # Should vary roughly ±10% (45s to 55s)
        # We want to ensure the jitter causes variation (range is expected to be ~40-60s)
        # The key assertion is that min < 50 and max > 50 (showing jitter in both directions)
        # With 100 iterations, we expect decent coverage of the ±10% range
        assert min_threshold < base_threshold, "Jitter should produce thresholds below base"
        assert max_threshold > base_threshold, "Jitter should produce thresholds above base"

    def test_cancel_refresh_clears_flag(self):
        """Test that cancel_refresh removes key from _refreshing_keys."""
        config = L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.5)
        cache = L1Cache(max_memory_mb=10, config=config)

        key = "test_key"
        value = b"test_value"
        ttl = 100.0

        with time_machine.travel(0, tick=False) as traveller:
            # Put at time 1000
            traveller.move_to(1000.0)
            cache.put(key, value, redis_ttl=ttl)

            # Trigger refresh
            traveller.move_to(1060.0)
            hit, val, needs_refresh, version = cache.get_with_swr(key, ttl)
            assert needs_refresh is True

            # Cancel refresh
            cache.cancel_refresh(key)

            # Next get should trigger refresh again (flag cleared)
            hit2, val2, needs_refresh2, version2 = cache.get_with_swr(key, ttl)
            assert needs_refresh2 is True

    @pytest.mark.critical
    def test_version_mismatch_aborts_refresh_concurrent(self):
        """Test that version mismatch prevents stale refresh under concurrent invalidation.

        CRITICAL RACE CONDITION TEST:
        Uses threading.Event for deterministic ordering (no sleep-based timing):
        1. Refresher triggers SWR refresh, signals ready, waits for invalidation
        2. Invalidator waits for ready signal, invalidates key, signals done
        3. Refresher attempts complete_refresh() — must see version mismatch
        """
        config = L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.5)
        cache = L1Cache(max_memory_mb=10, config=config)

        key = "test_key"
        old_value = b"old_value"
        new_value = b"new_value"
        ttl = 100.0

        # Deterministic synchronization — no timing assumptions
        refresh_triggered = threading.Event()
        invalidation_done = threading.Event()

        # Shared state
        refresh_result = [None]
        thread_errors = []

        def refresher_thread():
            """Thread that triggers refresh, waits for invalidation, then completes."""
            try:
                # Trigger refresh (time is frozen at 1060.0 by time-machine)
                hit, val, needs_refresh, version = cache.get_with_swr(key, ttl)

                if needs_refresh:
                    # Signal: refresh is in progress, invalidator can proceed
                    refresh_triggered.set()

                    # Wait for invalidation to complete before attempting refresh
                    invalidation_done.wait(timeout=2.0)

                    # Try to complete refresh — should fail (version mismatch)
                    success = cache.complete_refresh(key, version, new_value, 1065.0)
                    refresh_result[0] = success
            except Exception as e:
                thread_errors.append(e)

        def invalidator_thread():
            """Thread that invalidates key after refresh is triggered."""
            try:
                # Wait until refresher has triggered SWR
                refresh_triggered.wait(timeout=2.0)

                # Invalidate key (increments version)
                cache.invalidate_by_key(key)

                # Signal: invalidation complete, refresher can proceed
                invalidation_done.set()
            except Exception as e:
                thread_errors.append(e)

        # Setup: Put initial value and advance clock past SWR threshold
        with time_machine.travel(0, tick=False) as traveller:
            traveller.move_to(1000.0)
            cache.put(key, old_value, redis_ttl=ttl)

            # Advance past SWR threshold so get_with_swr triggers refresh
            traveller.move_to(1060.0)

            # Start concurrent threads
            t1 = threading.Thread(target=refresher_thread)
            t2 = threading.Thread(target=invalidator_thread)

            t1.start()
            t2.start()

            t1.join(timeout=5.0)
            t2.join(timeout=5.0)

        # Verify no thread errors
        assert len(thread_errors) == 0, f"Thread errors occurred: {thread_errors}"

        # CRITICAL: Refresh should be aborted (version mismatch)
        assert refresh_result[0] is False, "complete_refresh() should return False on version mismatch"

        # Entry should NOT exist (was invalidated)
        hit, val = cache.get(key)
        assert hit is False, "Entry should not exist after invalidation"
        assert val is None

    def test_swr_disabled_never_triggers_refresh(self):
        """Test that SWR disabled never triggers refresh."""
        config = L1CacheConfig(swr_enabled=False, swr_threshold_ratio=0.5)
        cache = L1Cache(max_memory_mb=10, config=config)

        key = "test_key"
        value = b"test_value"
        ttl = 100.0

        with time_machine.travel(0, tick=False) as traveller:
            # Put at time 1000
            traveller.move_to(1000.0)
            cache.put(key, value, redis_ttl=ttl)

            # Get at 1060 (way past threshold)
            traveller.move_to(1060.0)
            hit, val, needs_refresh, version = cache.get_with_swr(key, ttl)

            assert hit is True
            assert val == value
            assert needs_refresh is False  # SWR disabled

    def test_expired_entry_not_returned(self):
        """Test that expired entries are not returned by get_with_swr."""
        config = L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.5)
        cache = L1Cache(max_memory_mb=10, config=config)

        key = "test_key"
        value = b"test_value"
        ttl = 100.0

        with time_machine.travel(0, tick=False) as traveller:
            # Put at time 1000
            traveller.move_to(1000.0)
            cache.put(key, value, redis_ttl=ttl)

            # Get at 1101 (past expiry)
            traveller.move_to(1101.0)
            hit, val, needs_refresh, version = cache.get_with_swr(key, ttl)

            assert hit is False
            assert val is None
            assert needs_refresh is False

    def test_complete_refresh_with_evicted_entry(self):
        """Test that complete_refresh handles entry that was LRU evicted during refresh."""
        config = L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.5)
        cache = L1Cache(max_memory_mb=10, config=config)

        key = "test_key"
        value = b"test_value"
        new_value = b"new_value"
        ttl = 100.0

        with time_machine.travel(0, tick=False) as traveller:
            # Put entry
            traveller.move_to(1000.0)
            cache.put(key, value, redis_ttl=ttl)

            # Trigger refresh
            traveller.move_to(1060.0)
            hit, val, needs_refresh, version = cache.get_with_swr(key, ttl)
            assert needs_refresh is True

            # Manually evict entry (simulate LRU eviction)
            cache.invalidate_by_key(key)

            # Try to complete refresh (entry no longer exists)
            traveller.move_to(1065.0)
            success = cache.complete_refresh(key, version, new_value, 1065.0)

            # Should return False (entry was evicted, even though version might match)
            # NOTE: Current implementation returns False for evicted entries
            assert success is False
