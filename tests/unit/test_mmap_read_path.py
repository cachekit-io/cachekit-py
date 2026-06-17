"""#171 mmap read-path wiring.

Three units compose the zero-copy Arrow read fast path:
- ``CacheSerializationHandler.supports_mmap_read()`` — eligibility (plaintext Arrow -> pandas).
- ``StandardCacheHandler.get_buffer()`` — delegates to a backend that exposes ``get_buffer``.
- ``CacheOperationHandler.get_cached_value(_async)`` — when eligible, reads via the mmap handle,
  deserializes over the view, and closes the handle in a ``finally``. Crucially the mmap NEVER
  becomes the value the decorator holds (it returns the deserialized object), so it can't reach
  ``_l1_cache.put`` (blocker C).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cachekit.cache_handler import (
    CacheOperationHandler,
    CacheSerializationHandler,
    StandardCacheHandler,
)


@pytest.mark.unit
class TestSupportsMmapRead:
    def test_arrow_pandas_plaintext_is_eligible(self) -> None:
        sh = CacheSerializationHandler(serializer_name="arrow")
        assert sh.supports_mmap_read() is True

    def test_default_serializer_not_eligible(self) -> None:
        sh = CacheSerializationHandler(serializer_name="default")
        assert sh.supports_mmap_read() is False

    def test_encrypted_arrow_not_eligible(self) -> None:
        """Encrypted values can never mmap — AES-GCM decrypt owns its buffer."""
        sh = CacheSerializationHandler(serializer_name="arrow")
        sh.encryption = True
        assert sh.supports_mmap_read() is False

    def test_arrow_return_format_not_eligible(self) -> None:
        """A pyarrow.Table aliases the mmap; closing the handle would be a use-after-free. Pandas only."""
        from cachekit.serializers.arrow_serializer import ArrowSerializer

        sh = CacheSerializationHandler(serializer_name="arrow")
        # Swap in a throwaway serializer rather than mutating the process-wide cached instance, so
        # this test can never leak an "arrow" return_format into another test (no shared state).
        sh._base_serializer = ArrowSerializer(return_format="arrow")
        assert sh.supports_mmap_read() is False


@pytest.mark.unit
class TestStandardCacheHandlerGetBuffer:
    def test_delegates_to_backend_when_supported(self) -> None:
        backend = MagicMock()
        handle = object()
        backend.get_buffer.return_value = handle
        ch = StandardCacheHandler(backend)
        assert ch.get_buffer("k") is handle
        backend.get_buffer.assert_called_once_with("k")

    def test_returns_none_when_backend_lacks_get_buffer(self) -> None:
        class NoBufferBackend:
            def get(self, key, refresh_ttl=None):
                return None

        ch = StandardCacheHandler(NoBufferBackend())  # type: ignore[arg-type]
        assert ch.get_buffer("k") is None

    def test_returns_none_on_backend_error(self) -> None:
        """A backend error during get_buffer must degrade to None so the caller falls back to get()."""
        from cachekit.backends.errors import BackendError

        backend = MagicMock()
        backend.get_buffer.side_effect = BackendError("boom")
        ch = StandardCacheHandler(backend)
        assert ch.get_buffer("k") is None

    def test_returns_none_on_unexpected_error(self) -> None:
        """A non-BackendError exception must also degrade to None (mirrors get()'s catch-all)."""
        backend = MagicMock()
        backend.get_buffer.side_effect = RuntimeError("unexpected")
        ch = StandardCacheHandler(backend)
        assert ch.get_buffer("k") is None


@pytest.mark.unit
class TestGetCachedValueMmapBranch:
    @staticmethod
    def _handler(sh: MagicMock, ch: MagicMock) -> CacheOperationHandler:
        return CacheOperationHandler(sh, MagicMock(), cache_handler=ch)

    def test_eligible_reads_via_mmap_and_confines_the_handle(self) -> None:
        sentinel = object()
        sh = MagicMock()
        sh.supports_mmap_read.return_value = True
        sh.deserialize_data.return_value = sentinel
        handle = MagicMock()
        ch = MagicMock()
        ch.get_buffer.return_value = handle

        result = self._handler(sh, ch).get_cached_value("k")

        assert result == (True, sentinel)
        ch.get_buffer.assert_called_once_with("k")
        ch.get.assert_not_called()  # normal read path NOT used on the mmap hit
        sh.deserialize_data.assert_called_once_with(handle.view, "k")
        handle.close.assert_called_once()  # mmap released in finally, never escapes the frame

    def test_not_eligible_uses_normal_read_path(self) -> None:
        sh = MagicMock()
        sh.supports_mmap_read.return_value = False
        ch = MagicMock()
        ch.get.return_value = None  # miss

        self._handler(sh, ch).get_cached_value("k")

        ch.get_buffer.assert_not_called()
        ch.get.assert_called_once()

    def test_get_buffer_none_falls_through_to_normal_read(self) -> None:
        sh = MagicMock()
        sh.supports_mmap_read.return_value = True
        sh.deserialize_data.return_value = "val"
        ch = MagicMock()
        ch.get_buffer.return_value = None  # file ineligible (non-posix / too big / missing)
        ch.get.return_value = b"frame"

        result = self._handler(sh, ch).get_cached_value("k")

        ch.get_buffer.assert_called_once()
        ch.get.assert_called_once()  # fell through
        assert result == (True, "val")


@pytest.mark.unit
class TestMmapReadEndToEnd:
    """Real Arrow + File backend through the full handler stack: the mmap path is actually taken
    (not the os.read path) and the DataFrame round-trips intact."""

    def test_arrow_dataframe_roundtrips_through_real_mmap(self, tmp_path) -> None:
        pd = pytest.importorskip("pandas")
        pytest.importorskip("pyarrow")
        from unittest.mock import patch

        from cachekit.backends.file.backend import FileBackend
        from cachekit.backends.file.config import FileBackendConfig
        from cachekit.key_generator import CacheKeyGenerator

        sh = CacheSerializationHandler(serializer_name="arrow")
        backend = FileBackend(FileBackendConfig(cache_dir=tmp_path / "c", max_size_mb=64, max_value_mb=32))
        ch = StandardCacheHandler(backend)
        oh = CacheOperationHandler(sh, CacheKeyGenerator(), cache_handler=ch)

        df = pd.DataFrame({"a": range(2000), "b": [float(i) / 3 for i in range(2000)]})
        ch.set("k", sh.serialize_data(df, cache_key="k"), 300)

        with (
            patch.object(backend, "get", wraps=backend.get) as g,
            patch.object(backend, "get_buffer", wraps=backend.get_buffer) as gb,
        ):
            hit = oh.get_cached_value("k")

        assert hit is not None
        found, value = hit
        assert found is True
        pd.testing.assert_frame_equal(value, df)
        gb.assert_called_once()  # the real mmap path was taken
        g.assert_not_called()  # not the os.read fallback
