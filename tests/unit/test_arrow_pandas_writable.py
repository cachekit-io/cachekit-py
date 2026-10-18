"""A pandas DataFrame read back through the Arrow serializer is writable, as an uncached call's is.

A frame that fits one IPC record batch used to come back with read-only numpy columns: pyarrow
wraps a single-chunk column as a view of the IPC buffers, which alias the backend's bytes or the
File backend's mmap. ``df.loc[0, "a"] = 1`` then raised in production (with a backend) but not in
development (``backend=None``). Same contract as AutoSerializer's numpy paths (cachekit-py#157).
"""

from __future__ import annotations

import gc
from pathlib import Path
from unittest.mock import patch

import pytest

# The [data] extra is absent in the free-threaded CI lane, which installs only the test group.
pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")
pytest.importorskip("pyarrow")

from cachekit import cache  # noqa: E402
from cachekit.backends.file import FileBackend  # noqa: E402
from cachekit.backends.file.config import FileBackendConfig  # noqa: E402
from cachekit.cache_handler import _get_cached_serializer_instance  # noqa: E402
from cachekit.serializers.arrow_serializer import ArrowSerializer  # noqa: E402


def _frame() -> pd.DataFrame:
    """One column per dtype pyarrow converts differently; the int index is stored, not a RangeIndex."""
    return pd.DataFrame(
        {
            "i": np.arange(5, dtype="int64"),
            "f": np.arange(5, dtype="float64") / 2,
            "f_nan": [0.5, np.nan, 2.5, 3.5, 4.5],
            "t": pd.date_range("2024-01-01", periods=5),
            "c": pd.Categorical(list("abcab")),
            "s": list("vwxyz"),
            "b": [True, False, True, False, True],
            "n": pd.array([1, None, 3, 4, 5], dtype="Int64"),
        },
        index=pd.Index(np.arange(10, 15, dtype="int64")),
    )


def _cache_maps(cache_dir: Path) -> list[str]:
    with open("/proc/self/maps") as maps:
        return [line for line in maps if str(cache_dir) in line]


# (serializer route, read path). serializer="arrow" is the only route the File backend's mmap
# read serves; an instance or "auto" always reads through get().
ROUTES = [("arrow", "mmap"), ("arrow", "bytes"), ("instance", "bytes"), ("auto", "bytes")]


@pytest.mark.parametrize("codec", ["zstd", None])
@pytest.mark.parametrize("route, read_path", ROUTES)
def test_backend_hit_returns_a_frame_every_column_of_which_edits_in_place(
    route: str, read_path: str, codec: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if route == "instance":
        serializer: object = ArrowSerializer(compression=codec)
    else:
        serializer = route
        shared = _get_cached_serializer_instance(route)  # the instance the decorator resolves
        arrow = shared._arrow_serializer if route == "auto" else shared
        monkeypatch.setattr(arrow, "compression", codec)

    backend = FileBackend(FileBackendConfig(cache_dir=tmp_path))
    hits: list[str] = []
    for method in ("get", "get_buffer"):
        real = getattr(backend, method)

        def spy(key: str, _real=real, _method=method) -> object:
            if _method == "get_buffer" and read_path == "bytes":
                return None  # no mmap: the handler falls back to get()
            value = _real(key)
            if value is not None:
                hits.append(_method)
            return value

        monkeypatch.setattr(backend, method, spy)

    @cache(backend=backend, serializer=serializer, l1_enabled=False)
    def load() -> pd.DataFrame:
        return _frame()

    load()
    out = load()
    assert hits == ["get_buffer" if read_path == "mmap" else "get"]  # served by the backend, by that path

    expected = _frame()
    pd.testing.assert_frame_equal(out, expected)
    for column in out.columns:
        out.loc[out.index[0], column] = out[column].iloc[1]  # raised on main for i, f, t and c
        expected.loc[expected.index[0], column] = expected[column].iloc[1]
    pd.testing.assert_frame_equal(out, expected)


@pytest.mark.skipif(not Path("/proc/self/maps").exists(), reason="reads /proc/self/maps (Linux)")
def test_mmap_hit_leaves_no_mapping_of_the_cache_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Uncompressed IPC is read zero-copy over the mmap. Read-only views of it (an index included)
    used to keep the cache file mapped after the call, which the handler's close() cannot undo.

    No string column: on pandas 3 its default ``str`` dtype is Arrow-backed, so it references the
    payload by design. Immutable buffers like that may keep the mapping until the frame is released;
    the next test pins that lifetime.
    """
    monkeypatch.setattr(_get_cached_serializer_instance("arrow"), "compression", None)
    backend = FileBackend(FileBackendConfig(cache_dir=tmp_path))

    @cache(backend=backend, serializer="arrow", l1_enabled=False)
    def load() -> pd.DataFrame:
        return _frame().drop(columns="s")

    load()
    with patch.object(backend, "get_buffer", wraps=backend.get_buffer) as get_buffer:
        out = load()
    get_buffer.assert_called_once()  # the mmap path served the hit
    assert _cache_maps(tmp_path) == []
    pd.testing.assert_frame_equal(out, _frame().drop(columns="s"))


@pytest.mark.skipif(not Path("/proc/self/maps").exists(), reason="reads /proc/self/maps (Linux)")
def test_immutable_buffers_hold_the_mapping_only_while_the_result_lives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The categories of a numeric categorical and a CategoricalIndex stay zero-copy views of the
    payload (immutable in pandas), as do Arrow-backed columns. The result stays editable, and the
    mapping, with the disk space of a cache file deleted meanwhile, is released with the result."""
    monkeypatch.setattr(_get_cached_serializer_instance("arrow"), "compression", None)
    backend = FileBackend(FileBackendConfig(cache_dir=tmp_path))

    def frame() -> pd.DataFrame:
        return pd.DataFrame({"nc": pd.Categorical([1, 2, 1]), "s": ["a", "b", "c"]}, index=pd.CategoricalIndex([10, 20, 30]))

    @cache(backend=backend, serializer="arrow", l1_enabled=False)
    def load() -> pd.DataFrame:
        return frame()

    load()
    out = load()
    out.loc[out.index[0], "nc"] = 2  # editable whatever still references the payload
    expected = frame()
    expected.loc[expected.index[0], "nc"] = 2
    pd.testing.assert_frame_equal(out, expected)

    del out
    gc.collect()
    assert _cache_maps(tmp_path) == []


def test_multi_batch_frame_skips_the_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multi-chunk columns are concatenated into fresh arrays already, so only a one-batch read pays a copy."""
    import pyarrow as pa

    from cachekit.serializers import arrow_serializer

    df = _frame()
    serializer = ArrowSerializer()
    single, _ = serializer.serialize(df)
    monkeypatch.setattr(arrow_serializer, "_TARGET_BATCH_BYTES", 64)  # a few rows per batch
    multi, _ = serializer.serialize(df)
    assert pa.ipc.open_file(pa.py_buffer(multi[8:])).num_record_batches > 1

    for data, copies in ((single, 1), (multi, 0)):
        with patch.object(pa, "concat_tables", wraps=pa.concat_tables) as concat:
            out = serializer.deserialize(data)
        assert concat.call_count == copies
        out.loc[out.index[0], "i"] = 99  # writable either way
        assert out["i"].iloc[0] == 99
