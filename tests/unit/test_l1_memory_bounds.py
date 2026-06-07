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
