"""Unit tests for L1Cache invalidation functionality."""

import pytest

from cachekit.config.nested import L1CacheConfig
from cachekit.l1_cache import L1Cache


class TestL1CacheInvalidation:
    """Test L1 cache invalidation operations."""

    def test_invalidate_by_key_removes_entry(self):
        """Test that invalidate_by_key removes specific entry."""
        config = L1CacheConfig(invalidation_enabled=True, namespace_index=True)
        cache = L1Cache(max_memory_mb=10, config=config)

        # Put multiple entries
        cache.put("key1", b"value1", redis_ttl=100.0)
        cache.put("key2", b"value2", redis_ttl=100.0)
        cache.put("key3", b"value3", redis_ttl=100.0)

        # Invalidate key2
        result = cache.invalidate_by_key("key2")
        assert result is True

        # Verify key2 is gone, others remain
        hit1, val1 = cache.get("key1")
        assert hit1 is True
        assert val1 == b"value1"

        hit2, val2 = cache.get("key2")
        assert hit2 is False
        assert val2 is None

        hit3, val3 = cache.get("key3")
        assert hit3 is True
        assert val3 == b"value3"

    def test_invalidate_by_key_returns_false_for_missing(self):
        """Test that invalidate_by_key returns False for non-existent key."""
        config = L1CacheConfig(invalidation_enabled=True, namespace_index=True)
        cache = L1Cache(max_memory_mb=10, config=config)

        result = cache.invalidate_by_key("nonexistent")
        assert result is False

    def test_invalidate_by_namespace_clears_matching(self):
        """Test that invalidate_by_namespace clears all entries in namespace."""
        config = L1CacheConfig(invalidation_enabled=True, namespace_index=True)
        cache = L1Cache(max_memory_mb=10, config=config)

        # Put entries in different namespaces
        cache.put("key1", b"value1", redis_ttl=100.0, namespace="ns1")
        cache.put("key2", b"value2", redis_ttl=100.0, namespace="ns1")
        cache.put("key3", b"value3", redis_ttl=100.0, namespace="ns2")
        cache.put("key4", b"value4", redis_ttl=100.0, namespace="ns2")
        cache.put("key5", b"value5", redis_ttl=100.0)  # No namespace

        # Invalidate ns1
        count = cache.invalidate_by_namespace("ns1")
        assert count == 2

        # Verify ns1 entries are gone
        hit1, val1 = cache.get("key1")
        assert hit1 is False

        hit2, val2 = cache.get("key2")
        assert hit2 is False

        # Verify ns2 and no-namespace entries remain
        hit3, val3 = cache.get("key3")
        assert hit3 is True

        hit4, val4 = cache.get("key4")
        assert hit4 is True

        hit5, val5 = cache.get("key5")
        assert hit5 is True

    def test_invalidate_by_namespace_empty_namespace(self):
        """Test invalidate_by_namespace on empty namespace returns 0."""
        config = L1CacheConfig(invalidation_enabled=True, namespace_index=True)
        cache = L1Cache(max_memory_mb=10, config=config)

        count = cache.invalidate_by_namespace("nonexistent")
        assert count == 0

    def test_invalidate_all_clears_everything(self):
        """Test that invalidate_all removes all entries."""
        config = L1CacheConfig(invalidation_enabled=True, namespace_index=True)
        cache = L1Cache(max_memory_mb=10, config=config)

        # Put entries
        cache.put("key1", b"value1", redis_ttl=100.0, namespace="ns1")
        cache.put("key2", b"value2", redis_ttl=100.0, namespace="ns2")
        cache.put("key3", b"value3", redis_ttl=100.0)

        # Invalidate all
        count = cache.invalidate_all()
        assert count == 3

        # Verify all gone
        hit1, val1 = cache.get("key1")
        assert hit1 is False

        hit2, val2 = cache.get("key2")
        assert hit2 is False

        hit3, val3 = cache.get("key3")
        assert hit3 is False

    def test_namespace_index_tracks_entries(self):
        """Test that namespace index correctly tracks entries."""
        config = L1CacheConfig(invalidation_enabled=True, namespace_index=True)
        cache = L1Cache(max_memory_mb=10, config=config)

        # Verify namespace index exists
        assert hasattr(cache, "_namespace_index")

        # Put entries
        cache.put("key1", b"value1", redis_ttl=100.0, namespace="ns1")
        cache.put("key2", b"value2", redis_ttl=100.0, namespace="ns1")
        cache.put("key3", b"value3", redis_ttl=100.0, namespace="ns2")

        # Verify index tracking
        assert "key1" in cache._namespace_index["ns1"]
        assert "key2" in cache._namespace_index["ns1"]
        assert "key3" in cache._namespace_index["ns2"]
        assert len(cache._namespace_index["ns1"]) == 2
        assert len(cache._namespace_index["ns2"]) == 1

        # Invalidate and verify index cleanup
        cache.invalidate_by_namespace("ns1")
        assert "ns1" not in cache._namespace_index
        assert len(cache._namespace_index["ns2"]) == 1

    def test_invalidation_increments_version(self):
        """Test that invalidation increments entry version to prevent stale refresh."""
        config = L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.5, invalidation_enabled=True, namespace_index=True)
        cache = L1Cache(max_memory_mb=10, config=config)

        key = "test_key"
        cache.put(key, b"value", redis_ttl=100.0)

        # Get initial version
        initial_version = cache._entry_version.get(key, 0)
        assert initial_version == 0

        # Invalidate
        cache.invalidate_by_key(key)

        # Verify version incremented
        new_version = cache._entry_version.get(key, 0)
        assert new_version == initial_version + 1

    def test_invalidation_clears_refreshing_flag(self):
        """Test that invalidation clears _refreshing_keys flag."""
        config = L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.5, invalidation_enabled=True, namespace_index=True)
        cache = L1Cache(max_memory_mb=10, config=config)

        key = "test_key"

        # Manually add to refreshing set (simulate SWR trigger)
        cache._refreshing_keys.add(key)
        assert key in cache._refreshing_keys

        # Put entry
        cache.put(key, b"value", redis_ttl=100.0)

        # Invalidate
        cache.invalidate_by_key(key)

        # Verify refreshing flag cleared
        assert key not in cache._refreshing_keys

    def test_no_index_falls_back_to_scan(self):
        """Test that namespace invalidation works without index (O(n) scan fallback)."""
        # Config WITHOUT namespace_index
        config = L1CacheConfig(invalidation_enabled=True, namespace_index=False)
        cache = L1Cache(max_memory_mb=10, config=config)

        # Verify index does NOT exist
        assert not hasattr(cache, "_namespace_index")

        # Put entries with namespaces
        cache.put("key1", b"value1", redis_ttl=100.0, namespace="ns1")
        cache.put("key2", b"value2", redis_ttl=100.0, namespace="ns1")
        cache.put("key3", b"value3", redis_ttl=100.0, namespace="ns2")

        # Invalidate ns1 (should use O(n) fallback)
        count = cache.invalidate_by_namespace("ns1")
        assert count == 2

        # Verify correct entries removed
        hit1, val1 = cache.get("key1")
        assert hit1 is False

        hit2, val2 = cache.get("key2")
        assert hit2 is False

        hit3, val3 = cache.get("key3")
        assert hit3 is True

    def test_invalidate_all_increments_all_versions(self):
        """Test that invalidate_all increments version for all keys."""
        config = L1CacheConfig(invalidation_enabled=True, namespace_index=True)
        cache = L1Cache(max_memory_mb=10, config=config)

        # Put entries
        cache.put("key1", b"value1", redis_ttl=100.0)
        cache.put("key2", b"value2", redis_ttl=100.0)
        cache.put("key3", b"value3", redis_ttl=100.0)

        # Get initial versions (all 0)
        assert cache._entry_version.get("key1", 0) == 0
        assert cache._entry_version.get("key2", 0) == 0
        assert cache._entry_version.get("key3", 0) == 0

        # Invalidate all
        cache.invalidate_all()

        # Verify all versions incremented
        assert cache._entry_version["key1"] == 1
        assert cache._entry_version["key2"] == 1
        assert cache._entry_version["key3"] == 1

    def test_invalidate_all_clears_namespace_index(self):
        """Test that invalidate_all clears namespace index."""
        config = L1CacheConfig(invalidation_enabled=True, namespace_index=True)
        cache = L1Cache(max_memory_mb=10, config=config)

        # Put entries
        cache.put("key1", b"value1", redis_ttl=100.0, namespace="ns1")
        cache.put("key2", b"value2", redis_ttl=100.0, namespace="ns2")

        # Verify index populated
        assert len(cache._namespace_index) > 0

        # Invalidate all
        cache.invalidate_all()

        # Verify index cleared
        assert len(cache._namespace_index) == 0

    def test_invalidate_all_clears_refreshing_flags(self):
        """Test that invalidate_all clears all refreshing flags."""
        config = L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.5, invalidation_enabled=True, namespace_index=True)
        cache = L1Cache(max_memory_mb=10, config=config)

        # Manually add keys to refreshing set
        cache._refreshing_keys.add("key1")
        cache._refreshing_keys.add("key2")
        assert len(cache._refreshing_keys) == 2

        # Put entries
        cache.put("key1", b"value1", redis_ttl=100.0)
        cache.put("key2", b"value2", redis_ttl=100.0)

        # Invalidate all
        cache.invalidate_all()

        # Verify refreshing set cleared
        assert len(cache._refreshing_keys) == 0

    def test_namespace_index_updated_on_overwrite(self):
        """Test that namespace index is updated when entry is overwritten with different namespace."""
        config = L1CacheConfig(invalidation_enabled=True, namespace_index=True)
        cache = L1Cache(max_memory_mb=10, config=config)

        key = "test_key"

        # Put with ns1
        cache.put(key, b"value1", redis_ttl=100.0, namespace="ns1")
        assert key in cache._namespace_index["ns1"]

        # Overwrite with ns2
        cache.put(key, b"value2", redis_ttl=100.0, namespace="ns2")

        # Verify index updated
        assert key not in cache._namespace_index.get("ns1", set())
        assert key in cache._namespace_index["ns2"]

    @pytest.mark.critical
    def test_invalidate_during_swr_prevents_resurrection(self):
        """Test that invalidation during SWR refresh prevents stale data resurrection.

        CRITICAL: This is the version token mechanism test.
        """
        config = L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.5, invalidation_enabled=True, namespace_index=True)
        cache = L1Cache(max_memory_mb=10, config=config)

        key = "test_key"
        old_value = b"old_value"
        new_value = b"new_value"

        # Put initial value
        cache.put(key, old_value, redis_ttl=100.0)

        # Get version
        version = cache._entry_version.get(key, 0)
        assert version == 0

        # Simulate: SWR triggered, then key invalidated before refresh completes
        cache.invalidate_by_key(key)

        # Version should be incremented
        assert cache._entry_version[key] == 1

        # Try to complete refresh with old version
        success = cache.complete_refresh(key, version, new_value, 1000.0)

        # Should fail (version mismatch)
        assert success is False

        # Entry should NOT exist
        hit, val = cache.get(key)
        assert hit is False

    def test_invalidate_by_namespace_with_no_namespace_entries(self):
        """Test that entries without namespace are not affected by namespace invalidation."""
        config = L1CacheConfig(invalidation_enabled=True, namespace_index=True)
        cache = L1Cache(max_memory_mb=10, config=config)

        # Put entries with and without namespace
        cache.put("key1", b"value1", redis_ttl=100.0, namespace="ns1")
        cache.put("key2", b"value2", redis_ttl=100.0)  # No namespace
        cache.put("key3", b"value3", redis_ttl=100.0)  # No namespace

        # Invalidate ns1
        count = cache.invalidate_by_namespace("ns1")
        assert count == 1

        # Verify no-namespace entries remain
        hit2, val2 = cache.get("key2")
        assert hit2 is True

        hit3, val3 = cache.get("key3")
        assert hit3 is True

    def test_multiple_namespaces_independent(self):
        """Test that multiple namespaces are independent."""
        config = L1CacheConfig(invalidation_enabled=True, namespace_index=True)
        cache = L1Cache(max_memory_mb=10, config=config)

        # Put entries in different namespaces
        cache.put("key1", b"value1", redis_ttl=100.0, namespace="users")
        cache.put("key2", b"value2", redis_ttl=100.0, namespace="products")
        cache.put("key3", b"value3", redis_ttl=100.0, namespace="orders")

        # Invalidate products
        count = cache.invalidate_by_namespace("products")
        assert count == 1

        # Verify only products invalidated
        hit1, _ = cache.get("key1")
        assert hit1 is True

        hit2, _ = cache.get("key2")
        assert hit2 is False

        hit3, _ = cache.get("key3")
        assert hit3 is True
