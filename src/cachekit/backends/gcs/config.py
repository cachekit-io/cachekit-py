"""Google Cloud Storage backend configuration.

Holds no credential fields: the backend authenticates with Application Default Credentials.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import SettingsConfigDict

from cachekit.backends.base_config import BaseBackendConfig, inherit_config

# GCS caps an object name at 1024 bytes of UTF-8; an entry's name is the prefix plus 32 hex characters.
MAX_PREFIX_BYTES: int = 1024 - 32


class GCSBackendConfig(BaseBackendConfig):
    """Google Cloud Storage backend configuration.

    Attributes:
        bucket: Name of the bucket that holds the entries. Required.
        prefix: Object-name prefix, used verbatim (``"cache/"`` gives ``cache/<name>``). A storage
            location only: it does not change the cache key.
        project: Google Cloud project passed to the storage client. Optional: the object requests
            this backend sends need none.
        timeout: Seconds to wait for the server to connect, and between bytes of a response.

    Examples:
        Create with a bucket:

        >>> config = GCSBackendConfig(bucket="my-cache-bucket")
        >>> config.prefix
        ''
        >>> config.timeout
        10.0

        Keep entries under a prefix:

        >>> GCSBackendConfig(bucket="my-cache-bucket", prefix="cachekit/").prefix
        'cachekit/'
    """

    model_config = SettingsConfigDict(
        **inherit_config(BaseBackendConfig),
        env_prefix="CACHEKIT_GCS_",
    )

    bucket: str = Field(
        ...,  # Required field
        min_length=1,
        description="Bucket that holds the cache entries (a name, not a gs:// URL)",
    )
    prefix: str = Field(
        default="",
        description="Object-name prefix, used verbatim; a storage location only",
    )
    project: str | None = Field(
        default=None,
        description="Google Cloud project passed to the storage client; object requests need none",
    )
    timeout: float = Field(
        default=10.0,
        ge=0.1,
        le=600.0,
        description="Seconds to wait for a connection, and between bytes of a response",
    )

    @field_validator("bucket", mode="after")
    @classmethod
    def validate_bucket(cls, v: str) -> str:
        """Reject a URL or a path: the client would address a bucket of that literal name."""
        if "/" in v:
            raise ValueError("bucket must be a bucket name, without gs:// or a path; put a path in prefix")
        return v

    @field_validator("prefix", mode="after")
    @classmethod
    def validate_prefix(cls, v: str) -> str:
        """Reject a prefix GCS would refuse in an object name."""
        if "\r" in v or "\n" in v:
            raise ValueError("prefix must not contain a carriage return or line feed")
        if len(v.encode("utf-8")) > MAX_PREFIX_BYTES:
            raise ValueError(f"prefix must be at most {MAX_PREFIX_BYTES} bytes of UTF-8")
        return v

    @classmethod
    def from_env(cls) -> GCSBackendConfig:
        """Create configuration from environment variables.

        Reads CACHEKIT_GCS_BUCKET, CACHEKIT_GCS_PREFIX, CACHEKIT_GCS_PROJECT and CACHEKIT_GCS_TIMEOUT.

        Returns:
            GCSBackendConfig instance loaded from environment.
        """
        return cls()  # type: ignore[call-arg]
