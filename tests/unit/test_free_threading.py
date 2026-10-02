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
# pytest process has already imported cachekit. argv[1:] names modules to block. The [data] and
# [json] extras are always blocked: a default install does not have them, and some of their builds
# re-enable the GIL on their own.
_DEFAULT_INSTALL_PROBE = """
import json, sys
for blocked in ("numpy", "pandas", "pyarrow", "orjson", *sys.argv[1:]):
    sys.modules[blocked] = None
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
    # Package metadata, not find_spec: blocking hiredis via sys.modules must not make tests skip.
    import importlib.metadata

    try:
        importlib.metadata.distribution("hiredis")
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def _default_install_skip_reason() -> str | None:
    if not _FREE_THREADED_BUILD:
        return "requires a free-threaded CPython build"
    if not _hiredis_installed():
        return "the default install includes hiredis (redis[hiredis]); this environment excludes it"
    return None


_SKIP_REASON = _default_install_skip_reason()
_needs_default_install = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


def _probe_default_install(*blocked: str) -> dict[str, object]:
    """GIL state in a fresh interpreter after importing cachekit and building a RedisBackend."""
    return _run_probe(_DEFAULT_INSTALL_PROBE, *blocked, env_drop=("CACHEKIT_DISABLE_HIREDIS",))


def _run_probe(code: str, *args: str, env_drop: tuple[str, ...] = ()) -> dict[str, object]:
    """Run `code` in a fresh interpreter and return the JSON object its last stdout line holds."""
    import json
    import os
    import subprocess

    # PYTHON_GIL=0 would force a vacuous pass
    env = {k: v for k, v in os.environ.items() if k != "PYTHON_GIL" and k not in env_drop}
    try:
        proc = subprocess.run(  # noqa: S603 (trusted: sys.executable + literal code)
            [sys.executable, "-W", "ignore", "-c", code, *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            pytest.fail(f"probe exited {proc.returncode}:\n{proc.stderr}")
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (subprocess.TimeoutExpired, IndexError, json.JSONDecodeError) as exc:
        # pytest.fail, not an assertion: a broken probe must fail as broken, not as a GIL finding.
        pytest.fail(f"probe produced no result: {exc!r}")


@_needs_default_install
def test_gil_stays_disabled_with_hiredis_blocked():
    """Control for the test below: with hiredis kept out, nothing else in the default install re-enables the GIL."""
    state = _probe_default_install("hiredis")
    assert state["gil_enabled"] is False, state


@_needs_default_install
def test_default_install_keeps_gil_disabled_after_redis_backend():
    """With the default dependency set (redis[hiredis] included), cachekit must leave the GIL off.

    The free-threaded CI lane installs without hiredis, so the GIL assertions above never see what a
    plain `pip install cachekit` on 3.14t gets. Run this in a 3.14t environment that has hiredis, for
    example `uv sync --python 3.14t --no-default-groups --group test`.
    """
    state = _probe_default_install()
    assert state["gil_enabled"] is False, state
    assert state["redis_parser"] == "_RESP2Parser", state


# Runs in the CI lane without hiredis: what it pins is that hiredis is blocked before redis is imported.
_ORDERING_PROBE = """
import json, sys
import cachekit
import redis.connection
print(json.dumps({
    "hiredis_entry": repr(sys.modules.get("hiredis", "<absent>")),
    "redis_parser": redis.connection.DefaultParser.__name__,
}))
"""


@pytest.mark.skipif(not _FREE_THREADED_BUILD, reason="requires a free-threaded CPython build")
def test_import_blocks_hiredis_before_redis_loads():
    """With no override, `import cachekit` keeps hiredis out of redis-py, installed or not."""
    state = _run_probe(_ORDERING_PROBE, env_drop=("CACHEKIT_DISABLE_HIREDIS",))
    assert state == {"hiredis_entry": "None", "redis_parser": "_RESP2Parser"}, state


# Records, when redis or cachekit.backends is first imported, whether hiredis_compat had already finished.
_DECISION_ORDER_PROBE = """
import json, sys
seen = {}
class Spy:
    def find_spec(self, name, path=None, target=None):
        if name in ("redis", "cachekit.backends") and name not in seen:
            compat = sys.modules.get("cachekit.hiredis_compat")
            seen[name] = compat is not None and hasattr(compat, "HIREDIS_DISABLED")
sys.meta_path.insert(0, Spy())
import cachekit
print(json.dumps(seen))
"""


def test_hiredis_decision_runs_before_redis_is_imported():
    """hiredis_compat decides before anything imports redis, so it must not import cachekit.backends."""
    assert _run_probe(_DECISION_ORDER_PROBE) == {"redis": True, "cachekit.backends": True}


_PARSER_PROBE = """
import json
import cachekit
import redis
print(json.dumps({"parser": type(redis.Connection()._parser).__name__}))
"""


@pytest.mark.parametrize(
    ("setting", "parser"),
    [
        ("true", "_RESP2Parser"),
        pytest.param(
            "false",
            "_HiredisParser",
            marks=pytest.mark.skipif(not _hiredis_installed(), reason="requires hiredis"),
        ),
        pytest.param(
            None,
            "_HiredisParser",
            marks=pytest.mark.skipif(
                _FREE_THREADED_BUILD or not _hiredis_installed(), reason="requires a GIL build with hiredis"
            ),
        ),
    ],
)
def test_disable_hiredis_setting_selects_connection_parser(monkeypatch, setting, parser):
    """CACHEKIT_DISABLE_HIREDIS picks the parser a new connection gets; unset, GIL builds keep hiredis."""
    if setting is None:
        monkeypatch.delenv("CACHEKIT_DISABLE_HIREDIS", raising=False)
    else:
        monkeypatch.setenv("CACHEKIT_DISABLE_HIREDIS", setting)
    assert _run_probe(_PARSER_PROBE) == {"parser": parser}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("true", True),
        (" YES ", True),
        ("1", True),
        ("false", False),
        ("Off", False),
        ("0", False),
        ("maybe", None),
    ],
)
def test_disable_hiredis_setting_parsing(monkeypatch, raw, expected):
    """Unset and unparseable values are told apart from an explicit false."""
    from cachekit.hiredis_compat import _disable_hiredis_setting

    if raw is None:
        monkeypatch.delenv("CACHEKIT_DISABLE_HIREDIS", raising=False)
    else:
        monkeypatch.setenv("CACHEKIT_DISABLE_HIREDIS", raw)
    assert _disable_hiredis_setting() is expected


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
