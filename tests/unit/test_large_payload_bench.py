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


def test_payload_shapes_hit_their_sizes() -> None:
    for shape in bench.SHAPES:
        kind, size = shape.split("-")
        value = bench.payload(shape)
        if kind == "bin":
            assert len(value) == bench.SIZES[size]
        else:
            assert bench.SIZES[size] <= sum(map(len, value["reasons"])) + 32 < bench.SIZES[size] + 200


def test_parse_env_rejects_a_pair_without_a_value() -> None:
    with pytest.raises(SystemExit, match="KEY=VALUE"):
        bench.parse_env(["MALLOC_TOP_PAD_"])
