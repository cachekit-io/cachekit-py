"""The large-payload harness (tests/performance/large_payload_bench.py) still measures the path it names.

CI runs no doctests under tests/performance, so the harness's own checks live here.
"""

from __future__ import annotations

import argparse
import sys

import pytest

from tests.performance import large_payload_bench as bench


@pytest.mark.skipif(sys.platform != "linux", reason="the worker counts faults with Linux's RUSAGE_THREAD")
@pytest.mark.parametrize("path", bench.PATHS)
def test_worker_measures_the_named_path_in_a_fresh_process(path: str) -> None:
    # The worker raises if a hit computes or a miss does not, so a clean exit is the behavioural check.
    row = bench.spawn(sys.executable, {}, "json-64b", path, 0)
    assert row["cpu_us"] > 0
    assert row["faults"] >= 0
    assert row["maxrss_mb"] > 0


def test_payload_shapes_hit_their_sizes() -> None:
    for shape in bench.SHAPES:
        kind, size = shape.split("-")
        value = bench.payload(shape)
        if kind == "bin":
            assert len(value) == bench.SIZES[size]
        else:
            assert bench.SIZES[size] <= sum(map(len, value["reasons"])) + 32 < bench.SIZES[size] + 200


def test_an_arm_drops_the_calling_shells_allocator_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # Exported settings reaching the default arm would make a tuned-vs-default A/B tuned-vs-tuned.
    monkeypatch.setenv("MALLOC_MMAP_THRESHOLD_", "4194304")
    monkeypatch.setenv("GLIBC_TUNABLES", "glibc.malloc.trim_threshold=16777216")
    assert not any(k.startswith("MALLOC_") or k == "GLIBC_TUNABLES" for k in bench.allocator_env({}))
    assert bench.allocator_env({"MALLOC_TOP_PAD_": "1"})["MALLOC_TOP_PAD_"] == "1"


def test_parse_env_rejects_a_pair_without_a_value() -> None:
    with pytest.raises(SystemExit, match="KEY=VALUE"):
        bench.parse_env(["MALLOC_TOP_PAD_"])


def test_a_single_pair_is_refused() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="at least 2"):
        bench.at_least_two("1")


def test_paired_ci_brackets_the_mean_and_excludes_zero_for_a_consistent_drop() -> None:
    lo, hi = bench.paired_ci([-3.0, -2.5, -3.5, -2.0, -4.0] * 6)
    assert lo < -3.0 < hi < 0
    assert bench.verdict((lo, hi)) == "lower"
