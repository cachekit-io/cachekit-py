"""File-based backend configuration.

This module contains file-based backend configuration separated from generic cache config.
Backend-specific settings (cache directory, size limits, permissions) are encapsulated here
to maintain clean separation of concerns.
"""

from __future__ import annotations

import tempfile
import warnings
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class FileBackendConfig(BaseSettings):
    """File-based backend configuration.

    Configuration for file-based cache storage with size limits, entry count limits,
    and file permission controls.

    Attributes:
        cache_dir: Directory for cache files. Defaults to system temp directory.
        max_size_mb: Maximum cache size in MB (1 - 1,000,000).
        max_value_mb: Maximum single value size in MB (1 - 50% of max_size_mb).
        max_entry_count: Maximum number of cache entries (100 - 1,000,000).
        lock_timeout_seconds: Lock acquisition timeout in seconds (0.5 - 30.0).
        permissions: File permissions as octal (default 0o600 - owner-only).
        dir_permissions: Directory permissions as octal (default 0o700 - owner-only).

    Examples:
        Create with defaults:

        >>> config = FileBackendConfig()
        >>> config.max_size_mb
        1024
        >>> config.max_value_mb
        100
        >>> config.max_entry_count
        10000

        Override via constructor:

        >>> from pathlib import Path
        >>> custom = FileBackendConfig(
        ...     cache_dir=Path("/var/cache/myapp"),
        ...     max_size_mb=2048,
        ...     max_value_mb=200,
        ... )
        >>> custom.max_size_mb
        2048
        >>> custom.max_value_mb
        200
    """

    model_config = SettingsConfigDict(
        env_prefix="CACHEKIT_FILE_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="forbid",
        populate_by_name=True,
    )

    cache_dir: Path = Field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "cachekit",
        description="Directory for cache files",
    )
    max_size_mb: int = Field(
        default=1024,
        ge=1,
        le=1_000_000,
        description="Maximum cache size in MB",
    )
    max_value_mb: int = Field(
        default=100,
        ge=1,
        description="Maximum single value size in MB",
    )
    max_entry_count: int = Field(
        default=10_000,
        ge=100,
        le=1_000_000,
        description="Maximum number of cache entries",
    )
    lock_timeout_seconds: float = Field(
        default=5.0,
        ge=0.5,
        le=30.0,
        description="Lock acquisition timeout in seconds",
    )
    permissions: int = Field(
        default=0o600,
        description="File permissions as octal",
    )
    dir_permissions: int = Field(
        default=0o700,
        description="Directory permissions as octal",
    )

    @field_validator("max_value_mb", mode="after")
    @classmethod
    def validate_max_value_mb(cls, v: int, info) -> int:
        """Validate max_value_mb is within acceptable range.

        Args:
            v: The value to validate
            info: Validation context with data about other fields

        Returns:
            The validated value

        Raises:
            ValueError: If max_value_mb exceeds max_size_mb * 0.5
        """
        if "max_size_mb" in info.data:
            max_size_mb = info.data["max_size_mb"]
            max_allowed = max_size_mb * 0.5

            if v > max_allowed:
                raise ValueError(
                    f"max_value_mb ({v}) must be <= 50% of max_size_mb ({max_size_mb}). Max allowed: {max_allowed:.0f}"
                )

        return v

    @field_validator("permissions", mode="after")
    @classmethod
    def validate_permissions(cls, v: int) -> int:
        """Validate file permissions and warn if too permissive.

        Args:
            v: The permission value to validate

        Returns:
            The validated value
        """
        if v > 0o600:
            warnings.warn(
                f"File permissions {oct(v)} are more permissive than recommended (0o600). This may pose a security risk.",
                UserWarning,
                stacklevel=2,
            )

        return v

    @field_validator("dir_permissions", mode="after")
    @classmethod
    def validate_dir_permissions(cls, v: int) -> int:
        """Validate directory permissions and warn if too permissive.

        Args:
            v: The permission value to validate

        Returns:
            The validated value
        """
        if v > 0o700:
            warnings.warn(
                f"Directory permissions {oct(v)} are more permissive than recommended (0o700). This may pose a security risk.",
                UserWarning,
                stacklevel=2,
            )

        return v

    @classmethod
    def from_env(cls) -> FileBackendConfig:
        """Create file backend configuration from environment variables.

        Reads CACHEKIT_FILE_CACHE_DIR, CACHEKIT_FILE_MAX_SIZE_MB, etc.

        Returns:
            FileBackendConfig instance loaded from environment

        Examples:
            Set environment variables:

            .. code-block:: bash

                export CACHEKIT_FILE_CACHE_DIR="/tmp/mycache"
                export CACHEKIT_FILE_MAX_SIZE_MB=2048
                export CACHEKIT_FILE_MAX_VALUE_MB=200

            .. code-block:: python

                config = FileBackendConfig.from_env()
                print(config.cache_dir)  # /tmp/mycache
                print(config.max_size_mb)  # 2048
        """
        return cls()
