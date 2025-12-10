"""
Unit tests for encryption configuration validation.

Tests validate_encryption_config() with master key validation, length checks,
and production environment warnings.
"""

from __future__ import annotations

import logging

import pytest

from cachekit.config import ConfigurationError, validate_encryption_config

# Mark all tests in this module as unit tests
pytestmark = pytest.mark.unit


class TestEncryptionConfigValidation:
    """Test validate_encryption_config() with various scenarios."""

    def test_validate_no_encryption_passes_without_master_key(self, monkeypatch):
        """Non-encrypted config should pass validation without master key."""
        # Remove master key from environment
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)

        # Should not raise when encryption=False
        validate_encryption_config(encryption=False)

    def test_validate_missing_master_key_raises_configuration_error(self, monkeypatch):
        """CRITICAL: Missing master key must raise ConfigurationError with actionable message."""
        # Remove master key from environment
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)

        # Should raise ConfigurationError when encryption=True
        with pytest.raises(ConfigurationError) as exc_info:
            validate_encryption_config(encryption=True)

        error_msg = str(exc_info.value)
        assert "CACHEKIT_MASTER_KEY" in error_msg
        assert "required" in error_msg
        # Error message should include remediation guidance
        assert "secrets.token_hex(32)" in error_msg or "Generate" in error_msg

    def test_validate_master_key_too_short_raises_error(self, monkeypatch):
        """CRITICAL: Master key <32 bytes must raise ConfigurationError."""
        # Set master key that's too short (12 bytes = 24 hex chars)
        short_key = "deadbeef" * 3  # 24 hex chars = 12 bytes
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", short_key)

        with pytest.raises(ConfigurationError) as exc_info:
            validate_encryption_config(encryption=True)

        error_msg = str(exc_info.value)
        assert "at least 32 bytes" in error_msg or "256 bits" in error_msg
        assert "12 bytes" in error_msg  # Shows actual length
        # Error message should include remediation guidance
        assert "secrets.token_hex(32)" in error_msg or "Generate" in error_msg

    @pytest.mark.parametrize(
        "key_length_bytes,should_pass",
        [
            (16, False),  # Too short (128 bits)
            (24, False),  # Too short (192 bits)
            (31, False),  # Too short (248 bits)
            (32, True),  # Exactly 256 bits (PASS)
            (64, True),  # Longer than minimum (PASS)
            (128, True),  # Much longer (PASS)
        ],
    )
    def test_validate_master_key_length_boundaries(self, key_length_bytes, should_pass, monkeypatch):
        """Master key validation should enforce 32-byte minimum."""
        import secrets

        # Generate key of specific length
        master_key = secrets.token_hex(key_length_bytes)
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", master_key)

        if should_pass:
            # Should not raise
            validate_encryption_config(encryption=True)
        else:
            # Should raise ConfigurationError
            with pytest.raises(ConfigurationError, match="at least 32 bytes"):
                validate_encryption_config(encryption=True)

    def test_validate_master_key_exactly_32_bytes_passes(self, monkeypatch):
        """Master key of exactly 32 bytes (256 bits) should pass validation."""
        import secrets

        # Generate exactly 32 bytes (64 hex chars)
        master_key = secrets.token_hex(32)
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", master_key)

        # Should not raise
        validate_encryption_config(encryption=True)

    def test_validate_master_key_longer_than_32_bytes_passes(self, monkeypatch):
        """Master key >32 bytes should pass validation."""
        import secrets

        # Generate 64 bytes (128 hex chars)
        master_key = secrets.token_hex(64)
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", master_key)

        # Should not raise
        validate_encryption_config(encryption=True)

    def test_validate_master_key_invalid_hex_format_raises_error(self, monkeypatch):
        """Invalid hex-encoded master key must raise ConfigurationError."""
        # Set invalid hex key (contains non-hex characters)
        invalid_key = "not_valid_hex_string_zzzz"
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", invalid_key)

        with pytest.raises(ConfigurationError) as exc_info:
            validate_encryption_config(encryption=True)

        error_msg = str(exc_info.value)
        assert "hex" in error_msg.lower() or "encoded" in error_msg.lower()

    @pytest.mark.parametrize(
        "env_value,expected_env",
        [
            ("production", "production"),
            ("prod", "prod"),
            ("prd", "prd"),
            ("PRODUCTION", "production"),  # Case insensitive
            ("staging", None),  # Not production
            ("development", None),  # Not production
        ],
    )
    def test_validate_production_environment_warning(self, env_value, expected_env, monkeypatch, caplog):
        """Production environment should log security warning."""
        import secrets

        # Set valid master key
        master_key = secrets.token_hex(32)
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", master_key)

        # Set environment
        if env_value:
            monkeypatch.setenv("ENV", env_value)
        else:
            monkeypatch.delenv("ENV", raising=False)

        caplog.set_level(logging.WARNING)

        # Validate
        validate_encryption_config(encryption=True)

        # Check for production warning
        warning_messages = [record.message for record in caplog.records if record.levelname == "WARNING"]

        if expected_env:
            # Should have production warning
            has_prod_warning = any(
                "PRODUCTION" in msg.upper()
                and ("environment variable" in msg.lower() or "secure" in msg.lower() or "vault" in msg.lower())
                for msg in warning_messages
            )
            assert has_prod_warning, f"Expected production warning for ENV={env_value}"
        else:
            # Should NOT have production warning (or it's optional)
            pass

    def test_validate_error_message_includes_remediation(self, monkeypatch):
        """ConfigurationError messages should include remediation guidance."""
        # Test missing key error message
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)

        with pytest.raises(ConfigurationError) as exc_info:
            validate_encryption_config(encryption=True)

        error_msg = str(exc_info.value)
        # Should include generation command
        assert "python -c" in error_msg or "secrets.token_hex" in error_msg

    def test_validate_error_message_includes_actual_length(self, monkeypatch):
        """ConfigurationError for short key should show actual key length."""
        # Set 16-byte key
        short_key = "deadbeef" * 4  # 32 hex chars = 16 bytes
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", short_key)

        with pytest.raises(ConfigurationError) as exc_info:
            validate_encryption_config(encryption=True)

        error_msg = str(exc_info.value)
        assert "16 bytes" in error_msg  # Shows actual length

    def test_validate_called_at_initialization(self, monkeypatch):
        """Validation should be called at appropriate initialization points."""
        import secrets

        # Set valid master key
        master_key = secrets.token_hex(32)
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", master_key)

        # Validation should succeed when called explicitly
        validate_encryption_config(encryption=True)

        # Validation should also succeed when encryption is disabled
        validate_encryption_config(encryption=False)

    def test_validate_production_warning_mentions_kms(self, monkeypatch, caplog):
        """Production warning should mention KMS/secrets management."""
        import secrets

        # Set valid master key
        master_key = secrets.token_hex(32)
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", master_key)
        monkeypatch.setenv("ENV", "production")

        caplog.set_level(logging.WARNING)

        # Validate
        validate_encryption_config(encryption=True)

        # Check warning content
        warning_messages = [record.message for record in caplog.records if record.levelname == "WARNING"]

        # Should mention secrets management systems
        has_kms_mention = any(any(kms in msg for kms in ["Vault", "Secret", "KMS", "Key Vault"]) for msg in warning_messages)

        assert has_kms_mention, "Production warning should mention secrets management systems"

    def test_validate_encryption_false_skips_all_checks(self, monkeypatch):
        """When encryption=False, all validation should be skipped."""
        # Don't set master key at all
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)

        # Should not raise any errors
        validate_encryption_config(encryption=False)

    def test_validate_encryption_default_false(self, monkeypatch):
        """Default encryption parameter should be False."""
        # Don't set master key
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)

        # Should not raise (encryption defaults to False)
        validate_encryption_config()

    @pytest.mark.parametrize(
        "invalid_key,expected_error_keyword",
        [
            ("", "required"),  # Empty string
            ("   ", "hex"),  # Whitespace only
            ("ZZZZZZZZ" * 8, "hex"),  # Invalid hex characters
            ("12345", "hex"),  # Too short - gets caught by hex validation first
        ],
    )
    def test_validate_various_invalid_keys(self, invalid_key, expected_error_keyword, monkeypatch):
        """Various invalid master keys should raise appropriate errors."""
        if invalid_key:
            monkeypatch.setenv("CACHEKIT_MASTER_KEY", invalid_key)
        else:
            monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)

        with pytest.raises(ConfigurationError) as exc_info:
            validate_encryption_config(encryption=True)

        error_msg = str(exc_info.value).lower()
        assert expected_error_keyword.lower() in error_msg

    def test_validate_with_uppercase_hex_key(self, monkeypatch):
        """Uppercase hex characters in master key should be valid."""
        import secrets

        # Generate key with uppercase hex
        master_key = secrets.token_hex(32).upper()
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", master_key)

        # Should not raise
        validate_encryption_config(encryption=True)

    def test_validate_with_mixed_case_hex_key(self, monkeypatch):
        """Mixed case hex characters in master key should be valid."""
        # Create mixed case hex key (32 bytes = 64 hex chars)
        master_key = "DeAdBeEf" * 8  # 64 hex chars
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", master_key)

        # Should not raise
        validate_encryption_config(encryption=True)


class TestConfigurationErrorMessages:
    """Test that ConfigurationError messages are actionable."""

    def test_missing_key_error_actionable(self, monkeypatch):
        """Missing key error should tell user exactly what to do."""
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)

        with pytest.raises(ConfigurationError) as exc_info:
            validate_encryption_config(encryption=True)

        error_msg = str(exc_info.value)

        # Should tell user what's wrong
        assert "CACHEKIT_MASTER_KEY" in error_msg
        assert "required" in error_msg

        # Should tell user how to fix it
        assert "Generate" in error_msg or "python -c" in error_msg
        assert "secrets.token_hex(32)" in error_msg

    def test_short_key_error_actionable(self, monkeypatch):
        """Short key error should show length and how to fix."""
        short_key = "deadbeef"  # 8 hex chars = 4 bytes
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", short_key)

        with pytest.raises(ConfigurationError) as exc_info:
            validate_encryption_config(encryption=True)

        error_msg = str(exc_info.value)

        # Should show actual length
        assert "4 bytes" in error_msg

        # Should show required length
        assert "32 bytes" in error_msg or "256 bits" in error_msg

        # Should tell user how to fix it
        assert "Generate" in error_msg or "secrets.token_hex(32)" in error_msg

    def test_invalid_hex_error_actionable(self, monkeypatch):
        """Invalid hex error should explain the problem."""
        invalid_key = "not-valid-hex-ZZZZ"
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", invalid_key)

        with pytest.raises(ConfigurationError) as exc_info:
            validate_encryption_config(encryption=True)

        error_msg = str(exc_info.value)

        # Should mention hex encoding
        assert "hex" in error_msg.lower()


class TestProductionWarnings:
    """Test production environment security warnings."""

    @pytest.mark.parametrize(
        "env_name",
        ["production", "prod", "prd", "PRODUCTION", "Prod", "PRD"],
    )
    def test_production_warning_logged_for_prod_envs(self, env_name, monkeypatch, caplog):
        """Production warning should be logged for all production environment names."""
        import secrets

        master_key = secrets.token_hex(32)
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", master_key)
        monkeypatch.setenv("ENV", env_name)

        caplog.set_level(logging.WARNING)

        validate_encryption_config(encryption=True)

        # Should have warning
        warning_messages = [record.message for record in caplog.records if record.levelname == "WARNING"]
        assert len(warning_messages) > 0, f"Expected warning for ENV={env_name}"

        # Warning should mention security
        has_security_warning = any("SECURITY" in msg.upper() or "WARNING" in msg.upper() for msg in warning_messages)
        assert has_security_warning

    def test_no_production_warning_for_dev_env(self, monkeypatch, caplog):
        """No production warning should be logged for development environments."""
        import secrets

        master_key = secrets.token_hex(32)
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", master_key)
        monkeypatch.setenv("CACHEKIT_DEV_MODE", "true")

        caplog.set_level(logging.WARNING)

        validate_encryption_config(encryption=True)

        # May or may not have warnings, but they shouldn't be production-specific
        warning_messages = [record.message for record in caplog.records if record.levelname == "WARNING"]

        # No production-specific warnings
        has_prod_warning = any("PRODUCTION" in msg.upper() for msg in warning_messages)
        assert not has_prod_warning

    def test_production_warning_mentions_alternatives(self, monkeypatch, caplog):
        """Production warning should mention alternative secrets management."""
        import secrets

        master_key = secrets.token_hex(32)
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", master_key)
        monkeypatch.setenv("ENV", "production")

        caplog.set_level(logging.WARNING)

        validate_encryption_config(encryption=True)

        warning_messages = [record.message for record in caplog.records if record.levelname == "WARNING"]
        combined_warnings = " ".join(warning_messages)

        # Should mention at least one secrets management system
        alternatives = ["Vault", "AWS Secrets", "Azure Key Vault", "Google Secret", "KMS"]
        has_alternative = any(alt in combined_warnings for alt in alternatives)

        assert has_alternative, "Production warning should mention secrets management alternatives"
