"""The large-payload harness (tests/performance/large_payload_bench.py) still measures the path it names."""

from __future__ import annotations

import sys

import pytest

from tests.performance import large_payload_bench as bench


@pytest.mark.parametrize("path", bench.PATHS)
def test_worker_measures_the_named_path_in_a_fresh_process(path: str) -> None:
    # The worker raises if a hit computes or a miss does not, so a clean exit is the behavioural check.
    row = bench.spawn(sys.executable, {}, "json-64b", path, 0)
    assert row["cpu_us"] > 0
    assert row["faults"] >= 0
    assert row["maxrss_mb"] > 0
