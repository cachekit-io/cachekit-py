"""FileBackend performance benchmarks.

Comprehensive performance testing for file-based cache backend with:
- Sequential read/write latency (p50/p95/p99)
- Concurrent multi-threaded throughput
- Large value handling (1MB)
- Set/get latency against the directory's entry count
- Oldest-written-first eviction performance
- Redis comparison, on the suite's pytest-redis server

Nothing here asserts a latency or throughput figure: they are printed, and the headline ones
recorded with ``record_property``. Each test asserts the behaviour it timed instead (round trips
return the value written, eviction removes the oldest entries). A set() fsyncs and
renames its file and checks capacity on in-memory counters, so its usual cost does not grow with
the entry count; only eviction, rejection and the 30 s reconcile scan the directory.
test_bench_set_scaling_with_entry_count measures that; docs/backends/file.md points at it. get()
does no scan, but it waits for a concurrent set() in the same process, which holds the backend
lock throughout.
"""

from __future__ import annotations

import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from cachekit.backends.file import FileBackend
from cachekit.backends.file.config import FileBackendConfig


@pytest.mark.performance
def test_bench_sequential_read_write(tmp_path: Path, record_property) -> None:
    """Measure p50/p95/p99 latency for sequential read/write operations.

    This benchmark measures the core file I/O operations in isolation,
    showing the latency breakdown for typical cache access patterns.
    """
    config = FileBackendConfig(
        cache_dir=tmp_path,
        max_size_mb=1024,
        max_value_mb=100,
        max_entry_count=10_000,
    )
    backend = FileBackend(config)

    # Test data
    test_key = "bench:seq:key"
    test_value = b"x" * 1024  # 1KB value
    iterations = 1000

    print(f"\nBenchmarking sequential read/write ({iterations:,} iterations)...")

    # Warm up - 100 iterations to stabilize
    for i in range(100):
        backend.set(f"warmup:{i}", b"data", ttl=3600)
        backend.get(f"warmup:{i}")
        backend.delete(f"warmup:{i}")

    # Measure write latency
    write_latencies = []
    for i in range(iterations):
        start = time.perf_counter_ns()
        backend.set(f"{test_key}:write:{i}", test_value, ttl=3600)
        end = time.perf_counter_ns()
        write_latencies.append(end - start)

    # Measure read latency
    read_latencies = []
    reads = []
    for i in range(iterations):
        start = time.perf_counter_ns()
        reads.append(backend.get(f"{test_key}:write:{i}"))
        end = time.perf_counter_ns()
        read_latencies.append(end - start)

    # Measure delete latency
    delete_latencies = []
    deletes = []
    for i in range(iterations):
        start = time.perf_counter_ns()
        deletes.append(backend.delete(f"{test_key}:write:{i}"))
        end = time.perf_counter_ns()
        delete_latencies.append(end - start)

    # Calculate statistics
    write_stats = _calculate_stats(write_latencies)
    read_stats = _calculate_stats(read_latencies)
    delete_stats = _calculate_stats(delete_latencies)

    # Print results
    print(f"\n{'=' * 70}")
    print("FileBackend Sequential Read/Write Performance")
    print(f"{'=' * 70}")
    print(f"Value size: {len(test_value)} bytes")
    print(f"Iterations: {iterations:,}\n")

    print("WRITE Operations:")
    _print_stats("  ", write_stats)

    print("\nREAD Operations:")
    _print_stats("  ", read_stats)

    print("\nDELETE Operations:")
    _print_stats("  ", delete_stats)

    # Combined (typical cache line: set + get + delete)
    combined_latencies = [w + r + d for w, r, d in zip(write_latencies, read_latencies, delete_latencies, strict=True)]
    combined_stats = _calculate_stats(combined_latencies)

    print("\nCOMBINED (Set+Get+Delete):")
    _print_stats("  ", combined_stats)

    record_property("write_p99_us", round(write_stats["p99_us"], 1))
    record_property("read_p99_us", round(read_stats["p99_us"], 1))

    assert reads == [test_value] * iterations, "a timed read did not return the value written"
    assert deletes == [True] * iterations, "a timed delete did not find its entry"
    assert all(backend.get(f"{test_key}:write:{i}") is None for i in range(iterations))


@pytest.mark.performance
@pytest.mark.parametrize("entries", [0, 1_000, 5_000, 9_000])
def test_bench_set_scaling_with_entry_count(tmp_path: Path, entries: int) -> None:
    """Set/get/delete latency with ``entries`` already in the cache directory.

    The directory is prefilled by writing entry files directly, without a set() and its fsync
    per entry, and the measured backend is created afterwards so its counters start from them.
    The timed loop cycles 10 keys through set/get/delete, so the entry count stays at
    ``entries`` (+1) throughout. Run it on the filesystem you care about with ``--basetemp``;
    repeat runs to see the run-to-run spread.
    """
    config = FileBackendConfig(cache_dir=tmp_path, max_size_mb=1024, max_value_mb=100, max_entry_count=20_000)
    backend = FileBackend(config)
    value = b"x" * 1024  # 1KB value
    entry = FileBackend._build_header(0) + value
    for i in range(entries):
        Path(backend._key_to_path(f"fill:{i}")).write_bytes(entry)
    backend = FileBackend(config)

    iterations = 200
    set_ns, get_ns, delete_ns = [], [], []
    for i in range(iterations):
        key = f"bench:{i % 10}"
        start = time.perf_counter_ns()
        backend.set(key, value)
        set_ns.append(time.perf_counter_ns() - start)
        start = time.perf_counter_ns()
        assert backend.get(key) == value
        get_ns.append(time.perf_counter_ns() - start)
        start = time.perf_counter_ns()
        backend.delete(key)
        delete_ns.append(time.perf_counter_ns() - start)

    stats = {op: _calculate_stats(ns) for op, ns in (("set", set_ns), ("get", get_ns), ("delete", delete_ns))}
    print(f"\nentries={entries} value=1KB n={iterations}")
    for op, st in stats.items():
        print(f"  {op:<6} p50={st['p50_us'] / 1000:.3f}ms  p99={st['p99_us'] / 1000:.3f}ms")


@pytest.mark.performance
def test_bench_concurrent_10_threads(tmp_path: Path, record_property) -> None:
    """Measure throughput with 10 concurrent threads.

    Validates that FileBackend can handle concurrent access from multiple
    threads without significant lock contention.
    """
    config = FileBackendConfig(
        cache_dir=tmp_path,
        max_size_mb=1024,
        max_value_mb=100,
        max_entry_count=50_000,
    )
    backend = FileBackend(config)

    num_threads = 10
    ops_per_thread = 1000
    test_value = b"y" * 512  # 512 bytes

    print(f"\nBenchmarking concurrent access ({num_threads} threads, {ops_per_thread} ops/thread)...")

    # Warm up
    for i in range(100):
        backend.set(f"warmup:{i}", test_value, ttl=3600)

    def worker_thread(thread_id: int) -> tuple[list[int], int]:
        """Worker thread that performs read/write operations; counts only round trips that held."""
        latencies = []
        success_count = 0

        for op_idx in range(ops_per_thread):
            key = f"thread:{thread_id}:key:{op_idx}"

            # Set operation
            start = time.perf_counter_ns()
            backend.set(key, test_value, ttl=3600)
            latencies.append(time.perf_counter_ns() - start)

            # Get operation
            start = time.perf_counter_ns()
            result = backend.get(key)
            latencies.append(time.perf_counter_ns() - start)

            # Delete operation
            start = time.perf_counter_ns()
            deleted = backend.delete(key)
            latencies.append(time.perf_counter_ns() - start)

            if result == test_value and deleted:
                success_count += 3  # 3 ops per iteration

        return latencies, success_count

    # Measure concurrent throughput
    start_time = time.perf_counter()

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker_thread, i) for i in range(num_threads)]
        all_latencies = []
        total_ops = 0

        for future in as_completed(futures):
            latencies, ops = future.result()
            all_latencies.extend(latencies)
            total_ops += ops

    elapsed = time.perf_counter() - start_time
    throughput = total_ops / elapsed

    # Calculate statistics
    stats = _calculate_stats(all_latencies)

    # Print results
    print(f"\n{'=' * 70}")
    print(f"FileBackend Concurrent Throughput ({num_threads} threads)")
    print(f"{'=' * 70}")
    print(f"Total operations: {total_ops:,}")
    print(f"Elapsed time: {elapsed:.2f}s")
    print(f"Throughput: {throughput:,.0f} ops/sec\n")

    print("Latency Distribution:")
    _print_stats("  ", stats)

    record_property("throughput_ops_per_sec", round(throughput))

    # Each thread works on its own keys, so contention must never lose or cross a round trip.
    expected_ops = num_threads * ops_per_thread * 3
    assert total_ops == expected_ops, f"{expected_ops - total_ops} operations saw a wrong value or missing entry"


@pytest.mark.performance
def test_bench_large_value_1mb(tmp_path: Path, record_property) -> None:
    """Measure latency for 1MB values.

    Large values stress the I/O subsystem and fsync operations.
    """
    config = FileBackendConfig(
        cache_dir=tmp_path,
        max_size_mb=512,
        max_value_mb=100,
        max_entry_count=10_000,
    )
    backend = FileBackend(config)

    # Test with progressively larger values
    value_sizes = [
        (100 * 1024, "100KB"),  # 100KB
        (500 * 1024, "500KB"),  # 500KB
        (1 * 1024 * 1024, "1MB"),  # 1MB
    ]
    iterations = 100

    print(f"\nBenchmarking large value handling ({iterations} iterations per size)...")

    # Warm up
    for i in range(10):
        backend.set(f"warmup:{i}", b"x" * 10_000, ttl=3600)

    results = {}
    for value_size, label in value_sizes:
        test_value = b"z" * value_size
        write_latencies = []
        read_latencies = []

        for i in range(iterations):
            key = f"large:{label}:{i}"

            # Write
            start = time.perf_counter_ns()
            backend.set(key, test_value, ttl=3600)
            end = time.perf_counter_ns()
            write_latencies.append(end - start)

            # Read
            start = time.perf_counter_ns()
            result = backend.get(key)
            end = time.perf_counter_ns()
            read_latencies.append(end - start)

            # Verify round-trip
            assert result == test_value, "Round-trip verification failed"

        write_stats = _calculate_stats(write_latencies)
        read_stats = _calculate_stats(read_latencies)

        results[label] = {
            "write": write_stats,
            "read": read_stats,
        }

    # Print results
    print(f"\n{'=' * 70}")
    print("FileBackend Large Value Performance")
    print(f"{'=' * 70}\n")

    for label in [s[1] for s in value_sizes]:
        print(f"{label} Values:")
        print("  Write:")
        _print_stats("    ", results[label]["write"])
        print("  Read:")
        _print_stats("    ", results[label]["read"])
        print()

    record_property("write_1mb_p99_us", round(results["1MB"]["write"]["p99_us"], 1))
    record_property("read_1mb_p99_us", round(results["1MB"]["read"]["p99_us"], 1))


@pytest.mark.performance
def test_bench_eviction_1000_files(tmp_path: Path) -> None:
    """Measure time to evict 1000 files when cache exceeds capacity.

    Eviction is triggered when cache exceeds 90% capacity, and evicts the
    oldest-written files (by mtime) until it reaches 70% capacity.
    """
    # Small cache to trigger eviction (5MB)
    max_size_mb = 5
    config = FileBackendConfig(
        cache_dir=tmp_path,
        max_size_mb=max_size_mb,
        max_value_mb=2,
        max_entry_count=1_000,
    )
    backend = FileBackend(config)

    # Value size to reach 90% capacity with fewer entries
    # 5MB * 0.9 = 4.5MB / 50 entries = ~90KB per entry
    value_size = 90 * 1024  # 90KB

    print(f"\nBenchmarking eviction (cache: {max_size_mb}MB max)...")

    # Fill cache to just under 90% capacity
    # At 90%+ capacity, eviction triggers
    num_entries = 50
    print(f"  Filling cache with {num_entries} entries ({value_size} bytes each)...")

    for i in range(num_entries):
        backend.set(f"evict:entry:{i}", b"x" * value_size, ttl=3600)

    initial_size_mb, initial_count = backend._calculate_cache_size()
    print(
        f"  Cache after fill: {initial_size_mb:.2f}MB/{max_size_mb}MB ({100 * initial_size_mb / max_size_mb:.0f}%), {initial_count} files"
    )

    # Now add more entries to push over 90% threshold and trigger eviction
    print("  Adding entries to trigger eviction at 90% threshold...")
    eviction_start = time.perf_counter()

    # Add entries until we push over threshold (each write checks and evicts)
    for i in range(num_entries, num_entries + 30):
        backend.set(f"evict:entry:{i}", b"x" * value_size, ttl=3600)

    eviction_elapsed = time.perf_counter() - eviction_start

    final_size_mb, final_count = backend._calculate_cache_size()

    print(f"\n{'=' * 70}")
    print("FileBackend Eviction Performance")
    print(f"{'=' * 70}")
    print(f"Initial cache: {initial_size_mb:.2f}MB ({100 * initial_size_mb / max_size_mb:.0f}%)")
    print(f"Initial files: {initial_count}")
    print(f"Final cache: {final_size_mb:.2f}MB ({100 * final_size_mb / max_size_mb:.0f}%)")
    print(f"Final files: {final_count}")
    print(f"Eviction time: {eviction_elapsed:.3f}s")
    print(f"Files removed: {initial_count + 30 - final_count}")

    # 80 x 90KB is 7MB, past the 5MB cap: without eviction the backend would hold all 80 entries.
    assert initial_count == num_entries, "the fill stayed under the 90% trigger, so nothing was evicted yet"
    assert final_count < num_entries + 30, "no entry was evicted"
    assert final_size_mb <= max_size_mb * 0.9, f"cache left at {final_size_mb:.2f}MB, above the 90% trigger"
    assert backend.get("evict:entry:0") is None, "the oldest entry survived eviction"
    assert backend.get(f"evict:entry:{num_entries + 29}") == b"x" * value_size, "the newest entry was evicted"


@pytest.mark.performance
def test_bench_vs_redis_backend(tmp_path: Path, perf_redis_backend) -> None:
    """Compare FileBackend with RedisBackend on the suite's pytest-redis server."""
    file_backend = FileBackend(
        FileBackendConfig(
            cache_dir=tmp_path,
            max_size_mb=1024,
            max_value_mb=100,
            max_entry_count=10_000,
        )
    )
    redis_backend = perf_redis_backend

    # Benchmark parameters
    num_ops = 500
    test_value = b"benchmark" * 100  # ~900 bytes

    print(f"\nBenchmarking FileBackend vs RedisBackend ({num_ops} ops)...")

    # Warm up both backends
    for i in range(50):
        file_backend.set(f"warmup:file:{i}", test_value, ttl=3600)
        redis_backend.set(f"warmup:redis:{i}", test_value, ttl=3600)

    def bench(backend, prefix: str) -> list[int]:
        latencies = []
        for i in range(num_ops):
            key = f"bench:{prefix}:{i}"
            start = time.perf_counter_ns()
            backend.set(key, test_value, ttl=3600)
            value = backend.get(key)
            deleted = backend.delete(key)
            latencies.append(time.perf_counter_ns() - start)
            assert value == test_value, f"{prefix} round trip returned {value!r}"
            assert deleted, f"{prefix} delete did not find {key}"
        return latencies

    file_stats = _calculate_stats(bench(file_backend, "file"))
    redis_stats = _calculate_stats(bench(redis_backend, "redis"))

    # Print results
    print(f"\n{'=' * 70}")
    print("FileBackend vs RedisBackend Comparison")
    print(f"{'=' * 70}")
    print(f"Operations per backend: {num_ops} (set + get + delete)\n")

    print("FileBackend:")
    _print_stats("  ", file_stats)

    print("\nRedisBackend:")
    _print_stats("  ", redis_stats)

    # Ratio > 1 means FileBackend took less time than RedisBackend
    print(f"\nRedis/File time ratio: {redis_stats['p50_us'] / file_stats['p50_us']:.1f}x (p50)")
    print(f"Redis/File time ratio: {redis_stats['p99_us'] / file_stats['p99_us']:.1f}x (p99)")

    assert all(redis_backend.get(f"bench:redis:{i}") is None for i in range(num_ops))


# Helper functions


def _calculate_stats(latencies: list[int]) -> dict[str, float]:
    """Calculate latency statistics from nanosecond measurements.

    Args:
        latencies: List of latencies in nanoseconds

    Returns:
        Dictionary with p50, p95, p99, mean, stdev in both ns and μs
    """
    if not latencies:
        return {
            "mean_ns": 0.0,
            "mean_us": 0.0,
            "p50_ns": 0.0,
            "p50_us": 0.0,
            "p95_ns": 0.0,
            "p95_us": 0.0,
            "p99_ns": 0.0,
            "p99_us": 0.0,
            "stdev_ns": 0.0,
            "stdev_us": 0.0,
        }

    mean = statistics.mean(latencies)
    stdev = statistics.stdev(latencies) if len(latencies) > 1 else 0.0
    p50 = statistics.median(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18]  # 95th percentile
    p99 = statistics.quantiles(latencies, n=100)[98]  # 99th percentile

    return {
        "mean_ns": mean,
        "mean_us": mean / 1000.0,
        "p50_ns": p50,
        "p50_us": p50 / 1000.0,
        "p95_ns": p95,
        "p95_us": p95 / 1000.0,
        "p99_ns": p99,
        "p99_us": p99 / 1000.0,
        "stdev_ns": stdev,
        "stdev_us": stdev / 1000.0,
    }


def _print_stats(indent: str, stats: dict[str, float]) -> None:
    """Print latency statistics in a formatted table.

    Args:
        indent: Indentation prefix
        stats: Statistics dictionary from _calculate_stats
    """
    print(f"{indent}Mean:   {stats['mean_us']:>10.2f} μs")
    print(f"{indent}P50:    {stats['p50_us']:>10.2f} μs")
    print(f"{indent}P95:    {stats['p95_us']:>10.2f} μs")
    print(f"{indent}P99:    {stats['p99_us']:>10.2f} μs")
    print(f"{indent}StdDev: {stats['stdev_us']:>10.2f} μs")


@pytest.mark.performance
@pytest.mark.slow
@pytest.mark.skipif(os.name != "posix", reason="mmap read path is POSIX-only (#171)")
def test_mmap_read_rss_far_below_full_read(tmp_path: Path) -> None:
    """The whole point of #171: get_buffer() must NOT materialize the payload on the heap.

    Mapping a large value and touching a few pages should cost a fraction of the RSS that
    os.read of the same value does. A relative comparison (mmap vs full read of the SAME payload
    in the SAME process) is far more robust to allocator/GC noise than an absolute RSS bound.
    """
    import gc

    import psutil

    proc = psutil.Process()
    size = 150 * 1024 * 1024  # 150 MB
    backend = FileBackend(FileBackendConfig(cache_dir=tmp_path / "c", max_size_mb=1024, max_value_mb=512))
    backend.set("big", b"\x00" * size, ttl=300)
    gc.collect()

    # Full read: os.read pulls the whole payload onto the heap.
    base = proc.memory_info().rss
    data = backend.get("big")
    assert data is not None and len(data) == size
    full_read_rss = proc.memory_info().rss - base
    del data
    gc.collect()

    # mmap read: only the few pages we touch are faulted in.
    base = proc.memory_info().rss
    handle = backend.get_buffer("big")
    assert handle is not None
    try:
        _ = handle.view[0]
        _ = handle.view[len(handle.view) // 2]
        _ = handle.view[-1]
        mmap_rss = proc.memory_info().rss - base
    finally:
        handle.close()

    print(f"\n  full-read RSS: {full_read_rss / 1e6:.1f} MB   mmap RSS: {mmap_rss / 1e6:.1f} MB")
    assert mmap_rss < full_read_rss * 0.5, f"mmap RSS {mmap_rss} not < 0.5x full-read RSS {full_read_rss}"


def _rss_anon() -> int:
    """Anonymous (heap) RSS: excludes the file-backed page cache an mmap read faults in."""
    with open("/proc/self/status") as status:
        return next(int(line.split()[1]) * 1024 for line in status if line.startswith("RssAnon:"))


@pytest.mark.performance
@pytest.mark.slow
@pytest.mark.skipif(not os.path.exists("/proc/self/status"), reason="reads RssAnon from /proc (Linux)")
def test_arrow_format_hit_allocates_no_heap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An uncompressed ArrowSerializer(return_format="arrow") hit returns a table over the mapped
    cache file: its pages are file-backed, so anonymous RSS barely moves however large the value.

    Measured on a 62 MB table: +0.0 MB per hit (and 0 bytes from the Arrow pool). The bound
    leaves room for allocator noise while failing on a copy of any numeric, timestamp or string
    column (16 MB or more). Arrow bit-packs the bool column into 250 KB, so a copy of that one
    alone would pass; the Arrow-pool assertion catches it.
    """
    import gc

    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    pa = pytest.importorskip("pyarrow")
    from cachekit import cache
    from cachekit.serializers.arrow_serializer import ArrowSerializer

    # Before 3.13 the handler keeps Arrow results on the copying read (each held mapping would hold a
    # file descriptor); the heap cost of the mapped read does not depend on the version.
    monkeypatch.setattr("cachekit.cache_handler.MMAP_RELEASES_FD", True)

    n = 2_000_000
    rng = np.random.default_rng(0)
    df = pd.DataFrame(
        {
            "i64": np.arange(n, dtype="int64"),
            "f64": rng.random(n),
            "s": pd.Series(rng.integers(0, 1000, n)).astype(str),
            "b": rng.random(n) < 0.5,
            "ts": pd.date_range("2024-01-01", periods=n, freq="s"),
        }
    )
    backend = FileBackend(FileBackendConfig(cache_dir=tmp_path / "c", max_size_mb=1024, max_value_mb=512))

    @cache(backend=backend, serializer=ArrowSerializer(return_format="arrow", compression=None), l1_enabled=False)
    def load():
        return df

    load()
    gc.collect()
    base, pool = _rss_anon(), pa.total_allocated_bytes()
    table = load()
    gc.collect()
    grew = _rss_anon() - base

    print(f"\n  arrow hit: {table.nbytes / 1e6:.1f} MB table, anon RSS +{grew / 1e6:.2f} MB")
    assert pa.total_allocated_bytes() == pool
    assert grew < 1_000_000, f"anon RSS grew {grew} bytes on a {table.nbytes}-byte zero-copy hit"
