"""Gate arithmetic for the instruction budget (tests/performance/ir_budget.py); valgrind-free."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

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
    """A callgrind run peaks at up to a gigabyte, so the default must not grow with the core count."""
    assert 1 <= ir_budget.JOBS <= 8


# A stand-in for the measured process: records its pid, then sleeps unless it is a warm-up run (n=1).
SLEEPER = """
import os, sys, time
from pathlib import Path
Path(__file__).with_name("pids").joinpath(str(os.getpid())).write_text(sys.argv[2])
if sys.argv[2] != "1":
    time.sleep(60)
"""

# Runs the gate's measure() with the sleeper in place of callgrind + ir_workload.py.
DRIVER = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from tests.performance import ir_budget as b
b.WORKLOAD = Path(sys.argv[1])
b._measure_one = lambda path, n, workdir, env, *timeout: b._run([], path, n, env, *timeout) or 0
if len(sys.argv) > 3:  # signal itself while submitting the second run, the first one live
    import os, signal, time
    submit = b.ThreadPoolExecutor.submit
    calls = []
    def submit_then_signal(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            while sum(f.read_text() != "1" for f in Path(sys.argv[1]).with_name("pids").iterdir()) < 1:
                time.sleep(0.05)
            os.kill(os.getpid(), int(sys.argv[3]))
        return submit(self, *args, **kwargs)
    b.ThreadPoolExecutor.submit = submit_then_signal
b.measure(["l1_hit"], 2)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _sleeper(tmp_path: Path) -> tuple[Path, Path]:
    pids = tmp_path / "pids"
    pids.mkdir()
    script = tmp_path / "sleeper.py"
    script.write_text(SLEEPER)
    return script, pids


def test_a_run_past_the_child_timeout_fails_the_gate_and_is_killed(tmp_path, monkeypatch) -> None:
    script, pids = _sleeper(tmp_path)
    monkeypatch.setattr(ir_budget, "WORKLOAD", script)
    env = {"IR_BUDGET_LAYOUT": "48"}
    with pytest.raises(RuntimeError, match=r"l1_hit n=1000 layout=48 ran past --child-timeout \(0\.01 min\)"):
        ir_budget._run([], "l1_hit", 1000, env, timeout_s=0.6)
    [pid] = (int(f.name) for f in pids.iterdir())
    assert not _alive(pid)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_to_the_gate_leaves_no_run_behind(tmp_path, sig: signal.Signals) -> None:
    script, pids = _sleeper(tmp_path)
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    repo = Path(__file__).resolve().parents[2]
    gate = subprocess.Popen([sys.executable, str(driver), str(script), str(repo)])  # noqa: S603 (trusted: this test's files)
    try:
        deadline = time.monotonic() + 30
        while sum(f.read_text() != "1" for f in pids.iterdir()) < 2:  # both measured runs started
            assert gate.poll() is None and time.monotonic() < deadline, "the stub runs never started"
            time.sleep(0.05)
        gate.send_signal(sig)
        code = gate.wait(timeout=10)
        runs = [int(f.name) for f in pids.iterdir() if f.read_text() != "1"]
        deadline = time.monotonic() + 5
        while any(_alive(pid) for pid in runs) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not [pid for pid in runs if _alive(pid)], "a run outlived the gate"
        assert code == (128 + sig if sig == signal.SIGTERM else -sig)  # an uncaught KeyboardInterrupt re-raises SIGINT
    finally:
        gate.kill()
        for f in pids.iterdir():
            if _alive(int(f.name)):
                os.kill(int(f.name), signal.SIGKILL)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_while_runs_are_being_submitted_leaves_no_run_behind(tmp_path, sig: signal.Signals) -> None:
    """The pool waits for live runs when it unwinds, so they must be killed before it does."""
    script, pids = _sleeper(tmp_path)
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    repo = Path(__file__).resolve().parents[2]
    gate = subprocess.Popen([sys.executable, str(driver), str(script), str(repo), str(int(sig))])  # noqa: S603 (trusted: this test's files)
    try:
        code = gate.wait(timeout=20)  # the stub run sleeps 60 s: a gate that waits for it times out here
        runs = [int(f.name) for f in pids.iterdir() if f.read_text() != "1"]
        assert runs, "the stub run never started"
        deadline = time.monotonic() + 5
        while any(_alive(pid) for pid in runs) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not [pid for pid in runs if _alive(pid)], "a run outlived the gate"
        assert code == (128 + sig if sig == signal.SIGTERM else -sig)
    finally:
        gate.kill()
        for f in pids.iterdir():
            if _alive(int(f.name)):
                os.kill(int(f.name), signal.SIGKILL)


def test_every_path_has_a_committed_budget() -> None:
    """A new path must land with its budget, or `make perf-ir` fails on every machine."""
    data = json.loads(ir_budget.BASELINES.read_text())
    for key, entry in data["interpreters"].items():
        assert set(entry["budgets"]) == set(PATHS), key


def test_performance_docs_state_the_committed_budgets() -> None:
    """docs/performance.md's Budgets table is ir_baselines.json, figure for figure.

    A ratchet that rewrote the JSON and not the table once left seven rows stale, the worst by 60% (LAB-7801).
    """
    interpreters = json.loads(ir_budget.BASELINES.read_text())["interpreters"]
    columns = ("cpython-3.12-x86_64", "cpython-3.14-x86_64")  # the table's two figure columns, in order
    assert sorted(interpreters) == sorted(columns), "add or remove a column in docs/performance.md's Budgets table"
    lines = (ir_budget.BASELINES.parents[2] / "docs" / "performance.md").read_text().splitlines()
    rows = lines[lines.index("| Path | What one call does | CPython 3.12 | CPython 3.14 |") + 2 :]
    end = next(i for i, line in enumerate(rows) if not line.startswith("|"))
    table = {}
    for row in rows[:end]:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        table[cells[0].strip("`")] = tuple(int(cell.replace(",", "")) for cell in cells[2:])
    assert table == {path: tuple(interpreters[key]["budgets"][path] for key in columns) for path in PATHS}


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
