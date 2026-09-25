"""Configuration validation functions for cachekit."""

from __future__ import annotations

import logging
from typing import Any, get_args

from pydantic import ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from pydantic_core.core_schema import ErrorType
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

_BUILTIN_ERROR_TYPES = frozenset(get_args(ErrorType))


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


class RedactingSettings(BaseSettings):
    """``BaseSettings`` whose validation errors never carry a raw input (CWE-532).

    ``hide_input_in_errors`` only affects ``str()``/``repr()``: ``errors()`` and ``json()`` still
    snapshot the raw input, which for a settings model is cleartext credentials (a master key, an
    API key, a password in a URL) and is exactly what error trackers serialize. A model-level error
    (``loc == ()``) snapshots the whole input dict. A failed construction is re-raised as a copy
    with every input redacted and each error's type, loc, msg and ctx kept. The copy is still a
    ValidationError (a ValueError), so fail-loud propagation paths are unchanged.
    """

    def __init__(self, **kwargs: Any) -> None:
        sanitized_error: ValidationError | None = None
        try:
            super().__init__(**kwargs)
        except ValidationError as e:
            sanitized_error = _redacted_copy(e)
        # Raised OUTSIDE the except block so __context__/__cause__ stay None —
        # `raise ... from None` only suppresses display; the original (with raw
        # inputs recoverable via .errors()) would still hang off __context__
        # for anything that walks exception chains.
        if sanitized_error is not None:
            raise sanitized_error


def _redacted_copy(error: ValidationError) -> ValidationError:
    """Rebuild ``error`` with every input replaced by ``"[REDACTED]"``.

    Must not raise: it runs inside the ``except`` that caught ``error``, so an exception here would
    chain the original, raw inputs and all. Kept out of ``__init__`` so the raw ``err`` dicts are
    gone from the frame that raises.
    """
    sanitized: list[InitErrorDetails] = []
    for err in error.errors(include_url=False):
        ctx = err.get("ctx")
        error_type: str | PydanticCustomError = err["type"]
        if error_type not in _BUILTIN_ERROR_TYPES:
            # Only built-in types rebuild from their name. Any other (pydantic's own Path fields raise
            # "path_type", a validator may raise PydanticCustomError) rebuilds from its rendered msg,
            # which pydantic produced, hence not a LiteralString.
            error_type = PydanticCustomError(err["type"], err["msg"], ctx)  # pyright: ignore[reportArgumentType]
        detail: InitErrorDetails = {"type": error_type, "loc": err["loc"], "input": "[REDACTED]"}
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
