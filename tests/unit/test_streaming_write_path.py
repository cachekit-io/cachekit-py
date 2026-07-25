"""LAB-766 streaming write-path wiring (write-side twin of test_mmap_read_path.py).

Four units compose the streaming Arrow write path:
- ``CacheSerializationHandler.supports_streaming_write()`` — eligibility (plaintext Arrow).
- ``CacheSerializationHandler.write_serialized_to()`` — streams the SAME envelope bytes
  ``serialize_data`` produces (frame prefix, then [checksum][IPC]) into a seekable sink.
- ``StandardCacheHandler.set_streaming(_async)`` — delegates to a backend that exposes
  ``set_streaming``; returns None (fall back to buffered set) when the backend doesn't.
- ``CacheOperationHandler.store_result(_async)`` — when eligible AND the backend streams,
  writes via the callback and returns None so the multi-GB envelope never reaches L1;
  otherwise the buffered path runs unchanged and still returns bytes for L1.

FileBackend.set_streaming itself (atomicity, TTL, size limits) is covered here too — it is
the first and load-bearing BufferWritableBackend implementation.
"""

from __future__ import annotations

import io
import os
from unittest.mock import MagicMock

import pytest

from cachekit.backends.errors import BackendError
from cachekit.cache_handler import (
    CacheOperationHandler,
    CacheSerializationHandler,
    StandardCacheHandler,
    supports_streaming_write,
)
from cachekit.key_generator import CacheKeyGenerator

pd = pytest.importorskip("pandas")
pytest.importorskip("pyarrow")


@pytest.fixture
def df() -> pd.DataFrame:
    return pd.DataFrame({"a": range(1000), "b": [float(i) for i in range(1000)]})


@pytest.fixture
def file_backend(tmp_path):
    from cachekit.backends.file import FileBackend
    from cachekit.backends.file.config import FileBackendConfig

    return FileBackend(FileBackendConfig(cache_dir=str(tmp_path), max_size_mb=64, max_value_mb=32))


def _operation_handler(backend) -> CacheOperationHandler:
    handler = CacheOperationHandler(CacheSerializationHandler(serializer_name="arrow"), CacheKeyGenerator())
    handler.set_cache_handler(StandardCacheHandler(backend))
    return handler


class _PlainBackend:
    """Minimal BaseBackend without set_streaming (the fallback target)."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl=None):
        self.store[key] = bytes(value)

    def delete(self, key):
        return self.store.pop(key, None) is not None

    def exists(self, key):
        return key in self.store

    def health_check(self):
        return True, {}


@pytest.mark.unit
class TestSupportsStreamingWrite:
    def test_arrow_plaintext_is_eligible(self) -> None:
        sh = CacheSerializationHandler(serializer_name="arrow")
        assert sh.supports_streaming_write() is True

    def test_default_serializer_not_eligible(self) -> None:
        sh = CacheSerializationHandler(serializer_name="default")
        assert sh.supports_streaming_write() is False

    def test_encrypted_arrow_not_eligible(self) -> None:
        """AES-256-GCM emits its tag only after the whole ciphertext — the secure path stays buffered."""
        sh = CacheSerializationHandler(serializer_name="arrow")
        sh.encryption = True
        assert sh.supports_streaming_write() is False

    def test_backend_type_guard(self, file_backend) -> None:
        assert supports_streaming_write(file_backend) is True
        assert supports_streaming_write(_PlainBackend()) is False


@pytest.mark.unit
class TestWriteSerializedTo:
    def test_envelope_byte_identical_to_buffered(self, df) -> None:
        """The streamed frame must be indistinguishable from serialize_data's output —
        same prefix, same [checksum][IPC] payload — so every existing read path Just Works."""
        sh = CacheSerializationHandler(serializer_name="arrow")
        buffered = sh.serialize_data(df, cache_key="k")
        sink = io.BytesIO()
        sh.write_serialized_to(sink, df)
        assert sink.getvalue() == buffered

    def test_streamed_envelope_deserializes(self, df) -> None:
        sh = CacheSerializationHandler(serializer_name="arrow")
        sink = io.BytesIO()
        sh.write_serialized_to(sink, df)
        out = sh.deserialize_data(sink.getvalue(), cache_key="k")
        pd.testing.assert_frame_equal(out, df)

    def test_max_value_size_budget_enforced_mid_stream(self, df, monkeypatch) -> None:
        """The L2 oversized-entry ceiling (issue #163) applies to streamed writes too,
        and aborts mid-stream instead of after materializing."""
        from cachekit.config.singleton import reset_settings

        monkeypatch.setenv("CACHEKIT_MAX_VALUE_SIZE", "1024")
        reset_settings()
        try:
            sh = CacheSerializationHandler(serializer_name="arrow")
            with pytest.raises(ValueError, match="budget"):
                sh.write_serialized_to(io.BytesIO(), df)
        finally:
            reset_settings()

    def test_non_dataframe_raises_type_error(self) -> None:
        sh = CacheSerializationHandler(serializer_name="arrow")
        with pytest.raises(TypeError, match="only supports DataFrames"):
            sh.write_serialized_to(io.BytesIO(), 42)


@pytest.mark.unit
class TestStandardCacheHandlerSetStreaming:
    def test_returns_none_when_backend_lacks_set_streaming(self) -> None:
        ch = StandardCacheHandler(_PlainBackend())  # type: ignore[arg-type]
        assert ch.set_streaming("k", lambda sink: None) is None

    def test_delegates_to_backend_when_supported(self) -> None:
        backend = MagicMock()
        ch = StandardCacheHandler(backend)
        cb = MagicMock()
        assert ch.set_streaming("k", cb, ttl=60) is True
        backend.set_streaming.assert_called_once_with("k", cb, 60)

    def test_returns_false_on_backend_error(self) -> None:
        backend = MagicMock()
        backend.set_streaming.side_effect = BackendError("boom")
        ch = StandardCacheHandler(backend)
        assert ch.set_streaming("k", lambda sink: None) is False

    def test_returns_false_on_producer_error(self) -> None:
        """A serialization failure inside the callback degrades to False — never raises
        out of the cache write (a cache must not break the wrapped function)."""
        backend = MagicMock()
        backend.set_streaming.side_effect = ValueError("payload budget")
        ch = StandardCacheHandler(backend)
        assert ch.set_streaming("k", lambda sink: None) is False

    @pytest.mark.asyncio
    async def test_async_returns_none_when_unsupported(self) -> None:
        ch = StandardCacheHandler(_PlainBackend())  # type: ignore[arg-type]
        assert await ch.set_streaming_async("k", lambda sink: None) is None


@pytest.mark.unit
class TestStoreResultRouting:
    def test_streams_to_file_backend_and_skips_l1(self, df, file_backend) -> None:
        """Streaming success returns None: the envelope must never be copied into L1
        (mirrors the mmap read path's L1 exclusion, #171)."""
        op = _operation_handler(file_backend)
        assert op.store_result("k1", df, ttl=60) is None
        hit = op.get_cached_value("k1")
        assert hit is not None
        pd.testing.assert_frame_equal(hit[1], df)

    def test_streamed_bytes_identical_to_buffered_set(self, df, file_backend) -> None:
        op = _operation_handler(file_backend)
        op.store_result("k1", df, ttl=60)
        stored = file_backend.get("k1")
        assert stored == op.serialization_handler.serialize_data(df, cache_key="k1")

    def test_falls_back_to_buffered_set_and_returns_bytes_for_l1(self, df) -> None:
        backend = _PlainBackend()
        op = _operation_handler(backend)
        ret = op.store_result("k1", df, ttl=60)
        assert ret is not None  # L1 backfill contract unchanged on the buffered path
        assert backend.store["k1"] == ret

    def test_non_arrow_serializer_stays_buffered(self, file_backend) -> None:
        op = CacheOperationHandler(CacheSerializationHandler(serializer_name="default"), CacheKeyGenerator())
        op.set_cache_handler(StandardCacheHandler(file_backend))
        ret = op.store_result("k2", {"a": 1}, ttl=60)
        assert ret is not None
        assert op.get_cached_value("k2")[1] == {"a": 1}

    def test_stale_ttl_stays_buffered(self, df, file_backend, monkeypatch) -> None:
        """SWR writes carry stale_ttl, which set_streaming has no channel for — buffered path."""
        op = _operation_handler(file_backend)
        called = {"streaming": False}
        original = file_backend.set_streaming
        monkeypatch.setattr(
            file_backend, "set_streaming", lambda *a, **k: called.__setitem__("streaming", True) or original(*a, **k)
        )
        ret = op.store_result("k3", df, ttl=60, stale_ttl=30)
        assert called["streaming"] is False
        assert ret is not None  # buffered path returns bytes

    def test_streaming_failure_does_not_retry_buffered(self, df, file_backend, monkeypatch) -> None:
        """A failed stream must NOT re-materialize the payload via set(bytes)."""
        op = _operation_handler(file_backend)

        def explode(key, cb, ttl=None):
            raise BackendError("disk full")

        monkeypatch.setattr(file_backend, "set_streaming", explode)
        set_spy = MagicMock(wraps=file_backend.set)
        monkeypatch.setattr(file_backend, "set", set_spy)
        assert op.store_result("k4", df, ttl=60) is None
        set_spy.assert_not_called()
        assert file_backend.get("k4") is None

    @pytest.mark.asyncio
    async def test_async_streams_and_roundtrips(self, df, file_backend) -> None:
        op = _operation_handler(file_backend)
        assert await op.store_result_async("k5", df, ttl=60) is None
        hit = await op.get_cached_value_async("k5")
        assert hit is not None
        pd.testing.assert_frame_equal(hit[1], df)


@pytest.mark.unit
class TestFileBackendSetStreaming:
    def test_roundtrip_matches_set(self, file_backend) -> None:
        payload = os.urandom(64 * 1024)
        file_backend.set_streaming("k", lambda f: f.write(payload), ttl=60)
        assert file_backend.get("k") == payload

    def test_seek_patch_within_payload(self, file_backend) -> None:
        """Producers patch already-written bytes via tell()-anchored seeks (the checksum
        pattern); the backend's 14-byte header must be untouched by it."""

        def writer(f) -> None:
            start = f.tell()
            f.write(b"\x00" * 8)
            f.write(b"body-bytes")
            end = f.tell()
            f.seek(start)
            f.write(b"CHECKSUM")
            f.seek(end)

        file_backend.set_streaming("k", writer, ttl=60)
        assert file_backend.get("k") == b"CHECKSUM" + b"body-bytes"

    def test_ttl_zero_and_none_mean_no_expiry(self, file_backend) -> None:
        file_backend.set_streaming("k", lambda f: f.write(b"x"), ttl=None)
        assert file_backend.get("k") == b"x"

    def test_invalid_ttl_rejected_before_writing(self, file_backend, tmp_path) -> None:
        with pytest.raises(BackendError, match="out of range"):
            file_backend.set_streaming("k", lambda f: f.write(b"x"), ttl=-1)
        assert file_backend.get("k") is None
        assert not [f for f in os.listdir(tmp_path) if ".tmp." in f]

    def test_producer_exception_discards_partial_and_keeps_old_value(self, file_backend, tmp_path) -> None:
        class BoomError(Exception):
            pass

        file_backend.set("k", b"old", ttl=60)

        def exploding(f) -> None:
            f.write(b"partial")
            raise BoomError("mid-stream")

        with pytest.raises(BoomError):  # unwrapped, per BufferWritableBackend contract
            file_backend.set_streaming("k", exploding, ttl=60)
        assert file_backend.get("k") == b"old"
        assert not [f for f in os.listdir(tmp_path) if ".tmp." in f]

    def test_max_value_mb_enforced(self, tmp_path) -> None:
        from cachekit.backends.file import FileBackend
        from cachekit.backends.file.config import FileBackendConfig

        backend = FileBackend(FileBackendConfig(cache_dir=str(tmp_path), max_size_mb=4, max_value_mb=1))
        oversized = b"x" * (1024 * 1024 + 1)
        with pytest.raises(BackendError, match="max_value_mb"):
            backend.set_streaming("k", lambda f: f.write(oversized), ttl=60)
        assert backend.get("k") is None
        assert not [f for f in os.listdir(tmp_path) if ".tmp." in f]

    def test_mmap_read_path_reads_streamed_entry(self, tmp_path) -> None:
        """Write-side streaming and read-side mmap (#171) compose: an uncompressed Arrow
        entry streamed to disk is served back through get_buffer zero-copy."""
        from cachekit.backends.file import FileBackend
        from cachekit.backends.file.config import FileBackendConfig
        from cachekit.serializers.arrow_serializer import ArrowSerializer

        backend = FileBackend(FileBackendConfig(cache_dir=str(tmp_path), max_size_mb=64, max_value_mb=32))
        sh = CacheSerializationHandler(serializer_name="arrow")
        sh._base_serializer = ArrowSerializer(compression=None)  # mmap needs plaintext, uncompressed
        frame = pd.DataFrame({"a": range(100)})

        backend.set_streaming("k", lambda sink: sh.write_serialized_to(sink, frame), ttl=60)
        handle = backend.get_buffer("k")
        assert handle is not None
        try:
            out = sh.deserialize_data(handle.view, cache_key="k")
        finally:
            handle.close()
        pd.testing.assert_frame_equal(out, frame)
