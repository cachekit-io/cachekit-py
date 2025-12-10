"""Configuration validation functions for cachekit."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    """Exception raised for configuration errors.

    Examples:
        Raise with descriptive message:

        >>> raise ConfigurationError("REDIS_URL not configured")  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        cachekit.config.validation.ConfigurationError: REDIS_URL not configured

        Check exception message:

        >>> try:
        ...     raise ConfigurationError("Invalid TTL")
        ... except ConfigurationError as e:
        ...     str(e)
        'Invalid TTL'
    """

    pass


def validate_encryption_config(encryption: bool = False) -> None:
    """Validate encryption configuration when encryption is enabled.

    Checks that CACHEKIT_MASTER_KEY is set via pydantic-settings when encryption=True.

    Args:
        encryption: Whether encryption is enabled. If False, no validation.

    Raises:
        ConfigurationError: If encryption config is invalid

    Security Warning:
        Environment variables are NOT secure key storage for production.
        Use secrets management systems (HashiCorp Vault, AWS Secrets Manager, etc.)
        for production deployments.

    Examples:
        No-op when encryption is disabled:

        >>> validate_encryption_config(encryption=False)  # Returns None, no error

        Validation requires CACHEKIT_MASTER_KEY when enabled (requires env var):

        >>> validate_encryption_config(encryption=True)  # doctest: +SKIP
    """
    # Only validate if encryption is explicitly enabled
    if not encryption:
        return

    # Get master key from pydantic-settings (handles env vars properly)
    from cachekit.config.singleton import get_settings, reset_settings

    # Reset settings to pick up any environment changes (important for testing)
    reset_settings()
    settings = get_settings()
    master_key = settings.master_key.get_secret_value() if settings.master_key else None

    # Check if master_key is set
    if not master_key:
        raise ConfigurationError(
            "CACHEKIT_MASTER_KEY environment variable required when encryption=True. "
            "Generate with: python -c 'import secrets; print(secrets.token_hex(32))'"
        )

    # Production environment warning - check via settings (already loaded above)
    # Check deployment indicators
    if not settings.dev_mode:
        logger.warning(
            "🔒 SECURITY WARNING: Master key loaded from environment variable in PRODUCTION. "
            "Environment variables are NOT secure key storage. "
            "For production deployments, use secrets management system: "
            "HashiCorp Vault, AWS Secrets Manager, Azure Key Vault, or Google Secret Manager. "
            "KMS integration planned for future release."
        )

    # Validate key format and length
    try:
        key_bytes = bytes.fromhex(master_key)
        if len(key_bytes) < 32:
            raise ConfigurationError(
                f"CACHEKIT_MASTER_KEY must be at least 32 bytes (256 bits). "
                f"Got {len(key_bytes)} bytes. "
                "Generate with: python -c 'import secrets; print(secrets.token_hex(32))'"
            )
    except ValueError as e:
        raise ConfigurationError(f"CACHEKIT_MASTER_KEY must be hex-encoded: {e}") from e
