"""Zero-copy File-backend reads for cachekit Arrow, end to end.

Three properties compose:
- eligibility by serializer object: ``ArrowSerializer(...)`` instances take the mmap read and
  streaming write paths, for every return_format;
- an Arrow or polars result aliases the mapping and keeps it alive (the handle's close() leaves
  an exported mapping in place; refcounting unmaps it with the result's last buffer);
- uncompressed Arrow IPC starts on a 64-byte boundary of the cache file (the CK frame header is
  padded with spaces), so polars imports aligned columns without copying.
"""

from __future__ import annotations

import gc
import io
import os
import sys

import pytest

from cachekit import cache
from cachekit.backends.file import FileBackend, FileBackendConfig
from cachekit.backends.file.backend import HEADER_SIZE
from cachekit.cache_handler import CacheOperationHandler, CacheSerializationHandler, StandardCacheHandler
from cachekit.key_generator import CacheKeyGenerator
from cachekit.serializers.wrapper import SerializationWrapper

pd = pytest.importorskip("pandas")
pa = pytest.importorskip("pyarrow")
np = pytest.importorskip("numpy")

from cachekit.serializers.arrow_serializer import ArrowSerializer  # noqa: E402  (needs pyarrow)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(os.name != "posix", reason="the mmap read path is POSIX-only"),
]

_N = 100_000
_LINUX = sys.platform.startswith("linux")


def _frame() -> pd.DataFrame:
    rng = np.random.default_rng(0)
    return pd.DataFrame(
        {
            "i64": np.arange(_N, dtype="int64"),
            "f64": rng.random(_N),
            "b": rng.random(_N) < 0.5,
            "ts": pd.date_range("2024-01-01", periods=_N, freq="s"),
            "s": pd.Series(rng.integers(0, 1000, _N)).astype(str),
        }
    )


@pytest.fixture
def backend(tmp_path) -> FileBackend:
    return FileBackend(FileBackendConfig(cache_dir=str(tmp_path), max_size_mb=256, max_value_mb=64))


@pytest.fixture
def spies(monkeypatch) -> dict:
    """Record each mmap hit's mapping (address range, file path) and each streaming write."""
    state: dict = {"maps": [], "streamed": 0}
    get_buffer, set_streaming = FileBackend.get_buffer, FileBackend.set_streaming

    def spy_get_buffer(self, key):
        handle = get_buffer(self, key)
        if handle is not None:
            lo = pa.py_buffer(handle._mm).address
            state["maps"].append((lo, lo + len(handle._mm), str(self._key_to_path(key))))
        return handle

    def spy_set_streaming(self, *args, **kwargs):
        state["streamed"] += 1
        return set_streaming(self, *args, **kwargs)

    monkeypatch.setattr(FileBackend, "get_buffer", spy_get_buffer)
    monkeypatch.setattr(FileBackend, "set_streaming", spy_set_streaming)
    return state


def _mapped(path: str) -> bool:
    with open("/proc/self/maps") as maps:
        return any(path in line for line in maps)


def _aliased(buffers, mapping) -> tuple[int, int]:
    """(bytes inside the mapping, total bytes) over non-empty ``buffers``."""
    lo, hi, _ = mapping
    sized = [b for b in buffers if b is not None and b.size]
    return sum(b.size for b in sized if lo <= b.address < hi), sum(b.size for b in sized)


def _table_buffers(table):
    return [b for column in table.columns for chunk in column.chunks for b in chunk.buffers()]


def _ipc_offset(frame: bytes) -> int:
    """Offset of the Arrow IPC body in a CK frame: after the prefix and the 8-byte checksum."""
    return frame.index(b"ARROW1")


def _operation_handler(backend, serializer) -> CacheOperationHandler:
    op = CacheOperationHandler(CacheSerializationHandler(serializer_name=serializer), CacheKeyGenerator())
    op.set_cache_handler(StandardCacheHandler(backend))
    return op


class TestInstanceTakesZeroCopyPaths:
    @pytest.mark.parametrize("return_format", ["pandas", "arrow", "polars"])
    def test_streams_writes_and_mmaps_hits(self, backend, spies, return_format) -> None:
        if return_format == "polars":
            pytest.importorskip("polars")
        df = _frame()

        @cache(backend=backend, serializer=ArrowSerializer(return_format=return_format, compression=None), l1_enabled=False)
        def load():
            return df

        load()
        assert spies["streamed"] == 1
        assert spies["maps"] == []
        out = load()
        assert len(spies["maps"]) == 1
        assert len(out) == _N

    def test_subclass_stays_on_buffered_paths(self, backend, spies) -> None:
        class Custom(ArrowSerializer):
            pass

        @cache(backend=backend, serializer=Custom(compression=None), l1_enabled=False)
        def load():
            return _frame()

        load()
        load()
        assert spies["streamed"] == 0
        assert spies["maps"] == []


class TestArrowResultAliasesTheMapping:
    def test_hit_is_zero_copy(self, backend, spies) -> None:
        df = _frame()

        @cache(backend=backend, serializer=ArrowSerializer(return_format="arrow", compression=None), l1_enabled=False)
        def load():
            return df

        load()
        gc.collect()
        pool_before = pa.total_allocated_bytes()
        table = load()
        assert pa.total_allocated_bytes() == pool_before  # nothing allocated from the Arrow pool
        inside, total = _aliased(_table_buffers(table), spies["maps"][0])
        assert total > 0 and inside == total
        assert table.equals(pa.Table.from_pandas(df, preserve_index=None))

    @pytest.mark.skipif(not _LINUX, reason="reads /proc/self/maps")
    def test_mapping_lives_with_the_result(self, backend, spies) -> None:
        @cache(backend=backend, serializer=ArrowSerializer(return_format="arrow", compression=None), l1_enabled=False)
        def load():
            return _frame()

        load()
        table = load()
        path = spies["maps"][0][2]
        assert _mapped(path)
        del table
        gc.collect()
        assert not _mapped(path)

    def test_overwrite_and_delete_leave_a_held_result_intact(self, backend, spies) -> None:
        serializer = ArrowSerializer(return_format="arrow", compression=None)
        op = _operation_handler(backend, serializer)
        df = _frame()
        op.store_result("k", df, ttl=60)
        hit = op.get_cached_value("k")
        assert hit is not None and spies["maps"]
        table = hit.value

        op.store_result("k", df.iloc[::-1].reset_index(drop=True), ttl=60)  # temp file + rename
        backend.delete("k")  # unlink
        gc.collect()
        assert table.equals(pa.Table.from_pandas(df, preserve_index=None))


class TestPolarsResultAliasesTheMapping:
    def test_aligned_fixed_width_columns_are_zero_copy(self, backend, spies) -> None:
        pl = pytest.importorskip("polars")

        @cache(backend=backend, serializer=ArrowSerializer(return_format="polars", compression=None), l1_enabled=False)
        def load():
            return _frame()

        load()
        out = load()
        assert isinstance(out, pl.DataFrame)
        for name in ("i64", "f64", "b", "ts"):
            buffers = [b for chunk in out[name].get_chunks() for b in chunk.to_arrow().buffers()]
            inside, total = _aliased(buffers, spies["maps"][0])
            assert total > 0 and inside == total, f"{name}: {inside}/{total} bytes alias the mapping"
        assert out.equals(pl.from_pandas(_frame()))


class TestUncompressedIpcAlignment:
    def test_file_offset_is_a_multiple_of_64(self, backend) -> None:
        op = _operation_handler(backend, ArrowSerializer(compression=None))
        op.store_result("k", _frame(), ttl=60)
        with open(backend._key_to_path("k"), "rb") as f:
            stored = f.read()
        assert stored.index(b"ARROW1") % 64 == 0
        assert HEADER_SIZE + _ipc_offset(backend.get("k")) == stored.index(b"ARROW1")

    @pytest.mark.parametrize("by_name", [True, False], ids=["name", "instance"])
    def test_streamed_and_buffered_are_byte_identical(self, by_name) -> None:
        serializer = ArrowSerializer(compression=None)
        sh = CacheSerializationHandler(serializer_name="arrow" if by_name else serializer)
        # The process-wide "arrow" instance keeps the compression it was built with; swap in an
        # uncompressed one rather than mutate it.
        sh._base_serializer = serializer
        sink = io.BytesIO()
        sh.write_serialized_to(sink, _frame())
        buffered = sh.serialize_data(_frame(), cache_key="k")
        assert sink.getvalue() == buffered
        assert (HEADER_SIZE + _ipc_offset(buffered)) % 64 == 0

    def test_compressed_entries_are_not_padded(self) -> None:
        serializer = ArrowSerializer(compression="zstd")
        sh = CacheSerializationHandler(serializer_name=serializer)
        payload, metadata = serializer.serialize(_frame())
        assert sh.serialize_data(_frame(), cache_key="k") == SerializationWrapper.wrap(
            payload, metadata.to_dict(), "ArrowSerializer"
        )

    def test_encrypted_entries_are_not_padded(self) -> None:
        sh = CacheSerializationHandler(
            serializer_name=ArrowSerializer(compression=None), encryption=True, single_tenant_mode=True, master_key="a" * 64
        )
        frame = sh.serialize_data(_frame(), cache_key="k")
        prefix_len = 7 + int.from_bytes(frame[3:7], "big")
        assert not frame[:prefix_len].endswith(b" ")

    def test_unpadded_entry_still_reads(self, backend, spies) -> None:
        """An entry written before the padding (no spaces after the JSON header) reads unchanged."""
        serializer = ArrowSerializer(compression=None)
        payload, metadata = serializer.serialize(_frame())
        legacy = SerializationWrapper.wrap(payload, metadata.to_dict(), "ArrowSerializer")
        assert (HEADER_SIZE + _ipc_offset(legacy)) % 64 != 0
        backend.set("k", legacy, ttl=60)

        hit = _operation_handler(backend, serializer).get_cached_value("k")

        assert hit is not None and spies["maps"]
        pd.testing.assert_frame_equal(hit.value, _frame(), check_exact=True)
