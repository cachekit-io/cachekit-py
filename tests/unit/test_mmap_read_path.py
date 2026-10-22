"""#171 mmap read-path wiring.

Three units compose the zero-copy Arrow read fast path:
- ``CacheSerializationHandler.supports_mmap_read()`` — eligibility (plaintext cachekit Arrow).
- ``StandardCacheHandler.get_buffer()`` — delegates to a backend that exposes ``get_buffer``.
- ``CacheOperationHandler.get_cached_value`` (sync only; the async read never maps) — when
  eligible, reads via the mmap handle, deserializes over the view, and closes the handle in a
  ``finally``. The handle never leaves the frame and its bytes never reach ``_l1_cache.put``
  (blocker C); an Arrow or polars result may keep the mapping alive (see ``_MmapHandle``).
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from cachekit.cache_handler import (
    CacheHit,
    CacheOperationHandler,
    CacheSerializationHandler,
    StandardCacheHandler,
)


@pytest.mark.unit
class TestSupportsMmapRead:
    def test_default_serializer_not_eligible(self) -> None:
        sh = CacheSerializationHandler(serializer_name="default")
        assert sh.supports_mmap_read() is False

    def test_eligibility_checks_never_import_pyarrow(self) -> None:
        """Every store asks _frame_prefix whether the serializer is cachekit's Arrow one: that must
        not import pyarrow (hundreds of ms and MBs) for a handler that never uses it."""
        code = (
            "import sys\n"
            "from cachekit.cache_handler import CacheSerializationHandler\n"
            "sh = CacheSerializationHandler(serializer_name='default')\n"
            "sh.serialize_data({'a': 1}, cache_key='k')\n"
            "sh.supports_mmap_read(); sh.supports_streaming_write()\n"
            "print('pyarrow' in sys.modules)\n"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)  # noqa: S603 (trusted: literal code)
        assert out.stdout.strip() == "False"

    def test_encrypted_arrow_not_eligible(self) -> None:
        """Encrypted values can never mmap — AES-GCM decrypt owns its buffer."""
        pytest.importorskip("pyarrow")  # "arrow" handler init requires the [data] extra
        sh = CacheSerializationHandler(serializer_name="arrow")
        sh.encryption = True
        assert sh.supports_mmap_read() is False

    @pytest.mark.parametrize("releases_fd", [True, False], ids=["py313+", "pre-313"])
    @pytest.mark.parametrize("return_format", ["pandas", "arrow", "polars"])
    def test_return_formats_that_keep_the_mapping_need_an_fd_free_mmap(self, return_format, releases_fd, monkeypatch) -> None:
        """An Arrow or polars result keeps its mapping alive, and before Python 3.13 a mapping also
        keeps a file descriptor: those formats map only where the mmap holds none. pandas always maps."""
        pytest.importorskip("pyarrow")  # "arrow" handler init requires the [data] extra
        from cachekit.serializers.arrow_serializer import ArrowSerializer

        monkeypatch.setattr("cachekit.cache_handler.MMAP_RELEASES_FD", releases_fd)
        sh = CacheSerializationHandler(serializer_name="arrow")
        # Swap in a throwaway serializer rather than mutating the process-wide cached instance, so
        # this test can never leak a return_format into another test (no shared state).
        sh._base_serializer = ArrowSerializer(return_format=return_format)
        assert sh.supports_mmap_read() is (releases_fd or return_format == "pandas")

    def test_arrow_serializer_instance_is_eligible(self) -> None:
        """Eligibility is by serializer object: an instance's frame tag is its class name, not "arrow"."""
        pytest.importorskip("pyarrow")
        from cachekit.serializers.arrow_serializer import ArrowSerializer

        assert CacheSerializationHandler(serializer_name=ArrowSerializer(return_format="pandas")).supports_mmap_read() is True

    def test_arrow_serializer_subclass_not_eligible(self) -> None:
        """Exact type: a subclass may override serialize/deserialize, so it stays on the buffered path."""
        pytest.importorskip("pyarrow")
        from cachekit.serializers.arrow_serializer import ArrowSerializer

        class Custom(ArrowSerializer):
            pass

        assert CacheSerializationHandler(serializer_name=Custom()).supports_mmap_read() is False


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
        handle.view.nbytes = 4096
        ch = MagicMock()
        ch.get_buffer.return_value = handle

        result = self._handler(sh, ch).get_cached_value("k")

        # No envelope (the mmap view never reaches L1, blocker C), but the payload size still rides along
        assert result == CacheHit(sentinel, None, 4096)
        ch.get_buffer.assert_called_once_with("k")
        ch.get.assert_not_called()  # normal read path NOT used on the mmap hit
        sh.deserialize_data.assert_called_once_with(handle.view, "k", (), None)
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
        assert result == CacheHit("val", b"frame", 5)  # os.read fallback carries the envelope for L1


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
        payload = sh.serialize_data(df, cache_key="k")
        ch.set("k", payload, 300)

        with (
            patch.object(backend, "get", wraps=backend.get) as g,
            patch.object(backend, "get_buffer", wraps=backend.get_buffer) as gb,
        ):
            hit = oh.get_cached_value("k")

        assert hit is not None
        assert hit.envelope is None  # mmap hit: nothing to backfill into L1
        assert hit.size_bytes == len(payload)  # ...yet the payload size is accounted without an os.read copy
        pd.testing.assert_frame_equal(hit.value, df)
        gb.assert_called_once()  # the real mmap path was taken
        g.assert_not_called()  # not the os.read fallback
