"""Choose redis-py's reply parser before anything imports redis.

redis-py imports hiredis when it is installed and binds its default parser at import time, and on a
free-threaded CPython build importing hiredis re-enables the GIL for the whole process. The only way
to opt out is to keep hiredis from loading before the first ``import redis``, so this module must be
imported before any redis import and must not import anything that does: it reads
``CACHEKIT_DISABLE_HIREDIS`` straight from the environment, not through ``RedisBackendConfig``.

- ``CACHEKIT_DISABLE_HIREDIS=true`` blocks hiredis on every build.
- ``CACHEKIT_DISABLE_HIREDIS=false`` keeps hiredis on every build, re-enabling the GIL on a
  free-threaded one.
- Unset: a free-threaded build with the GIL still off blocks hiredis; a GIL build keeps it.

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


def configure_hiredis_for_free_threading() -> bool:
    """Block hiredis before redis loads when the setting or a GIL-free interpreter calls for it.

    Returns:
        bool: True if hiredis is blocked, so redis-py uses its pure-Python parser
    """
    disable = _disable_hiredis_setting()
    if disable is False:
        return False
    free_threaded = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    if disable is None and not free_threaded:
        return False
    if sys.modules.get("hiredis") is not None:
        logger.warning(
            "hiredis was imported before cachekit, so redis-py keeps the hiredis parser%s",
            " and the GIL is already on" if free_threaded else "",
        )
        return False
    if disable is None and sys._is_gil_enabled():  # type: ignore[attr-defined]
        return False  # Something else already re-enabled the GIL; hiredis would cost nothing more.
    sys.modules.setdefault("hiredis", None)  # type: ignore[arg-type]  # None makes `import hiredis` raise ImportError
    logger.debug("hiredis blocked - redis-py will use its pure-Python parser")
    return True


HIREDIS_DISABLED = configure_hiredis_for_free_threading()
