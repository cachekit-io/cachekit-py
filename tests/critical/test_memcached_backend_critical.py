"""Critical path tests for MemcachedBackend - fast smoke tests that run on every commit.

These tests cover core MemcachedBackend functionality with mocked pymemcache:
- Basic get/set/delete roundtrips
- exists() checks
- health_check() implementation
- Intent decorator integration
- Default backend integration

Performance target: < 1 second total for all tests.
Marked with @pytest.mark.critical for fast CI runs.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cachekit.backends.memcached.backend import MemcachedBackend
from cachekit.backends.memcached.config import MemcachedBackendConfig


@pytest.fixture
def mock_store():
    """Dict-backed store for mock Memcached client."""
    return {}


@pytest.fixture
def mock_hash_client(mock_store):
    """Patch HashClient so no real Memcached is needed.

    Wires get/set/delete/stats to a plain dict.
    """
    with patch("pymemcache.client.hash.HashClient") as mock_cls:
        instance = MagicMock()

        def _set(key, value, expire=0):
            mock_store[key] = value

        def _get(key):
            return mock_store.get(key)

        def _delete(key, noreply=True):
            if key in mock_store:
                del mock_store[key]
                return True
            return False

        def _stats():
            return {("127.0.0.1", 11211): {"pid": "1", "uptime": "1000"}}

        instance.set.side_effect = _set
        instance.get.side_effect = _get
        instance.delete.side_effect = _delete
        instance.stats.side_effect = _stats
        mock_cls.return_value = instance
        yield instance


@pytest.fixture
def backend(mock_hash_client):
    """Create MemcachedBackend with mocked HashClient."""
    return MemcachedBackend(MemcachedBackendConfig())


@pytest.mark.critical
def test_get_set_delete_roundtrip(backend):
    """Core get/set/delete operations work correctly."""
    # Set
    backend.set("key", b"value", ttl=60)

    # Get
    assert backend.get("key") == b"value"

    # Delete
    assert backend.delete("key") is True
    assert backend.get("key") is None
    assert backend.delete("key") is False  # Already deleted


@pytest.mark.critical
def test_exists_accurate(backend):
    """exists() returns correct True/False status."""
    assert backend.exists("missing") is False
    backend.set("present", b"data", ttl=300)
    assert backend.exists("present") is True


@pytest.mark.critical
def test_health_check_returns_tuple(backend):
    """health_check() returns (bool, dict) with required fields."""
    is_healthy, details = backend.health_check()

    assert isinstance(is_healthy, bool)
    assert is_healthy is True
    assert isinstance(details, dict)
    assert details["backend_type"] == "memcached"
    assert "latency_ms" in details
    assert isinstance(details["latency_ms"], float)
    assert details["servers"] == 1


@pytest.mark.critical
def test_intent_decorators_with_memcached_backend(mock_store):
    """Intent decorators work with explicit MemcachedBackend."""
    from cachekit import cache

    with patch("pymemcache.client.hash.HashClient") as mock_cls:
        instance = MagicMock()
        instance.set.side_effect = lambda k, v, expire=0: mock_store.__setitem__(k, v)
        instance.get.side_effect = lambda k: mock_store.get(k)
        instance.delete.side_effect = lambda k, noreply=True: mock_store.pop(k, None) is not None
        mock_cls.return_value = instance

        mb = MemcachedBackend(MemcachedBackendConfig())
        call_count = 0

        @cache.minimal(ttl=300, backend=mb)
        def compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        assert compute(5) == 10
        assert call_count == 1
        assert compute(5) == 10
        assert call_count == 1  # Cache hit


@pytest.mark.critical
def test_set_default_backend_with_memcached_backend(mock_store):
    """set_default_backend() is consulted when no explicit backend= provided."""
    from cachekit import cache
    from cachekit.config.decorator import get_default_backend, set_default_backend

    with patch("pymemcache.client.hash.HashClient") as mock_cls:
        instance = MagicMock()
        instance.set.side_effect = lambda k, v, expire=0: mock_store.__setitem__(k, v)
        instance.get.side_effect = lambda k: mock_store.get(k)
        instance.delete.side_effect = lambda k, noreply=True: mock_store.pop(k, None) is not None
        mock_cls.return_value = instance

        mb = MemcachedBackend(MemcachedBackendConfig())
        original = get_default_backend()

        try:
            set_default_backend(mb)
            call_count = 0

            @cache.minimal(ttl=300)
            def compute(x: int) -> int:
                nonlocal call_count
                call_count += 1
                return x * 3

            assert compute(4) == 12
            assert call_count == 1
            assert compute(4) == 12
            assert call_count == 1  # Cache hit
        finally:
            set_default_backend(original)
