"""Gate arithmetic for the instruction budget (tests/performance/ir_budget.py); valgrind-free."""

from __future__ import annotations

import json
import time
from collections import Counter

import pytest

from tests.performance import ir_budget, ir_workload
from tests.performance.ir_budget import PATHS, compare, main_thread_ir, per_op, ratchet


def test_per_op_is_the_difference_over_the_extra_iterations() -> None:
    assert per_op(2_000_000_000, 2_000_000_000 + 2000 * 75_000) == 75_000


def test_main_thread_ir_reads_thread_one_totals(tmp_path) -> None:
    (tmp_path / "cg-01").write_text("events: Ir\nsummary: 999\ntotals: 1234567\n")
    (tmp_path / "cg-02").write_text("totals: 1\n")
    assert main_thread_ir(tmp_path / "cg") == 1234567


def test_main_thread_ir_without_thread_one_names_what_callgrind_wrote(tmp_path) -> None:
    (tmp_path / "cg-02").write_text("totals: 1\n")
    with pytest.raises(RuntimeError, match=r"no main-thread file cg-01: callgrind wrote \['cg-02'\]"):
        main_thread_ir(tmp_path / "cg")


def test_main_thread_ir_without_totals_raises(tmp_path) -> None:
    (tmp_path / "cg-01").write_text("events: Ir\n")
    with pytest.raises(RuntimeError, match="no totals"):
        main_thread_ir(tmp_path / "cg")


@pytest.mark.parametrize(
    ("measured", "verdict", "ok"),
    [
        (100_000, "ok", True),
        (100_199, "ok", True),
        (100_200, "WARN", True),
        (100_999, "WARN", True),
        (101_000, "FAIL", False),
        (99_000, "LOWER", True),
    ],
)
def test_compare_thresholds(measured: int, verdict: str, ok: bool) -> None:
    lines, passed = compare({"l1_hit": 100_000}, {"l1_hit": measured})
    assert lines[0].split()[0] == verdict
    assert passed is ok


@pytest.mark.parametrize(
    ("measured", "verdict", "ok"),
    [(100_999, "ok", True), (101_000, "WARN", True), (101_999, "WARN", True), (102_000, "FAIL", False), (98_000, "LOWER", True)],
)
def test_compare_gates_orjson_at_its_own_tolerance(measured: int, verdict: str, ok: bool) -> None:
    lines, passed = compare({"serializer_orjson": 100_000}, {"serializer_orjson": measured})
    assert lines[0].split()[0] == verdict
    assert passed is ok


def test_compare_fails_a_path_without_a_budget() -> None:
    lines, passed = compare({}, {"l1_hit": 1})
    assert not passed
    assert "no budget" in lines[0]


def test_ratchet_only_lowers_unless_increase_allowed() -> None:
    budgets = {"l1_hit": 100, "miss": 100}
    assert ratchet(budgets, {"l1_hit": 90, "miss": 110, "l2_hit": 50}, allow_increase=False) == {
        "l1_hit": 90,
        "miss": 100,
        "l2_hit": 50,
    }
    assert ratchet(budgets, {"miss": 110}, allow_increase=True)["miss"] == 110
    assert budgets == {"l1_hit": 100, "miss": 100}  # input untouched


def test_default_parallelism_stays_small_on_a_big_machine() -> None:
    """Each callgrind run holds about half a gigabyte, so the default must not grow with the core count."""
    assert 1 <= ir_budget.JOBS <= 8


def test_every_path_has_a_committed_budget() -> None:
    """A new path must land with its budget, or `make perf-ir` fails on every machine."""
    data = json.loads(ir_budget.BASELINES.read_text())
    for key, entry in data["interpreters"].items():
        assert set(entry["budgets"]) == set(PATHS), key


def test_batched_metrics_path_queues_every_call_and_never_records_synchronously(monkeypatch) -> None:
    """``l2_hit_async_metrics`` claims to budget batched mode; a collector change must not turn it synchronous."""
    pytest.importorskip("pandas")  # the workload builder makes the Arrow path's DataFrame too
    from cachekit.reliability.async_metrics import AsyncMetricsCollector

    calls: Counter[str] = Counter()

    def counting(mode: str):
        real = getattr(AsyncMetricsCollector, f"_record_cache_operation_{mode}")

        def record(self, *args, **kwargs):
            calls[mode] += 1
            return real(self, *args, **kwargs)

        return record

    for mode in ("sync", "async"):
        monkeypatch.setattr(AsyncMetricsCollector, f"_record_cache_operation_{mode}", counting(mode))
    op = ir_workload.build_workload("l2_hit_async_metrics")
    calls.clear()
    for _ in range(20):
        op()
    assert calls == {"async": 20}


def test_pinned_clocks_tick_per_read_on_the_main_thread_only(monkeypatch) -> None:
    for name in ("time", "monotonic", "perf_counter"):
        monkeypatch.setattr(time, name, getattr(time, name))
        monkeypatch.setattr(time, f"{name}_ns", getattr(time, f"{name}_ns"))
    monkeypatch.setattr("random.seed", lambda *_: None)  # leave the suite's RNG alone
    ir_workload.pin_main_thread_clocks()

    a, b = time.perf_counter(), time.perf_counter()
    assert b - a == pytest.approx(1e-6, abs=1e-9)
    assert time.monotonic_ns() - time.monotonic_ns() == -1000

    import threading

    seen: list[float] = []
    worker = threading.Thread(target=lambda: seen.append(time.time()))
    worker.start()
    worker.join()
    assert seen[0] > 1.7e9 + 1e6  # the real clock: 2023-11 + 11 days at the very least
