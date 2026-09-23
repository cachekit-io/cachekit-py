"""Tests for single-tenant mode tenant_id resolution (MEDIUM-02, LAB-4666).

Single-tenant mode derives keys and builds AAD from ONE tenant_id, resolved once
in ``__init__``: explicit ``deployment_uuid`` → ``CACHEKIT_DEPLOYMENT_UUID`` → the
protocol literal ``"default"`` (spec/intent-presets.md § Master Key Input, rule 5).
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from cachekit.cache_handler import CacheSerializationHandler
from cachekit.config import ConfigurationError, reset_settings


class TestSingleTenantModeValidation:
    """Test single-tenant mode configuration validation (MEDIUM-02, Criterion 1)."""

    def test_encryption_requires_explicit_mode(self):
        """Encryption must specify either tenant_extractor OR single_tenant_mode."""
        with pytest.raises(
            ConfigurationError,
            match="Encryption requires explicit tenant mode",
        ):
            CacheSerializationHandler(
                encryption=True,
                # Neither tenant_extractor nor single_tenant_mode provided
            )

    def test_mutual_exclusivity_tenant_extractor_and_single_tenant(self):
        """Cannot enable both tenant_extractor and single_tenant_mode."""
        from cachekit.decorators.tenant_context import ArgumentNameExtractor

        extractor = ArgumentNameExtractor()

        with pytest.raises(
            ConfigurationError,
            match="Cannot use both tenant_extractor and single_tenant_mode",
        ):
            CacheSerializationHandler(
                encryption=True,
                tenant_extractor=extractor,
                single_tenant_mode=True,
            )

    def test_single_tenant_mode_allowed_without_encryption(self, monkeypatch: pytest.MonkeyPatch):
        """Single-tenant mode flag is ignored when encryption=False."""
        # Clear env to prevent auto-detect from upgrading encryption
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)
        reset_settings()
        # Should not raise - single_tenant_mode is only validated when encryption=True
        handler = CacheSerializationHandler(
            encryption=False,
            single_tenant_mode=True,
        )
        assert handler.single_tenant_mode is True
        assert handler.encryption is False


class TestTenantIdResolution:
    """Explicit tenant sources win; otherwise the protocol literal "default"."""

    def test_provided_uuid_has_highest_priority(self):
        """Explicitly provided UUID should be used."""
        provided_uuid = "550e8400-e29b-41d4-a716-446655440000"

        handler = CacheSerializationHandler(
            encryption=True,
            single_tenant_mode=True,
            deployment_uuid=provided_uuid,
        )

        assert handler._single_tenant_id == provided_uuid

    def test_provided_uuid_validation(self):
        """Invalid UUID format should raise ConfigurationError."""
        with pytest.raises(ConfigurationError, match="Invalid deployment_uuid parameter"):
            CacheSerializationHandler(
                encryption=True,
                single_tenant_mode=True,
                deployment_uuid="not-a-valid-uuid",
            )

    def test_env_var_used_when_no_provided_uuid(self):
        """Environment variable CACHEKIT_DEPLOYMENT_UUID should be used."""
        env_uuid = "660f9511-f30c-52e5-b827-557766551111"

        with patch.dict(os.environ, {"CACHEKIT_DEPLOYMENT_UUID": env_uuid}):
            reset_settings()  # Clear cached settings to pick up new env var
            handler = CacheSerializationHandler(
                encryption=True,
                single_tenant_mode=True,
            )

            assert handler._single_tenant_id == env_uuid

    def test_invalid_env_var_raises_error(self):
        """Invalid UUID in environment variable should raise ConfigurationError."""
        with patch.dict(os.environ, {"CACHEKIT_DEPLOYMENT_UUID": "invalid-uuid"}):
            reset_settings()  # Clear cached settings to pick up new env var
            with pytest.raises(ConfigurationError, match="Invalid CACHEKIT_DEPLOYMENT_UUID"):
                CacheSerializationHandler(
                    encryption=True,
                    single_tenant_mode=True,
                )

    def test_default_is_protocol_literal_and_never_machine_local(self, tmp_path, monkeypatch):
        """No explicit tenant → literal "default" (spec rule 5); the legacy persisted
        ``~/.cachekit/deployment_uuid`` is neither read nor written — a per-host value
        in a KDF input is a permanent cross-SDK auth failure, not a miss."""
        monkeypatch.delenv("CACHEKIT_DEPLOYMENT_UUID", raising=False)
        reset_settings()
        fake_home = tmp_path / "home"
        legacy_file = fake_home / ".cachekit" / "deployment_uuid"
        legacy_file.parent.mkdir(parents=True)
        legacy_file.write_text("770fa622-041d-63f6-c938-668877662222")
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        handler = CacheSerializationHandler(encryption=True, single_tenant_mode=True)

        assert handler._single_tenant_id == CacheSerializationHandler.DEFAULT_TENANT_ID == "default"
        assert legacy_file.read_text() == "770fa622-041d-63f6-c938-668877662222"  # untouched, ignored


class TestTenantIDUsage:
    """The resolved tenant_id is what HKDF derives from AND what the AAD binds."""

    # protocol test-vectors/encryption.json → default_tenant.derived_key_fingerprint_hex:
    # HKDF-SHA256(master_key = 0x61 * 32 — hex "61"*32, NOT "a"*64 — tenant "default") encryption-key fingerprint.
    DEFAULT_TENANT_FINGERPRINT = "52d54c97f8e5efaa5bdf58a301f92726"  # pragma: allowlist secret

    def test_default_tenant_reaches_hkdf_and_aad(self, monkeypatch):
        """With no tenant supplied, the wrapper derives under "default" and the AAD
        carries "default" — asserted on the wrapper, not on a round-trip."""
        pytest.importorskip("cachekit._rust_serializer")
        monkeypatch.delenv("CACHEKIT_DEPLOYMENT_UUID", raising=False)
        reset_settings()
        handler = CacheSerializationHandler(encryption=True, single_tenant_mode=True, master_key="61" * 32)

        handler.serialize_data({"message": "hello"}, cache_key="test:key")

        wrapper = handler._encryption_wrapper_cache["default"]
        assert wrapper.tenant_id == "default"
        # HKDF input: the derived key is the protocol's pinned default-tenant key.
        assert wrapper.tenant_keys.encryption_fingerprint().hex() == self.DEFAULT_TENANT_FINGERPRINT
        # AAD component 1 is the same literal (v0x03: version byte, then len(4 BE) + tenant_id).
        _, metadata = wrapper.serialize({"message": "hello"}, cache_key="test:key")
        aad = wrapper._create_aad(metadata, "test:key")
        assert aad[:1] == b"\x03"
        assert aad[1:5] == len(b"default").to_bytes(4, "big")
        assert aad[5:12] == b"default"

    def test_deployment_uuid_used_as_tenant_id(self):
        """An explicit deployment UUID is the tenant for HKDF and AAD."""
        pytest.importorskip("cachekit._rust_serializer")
        provided_uuid = "770fa622-041d-63f6-c938-668877662222"

        handler = CacheSerializationHandler(
            encryption=True,
            single_tenant_mode=True,
            deployment_uuid=provided_uuid,
            master_key="a" * 64,
        )

        serialized = handler.serialize_data({"message": "hello"}, cache_key="test:key")
        assert handler._encryption_wrapper_cache[provided_uuid].tenant_id == provided_uuid
        assert handler.deserialize_data(serialized, cache_key="test:key") == {"message": "hello"}

    def test_legacy_uuid_tenant_ciphertext_still_decrypts(self, monkeypatch):
        """Migration: entries written under the pre-"default" UUID tenant stay readable.

        The CK frame header carries tenant_id and the auto-mode read path derives
        the wrapper from THAT value (AAD-bound, so not a downgrade vector), so a
        handler now writing under "default" decrypts legacy entries until they age
        out. No flush, no fallback code."""
        pytest.importorskip("cachekit._rust_serializer")
        monkeypatch.delenv("CACHEKIT_DEPLOYMENT_UUID", raising=False)
        reset_settings()
        legacy_uuid = "770fa622-041d-63f6-c938-668877662222"
        legacy = CacheSerializationHandler(
            encryption=True, single_tenant_mode=True, deployment_uuid=legacy_uuid, master_key="a" * 64
        )
        current = CacheSerializationHandler(encryption=True, single_tenant_mode=True, master_key="a" * 64)
        assert current._single_tenant_id == "default"

        legacy_bytes = legacy.serialize_data({"who": "legacy"}, cache_key="user:1")

        assert current.deserialize_data(legacy_bytes, cache_key="user:1") == {"who": "legacy"}
        # And the reverse: a legacy-pinned reader still decrypts new "default" entries.
        assert legacy.deserialize_data(current.serialize_data({"who": "new"}, cache_key="user:2"), cache_key="user:2") == {
            "who": "new"
        }


class TestErrorMessages:
    """Test error message clarity (MEDIUM-02, Criterion 4)."""

    def test_missing_mode_error_message(self):
        """Error message should explain both options clearly."""
        with pytest.raises(ConfigurationError) as exc_info:
            CacheSerializationHandler(encryption=True)

        error_msg = str(exc_info.value)
        assert "tenant_extractor" in error_msg
        assert "single_tenant_mode" in error_msg

    def test_mutual_exclusivity_error_message(self):
        """Error message should explain mutual exclusivity."""
        from cachekit.decorators.tenant_context import ArgumentNameExtractor

        with pytest.raises(ConfigurationError) as exc_info:
            CacheSerializationHandler(
                encryption=True,
                tenant_extractor=ArgumentNameExtractor(),
                single_tenant_mode=True,
            )

        error_msg = str(exc_info.value)
        assert "both" in error_msg.lower()
        assert "multi-tenant" in error_msg
        assert "single-tenant" in error_msg


class TestBackwardCompatibility:
    """Test backward compatibility with existing code (MEDIUM-02, Criterion 5)."""

    def test_existing_tenant_extractor_still_works(self):
        """Existing code using tenant_extractor should continue working."""
        from cachekit.decorators.tenant_context import ArgumentNameExtractor

        # This should work without single_tenant_mode
        handler = CacheSerializationHandler(
            encryption=True,
            tenant_extractor=ArgumentNameExtractor(),
        )

        assert handler.encryption is True
        assert handler.tenant_extractor is not None
        assert handler.single_tenant_mode is False

    def test_no_encryption_unchanged(self, monkeypatch: pytest.MonkeyPatch):
        """Non-encrypted handlers should work identically."""
        # Clear env to prevent auto-detect from upgrading encryption
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)
        reset_settings()
        handler = CacheSerializationHandler(
            encryption=False,
        )

        assert handler.encryption is False
        assert handler.tenant_extractor is None
        assert handler.single_tenant_mode is False
