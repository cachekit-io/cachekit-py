"""Critical path tests for MemcachedBackend - fast smoke tests that run on every commit.

These tests cover core MemcachedBackend functionality with mocked pymemcache:
- Basic get/set/delete roundtrips
- exists() checks
- health_check() implementation (healthy + unhealthy)
- Error path coverage (all operations raise BackendError)
- Key prefix application
- TTL clamping to 30-day max
- Error classification (all pymemcache exception types)
- Lazy import via __getattr__
- Config validation edge cases
- Intent decorator integration
- Default backend integration

Performance target: < 1 second total for all tests.
Marked with @pytest.mark.critical for fast CI runs.
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest

from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.backends.memcached.backend import MemcachedBackend
from cachekit.backends.memcached.config import MAX_MEMCACHED_TTL, MemcachedBackendConfig
from cachekit.backends.memcached.error_handler import classify_memcached_error


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
    assert details["configured_servers"] == 1


@pytest.mark.critical
def test_health_check_unhealthy(mock_hash_client):
    """health_check() returns (False, ...) when server is down."""
    mock_hash_client.get.side_effect = ConnectionError("refused")
    backend = MemcachedBackend(MemcachedBackendConfig())
    is_healthy, details = backend.health_check()

    assert is_healthy is False
    assert "error" in details
    assert details["backend_type"] == "memcached"


@pytest.mark.critical
def test_error_paths_raise_backend_error(mock_hash_client):
    """All operations wrap exceptions in BackendError."""
    backend = MemcachedBackend(MemcachedBackendConfig())

    # get error path
    mock_hash_client.get.side_effect = ConnectionError("refused")
    with pytest.raises(BackendError):
        backend.get("key")

    # set error path
    mock_hash_client.set.side_effect = ConnectionError("refused")
    with pytest.raises(BackendError):
        backend.set("key", b"val", ttl=60)

    # delete error path
    mock_hash_client.delete.side_effect = ConnectionError("refused")
    with pytest.raises(BackendError):
        backend.delete("key")

    # exists error path (uses get internally)
    with pytest.raises(BackendError):
        backend.exists("key")


@pytest.mark.critical
def test_key_prefix_applied(mock_hash_client):
    """Key prefix is prepended to all operations."""
    backend = MemcachedBackend(MemcachedBackendConfig(key_prefix="app:"))
    mock_hash_client.get.return_value = b"data"
    backend.get("mykey")
    mock_hash_client.get.assert_called_with("app:mykey")


@pytest.mark.critical
def test_ttl_clamped_to_30_day_max(mock_hash_client):
    """TTL exceeding 30 days is clamped, not rejected."""
    backend = MemcachedBackend(MemcachedBackendConfig())
    huge_ttl = MAX_MEMCACHED_TTL + 86400  # 31 days
    backend.set("key", b"val", ttl=huge_ttl)
    mock_hash_client.set.assert_called_once_with("key", b"val", expire=MAX_MEMCACHED_TTL)


@pytest.mark.critical
def test_ttl_none_and_zero_mean_no_expiry(mock_hash_client):
    """TTL=None and TTL=0 both pass expire=0 (no expiry)."""
    backend = MemcachedBackend(MemcachedBackendConfig())

    backend.set("k1", b"v1", ttl=None)
    mock_hash_client.set.assert_called_with("k1", b"v1", expire=0)

    backend.set("k2", b"v2", ttl=0)
    mock_hash_client.set.assert_called_with("k2", b"v2", expire=0)


@pytest.mark.critical
def test_error_classification_all_types():
    """classify_memcached_error covers timeout, transient, permanent, unknown."""
    from pymemcache.exceptions import (
        MemcacheClientError,
        MemcacheServerError,
        MemcacheUnexpectedCloseError,
    )

    # Timeout
    err = classify_memcached_error(socket.timeout("timed out"), operation="get")
    assert err.error_type == BackendErrorType.TIMEOUT

    # Transient — connection close
    err = classify_memcached_error(MemcacheUnexpectedCloseError(), operation="get")
    assert err.error_type == BackendErrorType.TRANSIENT

    # Transient — server error
    err = classify_memcached_error(MemcacheServerError("error"), operation="set")
    assert err.error_type == BackendErrorType.TRANSIENT

    # Transient — ConnectionError
    err = classify_memcached_error(ConnectionError("refused"), operation="get")
    assert err.error_type == BackendErrorType.TRANSIENT

    # Permanent — client error
    err = classify_memcached_error(MemcacheClientError("bad key"), operation="set")
    assert err.error_type == BackendErrorType.PERMANENT

    # Unknown — fallback
    err = classify_memcached_error(RuntimeError("weird"), operation="get")
    assert err.error_type == BackendErrorType.UNKNOWN


@pytest.mark.critical
def test_lazy_import_memcached_backend():
    """MemcachedBackend is importable via lazy __getattr__ in backends/__init__."""
    from cachekit.backends import MemcachedBackend as LazyMB

    assert LazyMB is MemcachedBackend


@pytest.mark.critical
def test_lazy_import_unknown_raises():
    """Unknown attribute on backends package raises AttributeError."""
    import cachekit.backends

    with pytest.raises(AttributeError, match="has no attribute"):
        _ = cachekit.backends.NoSuchBackend  # type: ignore[attr-defined]


@pytest.mark.critical
def test_config_validates_port():
    """Config rejects non-numeric and out-of-range ports."""
    from pydantic import ValidationError

    # Empty server list
    with pytest.raises(ValidationError):
        MemcachedBackendConfig(servers=[])

    # No colon (missing port)
    with pytest.raises(ValidationError):
        MemcachedBackendConfig(servers=["localhost"])

    # Non-numeric port
    with pytest.raises(ValidationError):
        MemcachedBackendConfig(servers=["mc1:abc"])

    # Port out of range
    with pytest.raises(ValidationError):
        MemcachedBackendConfig(servers=["mc1:0"])

    with pytest.raises(ValidationError):
        MemcachedBackendConfig(servers=["mc1:70000"])


@pytest.mark.critical
def test_config_from_env():
    """from_env() returns a valid config with defaults."""
    config = MemcachedBackendConfig.from_env()
    assert config.servers == ["127.0.0.1:11211"]
    assert config.connect_timeout == 2.0


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
