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


def _lazy_import_child_outcome(parent_threads: int, fork: str) -> object:
    """What a child that first imports cachekit after a fork reports; run only in a fresh interpreter.

    The parent never imports cachekit. Its threads wait on an Event through the fork, so none holds or waits for
    the GIL. fork is "c" for a fork made from C (PyDLL keeps the GIL through the call), or "os" for os.fork().
    """
    import ctypes
    import faulthandler

    stop = threading.Event()
    for _ in range(parent_threads):
        threading.Thread(target=stop.wait, daemon=True).start()
    faulthandler.register(signal.SIGALRM, chain=True)  # the child inherits it: a hang dumps its stack
    r, w = os.pipe()
    child = ctypes.PyDLL(None).fork() if fork == "c" else os.fork()
    if child == 0:
        signal.alarm(10)
        os.close(r)
        requested: list[str] = []
        threading.Thread.start = lambda self: requested.append(getattr(self._target, "__name__", self.name))  # type: ignore[method-assign]

        from cachekit import l1_cache
        from cachekit.reliability import async_metrics

        l1_cache.get_l1_cache_manager()  # a worker's first L1 use builds the global manager
        async_metrics.AsyncMetricsCollector(sync_mode=False, auto_detect_mode=False)
        report(
            w,
            {
                "hookless": l1_cache._forked_without_hooks(),
                "metrics_hookless": async_metrics._in_hookless_child(),
                "requested": requested,
            },
        )
    os.close(w)
    try:
        return child_outcome(child, r)
    finally:
        stop.set()


class TestLazyImportAfterFork:
    """AC: the child of a fork made from C that first imports cachekit starts no thread iff its parent ran threads."""

    @pytest.mark.parametrize(
        ("parent_threads", "fork", "expected"),
        [
            (2, "c", {"hookless": True, "metrics_hookless": True, "requested": []}),
            (
                0,
                "c",
                {"hookless": False, "metrics_hookless": False, "requested": ["cleanup_worker", "_worker_loop"]},
            ),
            (
                2,
                "os",
                {"hookless": False, "metrics_hookless": False, "requested": ["cleanup_worker", "_worker_loop"]},
            ),
        ],
        ids=["c-fork-threaded-parent", "c-fork-threadless-parent", "os-fork-threaded-parent"],
    )
    def test_child_requests_threads_only_when_safe(self, parent_threads, fork, expected):
        """A fresh interpreter: this one has imported cachekit and runs threads it cannot end (fork_helpers.run_fresh)."""
        run = run_fresh(_lazy_import_child_outcome, parent_threads, fork)

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

    def test_the_current_thread_does_not_count(self, fork_mod, tasks, monkeypatch):
        """After a fork made from C the current thread keeps the forking thread's native id."""
        current = SimpleNamespace(native_id=_STALE)
        monkeypatch.setattr(threading, "current_thread", lambda: current)
        monkeypatch.setattr(threading, "enumerate", lambda: [current])

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

    def test_this_thread_absent_from_proc_cannot_tell(self, fork_mod, tasks, monkeypatch):
        """Another PID namespace's /proc: this thread's own id is not in it."""
        tasks.clear()
        tasks.add(_STALE - 1)
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

    def test_metric_locks_are_reset_once(self, monkeypatch):
        """None is no PID, so such a process replaces prometheus_client locks a parent thread may have held."""
        from cachekit.reliability import async_metrics

        monkeypatch.setattr(async_metrics, "_metric_locks_pid", None)
        calls: list[int] = []
        monkeypatch.setattr(async_metrics, "_reset_metric_locks", lambda: calls.append(1))

        async_metrics._reset_metric_locks_once()

        assert calls == [1]
