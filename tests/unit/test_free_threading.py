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


# Run in a fresh interpreter: the importing process's GIL state is what is under test, and the
# pytest process has already imported cachekit. The [data] and [json] extras are blocked because a
# default install does not have them, and some of their builds re-enable the GIL on their own.
_DEFAULT_INSTALL_PROBE = """
import json, sys
for extra in ("numpy", "pandas", "pyarrow", "orjson"):
    sys.modules[extra] = None
import cachekit
from cachekit.backends.redis import RedisBackend
RedisBackend(redis_url="redis://127.0.0.1:6379")
import redis.connection
print(json.dumps({
    "gil_enabled": sys._is_gil_enabled(),
    "hiredis_loaded": sys.modules.get("hiredis") is not None,
    "redis_parser": redis.connection.DefaultParser.__name__,
}))
"""


def _hiredis_installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("hiredis") is not None


@pytest.mark.skipif(not _FREE_THREADED_BUILD, reason="requires a free-threaded CPython build")
@pytest.mark.skipif(
    not _hiredis_installed(),
    reason="the default install includes hiredis (redis[hiredis]); this environment excludes it",
)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "hiredis_compat disables hiredis only after redis.connection has imported it, "
        "so the GIL is already back on; flips to a pass when hiredis is decided before any redis import"
    ),
)
def test_default_install_keeps_gil_disabled_after_redis_backend():
    """With the default dependency set (redis[hiredis] included), cachekit must leave the GIL off.

    The free-threaded CI lane installs without hiredis, so the GIL assertions above never see what a
    plain `pip install cachekit` on 3.14t gets. Run this in a 3.14t environment that has hiredis, for
    example `uv sync --python 3.14t --no-default-groups --group test`.
    """
    import json
    import os
    import subprocess

    env = {k: v for k, v in os.environ.items() if k != "PYTHON_GIL"}  # PYTHON_GIL=0 would force a vacuous pass
    proc = subprocess.run(  # noqa: S603 (trusted: sys.executable + literal code)
        [sys.executable, "-W", "ignore", "-c", _DEFAULT_INSTALL_PROBE],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    state = json.loads(proc.stdout.strip().splitlines()[-1])
    assert state["gil_enabled"] is False, state


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
