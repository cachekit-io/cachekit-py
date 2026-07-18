"""L1 memory-bound guarantees, especially the oversized-single-entry vector.

A cached value larger than the entire L1 budget must NOT be stored (it would push L1
permanently over its own limit and, for multi-GB DataFrame envelopes, become an OOM
vector that also evicts every other useful entry). Such values still live in L2.
"""

from __future__ import annotations

import pytest

from cachekit.l1_cache import L1Cache

MB = 1024 * 1024


@pytest.mark.unit
class TestOversizedEntryRejection:
    def test_entry_larger_than_budget_is_not_stored(self):
        cache = L1Cache(max_memory_mb=1)
        cache.put("big", b"\x00" * (2 * MB), redis_ttl=300)

        found, _ = cache.get("big")
        assert found is False
        assert cache._current_memory_bytes == 0

    def test_rejected_oversized_put_does_not_evict_existing_entries(self):
        """A doomed oversized put must not evict good entries on its way to failing."""
        cache = L1Cache(max_memory_mb=1)
        cache.put("keep", b"\x00" * (512 * 1024), redis_ttl=300)  # fits

        cache.put("toobig", b"\x00" * (5 * MB), redis_ttl=300)  # cannot ever fit

        assert cache.get("keep")[0] is True  # survivor
        assert cache.get("toobig")[0] is False
        assert cache._current_memory_bytes <= cache.max_memory_bytes

    def test_oversized_update_drops_stale_smaller_entry(self):
        """An oversized put for an EXISTING key must drop the stale value, not serve it."""
        cache = L1Cache(max_memory_mb=1)
        cache.put("k", b"\x00" * (256 * 1024), redis_ttl=300)  # fits
        assert cache.get("k")[0] is True

        cache.put("k", b"\x00" * (5 * MB), redis_ttl=300)  # same key, now oversized

        assert cache.get("k")[0] is False  # stale smaller value evicted, not served
        assert cache._current_memory_bytes == 0

    def test_entry_equal_to_budget_is_stored(self):
        cache = L1Cache(max_memory_mb=1)
        cache.put("exact", b"\x00" * (1 * MB), redis_ttl=300)
        assert cache.get("exact")[0] is True

    def test_normal_entry_still_stored(self):
        cache = L1Cache(max_memory_mb=10)
        cache.put("k", b"value", redis_ttl=300)
        assert cache.get("k") == (True, b"value")

    def test_memory_never_exceeds_budget_under_mixed_load(self):
        cache = L1Cache(max_memory_mb=2)
        for i in range(20):
            cache.put(f"k{i}", b"\x00" * (300 * 1024), redis_ttl=300)  # 300KB each
        cache.put("huge", b"\x00" * (50 * MB), redis_ttl=300)  # rejected
        assert cache._current_memory_bytes <= cache.max_memory_bytes


@pytest.mark.unit
class TestL1NonFiniteTtl:
    """A non-finite TTL (NaN/inf) must never produce an immortal L1 entry (#158)."""

    @pytest.mark.parametrize("bad_ttl", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_redis_ttl_not_stored(self, bad_ttl):
        cache = L1Cache(max_memory_mb=10)
        cache.put("k", b"value", redis_ttl=bad_ttl)
        assert cache.get("k")[0] is False
        assert cache._current_memory_bytes == 0

    @pytest.mark.parametrize("bad_ttl", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_expires_at_not_stored(self, bad_ttl):
        cache = L1Cache(max_memory_mb=10)
        cache.put("k", b"value", expires_at=bad_ttl)
        assert cache.get("k")[0] is False
        assert cache._current_memory_bytes == 0


@pytest.mark.unit
class TestNonBytesRejection:
    """#171 blocker C belt-and-suspenders: L1 stores raw bytes ONLY.

    An mmap-backed memoryview must never reach L1 — it would pin the mapped file's inode for the
    whole L1 TTL (silent staleness on POSIX, write failures on Windows, RSS blowup under hot keys).
    The mmap read path confines the view to the deserialize frame, but a future refactor could
    regress; this guard makes that regression a loud TypeError instead of a silent alias. bytearray
    is rejected too (a mutable buffer could change underneath the cache).
    """

    def test_put_rejects_memoryview(self):
        cache = L1Cache(max_memory_mb=10)
        with pytest.raises(TypeError):
            cache.put("k", memoryview(b"data"), redis_ttl=300)  # type: ignore[arg-type]
        assert cache.get("k")[0] is False

    def test_put_rejects_bytearray(self):
        cache = L1Cache(max_memory_mb=10)
        with pytest.raises(TypeError):
            cache.put("k", bytearray(b"data"), redis_ttl=300)  # type: ignore[arg-type]

    def test_put_accepts_bytes(self):
        cache = L1Cache(max_memory_mb=10)
        cache.put("k", b"data", redis_ttl=300)
        assert cache.get("k")[0] is True


@pytest.mark.unit
class TestConfiguredBudgetWiring:
    """Issue #163: l1_max_size_mb / L1CacheConfig.max_size_mb must reach L1Cache.

    Before the fix, the wrapper called get_l1_cache(namespace) and the manager
    hardcoded default_max_memory_mb=100 — configuring the budget was silently
    ignored. These tests pin the wiring at every layer.
    """

    def test_configured_budget_enforced_with_eviction(self):
        """Filling past a configured (non-default) 2MB budget evicts LRU entries."""
        cache = L1Cache(max_memory_mb=2)
        for i in range(5):  # 5 x 512KB = 2.5MB > 2MB budget
            cache.put(f"k{i}", b"\x00" * (512 * 1024), redis_ttl=300)

        assert cache._current_memory_bytes <= 2 * MB
        assert cache.get("k0")[0] is False  # oldest evicted
        assert cache.get("k4")[0] is True  # newest survives
        assert cache._evictions > 0

    def test_manager_default_reads_settings(self, monkeypatch):
        """L1CacheManager() with no explicit default uses CACHEKIT_L1_MAX_SIZE_MB."""
        from cachekit.config.singleton import reset_settings
        from cachekit.l1_cache import L1CacheManager

        monkeypatch.setenv("CACHEKIT_L1_MAX_SIZE_MB", "7")
        reset_settings()
        try:
            manager = L1CacheManager()
            cache = manager.get_cache("settings-budget-ns")
            assert cache.max_memory_bytes == 7 * MB
        finally:
            reset_settings()

    def test_manager_explicit_default_wins_over_settings(self):
        from cachekit.l1_cache import L1CacheManager

        manager = L1CacheManager(default_max_memory_mb=3)
        assert manager.get_cache("explicit-default-ns").max_memory_bytes == 3 * MB

    def test_get_cache_per_namespace_override(self):
        from cachekit.l1_cache import L1CacheManager

        manager = L1CacheManager(default_max_memory_mb=100)
        cache = manager.get_cache("override-ns", max_size_mb=5)
        assert cache.max_memory_bytes == 5 * MB

    def test_first_configuration_wins_per_namespace(self, caplog):
        """A second, conflicting budget for an existing namespace is ignored loudly."""
        import logging

        from cachekit.l1_cache import L1CacheManager

        manager = L1CacheManager(default_max_memory_mb=100)
        first = manager.get_cache("conflict-ns", max_size_mb=5)
        with caplog.at_level(logging.WARNING, logger="cachekit.l1_cache"):
            second = manager.get_cache("conflict-ns", max_size_mb=50)

        assert second is first
        assert second.max_memory_bytes == 5 * MB
        assert any("first configuration wins" in r.message for r in caplog.records)

    def test_decorator_config_budget_reaches_l1_cache(self):
        """End-to-end: DecoratorConfig(l1=L1CacheConfig(max_size_mb=N)) sizes the L1Cache."""
        import uuid

        from cachekit.config import DecoratorConfig
        from cachekit.config.nested import L1CacheConfig
        from cachekit.decorators.wrapper import create_cache_wrapper
        from cachekit.l1_cache import get_l1_cache

        namespace = f"budget-wiring-{uuid.uuid4().hex[:8]}"
        config = DecoratorConfig(namespace=namespace, l1=L1CacheConfig(max_size_mb=9))

        def fn(x: int) -> int:
            return x * 2

        create_cache_wrapper(fn, config=config)

        assert get_l1_cache(namespace).max_memory_bytes == 9 * MB
