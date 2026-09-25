"""Configuration validation functions for cachekit."""

from __future__ import annotations

import logging

from pydantic import ValidationError
from pydantic_core import InitErrorDetails

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


def redact_validation_error(error: ValidationError) -> ValidationError:
    """Return a copy of a settings ``ValidationError`` with every input redacted (CWE-532).

    ``hide_input_in_errors`` only affects ``str()``/``repr()``: ``errors()`` and ``json()`` still
    snapshot the raw input, which for a settings model is cleartext credentials (a master key, an
    API key, a password in a URL) and is exactly what error trackers serialize. A model-level error
    (``loc == ()``) snapshots the whole input dict. The copy keeps each error's type, loc, msg and ctx.

    Raise the copy OUTSIDE the ``except`` block that caught the original: ``raise ... from None``
    only hides the chain, and the original would still hang off ``__context__``.

    Only pydantic's built-in error types can be rebuilt; a ``PydanticCustomError`` type raises
    ``KeyError`` here.
    """
    sanitized: list[InitErrorDetails] = []
    for err in error.errors(include_url=False):
        detail: InitErrorDetails = {"type": err["type"], "loc": err["loc"], "input": "[REDACTED]"}
        ctx = err.get("ctx")
        if ctx:
            detail["ctx"] = ctx
        sanitized.append(detail)
    return ValidationError.from_exception_data(error.title, sanitized, hide_input=True)


def validate_encryption_config(encryption: bool | None = False, master_key: str | None = None) -> None:
    """Validate encryption configuration when encryption is enabled.

    Checks for a master key: first from the explicit parameter, then from
    CACHEKIT_MASTER_KEY env var via pydantic-settings.

    Args:
        encryption: Tri-state encryption flag. Falsy (None unset / False opt-out) skips
                    validation; only an explicit True requires a resolvable master key.
        master_key: Explicit master key (hex string). Takes precedence over env var.

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

    # Resolve master key: explicit param > env var via settings
    resolved_key = master_key
    if not resolved_key:
        from cachekit.config.singleton import get_settings

        settings = get_settings()
        resolved_key = settings.master_key.get_secret_value() if settings.master_key else None

    if not resolved_key:
        raise ConfigurationError(
            "Master key required when encryption=True. Either pass master_key= "
            "or set CACHEKIT_MASTER_KEY environment variable. "
            "Generate with: python -c 'import secrets; print(secrets.token_hex(32))'"
        )

    # Production environment warning when key came from env var (not inline)
    if not master_key:
        from cachekit.config.singleton import get_settings

        settings = get_settings()
        if not settings.dev_mode:
            logger.warning(
                "Master key loaded from environment variable. "
                "For production, use a secrets management system "
                "(HashiCorp Vault, AWS Secrets Manager, etc.)."
            )

    # Validate key format and length
    try:
        key_bytes = bytes.fromhex(resolved_key)
        if len(key_bytes) < 32:
            raise ConfigurationError(
                f"CACHEKIT_MASTER_KEY must be at least 32 bytes (256 bits). "
                f"Got {len(key_bytes)} bytes. "
                "Generate with: python -c 'import secrets; print(secrets.token_hex(32))'"
            )
    except ValueError as e:
        raise ConfigurationError(f"CACHEKIT_MASTER_KEY must be hex-encoded: {e}") from e
