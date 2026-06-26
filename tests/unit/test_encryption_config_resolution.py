"""Encryption-config resolution correctness.

#195: get_settings() built the config once and cached it process-wide. If the first build happened
before CACHEKIT_MASTER_KEY entered the environment (e.g. an import-time @cache decorator before the
app loads secrets), it froze master_key=None forever — encryption then silently never activated.
The singleton must re-read once the key appears.

#194: the missing-key error pointed users at REDIS_CACHE_MASTER_KEY, which is never read; the only
honored variable is CACHEKIT_MASTER_KEY.
"""

from __future__ import annotations

import pytest

from cachekit.config.singleton import get_settings, reset_settings
from cachekit.serializers.auto_serializer import AutoSerializer
from cachekit.serializers.encryption_wrapper import EncryptionError, EncryptionWrapper

_HEX_KEY = "a" * 64  # 32-byte hex master key


@pytest.mark.unit
class TestKeylessSingletonSelfHeals:
    """#195: a keyless singleton must not freeze permanently once the key is set."""

    def test_get_settings_rereads_when_master_key_appears(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)
        reset_settings()
        try:
            assert get_settings().master_key is None  # first build: env not yet set -> keyless
            monkeypatch.setenv("CACHEKIT_MASTER_KEY", _HEX_KEY)
            assert get_settings().master_key is not None  # must self-heal (the bug froze this at None)
        finally:
            reset_settings()

    def test_get_settings_stable_once_key_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Guard: self-heal must not re-read on every call once the key is loaded.
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _HEX_KEY)
        reset_settings()
        try:
            assert get_settings() is get_settings()
        finally:
            reset_settings()


@pytest.mark.unit
class TestMissingKeyErrorNamesCorrectEnvVar:
    """#194: the missing-key error must name the env var that is actually honored."""

    def test_error_names_cachekit_master_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)
        reset_settings()
        try:
            with pytest.raises(EncryptionError) as exc:
                EncryptionWrapper(serializer=AutoSerializer(), master_key=None)
            msg = str(exc.value)
            assert "CACHEKIT_MASTER_KEY" in msg
            assert "REDIS_CACHE_MASTER_KEY" not in msg
        finally:
            reset_settings()
