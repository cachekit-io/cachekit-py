"""cachekit.io backend configuration via pydantic-settings."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class CachekitIOBackendConfig(BaseSettings):
    """Configuration for cachekit.io backend.

    Loads from environment variables with CACHEKIT_ prefix.
    """

    model_config = SettingsConfigDict(
        env_prefix="CACHEKIT_",
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    api_url: str = Field(
        default="https://api.cachekit.io",
        description="cachekit API endpoint URL",
    )
    api_key: SecretStr = Field(
        ...,
        description="API key (ck_live_...) - required for authentication",
    )
    timeout: float = Field(
        default=5.0,
        gt=0,
        description="Request timeout in seconds",
    )
    max_retries: int = Field(
        default=3,
        ge=0,
        description="Maximum retry attempts for transient errors",
    )
    connection_pool_size: int = Field(
        default=10,
        gt=0,
        description="Maximum HTTP connections in pool",
    )

    @classmethod
    def from_env(cls) -> CachekitIOBackendConfig:
        """Create configuration from environment variables.

        Returns:
            CachekitIOBackendConfig instance loaded from environment
        """
        return cls()  # type: ignore[call-arg]
