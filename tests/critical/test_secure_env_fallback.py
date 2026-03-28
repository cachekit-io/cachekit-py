"""
CRITICAL PATH TEST: cache.secure env var fallback (issue #69)

Verifies cache.secure resolves CACHEKIT_MASTER_KEY from the environment
when master_key is not passed explicitly. No Redis required.
"""

from __future__ import annotations

import pytest

from cachekit.config.singleton import reset_settings
from cachekit.decorators import cache

pytestmark = [pytest.mark.critical]


class TestSecureEnvFallback:
    """Critical: cache.secure must resolve master_key from env var."""

    def test_secure_reads_master_key_from_env(self, monkeypatch):
        """@cache.secure(ttl=300) works when CACHEKIT_MASTER_KEY is set."""
        test_key = "ab" * 32  # 64 hex chars = 32 bytes
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", test_key)
        reset_settings()

        try:

            @cache.secure(backend=None, ttl=300)
            def secure_from_env(x: int) -> int:
                return x * 2

            assert secure_from_env is not None
        finally:
            reset_settings()

    def test_secure_raises_without_key_or_env(self, monkeypatch):
        """@cache.secure raises ValueError when no key available anywhere."""
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)
        reset_settings()

        try:
            with pytest.raises(ValueError, match="CACHEKIT_MASTER_KEY"):

                @cache.secure(backend=None)
                def secure_no_key():
                    pass
        finally:
            reset_settings()

    def test_explicit_master_key_takes_precedence(self, monkeypatch):
        """Explicit master_key param is used even when env var is set."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", "ff" * 32)
        reset_settings()

        explicit_key = "aa" * 32

        try:

            @cache.secure(master_key=explicit_key, backend=None, ttl=60)
            def secure_explicit(x: int) -> int:
                return x

            assert secure_explicit is not None
        finally:
            reset_settings()
