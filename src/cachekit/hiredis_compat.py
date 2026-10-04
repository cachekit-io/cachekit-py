"""Choose redis-py's reply parser before redis-py loads.

redis-py imports hiredis when it is installed and binds its default parser at import time, and on a
free-threaded CPython build importing hiredis re-enables the GIL for the whole process. The only way
to opt out is to keep hiredis from loading before the first ``import redis``, so this module imports
nothing that imports redis: it reads ``CACHEKIT_DISABLE_HIREDIS`` straight from the environment, not
through ``RedisBackendConfig``.

- ``CACHEKIT_DISABLE_HIREDIS=true`` blocks hiredis at ``import cachekit``, on every build.
- ``CACHEKIT_DISABLE_HIREDIS=false`` never blocks hiredis; on a free-threaded build that re-enables
  the GIL once redis-py loads.
- Unset: a free-threaded build whose GIL is still off blocks hiredis when cachekit's Redis backend
  package (``cachekit.backends.redis``) loads, before it imports redis-py. A program that never uses
  that package never loads redis-py, so nothing is blocked. A GIL build keeps hiredis.

Blocking is process-wide: ``sys.modules["hiredis"] = None`` makes any later ``import hiredis`` in the
process raise ImportError, and redis-py falls back to its pure-Python parser.
"""

import logging
import os
import sys
import sysconfig

logger = logging.getLogger(__name__)

_SETTING = "CACHEKIT_DISABLE_HIREDIS"
# Same spellings pydantic accepts for RedisBackendConfig.disable_hiredis.
_TRUE = frozenset({"1", "on", "t", "true", "y", "yes"})
_FALSE = frozenset({"0", "off", "f", "false", "n", "no"})


def _disable_hiredis_setting() -> bool | None:
    """CACHEKIT_DISABLE_HIREDIS as a bool, or None when unset (or unparseable, which is logged).

    The name matches case-insensitively, as RedisBackendConfig (case_sensitive=False) reads it.
    """
    raw = os.environ.get(_SETTING)
    if raw is None:
        raw = next((v for k, v in os.environ.items() if k.upper() == _SETTING), None)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    # Never log the value: a mis-wired variable can hold a secret, such as a Redis URL with a password.
    logger.warning("Ignoring %s: expected true or false", _SETTING)
    return None


# Read once, at `import cachekit`, so an unparseable value is logged once.
_DISABLE = _disable_hiredis_setting()


def _free_threaded() -> bool:
    return bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


def _block() -> bool:
    """Keep hiredis out of the process, unless it already loaded (then log why the block came too late)."""
    if sys.modules.get("hiredis") is not None:
        logger.warning(
            "hiredis was imported before cachekit could block it, so redis-py keeps the hiredis parser%s",
            " and the GIL is already on" if _free_threaded() else "",
        )
        return False
    sys.modules.setdefault("hiredis", None)  # type: ignore[arg-type]  # None makes `import hiredis` raise ImportError
    logger.debug("hiredis blocked - redis-py will use its pure-Python parser")
    return True


def block_hiredis_if_disabled() -> bool:
    """Block hiredis now when CACHEKIT_DISABLE_HIREDIS=true, on every build. Runs at ``import cachekit``.

    Returns:
        bool: True if hiredis is blocked
    """
    return _DISABLE is True and _block()


def block_hiredis_for_free_threading() -> bool:
    """With the setting unset on a free-threaded build whose GIL is still off, block hiredis.

    Runs when ``cachekit.backends.redis`` loads, before it imports redis-py.

    Returns:
        bool: True if hiredis is blocked by this call
    """
    if _DISABLE is not None or not _free_threaded():
        return False
    if sys.modules.get("hiredis") is None and sys._is_gil_enabled():  # type: ignore[attr-defined]
        return False  # Something else already re-enabled the GIL; hiredis would cost nothing more.
    return _block()


block_hiredis_if_disabled()
