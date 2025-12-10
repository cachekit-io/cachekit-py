"""Tests for single-tenant mode with deterministic UUID (MEDIUM-02).

This module tests the explicit single-tenant mode configuration
that requires deployment_uuid for cryptographic key isolation.
"""

import os
import uuid
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

    def test_single_tenant_mode_allowed_without_encryption(self):
        """Single-tenant mode flag is ignored when encryption=False."""
        # Should not raise - single_tenant_mode is only validated when encryption=True
        handler = CacheSerializationHandler(
            encryption=False,
            single_tenant_mode=True,
        )
        assert handler.single_tenant_mode is True
        assert handler.encryption is False


class TestDeterministicUUIDGeneration:
    """Test deterministic UUID generation (MEDIUM-02, Criterion 2)."""

    def test_provided_uuid_has_highest_priority(self, tmp_path):
        """Explicitly provided UUID should be used."""
        provided_uuid = "550e8400-e29b-41d4-a716-446655440000"

        handler = CacheSerializationHandler(
            encryption=True,
            single_tenant_mode=True,
            deployment_uuid=provided_uuid,
        )

        assert handler._deployment_uuid_value == provided_uuid

    def test_provided_uuid_validation(self):
        """Invalid UUID format should raise ConfigurationError."""
        with pytest.raises(ConfigurationError, match="Invalid deployment_uuid format"):
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

            assert handler._deployment_uuid_value == env_uuid

    def test_invalid_env_var_raises_error(self):
        """Invalid UUID in environment variable should raise ConfigurationError."""
        with patch.dict(os.environ, {"CACHEKIT_DEPLOYMENT_UUID": "invalid-uuid"}):
            reset_settings()  # Clear cached settings to pick up new env var
            with pytest.raises(ConfigurationError, match="Invalid deployment_uuid in configuration"):
                CacheSerializationHandler(
                    encryption=True,
                    single_tenant_mode=True,
                )

    def test_persistent_file_created_when_no_uuid_provided(self, tmp_path, monkeypatch):
        """Should create persistent file when no UUID provided or in env."""
        # Clear any environment variable from previous tests
        monkeypatch.delenv("CACHEKIT_DEPLOYMENT_UUID", raising=False)
        reset_settings()  # Clear cached settings

        # Use temporary home directory
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        handler = CacheSerializationHandler(
            encryption=True,
            single_tenant_mode=True,
        )

        # Verify UUID was generated and is valid
        assert handler._deployment_uuid_value is not None
        uuid.UUID(handler._deployment_uuid_value)  # Should not raise

        # Verify file was created
        uuid_file = fake_home / ".cachekit" / "deployment_uuid"
        assert uuid_file.exists()
        assert uuid_file.read_text().strip() == handler._deployment_uuid_value

        # Verify file permissions (owner read/write only)
        assert uuid_file.stat().st_mode & 0o777 == 0o600

    def test_persistent_file_reused_across_restarts(self, tmp_path, monkeypatch):
        """Same UUID should be used across multiple handler initializations (determinism)."""
        # Use temporary home directory
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        # First initialization - creates UUID file
        handler1 = CacheSerializationHandler(
            encryption=True,
            single_tenant_mode=True,
        )
        uuid1 = handler1._deployment_uuid_value

        # Second initialization - should reuse same UUID
        handler2 = CacheSerializationHandler(
            encryption=True,
            single_tenant_mode=True,
        )
        uuid2 = handler2._deployment_uuid_value

        # CRITICAL: Must be same UUID for determinism
        assert uuid1 == uuid2

    def test_corrupted_file_regenerated(self, tmp_path, monkeypatch):
        """Corrupted UUID file should be regenerated."""
        # Use temporary home directory
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        # Create corrupted UUID file
        uuid_file = fake_home / ".cachekit" / "deployment_uuid"
        uuid_file.parent.mkdir(parents=True, exist_ok=True)
        uuid_file.write_text("corrupted-not-a-uuid")

        # Should regenerate valid UUID
        handler = CacheSerializationHandler(
            encryption=True,
            single_tenant_mode=True,
        )

        # Verify new UUID is valid
        assert handler._deployment_uuid_value is not None
        uuid.UUID(handler._deployment_uuid_value)  # Should not raise
        assert handler._deployment_uuid_value != "corrupted-not-a-uuid"


class TestTenantIDUsage:
    """Test tenant_id usage in single-tenant mode (MEDIUM-02, Criterion 3)."""

    def test_deployment_uuid_used_as_tenant_id(self, tmp_path, monkeypatch):
        """Deployment UUID should be used as tenant_id for encryption."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        provided_uuid = "770fa622-041d-63f6-c938-668877662222"
        master_key_hex = "a" * 64  # 32-byte key in hex

        handler = CacheSerializationHandler(
            encryption=True,
            single_tenant_mode=True,
            deployment_uuid=provided_uuid,
            master_key=master_key_hex,
        )

        # Serialize some data
        test_data = {"message": "hello"}
        serialized = handler.serialize_data(test_data, cache_key="test:key")

        # Verify serialization succeeds (proves tenant_id was valid)
        assert serialized is not None

        # Verify deserialization works (proves determinism)
        deserialized = handler.deserialize_data(serialized, cache_key="test:key")
        assert deserialized == test_data


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

    def test_no_encryption_unchanged(self):
        """Non-encrypted handlers should work identically."""
        handler = CacheSerializationHandler(
            encryption=False,
        )

        assert handler.encryption is False
        assert handler.tenant_extractor is None
        assert handler.single_tenant_mode is False
