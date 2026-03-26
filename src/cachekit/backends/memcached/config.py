"""Memcached backend configuration.

Backend-specific settings for Memcached connections, separated from generic cache config
to maintain clean separation of concerns.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import SettingsConfigDict

from cachekit.backends.base_config import BaseBackendConfig, inherit_config

# Memcached maximum TTL: 30 days in seconds
MAX_MEMCACHED_TTL: int = 30 * 24 * 60 * 60  # 2,592,000


class MemcachedBackendConfig(BaseBackendConfig):
    """Memcached backend configuration.

    Configuration for Memcached cache storage with connection pooling and timeout controls.

    Attributes:
        servers: List of Memcached server addresses in "host:port" format.
        connect_timeout: Connection timeout in seconds.
        timeout: Operation timeout in seconds.
        max_pool_size: Maximum connections per server.
        retry_attempts: Number of retries on transient failures.
        key_prefix: Optional prefix prepended to all cache keys.

    Examples:
        Create with defaults:

        >>> config = MemcachedBackendConfig()
        >>> config.servers
        ['127.0.0.1:11211']
        >>> config.connect_timeout
        2.0

        Override via constructor:

        >>> custom = MemcachedBackendConfig(
        ...     servers=["mc1:11211", "mc2:11211"],
        ...     timeout=0.5,
        ...     max_pool_size=20,
        ... )
        >>> len(custom.servers)
        2
    """

    model_config = SettingsConfigDict(
        **inherit_config(BaseBackendConfig),
        env_prefix="CACHEKIT_MEMCACHED_",
    )

    servers: list[str] = Field(
        default=["127.0.0.1:11211"],
        description="Memcached server addresses (host:port)",
    )
    connect_timeout: float = Field(
        default=2.0,
        ge=0.1,
        le=30.0,
        description="Connection timeout in seconds",
    )
    timeout: float = Field(
        default=1.0,
        ge=0.1,
        le=30.0,
        description="Operation timeout in seconds",
    )
    max_pool_size: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum connections per server",
    )
    retry_attempts: int = Field(
        default=2,
        ge=0,
        le=10,
        description="Retries on transient failures",
    )
    key_prefix: str = Field(
        default="",
        description="Optional prefix for all cache keys",
    )

    @field_validator("servers", mode="after")
    @classmethod
    def validate_servers(cls, v: list[str]) -> list[str]:
        """Validate server list is non-empty and entries are well-formed.

        Args:
            v: List of server address strings.

        Returns:
            Validated server list.

        Raises:
            ValueError: If server list is empty or entries are malformed.
        """
        if not v:
            raise ValueError("At least one Memcached server must be specified")
        for server in v:
            if ":" not in server:
                raise ValueError(f"Server address must be in 'host:port' format, got: {server!r}")
            _, port_str = server.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                raise ValueError(f"Port must be numeric, got: {server!r}") from None
            if not (1 <= port <= 65535):
                raise ValueError(f"Port must be 1-65535, got {port} in {server!r}")
        return v

    @classmethod
    def from_env(cls) -> MemcachedBackendConfig:
        """Create configuration from environment variables.

        Reads CACHEKIT_MEMCACHED_SERVERS, CACHEKIT_MEMCACHED_CONNECT_TIMEOUT, etc.

        Returns:
            MemcachedBackendConfig instance loaded from environment.
        """
        return cls()
