"""Whenever the collector is batched, exactly one live worker of its own process must be reading its queue."""

import ast
import multiprocessing
import os
import queue
import signal
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from cachekit.reliability import async_metrics
from cachekit.reliability.async_metrics import PROMETHEUS_AVAILABLE, AsyncMetricsCollector
from tests.utils.fork_helpers import child_outcome, fork_dumping_stacks, report, run_fresh

pytestmark = pytest.mark.skipif(not PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")
needs_fork = pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")


def _steer(collector: AsyncMetricsCollector, ops_per_second: float) -> None:
    # The rate is a lifetime average, so fix it through its inputs and make the next record run the mode check.
    collector._start_time = time.time() - 100.0
    collector._operation_count = int(ops_per_second * 100)
    collector._last_mode_check = 0.0


def _record(collector: AsyncMetricsCollector, namespace: str) -> None:
    collector.record_cache_operation(operation="get", namespace=namespace, success=True, duration_ms=1.0)


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def _flushed(namespace: str) -> float:
    return _sample(
        "cache_operations_total", {"operation": "get", "namespace": namespace, "success": "True", "serializer": "unknown"}
    )


def test_records_after_returning_to_batched_mode_reach_their_metric():
    n = 10
    namespace = f"mode-switch-cycle-{uuid.uuid4().hex}"
    other = f"mode-switch-cycle-other-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(flush_interval=0.05, max_queue_size=n * 10)
    assert collector._sync_mode

    _steer(collector, 500)
    _record(collector, other)
    assert not collector._sync_mode
    first_worker, q = collector._worker_thread, collector._queue
    assert first_worker is not None and first_worker.is_alive()

    _steer(collector, 1)
    _record(collector, other)
    assert collector._sync_mode
    first_worker.join(timeout=5.0)
    assert not first_worker.is_alive()

    before = _flushed(namespace)
    _steer(collector, 500)
    for _ in range(n):
        _record(collector, namespace)

    assert not collector._sync_mode
    worker = collector._worker_thread
    assert worker is not None and worker is not first_worker and worker.is_alive()
    # The queue is reused, never replaced or cleared, so a producer never sees it missing.
    assert collector._queue is q
    collector.shutdown()
    assert not worker.is_alive()
    assert _flushed(namespace) - before == n
    assert collector.get_dropped_metrics_count() == 0


class _SignallingQueue(queue.Queue):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered_get = threading.Event()

    def get(self, *args, **kwargs):
        self.entered_get.set()
        return super().get(*args, **kwargs)


def test_no_new_worker_starts_while_the_previous_one_is_alive(monkeypatch):
    namespace = f"mode-switch-wait-{uuid.uuid4().hex}"
    fake_queue = SimpleNamespace(Queue=_SignallingQueue, Empty=queue.Empty, Full=queue.Full)
    monkeypatch.setattr(async_metrics, "queue", fake_queue)
    # A long get() timeout keeps the stopped worker alive until it is fed a record.
    collector = AsyncMetricsCollector(flush_interval=30.0, max_queue_size=100, sync_mode=False)
    old_worker, q = collector._worker_thread, collector._queue
    assert old_worker is not None and isinstance(q, _SignallingQueue)
    # Stop the worker only once it waits in get(). Stopped before its first loop check, it skips the loop and
    # snapshots an empty queue for its exit drain, so the record put below would be left behind.
    assert q.entered_get.wait(5)

    _steer(collector, 1)
    _record(collector, namespace)
    assert collector._sync_mode and old_worker.is_alive()

    _steer(collector, 500)
    _record(collector, namespace)
    # The old worker has not exited, so the collector stays synchronous and retries at the next check.
    assert collector._sync_mode
    assert collector._worker_thread is old_worker

    counter = _metric_name(namespace)
    q.put_nowait({"type": "counter", "name": counter, "labels": {}, "value": 1.0})
    old_worker.join(timeout=5.0)
    assert not old_worker.is_alive()
    # The old worker's exit drain flushed the record it was woken with.
    assert _sample(f"{counter}_total") == 1

    _steer(collector, 500)
    _record(collector, namespace)
    assert not collector._sync_mode
    assert collector._worker_thread is not old_worker and collector._worker_thread.is_alive()
    collector.shutdown()


def test_concurrent_switches_start_one_worker(monkeypatch):
    collector = AsyncMetricsCollector(flush_interval=0.05)
    assert collector._sync_mode
    started = []
    real_thread = threading.Thread

    class SlowStartThread(real_thread):
        def start(self):
            started.append(self)
            # Widen the window between creating the worker and it running, so an unlocked switch races.
            time.sleep(0.05)
            super().start()

    fake_threading = SimpleNamespace(Thread=SlowStartThread, Event=threading.Event, Lock=threading.Lock)
    monkeypatch.setattr(async_metrics, "threading", fake_threading)
    _steer(collector, 500)
    barrier = threading.Barrier(2)

    def switch():
        barrier.wait()
        collector._maybe_switch_mode()

    threads = [real_thread(target=switch) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not collector._sync_mode
    assert len(started) == 1
    collector.shutdown()


@needs_fork
def test_child_forked_mid_switch_can_still_switch(monkeypatch):
    namespace = f"mode-switch-fork-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(flush_interval=0.05)
    assert collector._sync_mode
    parent_pid = os.getpid()
    inside, release = threading.Event(), threading.Event()

    class ThreadThatBlocksInParent(threading.Thread):
        def start(self):
            # Park the parent's switching thread inside the switch, holding the mode lock, while the process forks.
            # Park before the worker starts: a running worker can hold the queue's mutex across the fork, which
            # hangs the child on the queue instead of testing the mode lock.
            if os.getpid() == parent_pid:
                inside.set()
                release.wait(5)
            super().start()

    fake_threading = SimpleNamespace(Thread=ThreadThatBlocksInParent, Event=threading.Event, Lock=threading.Lock)
    monkeypatch.setattr(async_metrics, "threading", fake_threading)
    _steer(collector, 500)
    switcher = threading.Thread(target=_record, args=(collector, namespace), daemon=True)
    switcher.start()
    assert inside.wait(5)

    pid = os.fork()
    if pid == 0:  # child: SIGALRM kills it if the switch blocks on the lock the parked thread holds
        try:
            signal.alarm(5)
            _steer(collector, 500)
            _record(collector, namespace)
            os._exit(0 if not collector._sync_mode else 1)
        finally:
            os._exit(2)
    release.set()
    switcher.join(5)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    collector.shutdown()


def test_mode_switch_after_shutdown_starts_no_worker():
    collector = AsyncMetricsCollector(flush_interval=0.05, sync_mode=False)
    namespace = f"mode-switch-after-shutdown-{uuid.uuid4().hex}"
    _steer(collector, 1)
    _record(collector, namespace)
    assert collector._sync_mode
    collector.shutdown()

    _steer(collector, 500)
    _record(collector, namespace)

    # shutdown() is final: a later rise in the rate must not bring a worker back.
    assert collector._sync_mode
    assert collector._worker_thread is not None and not collector._worker_thread.is_alive()


@pytest.mark.parametrize("ops_per_second", [None, 500], ids=["auto-detect-off", "high-rate"])
def test_records_after_shutdown_reach_their_metric(ops_per_second):
    n = 15
    namespace = f"after-shutdown-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(
        flush_interval=0.05, max_queue_size=10, sync_mode=False, auto_detect_mode=ops_per_second is not None
    )
    collector.shutdown()

    # More records than the queue holds: queued behind the stopped worker, the last five would be dropped.
    for _ in range(n):
        if ops_per_second is not None:
            _steer(collector, ops_per_second)
        _record(collector, namespace)

    assert collector._sync_mode
    assert _flushed(namespace) == n
    assert collector.get_dropped_metrics_count() == 0


def test_sync_records_make_no_getpid_call(monkeypatch):
    collector = AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False)
    namespace = f"sync-no-getpid-{uuid.uuid4().hex}"
    _record(collector, namespace)  # creating a metric looks up this process's lock; recording into it does not
    calls = []

    def getpid():
        calls.append(None)
        return os.getpid()

    monkeypatch.setattr(async_metrics, "os", SimpleNamespace(getpid=getpid))
    for _ in range(10):
        _record(collector, namespace)

    # The fork check costs a syscall, so it stays on the paths that touch batching state.
    assert calls == []


def _in_child_forked_holding(lock: Any, child: Callable[[], Any]) -> Any:
    """Return what ``child`` returns in a process forked while a thread of this one holds ``lock``."""
    ctx = multiprocessing.get_context("fork")
    results = ctx.Queue()
    with lock:
        process = ctx.Process(target=lambda: results.put(child()), daemon=True)
        process.start()
    try:
        return results.get(timeout=10)
    except queue.Empty:
        process.kill()
        pytest.fail("the forked child hung on a lock it inherited held")
    finally:
        process.join(timeout=5)


def _in_fresh_interpreter(scenario: Callable[..., object], *args: object) -> object:
    """scenario(*args)'s outcome, from a fresh interpreter that runs no thread but the scenario's own (run_fresh).

    For every fork made from C: pytest's interpreter runs threads it cannot end or park (xdist's, other tests'), and
    one waiting for the GIL at such a fork hangs the child in no cachekit code, more often the more of them there are.
    """
    run = run_fresh(scenario, *args)
    if run.returncode:
        pytest.fail(f"{scenario.__name__}{args!r} failed in a fresh interpreter:\n{run.stderr}")
    return ast.literal_eval(run.stdout)


# The only threads a fork made from C may run beside, each blocked in a lock wait it cannot leave during the fork
# (_until_waiting): a thread that wants the GIL at the fork can hang the child.
_PARKED_THREADS = ("AsyncMetricsWorker", "parked")


def _until_waiting(cond: threading.Condition, count: int) -> None:
    """Return once count threads block in cond.wait(), so none of them wants the GIL at a fork made from C."""
    deadline = time.monotonic() + 10
    while len(cond._waiters) < count:  # a waiter is listed just before it blocks
        if time.monotonic() > deadline:
            raise RuntimeError(f"only {len(cond._waiters)} of {count} threads waited on {cond}")
        time.sleep(0.001)
    time.sleep(0.05)  # gives up the GIL, so a thread listed but not yet blocked takes it and blocks


def _parked_batched_collector() -> AsyncMetricsCollector:
    """A batched collector whose worker waits on its empty queue for an hour, past any fork the test makes."""
    collector = AsyncMetricsCollector(flush_interval=3600.0, sync_mode=False)
    assert collector._queue is not None
    _until_waiting(collector._queue.not_empty, 1)
    return collector


def _in_c_forked_child_holding(lock: Any, child: Callable[[], Any], timeout: float = 20.0) -> Any:
    """Like ``_in_child_forked_holding``, but fork from C, skipping Python's after-fork handling as uWSGI can.

    Call it only from a scenario run by ``_in_fresh_interpreter``, beside no thread but parked ones (_PARKED_THREADS).
    ``child`` must not start a thread: CPython's own after-fork repair has not run either, so a thread started
    there can hang or crash the interpreter. A child still running at ``timeout`` writes every thread's stack to
    stderr and is killed, so a hang fails its test showing where it hung, even where no Python can run.
    """
    import ctypes

    me = threading.current_thread()
    strays = [t for t in threading.enumerate() if t is not me and t.name not in _PARKED_THREADS]
    if strays:  # not an assert: python -O strips those
        raise RuntimeError(f"a thread that may want the GIL would be alive at the fork: {strays}")
    libc_fork = ctypes.PyDLL(None).fork  # PyDLL keeps the GIL through the call
    r, w = os.pipe()
    with lock:
        pid, armed = fork_dumping_stacks(libc_fork)
        if pid == 0:
            try:
                os.close(r)
                report(w, child())
            except BaseException:
                traceback.print_exc()
                sys.stderr.flush()
            finally:
                os._exit(2)
    os.close(w)
    outcome = child_outcome(pid, r, timeout=timeout, dump_stacks=armed)
    if isinstance(outcome, str) and outcome.startswith("no outcome: "):
        pytest.fail(f"the child of a fork made from C hung or failed: {outcome}")
    return outcome


needs_c_fork = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs glibc fork() via ctypes")


def _metric_name(namespace: str) -> str:
    return namespace.replace("-", "_")


# Each entry point, how to read its series, and the value after one record in the child and one more batched.
ENTRY_POINTS = {
    "cache-operation": (_record, _flushed, 2),
    "counter": (lambda c, n: c.record_counter(_metric_name(n)), lambda n: _sample(f"{_metric_name(n)}_total"), 2),
    "histogram": (lambda c, n: c.record_histogram(_metric_name(n), 1.0), lambda n: _sample(f"{_metric_name(n)}_count"), 2),
}


@needs_fork
@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("held", ["queue", "pool"])
def test_child_of_a_batched_parent_records_past_a_lock_held_at_fork(held, entry):
    record, read, after_batched = ENTRY_POINTS[entry]
    namespace = f"fork-batched-{held}-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(flush_interval=0.05, sync_mode=False)
    assert collector._queue is not None
    lock = collector._queue.mutex if held == "queue" else collector._pool_lock

    def child():
        collector._last_mode_check = time.time()  # the first record takes the batched path, not a mode check
        record(collector, namespace)
        first = read(namespace)  # recorded synchronously: the child has no worker yet to flush a queued one
        _steer(collector, 500)
        record(collector, namespace)  # batched again, on a queue and pool of the child's own
        batched = not collector._sync_mode
        collector.shutdown()
        return first, batched, read(namespace)

    # The parent's worker did not survive the fork, and no thread in the child will ever release the lock.
    assert _in_child_forked_holding(lock, child) == (1, True, after_batched)
    collector.shutdown()


def _prometheus_lock(held: str) -> Any:
    """Return the lock named by ``held``, one that the cache-operation series or their registration take."""
    from prometheus_client import REGISTRY

    if held == "registry":
        return REGISTRY._lock
    if held == "series":
        # Every cache-operation series value reads and retires per-thread cells under this one lock (_ShardView).
        return async_metrics._shard_lock()
    return async_metrics._metrics_cache["cache_operations_total"]._lock  # taken by labels()


# The at-fork hook takes the series lock itself, so only a fork made from C can inherit it held.
PROMETHEUS_LOCKS = [
    pytest.param(held, fork, id=f"{held}-{fork}", marks=needs_c_fork if fork == "c-fork" else needs_fork)
    for held in ("counter", "series", "registry")
    for fork in ("fork", "c-fork")
    if not (held == "series" and fork == "fork")
]


def _prometheus_lock_outcome(held: str, fork: str) -> object:
    """What a child forked while the prometheus_client lock named by ``held`` is held records; fork is "fork" or "c-fork"."""
    namespace = f"fork-prometheus-{held}-{uuid.uuid4().hex}"
    # Create the series first, as a parent that has recorded before has; its worker updates them under these locks.
    AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False).record_cache_operation(
        operation="get", namespace=namespace, success=True, duration_ms=1.0
    )
    c_fork = fork == "c-fork"
    collector = _parked_batched_collector() if c_fork else AsyncMetricsCollector(flush_interval=0.05, sync_mode=False)

    def child():
        collector._last_mode_check = time.time()  # the first record takes the batched path, so the child takes over
        _record(collector, namespace)
        # A metric the parent never created registers in prometheus_client's registry, under the registry lock.
        collector.record_counter(_metric_name(namespace))
        return _flushed(namespace), _sample(f"{_metric_name(namespace)}_total")

    if c_fork:  # in a fresh interpreter, whose exit ends the parked worker
        return _in_c_forked_child_holding(_prometheus_lock(held), child)
    try:
        return _in_child_forked_holding(_prometheus_lock(held), child)
    finally:
        collector.shutdown()


@pytest.mark.parametrize(("held", "fork"), PROMETHEUS_LOCKS)
def test_child_records_past_a_prometheus_lock_held_at_fork(held, fork):
    if fork == "c-fork":
        outcome = _in_fresh_interpreter(_prometheus_lock_outcome, held, fork)
    else:
        outcome = _prometheus_lock_outcome(held, fork)
    # prometheus_client resets none of its locks after a fork, so the child must not use the parent's copies.
    assert outcome == (2, 1)


@needs_fork
@pytest.mark.parametrize("held", ["counter", "registry"])  # the series lock: see PROMETHEUS_LOCKS
def test_child_of_a_sync_parent_records_past_a_prometheus_lock_held_at_fork(held):
    namespace = f"fork-prometheus-sync-{held}-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False)
    _record(collector, namespace)

    def child():
        # A synchronous record makes no fork check, so only the at-fork hook can have replaced these locks.
        _record(collector, namespace)
        collector.record_counter(_metric_name(namespace))
        return _flushed(namespace), _sample(f"{_metric_name(namespace)}_total")

    assert _in_child_forked_holding(_prometheus_lock(held), child) == (2, 1)


@needs_fork
@pytest.mark.parametrize("first_call", ["switch-to-sync", "shutdown"])
def test_child_of_a_batched_parent_stops_batching_past_a_held_stop_event(first_call):
    namespace = f"fork-stop-event-{first_call}-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(flush_interval=0.05)
    _steer(collector, 500)
    _record(collector, f"{namespace}-parent")
    assert not collector._sync_mode and collector._stopped is not None

    def child():
        if first_call == "shutdown":
            collector.shutdown()
        else:
            _steer(collector, 1)
        _record(collector, namespace)
        return collector._sync_mode, _flushed(namespace)

    # Both calls set the stop event, whose Condition lock a parent thread holds at the fork.
    assert _in_child_forked_holding(collector._stopped._cond, child) == (True, 1)
    collector.shutdown()


@needs_fork
def test_child_of_a_parent_back_in_sync_mode_batches_on_a_queue_of_its_own():
    namespace = f"fork-old-queue-{uuid.uuid4().hex}"
    other = f"fork-old-queue-other-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(flush_interval=0.05)
    _steer(collector, 500)
    _record(collector, other)
    worker = collector._worker_thread
    _steer(collector, 1)
    _record(collector, other)
    assert collector._sync_mode and worker is not None
    worker.join(timeout=5.0)
    assert not worker.is_alive() and collector._queue is not None

    def child():
        _steer(collector, 500)
        _record(collector, namespace)
        batched = not collector._sync_mode
        collector.shutdown()  # the child's own worker flushes the record on its way out
        return batched, _flushed(namespace)

    # The parent no longer batches but still owns its old queue, which a switch in the child would otherwise reuse.
    assert _in_child_forked_holding(collector._queue.mutex, child) == (True, 1)


class _UnstartedThread(threading.Thread):
    started = []

    def start(self):
        # Record the start instead of making it: in the child of a fork made from C, a real one can hang or crash.
        self.started.append(self)


# The scenarios below fork from C, so each runs in a fresh interpreter (_in_fresh_interpreter), whose exit ends the
# workers they leave parked.
def _hookless_child_of_a_batched_parent_outcome() -> object:
    namespace = f"fork-hookless-{uuid.uuid4().hex}"
    collector = _parked_batched_collector()
    inherited_worker, q = collector._worker_thread, collector._queue
    assert inherited_worker is not None and q is not None

    def child():
        stale = inherited_worker.is_alive()  # the dead worker still looks alive, so only the PID shows the fork
        collector._last_mode_check = time.time()  # the first record takes the batched path
        _record(collector, namespace)
        first = _flushed(namespace)
        async_metrics.threading = SimpleNamespace(Thread=_UnstartedThread, Event=threading.Event, Lock=threading.Lock)
        _steer(collector, 500)  # the inherited rate alone would trip a switch to batched mode
        _record(collector, namespace)
        started = time.monotonic()
        collector.shutdown(timeout=2.0)  # must not wait on the dead worker that still looks alive
        prompt_shutdown = time.monotonic() - started < 1.0
        return stale, first, collector._sync_mode, len(_UnstartedThread.started), _flushed(namespace), prompt_shutdown

    return _in_c_forked_child_holding(q.mutex, child)


@needs_c_fork
def test_child_of_a_c_level_fork_records_synchronously_and_starts_no_thread():
    """A fork made from C skips Python's after-fork handling, as uWSGI's does without --py-call-uwsgi-fork-hooks."""
    # CPython's own after-fork repair has not run in this child either, so the collector never starts a thread here.
    assert _in_fresh_interpreter(_hookless_child_of_a_batched_parent_outcome) == (True, 1, True, 0, 2, True)


def _collector_built_in_a_hookless_child_outcome() -> object:
    namespace = f"fork-hookless-built-{uuid.uuid4().hex}"
    AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False).record_cache_operation(
        operation="get", namespace=namespace, success=True, duration_ms=1.0
    )

    def child():
        # Built in the child, so no take-over ever runs for them: the shared global is created on first use, and
        # every decorated function builds its own collector.
        async_metrics.threading = SimpleNamespace(Thread=_UnstartedThread, Event=threading.Event, Lock=threading.Lock)
        async_metrics._global_collector = None
        collectors = [AsyncMetricsCollector(sync_mode=False), async_metrics.get_async_metrics_collector()]
        for collector in collectors:
            _steer(collector, 500)
            _record(collector, namespace)
        modes = [(c._sync_mode, c._batching_disabled) for c in collectors]
        return len(_UnstartedThread.started), modes, _flushed(namespace)

    # A parent thread holds the series lock, so a record into it hangs unless the child's locks are fresh.
    return _in_c_forked_child_holding(_prometheus_lock("series"), child)


@needs_c_fork
def test_collector_built_in_a_c_level_fork_child_records_synchronously_and_starts_no_thread():
    assert _in_fresh_interpreter(_collector_built_in_a_hookless_child_outcome) == (0, [(True, True), (True, True)], 3)


def _hookless_child_resets_metric_locks_outcome() -> object:
    namespace = f"fork-hookless-reset-once-{uuid.uuid4().hex}"
    inherited = [_parked_batched_collector() for _ in range(2)]
    real_reset = async_metrics._reset_metric_locks

    def child():
        resets = []

        def counting_reset():
            resets.append(None)
            real_reset()

        async_metrics._reset_metric_locks = counting_reset
        for collector in inherited:
            collector._last_mode_check = time.time()  # each takes over on its first, batched record
            _record(collector, namespace)
        AsyncMetricsCollector(sync_mode=True)
        # Replacing a lock another thread is using can cost that thread an update, so it happens once per process.
        return len(resets), _flushed(namespace)

    return _in_c_forked_child_holding(inherited[0]._queue.mutex, child)


@needs_c_fork
def test_child_of_a_c_level_fork_resets_metric_locks_once():
    assert _in_fresh_interpreter(_hookless_child_resets_metric_locks_outcome) == (1, 2)


def _park_threads(count: int) -> None:
    """Start count threads that wait, until the process exits, on an event nothing sets."""
    never = threading.Event()

    def park_until_stopped():
        never.wait()

    for _ in range(count):
        threading.Thread(target=park_until_stopped, daemon=True, name="parked").start()
    _until_waiting(never._cond, count)


def _hung_child_outcome(parked: int) -> object:
    """How _in_c_forked_child_holding fails a child that hangs, forked beside parked threads: (message, seconds)."""
    lock = threading.Lock()
    _park_threads(parked)

    def wait_on_the_inherited_lock():
        return lock.acquire()  # held at the fork, and nothing in the child releases it

    started = time.monotonic()
    try:
        _in_c_forked_child_holding(lock, wait_on_the_inherited_lock, timeout=1)
    except pytest.fail.Exception as failed:
        return str(failed), time.monotonic() - started
    return "the hung child passed", time.monotonic() - started


# A child forked from C keeps every parent thread's state, and faulthandler dumps only the newest 100 of them: a busy
# xdist worker has more, and the oldest, cut off, is the main thread that forked and hung.
@needs_c_fork
@pytest.mark.parametrize("parked", [0, 120], ids=["quiet-parent", "busy-parent"])
def test_hung_child_of_a_c_level_fork_fails_its_test_with_its_stacks(parked):
    run = run_fresh(_hung_child_outcome, parked)

    assert run.returncode == 0, run.stderr
    message, seconds = ast.literal_eval(run.stdout)
    # Failed within the helper's bound instead of holding the job, and the dump names the frame the child hung in.
    assert "timed out after 1 s" in message and seconds < 10, run.stderr
    assert "in wait_on_the_inherited_lock" in run.stderr
    # And the other threads' frames as the fork left them.
    assert ("in park_until_stopped" in run.stderr) == bool(parked), run.stderr


# The fork tests above run the fork handling in a child process, which coverage does not measure. The tests below run
# the same code in this process, making it look like a forked child through the PIDs the module compares.
_PARENT_PID = -1


def _batched_collector_as_a_child_inherits_it() -> AsyncMetricsCollector:
    """Return a batched collector as its copy in a forked child looks: the parent's PID and a worker that never runs."""
    collector = AsyncMetricsCollector(flush_interval=0.05, sync_mode=False)
    assert collector._worker_thread is not None and collector._stopped is not None
    # A child inherits the worker's Thread object, not the thread. The real one would read whatever queue the
    # collector holds, the child's fresh one included, so stop it.
    collector._stopped.set()
    collector._worker_thread.join(5)
    collector._owner_pid = _PARENT_PID
    return collector


# A cache operation never queues, so it never takes over the batching state; a mode check does that for it.
@pytest.mark.parametrize("entry", [e for e in ENTRY_POINTS if e != "cache-operation"])
def test_take_over_replaces_the_inherited_batching_state(entry):
    record, read, after_batched = ENTRY_POINTS[entry]
    namespace = f"take-over-{entry}-{uuid.uuid4().hex}"
    collector = _batched_collector_as_a_child_inherits_it()
    inherited_worker = collector._worker_thread
    inherited = (collector._queue, collector._stopped, collector._pool_lock)

    collector._last_mode_check = time.time()  # the first record takes the batched path, so it takes over
    record(collector, namespace)
    # Queued on the inherited queue instead, with no worker to flush it, the record would never reach its metric.
    assert (collector._sync_mode, collector._worker_thread, read(namespace)) == (True, None, 1)
    fresh = (collector._queue, collector._stopped, collector._pool_lock)
    assert all(new is not old for new, old in zip(fresh, inherited, strict=True))

    _steer(collector, 500)
    record(collector, namespace)  # batched again, on the child's own queue and worker
    assert not collector._sync_mode and collector._worker_thread not in (None, inherited_worker)
    collector.shutdown()
    assert read(namespace) == after_batched


@pytest.mark.parametrize("origin", ["inherited", "built"])
def test_collector_in_a_child_of_a_c_level_fork_never_batches(origin, monkeypatch):
    namespace = f"hookless-{origin}-{uuid.uuid4().hex}"
    inherited = _batched_collector_as_a_child_inherits_it() if origin == "inherited" else None
    # Neither the process that imported the module nor one the at-fork hook ran in: a child of a fork made from C.
    monkeypatch.setattr(async_metrics, "_import_pid", _PARENT_PID)
    monkeypatch.setattr(async_metrics, "_hooked_fork_pid", None)
    collector = inherited or AsyncMetricsCollector(flush_interval=0.05, sync_mode=False)

    collector._last_mode_check = time.time()
    _record(collector, namespace)
    _steer(collector, 500)  # the rate alone would trip a switch to batched mode
    _record(collector, namespace)
    assert (collector._sync_mode, collector._batching_disabled, collector._worker_thread) == (True, True, None)
    assert _flushed(namespace) == 2


def test_metric_locks_are_replaced_once_in_a_child_of_a_c_level_fork(monkeypatch):
    from prometheus_client import CollectorRegistry, Counter, Histogram

    # A registry and metrics of the test's own, so no other test's metric gets a new lock while it records.
    registry = CollectorRegistry()
    counter = Counter("reset_counter", "test", ["k"], registry=registry)
    histogram = Histogram("reset_histogram", "test", ["k"], registry=registry)
    counter_series, histogram_series = counter.labels(k="v"), histogram.labels(k="v")
    monkeypatch.setattr(async_metrics, "REGISTRY", registry)
    monkeypatch.setattr(async_metrics, "_metrics_cache", {"counter": counter, "histogram": histogram})
    monkeypatch.setattr(async_metrics, "_metric_locks_pid", _PARENT_PID)
    guarded = [registry, counter, counter_series._value, histogram, histogram_series._sum, *histogram_series._buckets]
    inherited = [obj._lock for obj in guarded]
    for lock in inherited:
        lock.acquire()  # as a parent thread can hold each at the fork
    try:
        async_metrics._reset_metric_locks_once()
        fresh = [obj._lock for obj in guarded]
        assert all(new is not old and not new.locked() for new, old in zip(fresh, inherited, strict=True))
        # A lock replaced while another thread uses it can cost that thread an update, so it happens once.
        async_metrics._reset_metric_locks_once()
        assert all(obj._lock is lock for obj, lock in zip(guarded, fresh, strict=True))
    finally:
        for lock in inherited:
            lock.release()


def test_at_fork_hook_marks_the_child_and_resets_its_locks(monkeypatch):
    resets = []
    monkeypatch.setattr(async_metrics, "_import_pid", _PARENT_PID)
    monkeypatch.setattr(async_metrics, "_metrics_locks", {_PARENT_PID: threading.Lock()})
    monkeypatch.setattr(async_metrics, "_hooked_fork_pid", None)
    assert async_metrics._in_hookless_child()  # until the hook runs, this process looks like a C-level fork's child
    monkeypatch.setattr(async_metrics, "_reset_metric_locks", lambda: resets.append(None))

    async_metrics._after_fork_in_child()

    # Marked as hooked, so a collector here may start a worker; _in_hookless_child decides that per process.
    assert (async_metrics._metrics_locks, async_metrics._hooked_fork_pid, len(resets)) == ({}, os.getpid(), 1)
    assert not async_metrics._in_hookless_child()


def test_shutdown_during_a_switch_to_batched_stops_the_new_worker():
    namespace = f"mode-switch-shutdown-race-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(flush_interval=0.05, sync_mode=False)
    _steer(collector, 1)
    _record(collector, namespace)
    assert collector._sync_mode and collector._worker_thread is not None
    collector._worker_thread.join(5)
    inside, release = threading.Event(), threading.Event()

    class ParkingEvent(threading.Event):
        def clear(self):
            # Park the restart between its checks and clearing the stop signal, where shutdown() must not slip in.
            inside.set()
            release.wait(5)
            super().clear()

    collector._stopped = ParkingEvent()
    _steer(collector, 500)
    switcher = threading.Thread(target=_record, args=(collector, namespace), daemon=True)
    switcher.start()
    assert inside.wait(5)

    stopper = threading.Thread(target=collector.shutdown, daemon=True)
    stopper.start()
    threading.Timer(0.2, release.set).start()
    switcher.join(5)
    stopper.join(5)

    assert not stopper.is_alive()
    assert not collector._worker_thread.is_alive()
