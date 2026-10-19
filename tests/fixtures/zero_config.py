"""The environment that configures a backend, and how to remove it: the nothing-configured path.

The fixtures that use these (``nothing_configured``, ``no_redis_built``) live in ``tests/conftest.py``.
"""

from __future__ import annotations

import os

import pytest

SELECTORS = ("CACHEKIT_API_KEY", "CACHEKIT_REDIS_URL", "CACHEKIT_MEMCACHED_SERVERS", "CACHEKIT_FILE_CACHE_DIR")
BACKEND_ENV = (*SELECTORS, "REDIS_URL")


def clear_backend_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete every selector and REDIS_URL in any casing: _is_configured() reads names case-insensitively."""
    wanted = {var.lower() for var in BACKEND_ENV}
    for name in [name for name in os.environ if name.lower() in wanted]:
        monkeypatch.delenv(name)


def unconfigured_environ() -> dict[str, str]:
    """A copy of os.environ with REDIS_URL and every CACHEKIT_* variable removed, in any casing."""
    return {k: v for k, v in os.environ.items() if not (k.upper().startswith("CACHEKIT_") or k.upper() == "REDIS_URL")}
