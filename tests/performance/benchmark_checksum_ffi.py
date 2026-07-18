"""Benchmark: pure-Python xxhash package vs Rust-FFI checksum (cachekit-core#13).

Pinned contract (spec m-cachekit-core-checksum-only-api, Task 7): fixed sizes
64 B / 1 KB / 64 KB / 1 MB, >=30 iterations per point, per-size reporting.
This is a deliverable artifact, not a pass/fail gate — the crossover size is
the go/no-go datum for migrating serializer envelopes (Arrow/orjson) from the
py-xxhash package to the shared Rust FFI. Tiny inputs favoring in-process
Python is expected information, not failure: both implementations produce
byte-identical output (see tests/unit/test_checksum_ffi.py), so the choice
is purely a per-call-overhead question.

Run:
    uv run pytest tests/performance/benchmark_checksum_ffi.py \
        --benchmark-only --benchmark-group-by=group --benchmark-min-rounds=30

Results (2026-07-17, AMD Ryzen 9 5950X, CPython 3.13.12, median per call):

    checksum (compute)       py-xxhash    Rust FFI    winner
    64 B                      51.6 ns      39.7 ns    FFI  1.30x
    1 KB                      77.8 ns     130.0 ns    py   1.67x
    64 KB                     1.85 us      1.86 us    wash (~1%)
    1 MB                      28.1 us      28.7 us    wash (~2%)

    verify                   py-xxhash    Rust FFI    winner
    64 B                      88.1 ns      34.8 ns    FFI  2.53x
    1 KB                     125.0 ns      64.3 ns    FFI  1.94x
    64 KB                     1.87 us      1.78 us    FFI  ~5%
    1 MB                      28.0 us      27.9 us    wash

Crossover / go-no-go datum: there is NO size where either side wins by more
than ~65 ns/call on compute; both are throughput-bound and identical from
64 KB up. py-xxhash's C library has a stronger mid-size (240 B - 8 KB) path
than xxhash-rust, hence the 1 KB compute loss; the FFI wins verify at small
sizes because it is one boundary crossing instead of hash + compare in
Python. Verdict: serializer migration py-xxhash -> FFI is performance-neutral
(worst case ~50 ns/call against envelope operations measured in us-ms); decide
it on dependency hygiene, not speed.
"""

from __future__ import annotations

import pytest
import xxhash

from cachekit import _rust_serializer as rs

SIZES = [
    (64, "64B"),
    (1_024, "1KB"),
    (65_536, "64KB"),
    (1_048_576, "1MB"),
]

PAYLOADS = {label: bytes(i % 251 for i in range(size)) for size, label in SIZES}


@pytest.mark.benchmark
@pytest.mark.parametrize("label", [label for _, label in SIZES])
class TestChecksumComputeComparison:
    """py-xxhash vs Rust FFI, grouped per size for side-by-side comparison."""

    def test_python_xxhash(self, benchmark, label):
        data = PAYLOADS[label]
        benchmark.group = f"checksum-{label}"
        result = benchmark(xxhash.xxh3_64_digest, data)
        assert len(result) == 8

    def test_rust_ffi(self, benchmark, label):
        data = PAYLOADS[label]
        benchmark.group = f"checksum-{label}"
        result = benchmark(rs.checksum, data)
        assert len(result) == 8


@pytest.mark.benchmark
@pytest.mark.parametrize("label", [label for _, label in SIZES])
class TestVerifyComparison:
    """Verification path: python compare vs Rust FFI verify_checksum."""

    def test_python_xxhash_verify(self, benchmark, label):
        data = PAYLOADS[label]
        expected = xxhash.xxh3_64_digest(data)
        benchmark.group = f"verify-{label}"
        result = benchmark(lambda: xxhash.xxh3_64_digest(data) == expected)
        assert result is True

    def test_rust_ffi_verify(self, benchmark, label):
        data = PAYLOADS[label]
        expected = bytes(rs.checksum(data))
        benchmark.group = f"verify-{label}"
        result = benchmark(rs.verify_checksum, data, expected)
        assert result is True
