"""Redis-specific backend configuration.

This module contains Redis-specific configuration separated from generic cache config.
Backend-specific settings (connection URLs, pool sizes, etc.) are encapsulated here
to maintain clean separation of concerns.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RedisBackendConfig(BaseSettings):
    """Redis-specific backend configuration.

    Configuration for Redis connection management, connection pooling,
    and Redis-specific performance tuning.

    Attributes:
        redis_url: Redis server connection URL
        connection_pool_size: Maximum connections in the Redis pool
        socket_keepalive: Enable TCP keepalive for connections
        disable_hiredis: Disable hiredis parser (use pure Python)

    Examples:
        Create with defaults:

        >>> config = RedisBackendConfig()
        >>> config.redis_url
        'redis://localhost:6379'
        >>> config.connection_pool_size
        10
        >>> config.socket_keepalive
        True

        Override via constructor:

        >>> custom = RedisBackendConfig(
        ...     redis_url="redis://myhost:6380",
        ...     connection_pool_size=25,
        ... )
        >>> custom.redis_url
        'redis://myhost:6380'
        >>> custom.connection_pool_size
        25
    """

    model_config = SettingsConfigDict(
        env_prefix="CACHEKIT_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="forbid",
        populate_by_name=True,
    )

    redis_url: str = Field(
        default="redis://localhost:6379",
        description="Redis connection URL",
    )
    connection_pool_size: int = Field(
        default=10,
        gt=0,
        description="Maximum connections in the Redis pool",
    )
    socket_keepalive: bool = Field(
        default=True,
        description="Enable TCP keepalive for connections",
    )
    disable_hiredis: bool = Field(
        default=False,
        description="Disable hiredis parser (use pure Python)",
    )

    @classmethod
    def from_env(cls) -> RedisBackendConfig:
        """Create Redis configuration from environment variables.

        Reads CACHEKIT_REDIS_URL, CACHEKIT_CONNECTION_POOL_SIZE, etc.

        Returns:
            RedisBackendConfig instance loaded from environment

        Examples:
            Set environment variables:

            .. code-block:: bash

                export CACHEKIT_REDIS_URL="redis://localhost:6379"
                export CACHEKIT_CONNECTION_POOL_SIZE=20

            .. code-block:: python

                config = RedisBackendConfig.from_env()
                print(config.redis_url)  # redis://localhost:6379
        """
        return cls()
