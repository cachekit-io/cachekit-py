"""Tests for CacheSerializationHandler encryption auto-detection.

When CACHEKIT_MASTER_KEY is set and encryption is not explicitly configured,
the handler auto-enables encryption with single_tenant_mode=True.
"""

from __future__ import annotations

import pytest

from cachekit.cache_handler import CacheSerializationHandler
from cachekit.config.singleton import reset_settings

_FAKE_KEY = "ab" * 32  # pragma: allowlist secret


@pytest.mark.unit
class TestEncryptionAutoDetect:
    """CacheSerializationHandler auto-detects CACHEKIT_MASTER_KEY."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        yield
        reset_settings()

    def test_auto_detect_enables_encryption(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Handler enables encryption when CACHEKIT_MASTER_KEY is set."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        reset_settings()

        handler = CacheSerializationHandler(serializer_name="default")

        assert handler.encryption is True
        assert handler.master_key == _FAKE_KEY
        assert handler.single_tenant_mode is True

    def test_auto_detect_no_op_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Handler stays plaintext when CACHEKIT_MASTER_KEY is not set."""
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)
        reset_settings()

        handler = CacheSerializationHandler(serializer_name="default")

        assert handler.encryption is False
        assert handler.master_key is None

    def test_auto_detect_no_op_when_explicitly_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit encryption=True is not overwritten by env var."""
        env_key = "ab" * 32  # pragma: allowlist secret
        explicit_key = "cc" * 32  # pragma: allowlist secret
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", env_key)
        reset_settings()

        handler = CacheSerializationHandler(
            serializer_name="default",
            encryption=True,
            master_key=explicit_key,
            single_tenant_mode=True,
        )

        assert handler.master_key == explicit_key

    def test_auto_detect_no_op_when_tenant_extractor_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If tenant_extractor is passed, auto-detect is skipped (user expressing intent)."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        reset_settings()

        def extractor(*a, **kw):
            return "tenant-1"

        handler = CacheSerializationHandler(
            serializer_name="default",
            encryption=False,
            tenant_extractor=extractor,
        )

        # User passed tenant_extractor without encryption=True — auto-detect respects that
        assert handler.encryption is False
