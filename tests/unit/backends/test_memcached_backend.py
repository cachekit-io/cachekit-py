"""Unit tests for MemcachedBackend.

Tests for backends/memcached/backend.py covering:
- Protocol compliance with BaseBackend
- Basic operations (get, set, delete, exists)
- TTL behavior and 30-day clamping
- Key prefix application
- Error classification via classify_memcached_error
- Health check responses
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cachekit.backends.base import BaseBackend
from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.backends.memcached.backend import MemcachedBackend
from cachekit.backends.memcached.config import MAX_MEMCACHED_TTL, MemcachedBackendConfig
from cachekit.backends.memcached.error_handler import classify_memcached_error
from tests.utils.memcached_helpers import mock_hash_client as _mock_hash_client


@pytest.fixture
def config() -> MemcachedBackendConfig:
    """Create MemcachedBackendConfig with defaults."""
    return MemcachedBackendConfig()


@pytest.fixture
def mock_hash_client():
    """Patch HashClient and return the mock instance."""
    with patch("pymemcache.client.hash.HashClient") as mock_cls:
        mock_instance = _mock_hash_client()
        mock_cls.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def backend(config: MemcachedBackendConfig, mock_hash_client: MagicMock) -> MemcachedBackend:
    """Create MemcachedBackend with mocked HashClient."""
    return MemcachedBackend(config)


@pytest.mark.unit
class TestProtocolCompliance:
    """Test BaseBackend protocol compliance."""

    def test_implements_base_backend_protocol(self, backend: MemcachedBackend) -> None:
        """Verify MemcachedBackend satisfies BaseBackend protocol."""
        assert isinstance(backend, BaseBackend)

    def test_has_required_methods(self, backend: MemcachedBackend) -> None:
        """Verify all required methods exist and are callable."""
        assert callable(backend.get)
        assert callable(backend.set)
        assert callable(backend.delete)
        assert callable(backend.exists)
        assert callable(backend.health_check)


@pytest.mark.unit
class TestBasicOperations:
    """Test basic get/set/delete/exists operations."""

    def test_get_returns_bytes(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test get returns bytes when key exists."""
        mock_hash_client.get.return_value = b"cached_value"
        result = backend.get("mykey")
        assert result == b"cached_value"
        assert isinstance(result, bytes)

    def test_get_returns_none_for_missing_key(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test get returns None when key does not exist."""
        mock_hash_client.get.return_value = None
        result = backend.get("missing")
        assert result is None

    def test_set_stores_value(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test set calls client.set with correct arguments."""
        backend.set("mykey", b"myvalue", ttl=60)
        mock_hash_client.set.assert_called_once_with("mykey", b"myvalue", expire=60, noreply=False)

    def test_delete_returns_true_when_key_exists(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test delete returns True when key existed."""
        mock_hash_client.delete.return_value = True
        result = backend.delete("mykey")
        assert result is True

    def test_delete_returns_false_when_key_missing(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test delete returns False when key did not exist."""
        mock_hash_client.delete.return_value = False
        result = backend.delete("mykey")
        assert result is False

    def test_delete_passes_noreply_false(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test delete passes noreply=False for synchronous response."""
        mock_hash_client.delete.return_value = True
        backend.delete("mykey")
        mock_hash_client.delete.assert_called_once_with("mykey", noreply=False)

    def test_exists_returns_true_when_key_exists(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test exists returns True when get returns a value."""
        mock_hash_client.get.return_value = b"some_value"
        result = backend.exists("mykey")
        assert result is True

    def test_exists_returns_false_when_key_missing(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test exists returns False when get returns None."""
        mock_hash_client.get.return_value = None
        result = backend.exists("mykey")
        assert result is False

    def test_get_raises_backend_error_on_failure(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test get wraps exceptions in BackendError."""
        from cachekit.backends.errors import BackendError

        mock_hash_client.get.side_effect = ConnectionError("refused")
        with pytest.raises(BackendError) as exc_info:
            backend.get("key")
        assert exc_info.value.error_type == BackendErrorType.TRANSIENT

    def test_set_raises_backend_error_on_failure(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test set wraps exceptions in BackendError."""
        from cachekit.backends.errors import BackendError

        mock_hash_client.set.side_effect = ConnectionError("refused")
        with pytest.raises(BackendError) as exc_info:
            backend.set("key", b"val", ttl=60)
        assert exc_info.value.error_type == BackendErrorType.TRANSIENT

    def test_delete_raises_backend_error_on_failure(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test delete wraps exceptions in BackendError."""
        from cachekit.backends.errors import BackendError

        mock_hash_client.delete.side_effect = ConnectionError("refused")
        with pytest.raises(BackendError) as exc_info:
            backend.delete("key")
        assert exc_info.value.error_type == BackendErrorType.TRANSIENT

    def test_exists_raises_backend_error_on_failure(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test exists wraps exceptions in BackendError."""
        from cachekit.backends.errors import BackendError

        mock_hash_client.get.side_effect = ConnectionError("refused")
        with pytest.raises(BackendError) as exc_info:
            backend.exists("key")
        assert exc_info.value.error_type == BackendErrorType.TRANSIENT


@pytest.mark.unit
class TestTTLBehavior:
    """Test TTL handling and Memcached's 30-day maximum."""

    def test_ttl_none_passes_expire_zero(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test ttl=None passes expire=0 (no expiry)."""
        backend.set("key", b"val", ttl=None)
        mock_hash_client.set.assert_called_once_with("key", b"val", expire=0, noreply=False)

    def test_ttl_zero_passes_expire_zero(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test ttl=0 passes expire=0 (no expiry)."""
        backend.set("key", b"val", ttl=0)
        mock_hash_client.set.assert_called_once_with("key", b"val", expire=0, noreply=False)

    def test_ttl_positive_passes_expire(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test ttl=100 passes expire=100."""
        backend.set("key", b"val", ttl=100)
        mock_hash_client.set.assert_called_once_with("key", b"val", expire=100, noreply=False)

    def test_ttl_exceeding_30_days_gets_clamped(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test TTL > 30 days gets clamped to MAX_MEMCACHED_TTL (2592000)."""
        huge_ttl = MAX_MEMCACHED_TTL + 1000
        backend.set("key", b"val", ttl=huge_ttl)
        mock_hash_client.set.assert_called_once_with("key", b"val", expire=MAX_MEMCACHED_TTL, noreply=False)

    def test_ttl_exactly_30_days_not_clamped(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test TTL exactly at 30-day max passes through unchanged."""
        backend.set("key", b"val", ttl=MAX_MEMCACHED_TTL)
        mock_hash_client.set.assert_called_once_with("key", b"val", expire=MAX_MEMCACHED_TTL, noreply=False)

    def test_negative_ttl_passes_expire_zero(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test negative TTL is treated as no expiry."""
        backend.set("key", b"val", ttl=-5)
        mock_hash_client.set.assert_called_once_with("key", b"val", expire=0, noreply=False)


@pytest.mark.unit
class TestKeyPrefix:
    """Test key prefix application to all operations."""

    @pytest.fixture
    def prefixed_config(self) -> MemcachedBackendConfig:
        """Config with key_prefix set."""
        return MemcachedBackendConfig(key_prefix="app:")

    @pytest.fixture
    def prefixed_backend(self, prefixed_config: MemcachedBackendConfig, mock_hash_client: MagicMock) -> MemcachedBackend:
        """Backend with key prefix configured."""
        return MemcachedBackend(prefixed_config)

    def test_get_applies_prefix(self, prefixed_backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test get prepends prefix to key."""
        mock_hash_client.get.return_value = None
        prefixed_backend.get("mykey")
        mock_hash_client.get.assert_called_once_with("app:mykey")

    def test_set_applies_prefix(self, prefixed_backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test set prepends prefix to key."""
        prefixed_backend.set("mykey", b"val", ttl=60)
        mock_hash_client.set.assert_called_once_with("app:mykey", b"val", expire=60, noreply=False)

    def test_delete_applies_prefix(self, prefixed_backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test delete prepends prefix to key."""
        mock_hash_client.delete.return_value = True
        prefixed_backend.delete("mykey")
        mock_hash_client.delete.assert_called_once_with("app:mykey", noreply=False)

    def test_exists_applies_prefix(self, prefixed_backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test exists prepends prefix to key."""
        mock_hash_client.get.return_value = None
        prefixed_backend.exists("mykey")
        mock_hash_client.get.assert_called_once_with("app:mykey")

    def test_no_prefix_when_empty(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test no prefix applied when key_prefix is empty."""
        mock_hash_client.get.return_value = None
        backend.get("mykey")
        mock_hash_client.get.assert_called_once_with("mykey")


@pytest.mark.unit
class TestErrorClassification:
    """Test classify_memcached_error maps pymemcache exceptions correctly."""

    def test_timeout_error_maps_to_timeout(self) -> None:
        """Test TimeoutError is classified as TIMEOUT.

        socket.timeout is an alias of TimeoutError on Python >=3.10, so this
        single test covers both; a separate socket.timeout case would be a
        byte-for-byte duplicate.
        """
        exc = TimeoutError("operation timed out")
        error = classify_memcached_error(exc, operation="set", key="k2")
        assert error.error_type == BackendErrorType.TIMEOUT

    def test_unexpected_close_maps_to_transient(self) -> None:
        """Test MemcacheUnexpectedCloseError is classified as TRANSIENT."""
        from pymemcache.exceptions import MemcacheUnexpectedCloseError

        exc = MemcacheUnexpectedCloseError()
        error = classify_memcached_error(exc, operation="get", key="k3")
        assert error.error_type == BackendErrorType.TRANSIENT

    def test_server_error_maps_to_transient(self) -> None:
        """Test MemcacheServerError is classified as TRANSIENT."""
        from pymemcache.exceptions import MemcacheServerError

        exc = MemcacheServerError("SERVER_ERROR out of memory")
        error = classify_memcached_error(exc, operation="set")
        assert error.error_type == BackendErrorType.TRANSIENT

    def test_connection_error_maps_to_transient(self) -> None:
        """Test ConnectionError is classified as TRANSIENT."""
        exc = ConnectionError("Connection refused")
        error = classify_memcached_error(exc, operation="get")
        assert error.error_type == BackendErrorType.TRANSIENT

    def test_os_error_maps_to_transient(self) -> None:
        """Test OSError is classified as TRANSIENT."""
        exc = OSError("Network unreachable")
        error = classify_memcached_error(exc, operation="get")
        assert error.error_type == BackendErrorType.TRANSIENT

    def test_illegal_input_maps_to_permanent(self) -> None:
        """Test MemcacheIllegalInputError is classified as PERMANENT."""
        from pymemcache.exceptions import MemcacheIllegalInputError

        exc = MemcacheIllegalInputError("Key too long")
        error = classify_memcached_error(exc, operation="set", key="k4")
        assert error.error_type == BackendErrorType.PERMANENT

    def test_client_error_maps_to_permanent(self) -> None:
        """Test MemcacheClientError is classified as PERMANENT."""
        from pymemcache.exceptions import MemcacheClientError

        exc = MemcacheClientError("CLIENT_ERROR bad data")
        error = classify_memcached_error(exc, operation="set")
        assert error.error_type == BackendErrorType.PERMANENT

    def test_unknown_exception_maps_to_unknown(self) -> None:
        """Test unrecognized exception is classified as UNKNOWN."""
        exc = RuntimeError("something unexpected")
        error = classify_memcached_error(exc, operation="get", key="k5")
        assert error.error_type == BackendErrorType.UNKNOWN

    def test_error_preserves_operation(self) -> None:
        """Test that operation context is preserved in BackendError."""
        exc = RuntimeError("fail")
        error = classify_memcached_error(exc, operation="delete", key="k6")
        assert error.operation == "delete"
        assert error.key == "k6"

    def test_error_preserves_original_exception(self) -> None:
        """Test that original exception is preserved in BackendError."""
        exc = RuntimeError("original")
        error = classify_memcached_error(exc, operation="get")
        assert error.original_exception is exc


@pytest.mark.unit
class TestHealthCheck:
    """Test health_check method."""

    def test_healthy_returns_true_with_details(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test health_check returns (True, details) when server responds."""
        mock_hash_client.get.return_value = None  # health probe uses get()
        is_healthy, details = backend.health_check()

        assert is_healthy is True
        assert details["backend_type"] == "memcached"
        assert "latency_ms" in details
        assert isinstance(details["latency_ms"], float)
        assert details["configured_servers"] == 1

    def test_unhealthy_on_exception(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test health_check returns (False, details) on exception."""
        mock_hash_client.get.side_effect = ConnectionError("Connection refused")
        is_healthy, details = backend.health_check()

        assert is_healthy is False
        assert details["backend_type"] == "memcached"
        assert "latency_ms" in details
        assert isinstance(details["latency_ms"], float)
        assert "error" in details
        assert details["configured_servers"] == 1


@pytest.mark.unit
class TestLazyImport:
    """Test __getattr__ lazy import in backends/__init__.py."""

    def test_lazy_import_memcached_backend(self) -> None:
        """Test MemcachedBackend can be imported via lazy __getattr__."""
        from cachekit.backends import MemcachedBackend

        assert MemcachedBackend is not None
        assert callable(MemcachedBackend)

    def test_lazy_import_unknown_raises_attribute_error(self) -> None:
        """Test unknown attribute raises AttributeError."""
        import cachekit.backends

        with pytest.raises(AttributeError, match="has no attribute"):
            _ = cachekit.backends.NoSuchBackend  # type: ignore[attr-defined]


class _FakeServer:
    """Stands in for one server's pymemcache client inside a real HashClient."""

    def __init__(self, server: object) -> None:
        self.server = server
        self.sends: list[tuple[list[str], object]] = []  # delete_many sends
        self.commands: list[tuple[str, str]] = []  # single-key commands that reached the server
        self.store: dict[str, bytes] = {}
        self.error: Exception | None = None

    def _send(self, cmd: str, key: str) -> None:
        self.commands.append((cmd, key))
        if self.error is not None:
            raise self.error

    def delete_many(self, keys: list[str], noreply: object = None) -> bool:
        self.sends.append((list(keys), noreply))
        if self.error is not None:
            raise self.error
        for key in keys:
            self.store.pop(key, None)
        return True

    def get(self, key: str, default: object = None) -> object:
        self._send("get", key)
        return self.store.get(key, default)

    def set(self, key: str, value: bytes, expire: int = 0, noreply: object = None) -> bool:
        self._send("set", key)
        self.store[key] = value
        return True

    def delete(self, key: str, noreply: object = None) -> bool:
        self._send("delete", key)
        return self.store.pop(key, None) is not None

    def touch(self, key: str, expire: int = 0, noreply: object = None) -> bool:
        self._send("touch", key)
        return key in self.store


def _fake_backend(servers: int = 2, key_prefix: str = "") -> tuple[MemcachedBackend, dict[object, _FakeServer]]:
    """A MemcachedBackend on a real HashClient (real routing and retry handling), fake servers."""
    cfg = MemcachedBackendConfig(servers=[f"127.0.0.1:{21000 + i}" for i in range(servers)], key_prefix=key_prefix)
    backend = MemcachedBackend(cfg)
    fakes = {name: _FakeServer(client.server) for name, client in backend._client.clients.items()}
    backend._client.clients.update(fakes)
    return backend, fakes


def _open_retry_window(backend: MemcachedBackend, fake: _FakeServer) -> None:
    """Fail one command on fake's server, so HashClient skips that server until retry_timeout passes."""
    fake.error = OSError("connection refused")
    with pytest.raises(BackendError):
        backend.delete("__open_window__")
    fake.error = None
    fake.commands.clear()
    assert fake.server in backend._client._failed_clients


def _close_retry_window(backend: MemcachedBackend) -> None:
    """Age every failure past retry_timeout, as if the window had expired."""
    for meta in backend._client._failed_clients.values():
        meta["failed_time"] -= backend._client.retry_timeout + 1


@pytest.mark.unit
class TestDeleteMany:
    """_delete_many against a real HashClient (real routing and retry handling), fake servers."""

    _backend = staticmethod(_fake_backend)

    def test_one_acknowledged_send_per_server(self) -> None:
        backend, fakes = self._backend()
        keys = [f"k{i}" for i in range(200)]

        assert backend._delete_many(keys) == set()

        sends = [s for f in fakes.values() for s in f.sends]
        assert len(sends) == 2  # both servers own some keys; one send each
        assert all(noreply is False for _, noreply in sends)
        assert sorted(k for ks, _ in sends for k in ks) == sorted(keys)
        for name, fake in fakes.items():
            for sent, _ in fake.sends:
                assert all(backend._client.hasher.get_node(k) == name for k in sent)

    def test_sends_are_capped(self) -> None:
        backend, fakes = self._backend(servers=1)
        backend._delete_many([f"k{i}" for i in range(2500)])
        (fake,) = fakes.values()
        assert [len(ks) for ks, _ in fake.sends] == [1000, 1000, 500]

    def test_prefix_applied_and_failures_reported_raw(self) -> None:
        backend, fakes = self._backend(servers=1, key_prefix="app:")
        (fake,) = fakes.values()
        fake.error = OSError("connection reset")

        assert backend._delete_many(["a", "b"]) == {"a", "b"}
        assert fake.sends[0][0] == ["app:a", "app:b"]

    def test_failing_server_fails_only_its_keys(self) -> None:
        backend, fakes = self._backend()
        keys = [f"k{i}" for i in range(200)]
        bad_name, bad = next(iter(fakes.items()))
        bad.error = OSError("connection reset")

        failed = backend._delete_many(keys)

        assert failed and failed == {k for k in keys if backend._client.hasher.get_node(k) == bad_name}

    def test_server_in_retry_window_is_not_counted_deleted(self) -> None:
        backend, fakes = self._backend(servers=1)
        ((name, fake),) = fakes.items()
        fake.error = OSError("down")
        assert backend._delete_many(["a"]) == {"a"}  # marks the server failed
        fake.error = None
        fake.sends.clear()

        # Within retry_timeout HashClient skips the server and returns its default: not an ack.
        assert backend._delete_many(["a", "b"]) == {"a", "b"}
        assert fake.sends == []

    def test_invalid_key_fails_alone(self) -> None:
        backend, fakes = self._backend(servers=1)
        assert backend._delete_many(["good", "has space"]) == {"has space"}
        (fake,) = fakes.values()
        assert fake.sends[0][0] == ["good"]

    def test_empty(self) -> None:
        backend, fakes = self._backend()
        assert backend._delete_many([]) == set()
        assert all(f.sends == [] for f in fakes.values())

    @pytest.mark.parametrize("private", ["_get_client", "_safely_run_func"])
    def test_api_drift_raises_instead_of_failing_keys(self, private: str) -> None:
        """A renamed HashClient internal must raise, so the sweep falls back to per-key deletes."""
        backend, _ = self._backend(servers=1)
        with patch.object(backend._client, private, side_effect=AttributeError(private)):
            with pytest.raises(AttributeError):
                backend._delete_many(["a"])

    def test_api_drift_sweep_still_erases_every_key(self) -> None:
        """End to end: with a broken internal, a no-args sweep deletes through public delete()."""
        from cachekit import cache

        backend, _ = self._backend(servers=1)
        store: dict[str, bytes] = {}
        with (
            patch.object(backend, "get", side_effect=lambda k: store.get(k)),
            patch.object(backend, "set", side_effect=lambda k, v, ttl=None: store.__setitem__(k, v)),
            patch.object(MemcachedBackend, "delete", autospec=True, side_effect=lambda self, k: store.pop(k, None) is not None),
            patch.object(backend._client, "_safely_run_func", side_effect=AttributeError("renamed")),
        ):

            @cache(backend=backend, ttl=60, namespace="mc_api_drift")
            def f(x: int) -> int:
                return x

            for i in range(5):
                f(i)
            assert len(store) == 5
            f.invalidate_cache()
            assert store == {}


@pytest.mark.unit
class TestRetryWindowSkip:
    """A command HashClient skips for a server in its retry window never reports an outcome."""

    @pytest.mark.parametrize(
        "call",
        [
            pytest.param(lambda b: b.delete("k"), id="delete"),
            pytest.param(lambda b: b.set("k", b"v"), id="set"),
            pytest.param(lambda b: b.exists("k"), id="exists"),
        ],
    )
    def test_skipped_command_raises_transient(self, call) -> None:
        backend, fakes = _fake_backend(servers=1)
        (fake,) = fakes.values()
        fake.store["k"] = b"old"
        _open_retry_window(backend, fake)

        with pytest.raises(BackendError) as exc_info:
            call(backend)

        assert exc_info.value.error_type == BackendErrorType.TRANSIENT
        assert "retry window" in str(exc_info.value)
        assert fake.commands == []  # nothing sent: the old value is still there
        assert fake.store == {"k": b"old"}

    async def test_skipped_refresh_ttl_raises_transient(self) -> None:
        backend, fakes = _fake_backend(servers=1)
        (fake,) = fakes.values()
        fake.store["k"] = b"v"
        _open_retry_window(backend, fake)

        with pytest.raises(BackendError) as exc_info:
            await backend.refresh_ttl("k", 60)

        assert exc_info.value.error_type == BackendErrorType.TRANSIENT
        assert fake.commands == []

    def test_skipped_health_probe_is_unhealthy(self) -> None:
        backend, fakes = _fake_backend(servers=1)
        (fake,) = fakes.values()
        _open_retry_window(backend, fake)

        is_healthy, details = backend.health_check()

        assert is_healthy is False
        assert "retry window" in details["error"]
        assert fake.commands == []

    def test_skipped_get_reads_as_miss(self) -> None:
        """Deliberate: a miss on a read only makes the caller recompute."""
        backend, fakes = _fake_backend(servers=1)
        (fake,) = fakes.values()
        fake.store["k"] = b"v"
        _open_retry_window(backend, fake)

        assert backend.get("k") is None
        assert fake.commands == []

    def test_real_miss_still_returns_false(self) -> None:
        backend, fakes = _fake_backend(servers=1, key_prefix="app:")
        (fake,) = fakes.values()

        assert backend.delete("absent") is False
        assert backend.exists("absent") is False
        assert fake.commands == [("delete", "app:absent"), ("get", "app:absent")]

    def test_commands_reach_the_server_once_the_window_expires(self) -> None:
        backend, fakes = _fake_backend(servers=1)
        (fake,) = fakes.values()
        fake.store["k"] = b"v"
        _open_retry_window(backend, fake)
        _close_retry_window(backend)

        assert backend.delete("k") is True
        assert backend.delete("absent") is False
        assert fake.commands == [("delete", "k"), ("delete", "absent")]

    def test_server_errors_are_still_classified(self) -> None:
        backend, fakes = _fake_backend(servers=1)
        (fake,) = fakes.values()
        fake.error = TimeoutError("timed out")

        with pytest.raises(BackendError) as exc_info:
            backend.set("k", b"v")

        assert exc_info.value.error_type == BackendErrorType.TIMEOUT

    def test_api_drift_falls_back_to_the_public_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A renamed _safely_run_func must not stop the real per-key delete reaching the server."""
        from pymemcache.client.hash import HashClient

        # Simulate a pymemcache release that renamed the internal: HashClient's own commands
        # still work, but nothing answers to the old name.
        renamed = HashClient._safely_run_func
        monkeypatch.delattr(HashClient, "_safely_run_func")

        def run_cmd(self, cmd, key, default_val, *args, **kwargs):
            client = self._get_client(key)
            return renamed(self, client, getattr(client, cmd), default_val, key, *args, **kwargs)

        monkeypatch.setattr(HashClient, "_run_cmd", run_cmd)
        backend, fakes = _fake_backend(servers=1)
        (fake,) = fakes.values()
        fake.store["k"] = b"v"

        assert backend.delete("k") is True
        assert fake.commands == [("delete", "k")]


@pytest.mark.unit
class TestRetryWindowInvalidation:
    """Invalidation of a key whose server is in the retry window keeps the key tracked."""

    @staticmethod
    def _cached(namespace: str):
        from cachekit import cache

        backend, fakes = _fake_backend(servers=1)
        (fake,) = fakes.values()

        @cache(backend=backend, ttl=60, namespace=namespace)
        def f(x: int) -> int:
            return x

        for i in range(3):
            f(i)
        assert len(fake.store) == 3
        return backend, fake, f

    def test_sweep_per_key_fallback_keeps_skipped_keys_tracked(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        backend, fake, f = self._cached("mc_skip_sweep")
        _open_retry_window(backend, fake)

        # Force the sweep onto its per-key fallback, the path that trusted a skipped delete.
        with patch.object(backend, "_delete_many", side_effect=AttributeError("renamed")):
            with caplog.at_level(logging.DEBUG, logger="cachekit"):
                f.invalidate_cache()

        assert len(fake.store) == 3
        assert any("Failed to delete 3 L2 key(s)" in r.getMessage() for r in caplog.records)

        _close_retry_window(backend)
        f.invalidate_cache()  # the next sweep retries every key it kept
        assert fake.store == {}

    def test_single_key_invalidation_keeps_skipped_key_tracked(self) -> None:
        backend, fake, f = self._cached("mc_skip_single")
        _open_retry_window(backend, fake)

        f.invalidate_cache(0)
        assert len(fake.store) == 3  # skipped: nothing deleted

        _close_retry_window(backend)
        with patch.object(backend, "_delete_many", side_effect=AttributeError("renamed")):
            f.invalidate_cache()  # per-key sweep: reaches key 0 only if it was re-tracked
        assert fake.store == {}
