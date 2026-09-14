"""Memory regression guards for large-object (Arrow/DataFrame) caching.

These lock in the fixes that removed the base64+JSON wrapper inflation and the
Arrow serializer's copy chain, asserting DETERMINISTIC metrics only: tracemalloc
peaks and on-wire size in-process, and — for allocations tracemalloc cannot see
(Rust, Arrow pool, mmap residency) — peak RSS via VmHWM in dedicated subprocesses.

- base64+JSON wrap drove store tracemalloc peak to ~5.7x logical and the wire to 1.33x.
- the read path's base64-decode + JSON-parse + full-body slice drove read peak to ~5.4x.

Pre-fix these assertions fail; post-fix store peak is ~2x, read ~1.1x, wire ~1x.

Two measurement scopes live here — serializer-only (`test_store_path...` /
`test_load_path...`) and backend-inclusive (`test_file_backend_*`, cachekit-py#169);
see the "Backend-inclusive read paths" section banner below for the split and why
the headline low-read-memory claim maps to the backend-inclusive numbers.
"""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import textwrap
import tracemalloc
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cachekit.serializers.arrow_serializer import ArrowSerializer
from cachekit.serializers.base import SerializationMetadata
from cachekit.serializers.wrapper import SerializationWrapper

_MB = 1024 * 1024

# Measurement subprocesses finish in seconds; a wedged child must fail the test
# (TimeoutExpired), not hang the CI job to its ceiling. Generous for slow runners.
_SUBPROCESS_TIMEOUT_S = 300

# Subprocess peak-RSS reads use VmHWM from /proc/self/status, NOT resource.ru_maxrss:
# on Linux ru_maxrss lives in the signal struct and SURVIVES fork+exec, so a child
# spawned from a fat pytest process inherits the parent's watermark — the child's
# whole measurement then hides under the inherited peak and a real regression false-
# passes (observed: base 1997 MiB, read cost 0.00x). VmHWM is per-mm and starts fresh
# for the exec'd image. Linux-only, hence the skipif on the tests that embed this.
_VMHWM_SNIPPET = textwrap.dedent(
    """
    def vmhwm_kib():
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmHWM"):
                    return int(line.split()[1])
        raise RuntimeError("VmHWM not found in /proc/self/status")
    """
)


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
@pytest.mark.skipif(sys.platform != "linux", reason="peak-RSS via /proc/self/status VmHWM is Linux-only")
def test_byte_storage_store_has_no_full_payload_copy():
    """Rust-side allocation guard for ByteStorage.store() (cachekit-core#45).

    tracemalloc cannot see Rust allocations, so this invariant uses peak RSS
    (VmHWM) in a dedicated subprocess. Determinism comes from the payload: 512MB
    of a repeating 8-byte pattern LZ4-compresses to ~2MB, so every Rust-side
    buffer downstream of the input (compressed data, msgpack envelope, returned
    bytes) is negligible and peak RSS ~= interpreter + payload (~1.1x). The
    eliminated ``data.to_vec()`` full-payload copy (cachekit-core < 0.3.0)
    re-adds ~1.0x payload and trips the 1.7x bound with margin on both sides.
    """
    payload_mb = 512
    script = _VMHWM_SNIPPET + textwrap.dedent(
        f"""
        from cachekit._rust_serializer import ByteStorage

        payload = b"cachekit" * ({payload_mb} * 1024 * 1024 // 8)
        envelope = ByteStorage(None).store(payload, None)
        # Sanity: compressible payload => tiny envelope, or the RSS bound is meaningless.
        assert len(envelope) < 32 * 1024 * 1024, f"envelope unexpectedly large: {{len(envelope)}}"
        print(vmhwm_kib())
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S)  # noqa: S603 (trusted: sys.executable + literal code)
    assert result.returncode == 0, f"store subprocess failed: {result.stderr}"
    peak = int(result.stdout.strip()) * 1024
    payload_bytes = payload_mb * 1024 * 1024
    assert peak < payload_bytes * 1.7, (
        f"store() peak RSS {peak / payload_bytes:.2f}x payload — a full-payload copy is back "
        f"on the write path (expected ~1.1x without the to_vec copy, ~2.1x with it)"
    )


# ---------------------------------------------------------------------------
# Backend-inclusive read paths (cachekit-py#169)
#
# Everything above measures the serializer layer with the read input already in
# memory, so backend read-side copies (File os.read + the file_data[14:] slice)
# are structurally invisible. These tests run the read END-TO-END through the
# real stack (FileBackend -> StandardCacheHandler -> CacheOperationHandler ->
# unwrap -> deserialize) and bound what the whole path allocates.
# ---------------------------------------------------------------------------


def _file_read_stack(cache_dir: Path, serializer_name: str):
    """The real L2 read wiring the decorator uses, on a File backend.

    encryption=False is the explicit opt-out: it keeps supports_mmap_read() True for
    "arrow" regardless of any CACHEKIT_MASTER_KEY leaking in from the environment.
    """
    from cachekit.backends.file import FileBackend
    from cachekit.backends.file.config import FileBackendConfig
    from cachekit.cache_handler import CacheOperationHandler, CacheSerializationHandler, StandardCacheHandler
    from cachekit.key_generator import CacheKeyGenerator

    backend = FileBackend(FileBackendConfig(cache_dir=cache_dir, max_size_mb=1024, max_value_mb=400))
    operation = CacheOperationHandler(
        CacheSerializationHandler(serializer_name=serializer_name, encryption=False),
        CacheKeyGenerator(),
        cache_handler=StandardCacheHandler(backend),
    )
    return backend, operation


@pytest.mark.slow
@pytest.mark.performance
@pytest.mark.skipif(os.name != "posix", reason="mmap read path is POSIX-only (#171); non-POSIX falls back to os.read")
def test_file_backend_read_python_allocations_bounded(tmp_path: Path) -> None:
    """END-TO-END DataFrame read through the File backend (mmap fast path, #171).

    The whole pipeline — get_buffer mmap -> envelope unwrap -> Arrow deserialize ->
    pandas — allocates ~0.0x logical on the Python heap: the mapped payload and the
    Arrow-pool buffers are not Python allocations, and ``to_pandas(self_destruct,
    split_blocks)`` hands back numpy views over pool buffers rather than copies, so
    even the output df is invisible to tracemalloc. Any stage rematerializing the
    payload as Python bytes trips the 0.5x bound (fault-injected: a bytes() coercion
    of the zero-copy view measures ~1.0x; get_buffer regressing to os.read measures
    ~1.9x). If a pyarrow upgrade makes to_pandas copy again, this fails LOUD at
    ~1.0x — recalibrate consciously; the guard never silently widens.
    """
    df = _numeric_df(50)
    logical = _logical(df)
    backend, operation = _file_read_stack(tmp_path / "cache", "arrow")
    key = "perf:file-read:arrow"
    backend.set(key, operation.serialization_handler.serialize_data(df, cache_key=key))

    # Guard the measurement's precondition explicitly: if mmap doesn't apply here,
    # the test would silently measure the fallback path and fail confusingly.
    assert operation.serialization_handler.supports_mmap_read()
    probe = backend.get_buffer(key)
    assert probe is not None, "get_buffer returned None; end-to-end read would fall back to os.read"
    probe.close()

    gc.collect()
    tracemalloc.start()
    hit = operation.get_cached_value(key)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    assert hit is not None, "end-to-end File read missed (errors read as miss — check logs)"
    pd.testing.assert_frame_equal(hit[1], df)
    assert peak / logical < 0.5, (
        f"File-backend end-to-end read peak {peak / logical:.2f}x logical — a full-payload copy is "
        f"back in the read path (expected ~0.0x zero-copy; ~1x = bytes() coercion, ~2x = os.read "
        f"fallback, ~1x with neither = pyarrow to_pandas stopped returning views — recalibrate)"
    )


@pytest.mark.slow
@pytest.mark.performance
def test_file_backend_bytes_read_python_allocations_bounded(tmp_path: Path) -> None:
    """END-TO-END read through FileBackend.get() (default serializer, no mmap).

    This is the path most cached functions take (anything that isn't a plaintext
    Arrow DataFrame). Measured cost today: ~3x payload on the Python heap —
    FileBackend.get's single payload os.read (header read separately, LAB-770),
    the decompressed msgpack document, and the unpacked output. The two avoidable
    copies #169 called out are gone: the file_data[14:] slice (header-first read)
    and deserialize's ``bytes(data)`` coercion (Rust retrieve takes the buffer
    protocol, so unwrap's zero-copy memoryview flows through). The bound pins
    that: one full-payload copy creeping back (~4x) fails. ~3x is the floor —
    decompress + unpack are inherent (retrieve returns owned bytes by construction).
    """
    payload = np.random.default_rng(0).bytes(50 * _MB)  # incompressible: envelope ~= payload size
    backend, operation = _file_read_stack(tmp_path / "cache", "default")
    key = "perf:file-read:bytes"
    backend.set(key, operation.serialization_handler.serialize_data(payload, cache_key=key))

    gc.collect()
    tracemalloc.start()
    hit = operation.get_cached_value(key)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    assert hit is not None, "end-to-end File read missed (errors read as miss — check logs)"
    assert hit[1] == payload
    assert peak / len(payload) < 3.5, (
        f"File-backend bytes read peak {peak / len(payload):.2f}x payload — a full-payload read-side "
        f"copy crept back in (known cost ~3x: payload os.read + decode + output; LAB-770 removed "
        f"the header slice and the bytes() coercion)"
    )


@pytest.mark.slow
@pytest.mark.performance
@pytest.mark.skipif(sys.platform != "linux", reason="mmap read path is POSIX-only (#171); VmHWM measurement is Linux-only")
def test_file_backend_end_to_end_read_peak_rss_bounded(tmp_path: Path) -> None:
    """Peak-RSS bound for a large DataFrame read end-to-end through the File backend.

    tracemalloc cannot see mmap page residency, Arrow-pool buffers, or Rust-side
    allocations, so the headline low-read-RSS claim gets a real RSS measurement in a
    dedicated subprocess (same pattern as the ByteStorage store test above). The
    entry is written by a separate subprocess so neither payload creation nor the
    write path pollutes the read process's high-water mark, and this pytest process
    never holds payload-scale memory (which would skew later RSS-delta tests).

    Expected read cost above the post-import baseline: ~2x payload — the checksum
    pass faults every mapped page in (1x, file-backed) and to_pandas materializes
    the df (1x). Any ADDED full-payload buffer on that floor — an owned-bytes
    coercion of the zero-copy view, an Arrow-pool or Rust-side copy (invisible to
    tracemalloc) — measures ~3x and blows the 2.6x bound (verified by fault
    injection). The one class this metric cannot see: mmap falling back to os.read
    swaps file-backed pages for anonymous heap at the same ~2x total — that one is
    caught by the tracemalloc test above (~0.0x -> ~1.9x). Payload is uncompressed
    Arrow IPC of incompressible float64 (compression="none", the mmap-friendly
    on-disk format), so on-disk size ~= logical size and the ratios are meaningful.
    """
    payload_mb = 256
    cols = 20
    rows = payload_mb * _MB // (8 * cols)
    cache_dir = str(tmp_path / "cache")
    key = "perf:file-read:rss"

    backend_setup = textwrap.dedent(
        f"""
        from cachekit.backends.file import FileBackend
        from cachekit.backends.file.config import FileBackendConfig

        backend = FileBackend(FileBackendConfig(cache_dir={cache_dir!r}, max_size_mb=1024, max_value_mb=400))
        """
    )

    write_script = backend_setup + textwrap.dedent(
        f"""
        import numpy as np
        import pandas as pd

        from cachekit.serializers.arrow_serializer import ArrowSerializer
        from cachekit.serializers.wrapper import SerializationWrapper

        rng = np.random.default_rng(0)
        df = pd.DataFrame({{f"c{{i}}": rng.standard_normal({rows}) for i in range({cols})}})
        data, meta = ArrowSerializer(compression="none").serialize(df)
        backend.set({key!r}, SerializationWrapper.wrap(data, meta.to_dict(), "arrow"))
        """
    )

    read_script = (
        backend_setup
        + _VMHWM_SNIPPET
        + textwrap.dedent(
            f"""
        from cachekit.cache_handler import CacheOperationHandler, CacheSerializationHandler, StandardCacheHandler
        from cachekit.key_generator import CacheKeyGenerator

        operation = CacheOperationHandler(
            CacheSerializationHandler(serializer_name="arrow", encryption=False),
            CacheKeyGenerator(),
            cache_handler=StandardCacheHandler(backend),
        )
        assert operation.serialization_handler.supports_mmap_read()
        probe = backend.get_buffer({key!r})
        assert probe is not None, "get_buffer returned None; read would fall back to os.read"
        probe.close()

        base = vmhwm_kib()
        hit = operation.get_cached_value({key!r})
        peak = vmhwm_kib()
        assert hit is not None, "end-to-end File read missed"
        assert hit[1].shape == ({rows}, {cols}), f"wrong shape: {{hit[1].shape}}"
        print(base, peak)
        """
        )
    )

    write = subprocess.run([sys.executable, "-c", write_script], capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S)  # noqa: S603 (trusted: sys.executable + literal code)
    assert write.returncode == 0, f"write subprocess failed: {write.stderr}"
    read = subprocess.run([sys.executable, "-c", read_script], capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S)  # noqa: S603 (trusted: sys.executable + literal code)
    assert read.returncode == 0, f"read subprocess failed: {read.stderr}"

    base_kib, peak_kib = (int(v) for v in read.stdout.split())
    read_cost = (peak_kib - base_kib) * 1024
    payload_bytes = payload_mb * _MB
    assert read_cost < payload_bytes * 2.6, (
        f"end-to-end File read peak RSS {read_cost / payload_bytes:.2f}x payload above baseline "
        f"({base_kib / 1024:.0f} MiB) — a full-payload read-side copy is back "
        f"(expected ~2x: mapped pages + df; ~3x with a heap copy)"
    )
