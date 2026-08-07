"""Cachekit Settings - Backend-agnostic cache configuration.

This module contains the main configuration class for cachekit with enterprise-grade
validation and environment variable support.

Key features:
- Backend-agnostic cache configuration (no Redis-specific fields)
- Environment variable support for Kubernetes deployments
- Comprehensive validation with clear error messages
- Production-ready defaults based on real-world usage
- Type-safe configuration with full mypy compatibility

Note:
    Backend-specific configuration (Redis, DynamoDB, etc.) is handled by
    backend-specific config classes in backends/{backend}/config.py
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from pydantic import (
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Keyring cap from the protocol spec (spec/encryption.md → "Key Rotation (Keyring)"):
# at most 3 decrypt-only previous keys. Exceeding the cap is a configuration error,
# rejected at load — never silently truncated. Mirrors cachekit-core's
# MAX_DECRYPT_ONLY_KEYS, which re-validates behind the FFI boundary.
MAX_PREVIOUS_MASTER_KEYS = 3


class CachekitConfig(BaseSettings):
    """Backend-agnostic cache configuration.

    This configuration class provides validation for generic cache parameters
    including TTL limits, size limits, and monitoring.

    Backend-specific configuration (connection URLs, pool sizes, etc.) is
    handled by backend-specific config classes.

    Attributes:
        retry_on_timeout: Whether to retry operations on timeout
        max_retries: Maximum number of retry attempts
        retry_delay_ms: Delay between retries in milliseconds
        early_refresh_ratio: TTL ratio for early cache refresh
        enable_corruption_detection: Whether to enable data integrity checks
        enable_prometheus_metrics: Whether to enable Prometheus metrics collection
        default_ttl: Default time-to-live for cache entries in seconds
        ttl_min: Minimum allowed TTL in seconds
        ttl_max: Maximum allowed TTL in seconds
        max_key_size: Maximum cache key size in bytes
        max_value_size: Maximum cache value size in bytes
        l1_enabled: Enable L1 in-memory cache for performance
        l1_max_size_mb: Maximum L1 cache size per namespace in megabytes
        l1_cleanup_interval_seconds: Background cleanup interval for expired entries
        backend_provider_class: Backend provider class path (for testing)

    Examples:
        Create with defaults:

        >>> config = CachekitConfig()
        >>> config.default_ttl
        3600
        >>> config.l1_max_size_mb
        100
        >>> config.max_value_size
        104857600

        Override via constructor:

        >>> custom = CachekitConfig(default_ttl=7200, max_retries=5)
        >>> custom.default_ttl
        7200
        >>> custom.max_retries
        5

        TTL validation (default_ttl must be within ttl_min/ttl_max bounds):

        >>> CachekitConfig(default_ttl=30, ttl_min=60)  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        pydantic_core._pydantic_core.ValidationError: ... default_ttl (30) cannot be less than ttl_min (60)...

        Master key is masked in repr for security:

        >>> from pydantic import SecretStr
        >>> secure = CachekitConfig(master_key=SecretStr("deadbeef" * 8))
        >>> "REDACTED" in repr(secure)
        True
        >>> "deadbeef" not in repr(secure)
        True

        Key rotation: decrypt-only previous master keys (comma-separated hex via
        env CACHEKIT_PREVIOUS_MASTER_KEYS) keep entries written under a retired
        key readable — and are masked in repr like master_key:

        >>> rotated = CachekitConfig(
        ...     master_key=SecretStr("bb" * 32),
        ...     previous_master_keys=[SecretStr("aa" * 32)],
        ... )
        >>> len(rotated.previous_master_keys)
        1
        >>> "aa" * 32 not in repr(rotated)
        True

        More than 3 previous keys is rejected at load — never truncated:

        >>> CachekitConfig(
        ...     previous_master_keys=[SecretStr(f"{i:02x}" * 32) for i in range(1, 5)],
        ... )  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        pydantic_core._pydantic_core.ValidationError: ... at most 3 decrypt-only keys ...

        The current master_key re-appearing in previous_master_keys is rejected
        (forward-only rotation — re-promotion risks AES-GCM nonce reuse):

        >>> CachekitConfig(
        ...     master_key=SecretStr("aa" * 32),
        ...     previous_master_keys=[SecretStr("aa" * 32)],
        ... )  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        pydantic_core._pydantic_core.ValidationError: ... must not appear in previous_master_keys ...
    """

    model_config = SettingsConfigDict(
        env_prefix="CACHEKIT_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="forbid",
        populate_by_name=True,  # Allow using field names in addition to validation aliases
    )

    # Generic cache configuration (backend-agnostic)
    arrow_compression: Literal["zstd", "lz4", "none"] = Field(
        default="zstd",
        description=(
            "Arrow IPC compression codec for DataFrame caching (ArrowSerializer, compression='auto'). "
            "'zstd'/'lz4' shrink the stored payload but must be decompressed into the heap on read. "
            "'none' stores uncompressed Arrow IPC, which lets the File backend serve plaintext "
            "DataFrame reads via a zero-copy mmap (low steady-state read RSS; peak transiently "
            "higher) at the cost of a larger payload. Writes are codec-independent: plaintext "
            "Arrow streams to the File backend without materializing the payload (encrypted "
            "values stay buffered). Env: CACHEKIT_ARROW_COMPRESSION."
        ),
    )
    retry_on_timeout: bool = Field(
        default=True,
        description="Whether to retry operations on timeout",
    )
    max_retries: int = Field(
        default=3,
        ge=0,
        description="Maximum number of retry attempts for cache operations",
    )
    retry_delay_ms: int = Field(
        default=100,
        gt=0,
        description="Delay between retries in milliseconds",
    )
    early_refresh_ratio: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description="Ratio of TTL at which to refresh cache entries",
    )
    enable_corruption_detection: bool = Field(
        default=True,
        description="Whether to enable data integrity checks using checksums",
    )
    enable_prometheus_metrics: bool = Field(
        default=True,
        description="Whether to enable Prometheus metrics collection",
    )

    # TTL configuration
    default_ttl: int = Field(
        default=3600,
        gt=0,
        description="Default time-to-live for cache entries in seconds",
    )
    ttl_min: int = Field(
        default=60,
        gt=0,
        description="Minimum allowed TTL in seconds",
    )
    ttl_max: int = Field(
        default=86400,  # 24 hours
        gt=0,
        description="Maximum allowed TTL in seconds",
    )

    # Size limits
    max_key_size: int = Field(
        default=1024,  # 1KB
        gt=0,
        description="Maximum cache key size in bytes",
    )
    max_value_size: int = Field(
        default=104857600,  # 100MB
        gt=0,
        description="Maximum cache value size in bytes",
    )

    # L1 (In-Memory) Cache Configuration
    l1_enabled: bool = Field(
        default=True,
        description="Enable L1 in-memory cache for performance (eliminates network latency)",
    )
    l1_max_size_mb: int = Field(
        default=100,
        gt=0,
        description="Maximum L1 cache size per namespace in megabytes (prevents OOM)",
    )
    l1_cleanup_interval_seconds: int = Field(
        default=30,
        gt=0,
        description="Background cleanup interval for expired L1 entries",
    )

    # Logging configuration
    log_sampling_rate: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="Log sampling rate (0.0 to 1.0, default 10%)",
    )
    log_buffer_size: int = Field(
        default=10000,
        gt=0,
        description="Ring buffer size for async logging",
    )
    log_batch_size: int = Field(
        default=100,
        gt=0,
        description="Batch size for async log writes",
    )
    log_flush_interval: float = Field(
        default=1.0,
        gt=0,
        description="Flush interval for async logging in seconds",
    )

    # Deployment and feature flags
    deployment_uuid: Optional[str] = Field(
        default=None,
        description="Deployment UUID for tracking (env: CACHEKIT_DEPLOYMENT_UUID)",
    )
    dev_mode: bool = Field(
        default=False,
        description="Enable development mode features",
    )

    # Encryption configuration
    master_key: Optional[SecretStr] = Field(
        default=None,
        description="Master encryption key (hex-encoded, minimum 32 bytes for AES-256)",
    )
    previous_master_keys: Annotated[list[SecretStr], NoDecode] = Field(
        default_factory=list,
        description=(
            "Decrypt-only previous master keys for key rotation (env: "
            "CACHEKIT_PREVIOUS_MASTER_KEYS, comma-separated hex). Entries written "
            "under a listed key stay readable through the rotation window; writes "
            "always use master_key. At most 3 keys — more is rejected at load, "
            "never truncated. Per-key validation is identical to master_key "
            "(hex-encoded, minimum 32 bytes). Spec: protocol spec/encryption.md "
            "→ 'Key Rotation (Keyring)'."
        ),
    )
    encryption_fail_closed: bool = Field(
        default=False,
        description=(
            "Fail closed on decrypt authentication failures (env: CACHEKIT_ENCRYPTION_FAIL_CLOSED). "
            "When True, AES-GCM authentication failures and key-fingerprint mismatches raise "
            "DecryptionAuthenticationError to the caller instead of silently recomputing. "
            "Per-decorator EncryptionConfig(fail_closed=...) overrides this fleet-wide default."
        ),
    )

    # Backend provider configuration (for testing)
    backend_provider_class: Optional[str] = Field(
        default=None,
        description="Backend provider class path (e.g., 'cachekit.backends.redis.provider.RedisBackendProvider')",
    )

    @field_validator("previous_master_keys", mode="before")
    @classmethod
    def _split_previous_master_keys(cls, value: Any) -> Any:
        """Parse the env representation: comma-separated hex, blanks ignored.

        NoDecode on the field disables pydantic-settings' default JSON parsing
        for complex types, so the raw env string arrives here intact.
        """
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def validate_previous_master_keys(self) -> CachekitConfig:
        """Keyring configuration validation at load (spec: 'Key Rotation (Keyring)').

        - At most MAX_PREVIOUS_MASTER_KEYS entries — rejected, never truncated.
        - Per-key validation identical to master_key: hex-encoded, ≥32 bytes decoded.
        - master_key must not re-appear in the decrypt-only list: a key that ever
          occupied the encrypting slot is never re-promoted (the detectable subset
          of the spec's forward-only invariant — re-promotion would resume a used,
          unknowable AES-GCM nonce budget). Compared as decoded bytes, so hex case
          differences cannot smuggle the current key past the check.

        Raises:
            ValueError: On any keyring configuration violation (pydantic wraps
                this in a ValidationError at load).
        """
        if len(self.previous_master_keys) > MAX_PREVIOUS_MASTER_KEYS:
            raise ValueError(
                f"previous_master_keys accepts at most {MAX_PREVIOUS_MASTER_KEYS} decrypt-only keys, "
                f"got {len(self.previous_master_keys)}. The keyring cap is never silently truncated; "
                f"drop retired keys explicitly (protocol spec/encryption.md → 'Key Rotation (Keyring)')."
            )

        previous_key_bytes: list[bytes] = []
        for position, key in enumerate(self.previous_master_keys):
            try:
                decoded = bytes.fromhex(key.get_secret_value())
            except ValueError as e:
                raise ValueError(f"previous_master_keys[{position}] is not valid hex: {e}") from e
            if len(decoded) < 32:
                raise ValueError(
                    f"previous_master_keys[{position}] must be at least 32 bytes (256 bits) decoded, got {len(decoded)}"
                )
            previous_key_bytes.append(decoded)

        if self.master_key is not None:
            try:
                master_key_bytes = bytes.fromhex(self.master_key.get_secret_value())
            except ValueError:
                # An invalid master_key is not this validator's concern — it fails
                # loudly at EncryptionWrapper setup, exactly as before this field
                # existed. Only the subset check is skipped.
                master_key_bytes = None
            if master_key_bytes is not None and master_key_bytes in previous_key_bytes:
                raise ValueError(
                    "master_key must not appear in previous_master_keys: this configuration is the "
                    "detectable signature of re-promoting a retired key to the current (encrypting) "
                    "slot, which resumes a used AES-GCM nonce budget and risks catastrophic nonce "
                    "reuse. Rotate forward to a fresh key instead (protocol decisions/key-rotation.md)."
                )

        return self

    @model_validator(mode="after")
    def validate_interdependent_fields(self) -> CachekitConfig:
        """Validate interdependent field relationships.

        Returns:
            The validated configuration instance

        Raises:
            ValueError: If field combinations are invalid
        """
        # Check retry configuration consistency
        if not self.retry_on_timeout and self.max_retries > 0:
            # This is potentially inconsistent but not necessarily wrong
            # We'll allow it but could log a warning in the future
            pass

        # Check TTL bounds
        if self.default_ttl < self.ttl_min:
            raise ValueError(f"default_ttl ({self.default_ttl}) cannot be less than ttl_min ({self.ttl_min})")

        if self.default_ttl > self.ttl_max:
            raise ValueError(f"default_ttl ({self.default_ttl}) cannot be greater than ttl_max ({self.ttl_max})")

        return self

    def __repr__(self) -> str:
        """Return string representation with sensitive information masked.

        Returns:
            String representation with master_key masked
        """
        attrs = []
        for k, v in self.model_dump(mode="python").items():
            # Handle SecretStr fields - check actual attribute
            if k == "master_key":
                actual_value = getattr(self, k)
                if actual_value is None:
                    attrs.append(f"{k}=None")
                else:
                    attrs.append(f"{k}='[REDACTED]'")
                continue
            if k == "previous_master_keys":
                attrs.append(f"{k}=[{len(self.previous_master_keys)} key(s) REDACTED]")
                continue
            attrs.append(f"{k}={v!r}")
        return f"{self.__class__.__name__}({', '.join(attrs)})"

    def __str__(self) -> str:
        """Return string representation with sensitive information masked.

        Returns:
            String representation with master_key masked
        """
        attrs = []
        for k, v in self.model_dump(mode="python").items():
            # Handle SecretStr fields - check actual attribute
            if k == "master_key":
                actual_value = getattr(self, k)
                if actual_value is None:
                    attrs.append(f"{k}=None")
                else:
                    attrs.append(f"{k}=[REDACTED]")
                continue
            if k == "previous_master_keys":
                attrs.append(f"{k}=[{len(self.previous_master_keys)} key(s) REDACTED]")
                continue
            attrs.append(f"{k}={v}")
        return " ".join(attrs)

    def get_safe_repr(self) -> dict[str, Any]:
        """Return configuration dict with sensitive information masked.

        Returns:
            Dictionary with masked sensitive values for safe logging
        """
        config_dict = self.model_dump()
        # Mask master_key if present
        if config_dict.get("master_key"):
            config_dict["master_key"] = "[REDACTED]"
        if config_dict.get("previous_master_keys"):
            config_dict["previous_master_keys"] = f"[{len(self.previous_master_keys)} key(s) REDACTED]"
        return config_dict

    @classmethod
    def from_env(cls) -> CachekitConfig:
        """Create configuration instance from environment variables.

        Pydantic-settings automatically loads from environment variables
        with the CACHEKIT_ prefix.

        Returns:
            CachekitConfig instance loaded from environment variables

        Examples:
            Set environment variables before calling from_env():

            .. code-block:: bash

                export CACHEKIT_DEFAULT_TTL=7200
                export CACHEKIT_L1_MAX_SIZE_MB=200

            .. code-block:: python

                config = CachekitConfig.from_env()
                print(config.default_ttl)  # 7200
        """
        # pydantic-settings handles all environment variable reading automatically
        return cls()
