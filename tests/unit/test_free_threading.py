"""Free-threaded CPython guarantees (LAB-511).

The CI lane `test-freethreaded` runs the core suites on a free-threaded 3.14
build. These tests make the lane's central claim self-verifying from inside
the suite: on a free-threaded interpreter, importing cachekit (including the
Rust extension) must not re-enable the GIL. On GIL builds they skip — the
claim is about free-threaded builds only, and the session-identity hammer
below runs everywhere as a plain thread-safety regression net.
"""

from __future__ import annotations

import sys
import sysconfig
import threading

import pytest

_FREE_THREADED_BUILD = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


@pytest.mark.skipif(not _FREE_THREADED_BUILD, reason="requires a free-threaded CPython build")
def test_gil_stays_disabled_after_importing_cachekit():
    """cachekit (incl. the PyO3 extension, gil_used=false) must not force the GIL back on.

    A dependency without a free-threaded declaration re-enables the GIL for
    the whole process at import time, silently turning the free-threaded lane
    back into a GIL run — this asserts the lane actually tests what it claims.
    """
    import cachekit  # noqa: F401
    import cachekit._rust_serializer  # noqa: F401

    assert sys._is_gil_enabled() is False


def test_session_init_hammer_no_partial_publish_observed():
    """Many threads racing first-touch session init never observe a partial identity.

    On GIL builds this is a smoke test; on the free-threaded lane it races for
    real. get_session_start_ms() raising RuntimeError here is exactly the
    mid-publish observation the LAB-511 guard in _ensure_session_initialized
    exists to prevent.
    """
    from cachekit.decorators import session as session_module

    saved = (
        session_module._session_pid,
        session_module._session_id,
        session_module._session_start_ms,
    )
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def hammer() -> None:
        try:
            barrier.wait()
            for _ in range(100):
                assert session_module.get_session_start_ms() > 0
                assert session_module.get_session_id()
        except BaseException as exc:  # noqa: BLE001 — collected and re-raised below
            errors.append(exc)

    # Reset to uninitialized so the racing threads perform first-touch init.
    session_module._session_pid = None
    session_module._session_id = None
    session_module._session_start_ms = None
    try:
        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        (
            session_module._session_pid,
            session_module._session_id,
            session_module._session_start_ms,
        ) = saved

    assert not errors, f"session init raced: {errors!r}"


def _race_first_put_after_fork(threads: int) -> int:
    """Race `threads` first puts in a simulated forked child; return how many cleanup restarts ran."""
    from cachekit.l1_cache import L1CacheManager

    manager = L1CacheManager(default_max_memory_mb=10)
    cache = manager.get_cache("hammer-ns")
    manager._cleanup_thread = threading.Thread(target=lambda: None)  # dead, as fork leaves it
    manager._owner_pid = -1  # owned by another process
    starts: list[float] = []
    start = manager.start_background_cleanup

    def counting_start(interval_seconds: float = 30.0) -> None:
        starts.append(interval_seconds)
        start(interval_seconds)

    manager.start_background_cleanup = counting_start
    barrier = threading.Barrier(threads)

    def put() -> None:
        barrier.wait()
        cache.put("k", b"v")

    workers = [threading.Thread(target=put) for _ in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    manager.stop_background_cleanup()
    return len(starts)


def test_l1_fork_takeover_hammer_starts_one_cleanup_thread():
    """Threads racing a forked child's first L1 put restart the cleanup thread exactly once.

    Smoke test on GIL builds; on the free-threaded lane, dropping the PID-keyed
    take-over lock in L1CacheManager._check_fork (LAB-4772) starts duplicates here.
    """
    duplicated = sum(_race_first_put_after_fork(threads=32) != 1 for _ in range(200))

    assert duplicated == 0, f"{duplicated}/200 forked children restarted cleanup more than once"
