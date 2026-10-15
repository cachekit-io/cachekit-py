"""cachekit._fork: a process that first imports cachekit after a fork made from C of a threaded parent starts no thread.

This module imports cachekit only inside its tests: run_fresh imports it in a parent that must not have imported
cachekit before it forks.
"""

from __future__ import annotations

import ast
import os
import signal
import sys
import threading
from types import SimpleNamespace

import pytest

from tests.utils.fork_helpers import child_outcome, report, run_fresh

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the signal reads /proc/self/task")

_STALE = 2**31 - 7  # a native id no task of this process has


def _wait_until_blocked(threads: list[threading.Thread]) -> None:
    """Return once every thread is inside its wait: a thread still waiting for the GIL at a fork made from C hangs
    the child at its first GIL release, in no cachekit code (fork_helpers.run_fresh)."""
    import time

    deadline = time.monotonic() + 5.0
    while True:
        frames = sys._current_frames()
        if all(frames.get(t.ident) is not None and frames[t.ident].f_code.co_name == "wait" for t in threads):
            break
        if time.monotonic() > deadline:  # not an assert: python -O strips those
            raise RuntimeError("a parent thread never reached its wait")
        time.sleep(0.01)
    time.sleep(0.05)  # from wait's frame into its lock acquire, which releases the GIL


def _lazy_import_child_outcome(parent_threads: int, fork: str, import_on: str) -> object:
    """What a child that first imports cachekit after a fork reports; run only in a fresh interpreter.

    The parent never imports cachekit. Its threads wait on an Event through the fork. fork is "c" for a fork made
    from C (PyDLL keeps the GIL through the call), or "os" for os.fork(). import_on is "forker" to import on the
    thread that forked, or "new" to import on a thread the child starts first, as a request thread would.
    """
    import ctypes
    import faulthandler

    stop = threading.Event()
    threads = [threading.Thread(target=stop.wait, daemon=True) for _ in range(parent_threads)]
    for thread in threads:
        thread.start()
    _wait_until_blocked(threads)
    faulthandler.register(signal.SIGALRM, chain=True)  # the child inherits it: a hang dumps its stack
    r, w = os.pipe()
    child = ctypes.PyDLL(None).fork() if fork == "c" else os.fork()
    if child == 0:
        signal.alarm(10)
        os.close(r)
        requested: list[str] = []
        start = threading.Thread.start
        threading.Thread.start = lambda self: requested.append(getattr(self._target, "__name__", self.name))  # type: ignore[method-assign]
        outcome: dict[str, object] = {}

        def first_import() -> None:
            from cachekit import l1_cache
            from cachekit.reliability import async_metrics

            outcome["metric_locks_at_import"] = async_metrics._metric_locks_pid == os.getpid()
            l1_cache.get_l1_cache_manager()  # a worker's first L1 use builds the global manager
            async_metrics.AsyncMetricsCollector(sync_mode=False, auto_detect_mode=False)
            outcome["metric_locks_after"] = async_metrics._metric_locks_pid == os.getpid()
            outcome["hookless"] = l1_cache._forked_without_hooks()
            outcome["metrics_hookless"] = async_metrics._in_hookless_child()

        if import_on == "new":
            importer = threading.Thread(target=first_import)
            start(importer)  # the real start: only the threadless-parent cases import on a new thread
            importer.join(5)
        else:
            first_import()
        report(w, {**outcome, "requested": requested})
    os.close(w)
    try:
        return child_outcome(child, r)
    finally:
        stop.set()


_HOOKLESS = {
    "metric_locks_at_import": False,  # None until the first collector replaces the inherited locks
    "metric_locks_after": True,
    "hookless": True,
    "metrics_hookless": True,
    "requested": [],
}
_FRESH = {
    "metric_locks_at_import": True,
    "metric_locks_after": True,
    "hookless": False,
    "metrics_hookless": False,
    "requested": ["cleanup_worker", "_worker_loop"],
}


class TestLazyImportAfterFork:
    """AC: the child of a fork made from C that first imports cachekit starts no thread iff its parent ran threads."""

    @pytest.mark.parametrize(
        ("parent_threads", "fork", "import_on", "expected"),
        [
            (2, "c", "forker", _HOOKLESS),
            (0, "c", "forker", _FRESH),
            (2, "os", "forker", _FRESH),
            # The forking thread keeps the parent's native id (always after a C fork; after os.fork() before 3.13).
            (0, "c", "new", _FRESH),
            (0, "os", "new", _FRESH),
        ],
        ids=[
            "c-fork-threaded-parent",
            "c-fork-threadless-parent",
            "os-fork-threaded-parent",
            "c-fork-threadless-parent-import-on-new-thread",
            "os-fork-threadless-parent-import-on-new-thread",
        ],
    )
    def test_child_requests_threads_only_when_safe(self, parent_threads, fork, import_on, expected):
        """A fresh interpreter: this one has imported cachekit and runs threads it cannot end (fork_helpers.run_fresh)."""
        run = run_fresh(_lazy_import_child_outcome, parent_threads, fork, import_on)

        assert run.returncode == 0, run.stderr
        assert ast.literal_eval(run.stdout) == expected, run.stderr  # a hung child's stack, dumped on its alarm


@pytest.fixture
def fork_mod():
    from cachekit import _fork

    return _fork


@pytest.fixture
def tasks(monkeypatch):
    """Fake /proc/self/task: this process has the threads whose native ids are in the set, this one's included."""
    present = {threading.get_native_id()}
    real_exists = os.path.exists

    def exists(path):
        prefix = "/proc/self/task/"
        if isinstance(path, str) and path.startswith(prefix):
            return int(path[len(prefix) :]) in present
        return real_exists(path)

    monkeypatch.setattr(os.path, "exists", exists)
    return present


def _list_threads(monkeypatch, *others: object) -> None:
    monkeypatch.setattr(threading, "enumerate", lambda: [threading.current_thread(), *others])


def _dummy_thread(native_id: int) -> threading._DummyThread:
    """A _DummyThread object that is not registered: constructing one registers it under this thread's ident."""
    dummy = object.__new__(threading._DummyThread)
    dummy.__dict__.update(_initialized=True, _native_id=native_id)
    return dummy


class TestInheritsDeadThreads:
    def test_this_process_has_every_thread_it_lists(self, fork_mod):
        assert fork_mod.inherits_dead_threads() is False
        assert fork_mod.IMPORT_PID == os.getpid()  # pytest imported cachekit in this process, not after a fork

    def test_threads_starting_and_ending_meanwhile_never_count(self, fork_mod):
        """Real threads come and go while it runs: a listing of /proc/self/task skips live tasks then (free-threaded)."""
        import time

        stop = threading.Event()

        def churn():
            while not stop.is_set():
                batch = [threading.Thread(target=lambda: None) for _ in range(8)]
                for thread in batch:
                    thread.start()
                for thread in batch:
                    thread.join()

        churners = [threading.Thread(target=churn, daemon=True) for _ in range(4)]
        for thread in churners:
            thread.start()
        try:
            deadline = time.monotonic() + 1.0
            seen = []
            while time.monotonic() < deadline:
                seen.append(fork_mod.inherits_dead_threads())
        finally:
            stop.set()
            for thread in churners:
                thread.join(5)

        assert not any(seen), f"{sum(seen)} of {len(seen)} checks took live threads for dead ones"

    def test_a_listed_thread_with_no_task_is_inherited(self, fork_mod, tasks, monkeypatch):
        _list_threads(monkeypatch, SimpleNamespace(native_id=_STALE))

        assert fork_mod.inherits_dead_threads() is True

    def test_a_listed_thread_with_a_task_is_live(self, fork_mod, tasks, monkeypatch):
        tasks.add(_STALE)
        _list_threads(monkeypatch, SimpleNamespace(native_id=_STALE))

        assert fork_mod.inherits_dead_threads() is False

    def test_a_thread_that_ends_during_the_check_does_not_count(self, fork_mod, tasks, monkeypatch):
        """Listed, then its task is gone, and threading no longer lists it: it ended meanwhile."""
        ended = SimpleNamespace(native_id=_STALE)
        listings = iter([[threading.current_thread(), ended], [threading.current_thread()]])
        monkeypatch.setattr(threading, "enumerate", lambda: next(listings))

        assert fork_mod.inherits_dead_threads() is False

    def test_a_stale_dummy_thread_does_not_count(self, fork_mod, tasks, monkeypatch):
        _list_threads(monkeypatch, _dummy_thread(_STALE))

        assert fork_mod.inherits_dead_threads() is False

    def test_a_thread_with_no_native_id_does_not_count(self, fork_mod, tasks, monkeypatch):
        _list_threads(monkeypatch, SimpleNamespace(native_id=None))

        assert fork_mod.inherits_dead_threads() is False

    def test_the_main_thread_with_a_stale_id_does_not_count(self, fork_mod, tasks, monkeypatch):
        """After os.fork() before 3.13 the main thread keeps the parent's native id; cachekit imported off it."""
        main = SimpleNamespace(native_id=_STALE)
        monkeypatch.setattr(threading, "main_thread", lambda: main)
        _list_threads(monkeypatch, main, SimpleNamespace(native_id=_STALE))  # and a green thread that ran on it

        assert fork_mod.inherits_dead_threads() is False

    def test_a_thread_on_the_current_threads_native_id_does_not_count(self, fork_mod, tasks, monkeypatch):
        """A gevent green thread of the parent ran on the forking thread, whose native id the current thread keeps."""
        current = SimpleNamespace(native_id=_STALE)
        monkeypatch.setattr(threading, "current_thread", lambda: current)
        monkeypatch.setattr(threading, "enumerate", lambda: [current, SimpleNamespace(native_id=_STALE)])

        assert fork_mod.inherits_dead_threads() is False

    def test_no_proc_view_cannot_tell(self, fork_mod, tasks, monkeypatch):
        tasks.clear()  # no /proc/self/task: nothing is found there, this thread included
        _list_threads(monkeypatch, SimpleNamespace(native_id=_STALE))

        assert fork_mod.inherits_dead_threads() is False

    def test_a_patched_get_native_id_cannot_tell(self, fork_mod, monkeypatch):
        """A monkey-patched get_native_id; the real /proc listing lacks what it returns."""
        monkeypatch.setattr(threading, "get_native_id", lambda: _STALE)
        _list_threads(monkeypatch, SimpleNamespace(native_id=_STALE - 1))

        assert fork_mod.inherits_dead_threads() is False


class TestNoImportPid:
    """A process with no import PID (IMPORT_PID None) is hookless in both modules, until an at-fork hook runs."""

    def test_l1_cache(self, monkeypatch):
        from cachekit import l1_cache

        monkeypatch.setattr(l1_cache, "_import_pid", None)
        monkeypatch.setattr(l1_cache, "_hooked_pid", None)
        assert l1_cache._forked_without_hooks() is True

        monkeypatch.setattr(l1_cache, "_hooked_pid", os.getpid())  # an os.fork() child of it ran the hook
        assert l1_cache._forked_without_hooks() is False

    def test_async_metrics(self, monkeypatch):
        from cachekit.reliability import async_metrics

        monkeypatch.setattr(async_metrics, "_import_pid", None)
        monkeypatch.setattr(async_metrics, "_hooked_fork_pid", None)
        assert async_metrics._in_hookless_child() is True

        monkeypatch.setattr(async_metrics, "_hooked_fork_pid", os.getpid())
        assert async_metrics._in_hookless_child() is False
