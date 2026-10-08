"""Serializer and cache-path round trips on a real public dataset, not generated frames.

Hand-written frames of 1 to 5 rows pass where real data fails: a correct but 100x slower NumPy
route once passed every correctness test. So these tests use a pinned slice of the USGS earthquake
catalogue (``tests/data/README.md``) and four data-science results derived from it, assert strict
equality, and guard each encoded size, which catches a wire-format blow-up without timing anything.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
import xxhash

# The [data] extra is absent in the free-threaded CI lane, which installs only the test group.
pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")
pytest.importorskip("pyarrow")

from cachekit import cache  # noqa: E402
from cachekit.backends.file import FileBackend  # noqa: E402
from cachekit.backends.file.config import FileBackendConfig  # noqa: E402
from cachekit.config.validation import ConfigurationError  # noqa: E402
from cachekit.serializers import get_serializer  # noqa: E402

DATA = Path(__file__).parents[1] / "data"
FIXTURE = DATA / "usgs_earthquakes_2024-01.parquet"
MASTER_KEY = "ab" * 32

# Encoded bytes per (serializer, workload) at the fixture's sha256. AutoSerializer hands a DataFrame
# to ArrowSerializer, so their frame figures match. Each size may sit at most 10% above its figure,
# which absorbs compressor drift across pyarrow releases. A smaller encoding passes: lost data fails
# the exact-equality check, not this one. A re-slice or an intended format change updates the figures.
ENCODED_BYTES = {
    ("auto", "full"): 673_906,
    ("auto", "groupby"): 4_026,
    ("auto", "pivot"): 4_890,
    ("auto", "filtered"): 118_938,
    ("auto", "ndarray"): 401_156,
    ("arrow", "full"): 673_906,
    ("arrow", "groupby"): 4_026,
    ("arrow", "pivot"): 4_890,
    ("arrow", "filtered"): 118_938,
}
SIZE_TOLERANCE = 0.10


def _workloads(df: pd.DataFrame) -> dict[str, object]:
    """The frame, plus what an analysis typically caches from it."""
    return {
        "full": df,
        "groupby": df.groupby("net", observed=True).agg(
            events=("id", "count"), mean_mag=("mag", "mean"), max_depth=("depth", "max"), first=("time", "min")
        ),
        "pivot": df.pivot_table(index="net", columns="type", values="mag", aggfunc="mean", observed=True),
        "ndarray": df[["latitude", "longitude", "depth", "mag"]].to_numpy(dtype=np.float64),
        "filtered": df[df["mag"] >= 2.5].sort_values("mag", ascending=False).set_index("id"),
    }


@pytest.fixture(scope="module")
def workloads() -> dict[str, object]:
    return _workloads(pd.read_parquet(FIXTURE))


def test_fixture_matches_the_sha256_its_readme_records() -> None:
    recorded = re.search(r"\| sha256 \| `([0-9a-f]{64})` \|", (DATA / "README.md").read_text())
    assert recorded, "tests/data/README.md has no sha256 row"
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == recorded.group(1)


def test_workloads_have_the_shape_the_round_trips_rely_on(workloads: dict[str, object]) -> None:
    """Guards the claims the tests below rest on, so a re-slice cannot quietly weaken them."""
    df = workloads["full"]
    assert len(df) >= 10_000
    assert str(df["nst"].dtype) == "Int64" and df["nst"].isna().any()
    assert df["mag"].isna().any()
    assert df["id"].nunique() == len(df)
    assert isinstance(df["magType"].dtype, pd.CategoricalDtype)
    assert str(df["time"].dtype) == "datetime64[ns, UTC]"
    assert workloads["pivot"].isna().any().any()  # most networks record only some event types
    assert not isinstance(workloads["filtered"].index, pd.RangeIndex)


@pytest.mark.parametrize("serializer_name, workload", sorted(ENCODED_BYTES))
def test_round_trip_is_exact_and_its_encoded_size_is_stable(
    serializer_name: str, workload: str, workloads: dict[str, object]
) -> None:
    obj = workloads[workload]
    serializer = get_serializer(serializer_name)
    data, metadata = serializer.serialize(obj)
    out = serializer.deserialize(data, metadata)

    # Both routes write [xxHash3-64 of the rest][payload]. The reader also accepts legacy payloads
    # with no checksum, so equality and the size ceiling alone would pass a writer that dropped it.
    assert data[:8] == xxhash.xxh3_64_digest(data[8:]), "the encoding lost its integrity checksum"
    if isinstance(obj, np.ndarray):
        np.testing.assert_array_equal(out, obj, strict=True)
    else:
        pd.testing.assert_frame_equal(out, obj, check_exact=True)
    size, ceiling = len(data), ENCODED_BYTES[serializer_name, workload] * (1 + SIZE_TOLERANCE)
    assert size <= ceiling


@pytest.mark.parametrize("intent, serializer_name", [("plain", "auto"), ("plain", "arrow"), ("secure", "arrow")])
def test_decorated_aggregation_is_served_from_the_backend(
    intent: str, serializer_name: str, workloads: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With L1 off, the second call can only come from the FileBackend's stored bytes.

    The backend has two read methods: ``get`` and the mmap ``get_buffer`` the Arrow route uses.
    """
    backend = FileBackend(FileBackendConfig(cache_dir=tmp_path))
    hits: list[str] = []
    for method in ("get", "get_buffer"):
        real = getattr(backend, method)

        def spy(key: str, _real=real, _method=method) -> object:
            value = _real(key)
            if value is not None:
                hits.append(_method)
            return value

        monkeypatch.setattr(backend, method, spy)
    options = {"backend": backend, "serializer": serializer_name, "l1_enabled": False}
    decorate = cache.secure(master_key=MASTER_KEY, **options) if intent == "secure" else cache(**options)
    expected = workloads["groupby"]
    runs = 0

    @decorate
    def events_by_network() -> pd.DataFrame:
        nonlocal runs
        runs += 1
        return expected.copy()

    first = events_by_network()
    assert hits == []
    second = events_by_network()

    pd.testing.assert_frame_equal(first, expected, check_exact=True)
    pd.testing.assert_frame_equal(second, expected, check_exact=True)
    assert runs == 1
    assert len(hits) == 1


def test_secure_refuses_the_auto_serializer(tmp_path: Path) -> None:
    """Documented: docs/error-codes.md:103, "Single-SDK serializer under encryption". A secure
    DataFrame cache uses ``serializer="arrow"``, which the test above covers."""
    backend = FileBackend(FileBackendConfig(cache_dir=tmp_path))
    with pytest.raises(ConfigurationError, match="Encryption requires a cross-SDK-compatible serializer"):
        cache.secure(master_key=MASTER_KEY, backend=backend, serializer="auto")(lambda: None)
