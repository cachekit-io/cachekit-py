"""A cache hit must behave like the value that was cached.

Every round trip elsewhere in the suite asserts equality, and equality passes on a read-only
DataFrame: a single-batch Arrow read once came back read-only from every L2 backend while
``backend=None`` returned a writable frame, so the defect showed up in production and not in
development. This contract is checked on the second and third calls, which are hits:

- the hit has the cached value's type and equals it exactly;
- an in-place edit of the hit works (never ``flags.writeable``: pandas 3's copy-on-write sets
  it False on data pandas owns, while edits still work);
- with a backend, an edit of one hit does not reach the next hit, because each hit decodes the
  stored bytes. ``backend=None`` documents the opposite (docs/backends/none.md): it hands every
  caller the cached object itself, so there the two hits are asserted to be the same object.

Axes: every serializer spelling that accepts the value, File (mmap read where the serializer is
eligible, and the bytes read), Redis, and ``backend=None``.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest

# The [data] extra is absent in the free-threaded CI lane, which installs only the test group.
np = pytest.importorskip("numpy")
pd = pytest.importorskip("pandas")
pa = pytest.importorskip("pyarrow")
pytest.importorskip("orjson")

from cachekit import cache  # noqa: E402
from cachekit.backends.file import FileBackend  # noqa: E402
from cachekit.backends.file.config import FileBackendConfig  # noqa: E402
from cachekit.serializers import arrow_serializer  # noqa: E402
from cachekit.serializers.arrow_serializer import ArrowSerializer  # noqa: E402

pytestmark = pytest.mark.unit

# Shrinks the Arrow writer's record-batch budget (8 MiB in production) so a 2,000-row frame spans
# several batches. The reader takes the same multi-chunk route as for a real 8 MiB+ frame;
# test_frames_span_the_batch_counts_they_claim pins both batch counts.
BATCH_BYTES = 16 * 1024
ROWS_MULTI_BATCH = 2_000

# Remove when cachekit-io/cachekit-py#582 merges: strict, so the cases fail the moment
# the fix lands and this marker is forgotten.
READ_ONLY_ARROW_FRAME = pytest.mark.xfail(strict=True, reason="#582: a single-batch Arrow read returns read-only pandas columns")


def _frame(rows: int) -> pd.DataFrame:
    """int64, float64, datetime64[ns], categorical, string and bool columns."""
    return pd.DataFrame(
        {
            "i": np.arange(rows, dtype=np.int64),
            "f": np.arange(rows, dtype=np.float64) / 3,
            "t": pd.date_range("2024-01-01", periods=rows, freq="s"),
            "c": pd.Categorical(np.array(["red", "green", "blue"])[np.arange(rows) % 3]),
            "s": [f"row-{n}" for n in range(rows)],
            "b": np.arange(rows) % 2 == 0,
        }
    )


VALUES: dict[str, Any] = {
    "frame-one-batch": _frame(5),
    "frame-multi-batch": _frame(ROWS_MULTI_BATCH),
    "series": pd.Series(np.arange(8, dtype=np.float64), name="v"),
    "ndarray": np.arange(12, dtype=np.float64).reshape(3, 4),
    "nested": {"ids": [1, 2, 3], "meta": {"name": "x", "ratio": 0.5, "ok": True, "none": None, "rows": [[1, 2], {"k": "v"}]}},
}
FRAMES = ("frame-one-batch", "frame-multi-batch")

# Every serializer spelling, with the values it accepts; the others raise TypeError on write.
SERIALIZERS: dict[str, tuple[Callable[[], Any], tuple[str, ...]]] = {
    "default": (lambda: "default", ("nested",)),
    "auto": (lambda: "auto", (*FRAMES, "series", "ndarray", "nested")),
    "arrow": (lambda: "arrow", FRAMES),
    "ArrowSerializer()": (ArrowSerializer, FRAMES),
    "ArrowSerializer(compression=None)": (lambda: ArrowSerializer(compression=None), FRAMES),
    "orjson": (lambda: "orjson", ("nested",)),
}
ARROW_ROUTED = {"auto", "arrow", "ArrowSerializer()", "ArrowSerializer(compression=None)"}
BACKENDS = ("file-mmap", "file-bytes", "redis", "none")


class _MmapCountingFileBackend(FileBackend):
    """Counts the reads served from the mmap, so the mmap axis cannot silently become a bytes read."""

    mmap_hits = 0

    def get_buffer(self, key: str) -> Any:
        handle = super().get_buffer(key)
        if handle is not None:
            self.mmap_hits += 1
        return handle


class _BytesOnlyFileBackend(FileBackend):
    """A File backend without the mmap read: every hit takes ``get``, as Redis and memcached do."""

    def get_buffer(self, key: str) -> None:
        return None


def _cases() -> list[Any]:
    cases = []
    for serializer, (_, values) in SERIALIZERS.items():
        for value in values:
            for backend in BACKENDS:
                read_only = value == "frame-one-batch" and serializer in ARROW_ROUTED and backend != "none"
                marks = [READ_ONLY_ARROW_FRAME] if read_only else []
                if backend == "redis":  # in tests/unit so the PR lane runs it; the marker says it needs Redis
                    marks.append(pytest.mark.integration)
                cases.append(pytest.param(serializer, value, backend, marks=marks, id=f"{serializer}-{value}-{backend}"))
    return cases


@pytest.fixture(autouse=True)
def _small_record_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arrow_serializer, "_TARGET_BATCH_BYTES", BATCH_BYTES)


def _assert_equal(out: Any, expected: Any) -> None:
    assert type(out) is type(expected)
    if isinstance(expected, pd.DataFrame):
        pd.testing.assert_frame_equal(out, expected, check_exact=True)
    elif isinstance(expected, pd.Series):
        pd.testing.assert_series_equal(out, expected, check_exact=True)
    elif isinstance(expected, np.ndarray):
        np.testing.assert_array_equal(out, expected, strict=True)
    else:
        assert out == expected


def _edit_in_place(hit: Any) -> None:
    """Edit every column (or element kind) of ``hit`` in place and check each edit took."""
    if isinstance(hit, pd.DataFrame):
        edits = {
            "i": 99,
            "f": -1.5,
            "t": pd.Timestamp("2000-01-01"),
            "c": "green" if hit.loc[0, "c"] != "green" else "red",
            "s": "edited",
            "b": not hit.loc[0, "b"],
        }
        for column, new in edits.items():
            hit.loc[0, column] = new
        assert {column: hit.loc[0, column] for column in edits} == edits
    elif isinstance(hit, pd.Series):
        hit.iloc[0] = -1.0
        assert hit.iloc[0] == -1.0
    elif isinstance(hit, np.ndarray):
        hit[0, 0] = -1.0
        assert hit[0, 0] == -1.0
    else:
        hit["ids"].append(4)
        hit["meta"]["rows"][1]["k"] = "edited"
        assert hit["ids"][-1] == 4 and hit["meta"]["rows"][1]["k"] == "edited"


def test_frames_span_the_batch_counts_they_claim() -> None:
    """The two frame values exercise different read routes only while their batch counts differ."""
    serializer = ArrowSerializer()
    for name, batches in (("frame-one-batch", 1), ("frame-multi-batch", None)):
        data, _ = serializer.serialize(VALUES[name])
        count = pa.ipc.open_file(pa.py_buffer(data[8:])).num_record_batches  # past the 8-byte checksum
        assert count == batches if batches else count > 1, f"{name}: {count} record batches"


@pytest.mark.parametrize("serializer, value_name, backend_name", _cases())
def test_cache_hit_behaves_like_the_cached_value(
    serializer: str, value_name: str, backend_name: str, tmp_path: Any, request: pytest.FixtureRequest
) -> None:
    expected = VALUES[value_name]
    backend: Any = None
    if backend_name == "file-mmap":
        backend = _MmapCountingFileBackend(FileBackendConfig(cache_dir=tmp_path))
    elif backend_name == "file-bytes":
        backend = _BytesOnlyFileBackend(FileBackendConfig(cache_dir=tmp_path))
    elif backend_name == "redis":
        backend = request.getfixturevalue("redis_backend")
    # With a backend, L1 is off so every hit reads the backend's stored bytes.
    options: dict[str, Any] = {"backend": backend, "serializer": SERIALIZERS[serializer][0]()}
    if backend is not None:
        options["l1_enabled"] = False
    runs = 0

    @cache(**options)
    def compute() -> Any:
        nonlocal runs
        runs += 1
        return copy.deepcopy(expected)

    compute()
    second = compute()
    third = compute()

    assert runs == 1, "the second and third calls must be hits"
    if backend_name == "file-mmap" and serializer == "arrow":
        assert backend.mmap_hits == 2, "the mmap axis must read through the mmap"
    _assert_equal(second, expected)
    _edit_in_place(second)
    if backend is None:
        assert third is second, "backend=None hands back the cached object itself (docs/backends/none.md)"
    else:
        assert third is not second
        _assert_equal(third, expected)
