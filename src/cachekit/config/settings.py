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

import functools
import threading
from typing import Annotated, Any, Literal, Optional

from pydantic import (
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .validation import RedactingSettings, _redacting, refuse_current_key_in_previous_keys

# Keyring cap from the protocol spec (spec/encryption.md → "Key Rotation (Keyring)"):
# at most 3 decrypt-only previous keys. Exceeding the cap is a configuration error,
# rejected at load — never silently truncated. Mirrors cachekit-core's
# MAX_DECRYPT_ONLY_KEYS, which re-validates behind the FFI boundary.
MAX_PREVIOUS_MASTER_KEYS = 3

# Serializes CachekitConfig assignments: each validates the whole state it would leave, so two
# assignments that are each valid alone (a new master_key, and that key added to previous_master_keys)
# cannot land together unchecked. An assignment commits by swapping in its copy's whole state, so every
# other change (a private attribute, a delete) takes the lock too, or the swap would undo it. Reentrant:
# a subclass's validator or property setter may assign a field or a private attribute while it is held.
_ASSIGNMENT_LOCK = threading.RLock()


class CachekitConfig(RedactingSettings):
    """Backend-agnostic cache configuration.

    This configuration class provides validation for generic cache parameters
    including the value-size limit, L1 sizing, Arrow compression and encryption keys.

    Backend-specific configuration (connection URLs, pool sizes, etc.) is
    handled by backend-specific config classes.

    Attributes:
        max_value_size: Maximum cache value size in bytes
        l1_max_size_mb: Maximum L1 cache size per namespace in megabytes. With a backend, one value
            larger than an eighth of it is not kept in L1
        l1_cleanup_interval_seconds: Background cleanup interval for expired entries

    Examples:
        Create with defaults:

        >>> config = CachekitConfig()
        >>> config.l1_max_size_mb
        100
        >>> config.max_value_size
        104857600

        Constructor kwargs beat env vars and defaults (a standalone instance; the SDK reads get_settings()):

        >>> custom = CachekitConfig(l1_max_size_mb=256)
        >>> custom.l1_max_size_mb
        256

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
        # SECURITY (CWE-532): never echo raw inputs in str(ValidationError).
        # Without this, any validation failure on this model (an out-of-range size limit,
        # keyring misconfig, ...) embeds the full raw input — including
        # env-sourced master_key and previous_master_keys hex — in startup
        # logs. errors()/json() ignore this flag; RedactingSettings sanitizes
        # those surfaces.
        hide_input_in_errors=True,
        # Assignment after load runs every field and model validator; __setattr__ makes a refusal write nothing.
        validate_assignment=True,
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

    # Size limits
    max_value_size: int = Field(
        default=104857600,  # 100MB
        gt=0,
        description="Maximum cache value size in bytes",
    )

    # L1 (In-Memory) Cache Configuration
    l1_max_size_mb: int = Field(
        default=100,
        gt=0,
        description="Maximum L1 cache size per namespace in megabytes (prevents OOM); with a backend, one value larger than an eighth of it is not kept in L1",
    )
    l1_cleanup_interval_seconds: int = Field(
        default=30,
        gt=0,
        description="Background cleanup interval for expired L1 entries",
    )
    invalidation_listener_enabled: bool = Field(
        default=False,
        description=(
            "Run the cross-process L1 invalidation listener in this process (env: "
            "CACHEKIT_INVALIDATION_LISTENER_ENABLED): one background thread and one dedicated Redis "
            "connection that evict this process's L1 copies of keys other processes invalidate. "
            "Needs the tenant-scoped Redis backend (env-resolved Redis or RedisBackendProvider). "
            "Publishing needs no setting: every process on that backend announces its invalidations."
        ),
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
        description='Explicit single-tenant encryption tenant_id (validated UUID); unset → the protocol literal "default" (env: CACHEKIT_DEPLOYMENT_UUID)',
    )
    dev_mode: bool = Field(
        default=False,
        description="Enable development mode features",
    )

    # Encryption configuration
    master_key: Optional[SecretStr] = Field(
        default=None,
        description="Master encryption key (hex-encoded; use exactly 32 bytes, 64 hex characters)",
    )
    # A tuple, so the keyring cannot be edited in place, where no validator runs: a change is an assignment.
    previous_master_keys: Annotated[tuple[SecretStr, ...], NoDecode] = Field(
        default_factory=tuple,
        description=(
            "Decrypt-only previous master keys for key rotation (env: "
            "CACHEKIT_PREVIOUS_MASTER_KEYS, comma-separated hex). Entries written "
            "under a listed key stay readable through the rotation window; writes "
            "always use master_key. At most 3 keys — more is rejected at load, "
            "never truncated. Per-key validation is identical to master_key "
            "(hex-encoded, at least 32 bytes; use exactly 32). Spec: protocol spec/encryption.md "
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

        for position, key in enumerate(self.previous_master_keys):
            try:
                key_length = len(bytes.fromhex(key.get_secret_value()))
            except ValueError as e:
                raise ValueError(f"previous_master_keys[{position}] is not valid hex: {e}") from e
            if key_length < 32:
                raise ValueError(
                    f"previous_master_keys[{position}] must be at least 32 bytes (256 bits) decoded, got {key_length}"
                )

        # Shared with CacheSerializationHandler, which applies it to a master_key= this validator never sees.
        refuse_current_key_in_previous_keys(self.master_key, self.previous_master_keys)

        return self

    def __setattr__(self, name: str, value: Any) -> None:
        """Validate an assignment on a copy first, and write it only if the copy passes.

        validate_assignment alone writes the new value before the model validator runs, so a refused keyring
        assignment would stay on the instance, readable by other threads until the error surfaces. The value
        is dropped in a finally: the raised error's traceback holds this frame (CWE-532).
        """
        # A private attribute: nothing to validate, so no copy. It still takes the lock, so it cannot land between
        # another thread's model_copy() and swap and be lost. Any other name goes through the guard, so a mistyped
        # field's value is refused inside _redacting too.
        if name in type(self).__private_attributes__:
            with _ASSIGNMENT_LOCK:
                super().__setattr__(name, value)
            return
        candidate = None
        try:
            with _ASSIGNMENT_LOCK:
                # A subclass property: run its setter once, on this instance. Each field it assigns comes back
                # through this guard; replaying it on a copy would apply a non-idempotent setter twice. Redacted like
                # the copy path, so a refused key leaves no pydantic frame holding it.
                if isinstance(getattr(type(self), name, None), property):
                    _redacting(functools.partial(BaseSettings.__setattr__, self, name, value), type(self).__name__)
                    return
                candidate = self.model_copy()
                # BaseSettings.__setattr__, not this override: pydantic's validated assignment, on the copy.
                _redacting(functools.partial(BaseSettings.__setattr__, candidate, name, value), type(self).__name__)
                # Commit the state the copy validated, with no second validation: `value` may be a spent generator,
                # and a non-idempotent validator would change it again. One swap of __dict__, as pydantic commits, plus
                # the private state a subclass's model validator may have derived on the copy.
                object.__setattr__(self, "__pydantic_private__", candidate.__pydantic_private__)
                object.__setattr__(self, "__pydantic_fields_set__", candidate.__pydantic_fields_set__)
                object.__setattr__(self, "__dict__", candidate.__dict__)
        finally:
            del value, candidate  # the copy holds the refused value

    def __delattr__(self, name: str) -> None:
        """Delete under the assignment lock, so an assignment's swap cannot undo the delete."""
        with _ASSIGNMENT_LOCK:
            super().__delattr__(name)

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

                export CACHEKIT_L1_MAX_SIZE_MB=200

            .. code-block:: python

                config = CachekitConfig.from_env()
                print(config.l1_max_size_mb)  # 200
        """
        # pydantic-settings handles all environment variable reading automatically
        return cls()
