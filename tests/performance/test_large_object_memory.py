"""Memory regression guards for large-object (Arrow/DataFrame) caching.

These lock in the fixes that removed the base64+JSON wrapper inflation and the
Arrow serializer's copy chain. They assert DETERMINISTIC, environment-independent
metrics (Python-tracked peak via tracemalloc + on-wire size), not process RSS — so
they are stable in CI yet fail loudly if the regressions return:

- base64+JSON wrap drove store tracemalloc peak to ~5.7x logical and the wire to 1.33x.
- the read path's base64-decode + JSON-parse + full-body slice drove read peak to ~5.4x.

Pre-fix these assertions fail; post-fix store peak is ~2x, read ~1.1x, wire ~1x.
"""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import textwrap
import tracemalloc

import numpy as np
import pandas as pd
import pytest

from cachekit.serializers.arrow_serializer import ArrowSerializer
from cachekit.serializers.base import SerializationMetadata
from cachekit.serializers.wrapper import SerializationWrapper

_MB = 1024 * 1024


def _numeric_df(mb: int) -> pd.DataFrame:
    """Incompressible float64 frame ~mb MiB (worst case for compression)."""
    cols = 20
    rows = mb * _MB // (8 * cols)
    rng = np.random.default_rng(0)
    return pd.DataFrame({f"c{i}": rng.standard_normal(rows) for i in range(cols)})


def _logical(df: pd.DataFrame) -> int:
    return int(df.memory_usage(deep=True, index=True).sum())


@pytest.mark.slow
@pytest.mark.performance
def test_store_path_python_allocations_bounded():
    df = _numeric_df(50)
    logical = _logical(df)
    serializer = ArrowSerializer()

    gc.collect()
    tracemalloc.start()
    data, meta = serializer.serialize(df)  # df allocated before start() -> not counted
    wrapped = SerializationWrapper.wrap(data, meta.to_dict(), "arrow")
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    # base64+JSON wrap drove this to ~5.7x; binary frame + zero-copy hashing keeps it ~2x.
    assert peak / logical < 3.0, f"store tracemalloc peak {peak / logical:.2f}x logical (regressed?)"
    # base64 inflated the wire 1.33x; raw binary frame is ~1x (zstd only shrinks).
    assert len(wrapped) / logical < 1.1, f"wire size {len(wrapped) / logical:.2f}x logical (base64 back?)"


@pytest.mark.slow
@pytest.mark.performance
def test_load_path_python_allocations_bounded():
    df = _numeric_df(50)
    logical = _logical(df)
    serializer = ArrowSerializer()
    data, meta = serializer.serialize(df)
    wrapped = SerializationWrapper.wrap(data, meta.to_dict(), "arrow")
    raw, md, _ = SerializationWrapper.unwrap(wrapped)
    meta2 = SerializationMetadata.from_dict(md)

    gc.collect()
    tracemalloc.start()  # wrapped/raw allocated before start() -> not counted
    out = serializer.deserialize(raw, meta2)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    assert len(out) == len(df)
    pd.testing.assert_frame_equal(out, df)
    # base64-decode + JSON-parse + data[8:] slice drove read peak to ~5.4x; now ~1.1x.
    assert peak / logical < 2.5, f"load tracemalloc peak {peak / logical:.2f}x logical (regressed?)"


@pytest.mark.slow
@pytest.mark.performance
def test_full_roundtrip_through_cache_handler_is_correct_and_compact():
    """End-to-end through the real serialize_data/deserialize_data envelope path."""
    from cachekit.cache_handler import CacheSerializationHandler

    df = _numeric_df(20)
    handler = CacheSerializationHandler(serializer_name="arrow")
    blob = handler.serialize_data(df, cache_key="k")
    assert blob[:2] == b"CK"  # new binary frame, not legacy JSON
    assert len(blob) / _logical(df) < 1.1
    out = handler.deserialize_data(blob, cache_key="k")
    pd.testing.assert_frame_equal(out, df)


@pytest.mark.slow
@pytest.mark.performance
def test_streaming_write_path_peak_rss_bounded():
    """LAB-766: the File-backend streaming write path never materializes the envelope.

    Peak RSS in a dedicated subprocess (tracemalloc cannot see pyarrow's C++ pools,
    which is exactly where the buffered path's residual lives). A 400MB incompressible
    frame is stored through the REAL write flow (store_result -> set_streaming ->
    FileBackend), record batch by record batch.

    Measured on the reference box: streaming peaks at ~2.3x logical (frame + the Arrow
    table conversion + interpreter); the buffered path peaks at ~5.6x (BufferOutputStream
    doubling-growth + memory-pool retention + envelope + wrap copies). The 3.2x bound
    sits between them with margin on both sides: a full-payload materialization
    returning to this path re-adds ~2-3x and trips it loudly.
    """
    frame_mb = 400
    script = textwrap.dedent(
        f"""
        import resource, tempfile

        import numpy as np
        import pandas as pd

        from cachekit.backends.file import FileBackend
        from cachekit.backends.file.config import FileBackendConfig
        from cachekit.cache_handler import CacheOperationHandler, CacheSerializationHandler, StandardCacheHandler
        from cachekit.key_generator import CacheKeyGenerator
        from cachekit.serializers.arrow_serializer import ArrowSerializer

        _MB = 1024 * 1024
        cols = 20
        rows = {frame_mb} * _MB // (8 * cols)
        rng = np.random.default_rng(0)
        df = pd.DataFrame({{f"c{{i}}": rng.standard_normal(rows) for i in range(cols)}})
        logical = int(df.memory_usage(deep=True, index=True).sum())

        with tempfile.TemporaryDirectory() as d:
            backend = FileBackend(FileBackendConfig(cache_dir=d, max_size_mb=4096, max_value_mb=2048))
            sh = CacheSerializationHandler(serializer_name="arrow")
            sh._base_serializer = ArrowSerializer(compression=None)  # uncompressed: on-disk ~1x, mmap-readable
            op = CacheOperationHandler(sh, CacheKeyGenerator())
            op.set_cache_handler(StandardCacheHandler(backend))

            ret = op.store_result("k", df, ttl=60)
            assert ret is None, "streaming path did not engage (fell back to buffered set)"
            # Sanity via on-disk size, NOT backend.get(): a full readback would materialize
            # the envelope and pollute the ru_maxrss high-water mark this test measures.
            import os as _os

            (entry,) = [f for f in _os.scandir(d) if ".tmp." not in f.name]
            assert entry.stat().st_size > logical * 0.95, "streamed entry missing/truncated"

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024  # KiB on Linux
        print(logical, peak)
        """
    )
    env = dict(os.environ, CACHEKIT_MAX_VALUE_SIZE=str(2 * 1024**3))
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)  # noqa: S603 (trusted: sys.executable + literal code)
    assert result.returncode == 0, f"streaming-write subprocess failed: {result.stderr}"
    logical, peak = (int(v) for v in result.stdout.split())
    assert peak < logical * 3.2, (
        f"streaming write peak RSS {peak / logical:.2f}x logical — the envelope is being "
        f"materialized again on the write path (streaming ~2.3x, buffered ~5.6x)"
    )


@pytest.mark.slow
@pytest.mark.performance
def test_byte_storage_store_has_no_full_payload_copy():
    """Rust-side allocation guard for ByteStorage.store() (cachekit-core#45).

    tracemalloc cannot see Rust allocations, so this invariant uses peak RSS in
    a dedicated subprocess. Determinism comes from the payload: 512MB of a
    repeating 8-byte pattern LZ4-compresses to ~2MB, so every Rust-side buffer
    downstream of the input (compressed data, msgpack envelope, returned bytes)
    is negligible and peak RSS ~= interpreter + payload (~1.1x). The eliminated
    ``data.to_vec()`` full-payload copy (cachekit-core < 0.3.0) re-adds ~1.0x
    payload and trips the 1.7x bound with margin on both sides.
    """
    payload_mb = 512
    script = textwrap.dedent(
        f"""
        import resource

        from cachekit._rust_serializer import ByteStorage

        payload = b"cachekit" * ({payload_mb} * 1024 * 1024 // 8)
        envelope = ByteStorage(None).store(payload, None)
        # Sanity: compressible payload => tiny envelope, or the RSS bound is meaningless.
        assert len(envelope) < 32 * 1024 * 1024, f"envelope unexpectedly large: {{len(envelope)}}"
        print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)  # KiB on Linux
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)  # noqa: S603 (trusted: sys.executable + literal code)
    assert result.returncode == 0, f"store subprocess failed: {result.stderr}"
    peak = int(result.stdout.strip()) * 1024
    payload_bytes = payload_mb * 1024 * 1024
    assert peak < payload_bytes * 1.7, (
        f"store() peak RSS {peak / payload_bytes:.2f}x payload — a full-payload copy is back "
        f"on the write path (expected ~1.1x without the to_vec copy, ~2.1x with it)"
    )
