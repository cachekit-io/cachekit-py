"""Gate arithmetic for the instruction budget (tests/performance/ir_budget.py); valgrind-free."""

from __future__ import annotations

import json
import os
import platform
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
    assert per_op(2_000_000_000, 2_000_000_000 + 2000 * 75_000, 1000, 3000) == 75_000


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
@pytest.mark.parametrize("path", ["l1_hit", "serializer_orjson"])  # orjson had a 2% exception until its noise was fixed
def test_compare_thresholds(path: str, measured: int, verdict: str, ok: bool) -> None:
    lines, passed = compare({path: 100_000}, {path: measured})
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
if sys.argv[2] != "1" or Path(__file__).with_name("sleep-warmup").exists():
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
mode = sys.argv[4] if len(sys.argv) > 4 else ""
if mode == "spawn":  # signal itself as soon as the first run is spawned
    import os
    class SignalOnSpawn(b.subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            Path(sys.argv[1]).with_name("spawned").write_text(str(self.pid))
            os.kill(os.getpid(), int(sys.argv[3]))
    b.subprocess.Popen = SignalOnSpawn
elif mode == "submit":  # signal itself while submitting the second run, the first one live
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
elif mode == "again":  # signal itself again once the main thread takes the run lock after both runs started
    import os, threading
    class SignalOnceTaken:
        def __init__(self, lock):
            self.lock, self.fired = lock, False
        def __enter__(self):
            self.lock.acquire()
            if self.fired or threading.current_thread() is not threading.main_thread():
                return
            if sum(f.read_text() != "1" for f in Path(sys.argv[1]).with_name("pids").iterdir()) >= 2:
                self.fired = True
                os.kill(os.getpid(), int(sys.argv[3]))
        def __exit__(self, *exc):
            self.lock.release()
    b._children_lock = SignalOnceTaken(b._children_lock)
elif mode == "inside":  # signal itself again at the Nth line run inside the first _kill_children(), callees included
    import os, sys
    state = {"armed": False, "lines": 0}
    def line(frame, event, arg):
        if event == "line" and state["lines"] < int(sys.argv[5]):
            state["lines"] += 1
            if state["lines"] == int(sys.argv[5]):
                os.kill(os.getpid(), int(sys.argv[3]))
        return line
    def call(frame, event, arg):
        if frame.f_code is b._kill_children.__code__:
            state["armed"] = True
        return line if state["armed"] and state["lines"] < int(sys.argv[5]) else None
    sys.settrace(call)
elif mode == "thread":  # once the main thread sleeps collecting results, signal another thread, which does not wake it
    import signal, threading, time
    main = threading.main_thread()
    def asleep():  # where the main thread is, if it sleeps in the lock acquire of a Condition.wait() under measure()'s wait()
        top = frame = sys._current_frames().get(main.ident)
        while frame is not None and frame.f_code is not b.wait.__code__:
            frame = frame.f_back
        if frame is None or top.f_code is not threading.Condition.wait.__code__:
            return None
        stat = Path(f"/proc/self/task/{main.native_id}/stat").read_text()
        return (top, top.f_lasti) if stat[stat.rindex(")") + 2] == "S" else None
    def parked():
        # A signal sent before the main thread parks runs its handler at once and proves nothing. Asleep alone
        # also covers a wait for the GIL, but that thread takes the GIL while this one sleeps, so finding it
        # asleep at the same instruction of the same call after a sleep proves it is blocked in the acquire.
        seen = asleep()
        if seen is None:
            return False
        time.sleep(0.02)
        return asleep() == seen
    def signal_this_thread():
        while sum(f.read_text() != "1" for f in Path(sys.argv[1]).with_name("pids").iterdir()) < 2 or not parked():
            time.sleep(0.01)
        signal.pthread_kill(threading.get_ident(), int(sys.argv[3]))
    threading.Thread(target=signal_this_thread, daemon=True).start()
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


@pytest.mark.parametrize("minutes", ["0", "-1", "inf", "nan"])
def test_the_child_timeout_must_be_a_positive_number_of_minutes(minutes: str, monkeypatch, capsys) -> None:
    """0 or less times out every run, and inf or nan crash the first run instead of bounding it."""
    monkeypatch.setattr(sys, "argv", ["ir_budget.py", "--child-timeout", minutes])
    with pytest.raises(SystemExit, match="2"):
        ir_budget._main()
    assert "must be a positive number of minutes" in capsys.readouterr().err


def test_a_fractional_child_timeout_is_kept() -> None:
    assert ir_budget._positive_minutes("0.5") == 0.5


def test_a_failed_run_fails_the_gate_without_waiting_for_the_runs_before_it(tmp_path, monkeypatch) -> None:
    """Results are read in submission order, and an earlier run can take minutes: a failure must not queue behind it."""
    script, pids = _sleeper(tmp_path)
    monkeypatch.setattr(ir_budget, "WORKLOAD", script)
    monkeypatch.setattr(ir_budget, "_stopped", False)  # measure() leaves it set, and _run reads it

    def measure_one(path: str, n: int, workdir: Path, env: dict[str, str], timeout_s: float, *tree: object) -> int:
        if n == ir_budget.N_LO:  # the earlier run: sleeps 60 s
            ir_budget._run([], path, n, env, timeout_s, *tree)
            return 0
        deadline = time.monotonic() + 30
        while not any(f.read_text() != "1" for f in pids.iterdir()) and time.monotonic() < deadline:
            time.sleep(0.05)
        raise RuntimeError("stub run failed")

    monkeypatch.setattr(ir_budget, "_measure_one", measure_one)
    start = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="stub run failed"):
            ir_budget.measure(["l1_hit"], 2)
        assert time.monotonic() - start < 30, "the gate waited for the earlier run"
        runs = [int(f.name) for f in pids.iterdir() if f.read_text() != "1"]
        assert runs, "the earlier run never started"
        deadline = time.monotonic() + 5
        while any(_alive(pid) for pid in runs) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not [pid for pid in runs if _alive(pid)], "a run outlived the gate"
    finally:
        for f in pids.iterdir():
            if _alive(int(f.name)):
                os.kill(int(f.name), signal.SIGKILL)


@pytest.mark.parametrize("again", [False, True], ids=["once", "again-while-killing"])
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_to_the_gate_leaves_no_run_behind(tmp_path, sig: signal.Signals, again: bool) -> None:
    """A second signal that lands while the gate holds its run lock must not leave the lock held and the runs alive."""
    script, pids = _sleeper(tmp_path)
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    repo = Path(__file__).resolve().parents[2]
    extra = [str(int(sig)), "again"] if again else []
    gate = subprocess.Popen([sys.executable, str(driver), str(script), str(repo), *extra])  # noqa: S603 (trusted: this test's files)
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
    gate = subprocess.Popen([sys.executable, str(driver), str(script), str(repo), str(int(sig)), "submit"])  # noqa: S603 (trusted: this test's files)
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


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_between_spawning_a_run_and_tracking_it_leaves_no_run_behind(tmp_path, sig: signal.Signals) -> None:
    script, pids = _sleeper(tmp_path)
    (tmp_path / "sleep-warmup").touch()  # the warm-up run sleeps too, so an orphan stays visible
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    repo = Path(__file__).resolve().parents[2]
    gate = subprocess.Popen([sys.executable, str(driver), str(script), str(repo), str(int(sig)), "spawn"])  # noqa: S603 (trusted: this test's files)
    try:
        code = gate.wait(timeout=20)
        run = int((tmp_path / "spawned").read_text())  # the run may be killed before it records its own pid
        deadline = time.monotonic() + 5
        while _alive(run) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(run), "a run outlived the gate"
        assert code == (128 + sig if sig == signal.SIGTERM else -sig)
    finally:
        gate.kill()
        for f in [*pids.iterdir(), tmp_path / "spawned"]:
            pid = int(f.read_text() if f.name == "spawned" else f.name) if f.exists() else 0
            if pid and _alive(pid):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(sys.platform != "linux", reason="the driver reads thread states from /proc")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_that_does_not_wake_the_main_thread_still_stops_the_gate(tmp_path, sig: signal.Signals) -> None:
    """The handler runs only once the main thread wakes, and a signal can arrive without waking it.

    On a free-threaded build, a lock acquire spins before it sleeps and does not look for signals while it
    spins, so a signal that lands then is held until the thread next wakes. Under CPU load the spin is long:
    a gate blocked without a timeout slept through about 1 signal in 10 until its 60 s runs ended. A signal
    sent to another thread is held the same way, on any build, and this test sends it that way.
    """
    script, pids = _sleeper(tmp_path)
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    repo = Path(__file__).resolve().parents[2]
    gate = subprocess.Popen([sys.executable, str(driver), str(script), str(repo), str(int(sig)), "thread"])  # noqa: S603 (trusted: this test's files)
    try:
        code = gate.wait(timeout=20)  # the stub runs sleep 60 s: a gate that sleeps through the signal times out here
        runs = [int(f.name) for f in pids.iterdir() if f.read_text() != "1"]
        assert len(runs) >= 2, "the stub runs never started"  # a queued run can start before the pool shuts
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


@pytest.mark.parametrize("line", range(1, 13))
def test_a_second_signal_anywhere_inside_the_kill_leaves_no_run_behind(tmp_path, line: int) -> None:
    """The handler re-enters _kill_children() on the main thread, so nothing on that path may take a plain lock."""
    script, pids = _sleeper(tmp_path)
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    repo = Path(__file__).resolve().parents[2]
    sig = signal.SIGTERM
    gate = subprocess.Popen([sys.executable, str(driver), str(script), str(repo), str(int(sig)), "inside", str(line)])  # noqa: S603 (trusted: this test's files)
    try:
        deadline = time.monotonic() + 30
        while sum(f.read_text() != "1" for f in pids.iterdir()) < 2:  # both measured runs started
            assert gate.poll() is None and time.monotonic() < deadline, "the stub runs never started"
            time.sleep(0.05)
        gate.send_signal(sig)
        code = gate.wait(timeout=20)  # a handler blocked on a lock its own thread holds never returns
        runs = [int(f.name) for f in pids.iterdir() if f.read_text() != "1"]
        deadline = time.monotonic() + 5
        while any(_alive(pid) for pid in runs) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not [pid for pid in runs if _alive(pid)], "a run outlived the gate"
        assert code == 128 + sig
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

    A ratchet that rewrote the JSON and not the table once left seven rows stale, the worst by 60%.
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


def test_batched_metrics_path_records_every_call_per_thread_and_queues_none(monkeypatch) -> None:
    """``l2_hit_async_metrics`` budgets a batched collector, whose cache operations record per thread, not on the queue."""
    pytest.importorskip("pandas")  # the workload builder makes the Arrow path's DataFrame too
    from cachekit.reliability import async_metrics
    from cachekit.reliability.async_metrics import AsyncMetricsCollector

    made: list[AsyncMetricsCollector] = []
    real_init = AsyncMetricsCollector.__init__

    def tracking_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        made.append(self)

    direct: Counter[str] = Counter()
    monkeypatch.setattr(AsyncMetricsCollector, "__init__", tracking_init)
    monkeypatch.setattr(AsyncMetricsCollector, "_record_cache_operation_direct", lambda *a, **k: direct.update(["direct"]))
    op = ir_workload.build_workload("l2_hit_async_metrics")
    (collector,) = made
    before = collector._operation_count
    for _ in range(20):
        op()
    assert async_metrics._PER_THREAD
    assert (collector._sync_mode, collector._queue.qsize(), collector._operation_count - before) == (False, 0, 20)
    assert direct == {}


def test_backed_l1_hit_path_is_a_plaintext_l1_hit_on_held_msgpack(monkeypatch) -> None:
    """``l1_hit_backed`` budgets an L1 hit with a backend: after warmup, every call finds its key in L1, holding
    the verified MessagePack, so it never reaches L2."""
    pytest.importorskip("pandas")  # the workload builder makes the Arrow path's DataFrame too
    import cachekit
    from cachekit.l1_cache import L1Cache
    from cachekit.serializers.standard_serializer import VerifiedMsgpack

    entries: list[object] = []
    real_get = L1Cache.get

    def tracking_get(self, key):
        found, entry = real_get(self, key)
        entries.append(entry if found else None)
        return found, entry

    # build_workload imports cache at call time, so this wrapper sees the backend the path is built with.
    backend_reads: list[str] = []
    real_cache = cachekit.cache

    def counting_cache(*args, backend, **kwargs):
        real_backend_get = backend.get

        def counting_get(key):
            backend_reads.append(key)
            return real_backend_get(key)

        backend.get = counting_get
        return real_cache(*args, backend=backend, **kwargs)

    monkeypatch.setattr(L1Cache, "get", tracking_get)
    monkeypatch.setattr(cachekit, "cache", counting_cache)
    op = ir_workload.build_workload("l1_hit_backed")
    for _ in range(3):  # warmup: the miss stores the envelope, the first hit swaps in the MessagePack
        op()
    assert backend_reads, "the warmup miss reads the backend, so the count reaches it"
    entries.clear()
    backend_reads.clear()
    for _ in range(20):
        assert op() == {"id": 42, "name": "Ada Lovelace", "roles": ["admin", "ops"], "score": 97.5, "tags": list(range(16))}
    assert len(entries) == 20
    assert all(type(entry) is VerifiedMsgpack for entry in entries)
    assert backend_reads == []


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


def test_pinned_os_entropy_starts_numpy_in_the_same_state_in_every_run() -> None:
    """numpy.random seeds its global generator from OS entropy at import, which moved every path's count."""
    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]); import ir_workload\n"
        "if sys.argv[2] == 'pin': ir_workload.pin_os_entropy()\n"
        "import numpy.random; print(numpy.random.get_state()[1][:8].tolist())"
    )
    perf = str(Path(ir_workload.__file__).parent)

    def first_words(mode: str) -> str:
        run = [sys.executable, "-c", probe, perf, mode]
        return subprocess.run(run, capture_output=True, text=True, check=True).stdout  # noqa: S603 (trusted: this test's probe)

    assert first_words("pin") == first_words("pin")
    assert first_words("os") != first_words("os")  # unpinned, numpy's start does come from OS entropy


@pytest.mark.parametrize("n", [1, 4, len(PATHS) + 1])
def test_shards_split_the_paths_exactly_once(n: int) -> None:
    """N machines running --shard 1/N .. N/N measure every path that is not skipped, and none twice."""
    skip = ["serializer_arrow_usgs", "serializer_auto_usgs"]
    shards = [ir_budget.select_paths(None, skip, ir_budget._shard(f"{k}/{n}")) for k in range(1, n + 1)]
    assert sorted(p for shard in shards for p in shard) == sorted(p for p in PATHS if p not in skip)


@pytest.mark.parametrize("text", ["0/4", "5/4", "1", "a/b", "-1/2"])
def test_a_shard_must_be_k_of_n(text: str) -> None:
    with pytest.raises(ir_budget.argparse.ArgumentTypeError):
        ir_budget._shard(text)


def _trees(tmp_path: Path, base_paths: tuple[str, ...], base_name: str = "base") -> Path:
    """A head and a base checkout side by side, the base with a venv interpreter and its baselines."""
    for tree in ("head", base_name):
        (tmp_path / tree / "tests" / "performance").mkdir(parents=True)
        (tmp_path / tree / "tests" / "performance" / "ir_workload.py").touch()
    base = tmp_path / base_name
    (base / ".venv" / "bin").mkdir(parents=True)
    (base / ".venv" / "bin" / "python").symlink_to(sys.executable)
    # The base's committed budgets are this checkout's, less the paths the base does not have yet.
    data = json.loads(ir_budget.BASELINES.read_text())
    for entry in data["interpreters"].values():
        entry["budgets"] = {p: b for p, b in entry["budgets"].items() if p in base_paths}
    (base / "tests" / "performance" / "ir_baselines.json").write_text(json.dumps(data))
    return base


def test_against_base_gates_this_checkout_on_the_base_measured_in_the_same_run(tmp_path, monkeypatch, capsys) -> None:
    base = _trees(tmp_path, ("l1_hit", "l2_hit"))
    monkeypatch.setattr(ir_budget, "WORKLOAD", tmp_path / "head" / "tests" / "performance" / "ir_workload.py")
    calls = []

    def fake_measure(paths, jobs, timeout_s, python, workload):
        calls.append((tuple(paths), python, workload))
        on_base = python == str(base / ".venv" / "bin" / "python")
        return {"l1_hit": 1000, "l2_hit": 1000 if on_base else 1010}

    monkeypatch.setattr(ir_budget, "measure", fake_measure)
    report = tmp_path / "report.json"
    assert ir_budget._against_base(base, ["l1_hit", "l2_hit", "file_set"], 2, 60, report) == 1
    out = capsys.readouterr().out
    assert "FAIL  l2_hit" in out and "base     1,000  +1.00%" in out
    assert "new   file_set" in out  # absent from the base: reported, not measured
    assert json.loads(report.read_text()) == {
        "verdict": "FAIL",
        "interpreter": ir_budget.interpreter_key(),
        "python": platform.python_version(),
        "rows": [
            {"path": "l1_hit", "verdict": "ok", "ir": 1000, "budget": 1000, "pct": 0.0},
            {"path": "l2_hit", "verdict": "FAIL", "ir": 1010, "budget": 1000, "pct": 1.0},
            {"path": "file_set", "verdict": "new", "ir": None, "budget": None, "pct": None},
        ],
    }
    assert calls == [
        (("l1_hit", "l2_hit"), sys.executable, ir_budget.WORKLOAD.resolve()),
        (("l1_hit", "l2_hit"), str(base / ".venv" / "bin" / "python"), base / "tests" / "performance" / "ir_workload.py"),
    ]


def test_against_base_writes_no_report_without_a_verdict(tmp_path, monkeypatch) -> None:
    """CI reads a missing report as a shard with no result, so an environment error must not write one."""
    base = _trees(tmp_path, ("l1_hit",))
    (base / ".venv" / "bin" / "python").unlink()
    monkeypatch.setattr(ir_budget, "WORKLOAD", tmp_path / "head" / "tests" / "performance" / "ir_workload.py")
    assert ir_budget._against_base(base, ["l1_hit"], 2, 60, tmp_path / "report.json") == 2
    assert not (tmp_path / "report.json").exists()


def test_report_needs_base(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["ir_budget.py", "--report", "r.json"])
    with pytest.raises(SystemExit):
        ir_budget._main()
    assert "--report writes the --base verdict" in capsys.readouterr().err


def test_against_base_refuses_a_base_whose_path_length_differs(tmp_path, monkeypatch, capsys) -> None:
    """Path length moves the measured process's heap layout, so it would be a verdict on the paths."""
    base = _trees(tmp_path, ("l1_hit",), base_name="base-longer")
    monkeypatch.setattr(ir_budget, "WORKLOAD", tmp_path / "head" / "tests" / "performance" / "ir_workload.py")
    monkeypatch.setattr(ir_budget, "measure", lambda *a: pytest.fail("measured despite the mismatch"))
    assert ir_budget._against_base(base, ["l1_hit"], 2, 60) == 2
    assert "must be as long as" in capsys.readouterr().err


def test_against_base_refuses_a_base_without_its_own_venv(tmp_path, monkeypatch, capsys) -> None:
    base = _trees(tmp_path, ("l1_hit",))
    (base / ".venv" / "bin" / "python").unlink()
    monkeypatch.setattr(ir_budget, "WORKLOAD", tmp_path / "head" / "tests" / "performance" / "ir_workload.py")
    monkeypatch.setattr(ir_budget, "measure", lambda *a: pytest.fail("measured without a base venv"))
    assert ir_budget._against_base(base, ["l1_hit"], 2, 60) == 2
    assert "uv sync" in capsys.readouterr().err


def test_against_base_ignores_the_committed_budgets(tmp_path, monkeypatch, capsys) -> None:
    """A budget diff cannot tell a deliberate increase from build drift, so it never moves the CI verdict."""
    base = _trees(tmp_path, ("l1_hit",))
    baselines = base / "tests" / "performance" / "ir_baselines.json"
    data = json.loads(baselines.read_text())
    for entry in data["interpreters"].values():
        entry["budgets"]["l1_hit"] //= 2  # the PR "raised" l1_hit's committed budget 100%
    baselines.write_text(json.dumps(data))
    monkeypatch.setattr(ir_budget, "WORKLOAD", tmp_path / "head" / "tests" / "performance" / "ir_workload.py")
    on_base = str(base / ".venv" / "bin" / "python")
    monkeypatch.setattr(ir_budget, "measure", lambda paths, *a: {"l1_hit": 100_000 if a[2] == on_base else 102_000})
    assert ir_budget._against_base(base, ["l1_hit"], 2, 60) == 1
    assert "FAIL  l1_hit" in capsys.readouterr().out
