"""The environment that configures cachekit, and how to remove it: the nothing-configured path.

The fixtures that use these (``nothing_configured``, ``no_redis_built``) live in ``tests/conftest.py``.
"""

from __future__ import annotations

import os

import pytest

SELECTORS = ("CACHEKIT_API_KEY", "CACHEKIT_REDIS_URL", "CACHEKIT_MEMCACHED_SERVERS", "CACHEKIT_FILE_CACHE_DIR")
BACKEND_ENV = (*SELECTORS, "REDIS_URL")


def _configures(name: str) -> bool:
    """REDIS_URL or any CACHEKIT_* variable, in any casing: cachekit's settings read names case-insensitively.

    Not only the backend selectors: an ambient CACHEKIT_MASTER_KEY makes a bare @cache raise, and any other
    setting would move a default a test asserts.
    """
    upper = name.upper()
    return upper.startswith("CACHEKIT_") or upper == "REDIS_URL"


def clear_cachekit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete REDIS_URL and every CACHEKIT_* variable, in any casing."""
    for name in list(os.environ):
        if _configures(name):
            monkeypatch.delenv(name)


def unconfigured_environ() -> dict[str, str]:
    """A copy of os.environ without REDIS_URL or any CACHEKIT_* variable, in any casing."""
    return {name: value for name, value in os.environ.items() if not _configures(name)}
