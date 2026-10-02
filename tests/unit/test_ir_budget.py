"""Gate arithmetic for the instruction budget (tests/performance/ir_budget.py); valgrind-free."""

from __future__ import annotations

import json
import time

import pytest

from tests.performance import ir_budget
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


def test_every_path_has_a_committed_budget() -> None:
    """A new path must land with its budget, or `make perf-ir` fails on every machine."""
    data = json.loads(ir_budget.BASELINES.read_text())
    for key, entry in data["interpreters"].items():
        assert set(entry["budgets"]) == set(PATHS), key


def test_pinned_clocks_tick_per_read_on_the_main_thread_only(monkeypatch) -> None:
    for name in ("time", "monotonic", "perf_counter"):
        monkeypatch.setattr(time, name, getattr(time, name))
        monkeypatch.setattr(time, f"{name}_ns", getattr(time, f"{name}_ns"))
    monkeypatch.setattr("random.seed", lambda *_: None)  # leave the suite's RNG alone
    ir_budget._pin_main_thread_clocks()

    a, b = time.perf_counter(), time.perf_counter()
    assert b - a == pytest.approx(1e-6, abs=1e-9)
    assert time.monotonic_ns() - time.monotonic_ns() == -1000

    import threading

    seen: list[float] = []
    worker = threading.Thread(target=lambda: seen.append(time.time()))
    worker.start()
    worker.join()
    assert seen[0] > 1.7e9 + 1e6  # the real clock: 2023-11 + 11 days at the very least
