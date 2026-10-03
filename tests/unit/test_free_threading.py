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


# Runs in the CI lane without hiredis. argv[1] names the program that reaches redis-py through cachekit; a
# spy records what sys.modules held for hiredis when redis-py was first requested.
_ORDERING_PROBE = """
import json, sys
seen = {}
class Spy:
    def find_spec(self, name, path=None, target=None):
        if name == "redis" and name not in seen:
            seen[name] = repr(sys.modules.get("hiredis", "<absent>"))
sys.meta_path.insert(0, Spy())
import cachekit
if sys.argv[1] == "redis-backend":
    from cachekit.backends import RedisBackend
    RedisBackend(redis_url="redis://127.0.0.1:6379")
else:
    from cachekit.backends.provider import PooledClientProvider
    PooledClientProvider("redis://127.0.0.1:6379")
import redis.connection
print(json.dumps({
    "hiredis_when_redis_requested": seen.get("redis"),
    "hiredis_entry": repr(sys.modules.get("hiredis", "<absent>")),
    "redis_parser": redis.connection.DefaultParser.__name__,
}))
"""


@pytest.mark.skipif(not _FREE_THREADED_BUILD, reason="requires a free-threaded CPython build")
@pytest.mark.parametrize("program", ["redis-backend", "pooled-provider"])
def test_redis_backend_blocks_hiredis_before_redis_loads(program):
    """With no override, cachekit's Redis paths keep hiredis out of redis-py, installed or not."""
    state = _run_probe(_ORDERING_PROBE, program, env_drop=("CACHEKIT_DISABLE_HIREDIS",))
    assert state == {
        "hiredis_when_redis_requested": "None",
        "hiredis_entry": "None",
        "redis_parser": "_RESP2Parser",
    }, state


# Runs in every lane: a program that uses no Redis backend never loads redis-py, so hiredis stays importable.
_NO_REDIS_PROBE = """
import json, sys
import cachekit
from cachekit import cache

@cache(backend=None)
def double(x):
    return x * 2

assert double(2) == double(2) == 4
from cachekit.backends.cachekitio import CachekitIOBackend
CachekitIOBackend(api_key="ck_test_" + "0" * 32)  # pragma: allowlist secret (dummy key; the probe makes no request)
print(json.dumps({"redis_loaded": "redis" in sys.modules, "hiredis_entry": repr(sys.modules.get("hiredis", "<absent>"))}))
"""


def test_non_redis_programs_never_load_redis_py():
    """`import cachekit`, an L1-only call and a CachekitIO backend load no redis-py, so nothing is blocked."""
    state = _run_probe(_NO_REDIS_PROBE, env_drop=("CACHEKIT_DISABLE_HIREDIS",))
    assert state == {"redis_loaded": False, "hiredis_entry": "'<absent>'"}, state


# Records, when cachekit.backends or redis-py is first requested, whether hiredis_compat had already finished.
_DECISION_ORDER_PROBE = """
import json, sys
seen = {}
class Spy:
    def find_spec(self, name, path=None, target=None):
        if name in ("redis", "cachekit.backends") and name not in seen:
            compat = sys.modules.get("cachekit.hiredis_compat")
            seen[name] = compat is not None and hasattr(compat, "block_hiredis_for_free_threading")
sys.meta_path.insert(0, Spy())
import cachekit
from cachekit.backends import RedisBackend
print(json.dumps(seen))
"""


def test_hiredis_settings_are_read_before_backends_load():
    """hiredis_compat reads its setting before cachekit.backends or redis-py load, so it imports neither."""
    assert _run_probe(_DECISION_ORDER_PROBE) == {"redis": True, "cachekit.backends": True}


_PARSER_PROBE = """
import json
import cachekit
import redis
# redis-py 5+ names its parsers _HiredisParser / _RESP2Parser; 4.x names them HiredisParser / PythonParser.
print(json.dumps({"hiredis_parser": "Hiredis" in type(redis.Connection()._parser).__name__}))
"""


@pytest.mark.parametrize(
    ("setting", "hiredis_parser"),
    [
        ("true", False),
        pytest.param(
            "false",
            True,
            marks=pytest.mark.skipif(not _hiredis_installed(), reason="requires hiredis"),
        ),
        pytest.param(
            None,
            True,
            marks=pytest.mark.skipif(
                _FREE_THREADED_BUILD or not _hiredis_installed(), reason="requires a GIL build with hiredis"
            ),
        ),
    ],
)
def test_disable_hiredis_setting_selects_connection_parser(monkeypatch, setting, hiredis_parser):
    """CACHEKIT_DISABLE_HIREDIS picks the parser a new connection gets; unset, GIL builds keep hiredis."""
    if setting is None:
        monkeypatch.delenv("CACHEKIT_DISABLE_HIREDIS", raising=False)
    else:
        monkeypatch.setenv("CACHEKIT_DISABLE_HIREDIS", setting)
    assert _run_probe(_PARSER_PROBE) == {"hiredis_parser": hiredis_parser}


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


def test_disable_hiredis_setting_name_is_case_insensitive(monkeypatch):
    """Matches RedisBackendConfig, which reads the variable case-insensitively."""
    from cachekit.hiredis_compat import _disable_hiredis_setting

    monkeypatch.delenv("CACHEKIT_DISABLE_HIREDIS", raising=False)
    monkeypatch.setenv("cachekit_disable_hiredis", "true")
    assert _disable_hiredis_setting() is True


def test_unparseable_disable_hiredis_setting_is_not_logged(monkeypatch, caplog):
    """A mis-wired value can be a secret (a Redis URL with a password); the warning must not echo it."""
    from cachekit.hiredis_compat import _disable_hiredis_setting

    monkeypatch.setenv("CACHEKIT_DISABLE_HIREDIS", "redis://:supersecret@cache.example:6379")
    with caplog.at_level("WARNING", logger="cachekit.hiredis_compat"):
        assert _disable_hiredis_setting() is None
    assert "CACHEKIT_DISABLE_HIREDIS" in caplog.text
    assert "supersecret" not in caplog.text


_LOADED = object()  # stands in for an already-imported hiredis module


@pytest.mark.parametrize(
    ("step", "setting", "free_threaded", "gil_on", "hiredis", "blocked", "warns"),
    [
        ("import", True, False, True, None, True, None),
        ("import", True, True, False, _LOADED, False, "keeps the hiredis parser and the GIL is already on"),
        ("import", True, False, True, _LOADED, False, "keeps the hiredis parser"),
        ("import", False, True, False, None, False, None),
        ("import", None, True, False, None, False, None),
        ("redis", None, True, False, None, True, None),
        ("redis", None, True, True, None, False, None),
        ("redis", None, True, True, _LOADED, False, "GIL is already on"),
        ("redis", None, False, True, None, False, None),
        ("redis", False, True, False, None, False, None),
        ("redis", True, True, False, None, False, None),
    ],
    ids=[
        "import-true",
        "import-true-loaded-ft",
        "import-true-loaded-gil",
        "import-false",
        "import-unset",
        "redis-unset-ft",
        "redis-unset-ft-gil-on",
        "redis-unset-ft-loaded",
        "redis-unset-gil",
        "redis-false-ft",
        "redis-true-ft",
    ],
)
def test_hiredis_decision_table(monkeypatch, caplog, step, setting, free_threaded, gil_on, hiredis, blocked, warns):
    """Both decision points in-process: `true` blocks at import, the unset free-threaded default when Redis loads."""
    import sysconfig

    from cachekit import hiredis_compat

    monkeypatch.setattr(hiredis_compat, "_DISABLE", setting)
    monkeypatch.setattr(sysconfig, "get_config_var", lambda name: 1 if free_threaded else 0)
    monkeypatch.setattr(sys, "_is_gil_enabled", lambda: gil_on, raising=False)
    monkeypatch.setitem(sys.modules, "hiredis", hiredis)  # recorded first, so teardown restores the real entry
    if hiredis is None:
        monkeypatch.delitem(sys.modules, "hiredis")
    decide = hiredis_compat.block_hiredis_if_disabled if step == "import" else hiredis_compat.block_hiredis_for_free_threading

    with caplog.at_level("WARNING", logger="cachekit.hiredis_compat"):
        assert decide() is blocked
    if blocked:
        assert sys.modules["hiredis"] is None
    elif hiredis is None:
        assert "hiredis" not in sys.modules
    assert (warns in caplog.text) if warns else not caplog.text


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
