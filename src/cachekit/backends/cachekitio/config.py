"""cachekit.io backend configuration via pydantic-settings.

Includes SSRF protection to prevent requests to private/internal networks.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import SettingsConfigDict

from cachekit.backends.base_config import BaseBackendConfig, inherit_config

# Allowed hostnames for API URL (SSRF protection)
ALLOWED_HOSTS: tuple[str, ...] = ("api.cachekit.io", "api.staging.cachekit.io")


def is_private_ip(hostname: str) -> bool:
    """Check if hostname is a private/internal IP address (SSRF protection).

    Uses string pattern matching for standard IP notation. This is defense-in-depth;
    the hostname allowlist is the primary security control.

    Note:
        Does NOT perform DNS resolution to avoid network dependencies during config
        loading. Alternative IP encodings (hex, decimal, abbreviated) are not blocked.
        When allow_custom_host=True, ensure URLs come from trusted configuration only.

    Args:
        hostname: Hostname or IP address to check

    Returns:
        True if the hostname matches a private/internal IP pattern
    """
    # Normalize: remove brackets from IPv6
    normalized = hostname.strip("[]").lower()

    # Localhost variants
    if normalized in ("localhost", "127.0.0.1", "::1"):
        return True

    # IPv6 link-local (fe80::/10)
    if normalized.startswith("fe80:"):
        return True

    # IPv6 unique local (fc00::/7 = fc00:: through fdff::)
    if normalized[:2] in ("fc", "fd"):
        return True

    # IPv4-mapped IPv6 (::ffff:x.x.x.x)
    if normalized.startswith("::ffff:"):
        ipv4_part = normalized[7:]  # Remove "::ffff:" prefix
        return is_private_ip(ipv4_part)

    # Check for IPv4 private ranges
    ipv4_match = re.match(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$", normalized)
    if ipv4_match:
        a, b = int(ipv4_match.group(1)), int(ipv4_match.group(2))

        # 127.0.0.0/8 - Loopback
        if a == 127:
            return True

        # 10.0.0.0/8 - Private
        if a == 10:
            return True

        # 172.16.0.0/12 - Private
        if a == 172 and 16 <= b <= 31:
            return True

        # 192.168.0.0/16 - Private
        if a == 192 and b == 168:
            return True

        # 169.254.0.0/16 - Link-local (includes cloud metadata endpoints)
        if a == 169 and b == 254:
            return True

        # 0.0.0.0/8 - Current network
        if a == 0:
            return True

    return False


class CachekitIOBackendConfig(BaseBackendConfig):
    """Configuration for cachekit.io backend.

    Loads from environment variables with CACHEKIT_ prefix.

    Security:
        SSRF protection is enabled by default. The api_url is validated to:
        - Require HTTPS protocol
        - Reject private/internal IP addresses (10.x, 172.16-31.x, 192.168.x, etc.)
        - Only allow known hostnames (api.cachekit.io, api.staging.cachekit.io)

        To use a custom host (e.g., for testing), set CACHEKIT_ALLOW_CUSTOM_HOST=true
    """

    model_config = SettingsConfigDict(
        **inherit_config(BaseBackendConfig),
        env_prefix="CACHEKIT_",
        # This class is public: built directly (or via from_env()), a failed validation would
        # print the raw api_key in str(ValidationError) — tracebacks, logs (CWE-532).
        hide_input_in_errors=True,
    )

    api_url: str = Field(
        default="https://api.cachekit.io",
        description="cachekit API endpoint URL",
    )
    api_key: SecretStr = Field(
        ...,  # Required field
        min_length=1,  # an empty key would go out as "Bearer " and fail on the first call, not here
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
    allow_custom_host: bool = Field(
        default=False,
        description="Allow custom API hostnames (disables SSRF hostname allowlist)",
    )

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: SecretStr) -> SecretStr:
        # A bearer token never contains whitespace (RFC 6750); a key read from a secrets file
        # usually carries a trailing newline. Accepted, it fails on the first request with an h11
        # error that echoes "Bearer <key>". Reject rather than strip: never rewrite a credential.
        if any(c.isspace() for c in v.get_secret_value()):
            raise ValueError("contains whitespace (a trailing newline from a secrets file is the usual cause)")
        return v

    @field_validator("api_url")
    @classmethod
    def validate_api_url(cls, v: str) -> str:
        """Validate API URL with SSRF protection.

        Raises:
            ValueError: If URL is invalid, uses non-HTTPS, or targets private IP
        """
        try:
            parsed = urlparse(v)
        except Exception as e:
            raise ValueError(f"Invalid API URL: {v}") from e

        # Enforce HTTPS protocol
        if parsed.scheme != "https":
            raise ValueError(f"API URL must use HTTPS protocol, got: {parsed.scheme}://")

        # Reject private/internal IP addresses
        hostname = parsed.hostname or ""
        if is_private_ip(hostname):
            raise ValueError(f"API URL cannot use private/internal IP address: {hostname}")

        return v

    def validate_hostname_allowlist(self) -> None:
        """Validate hostname against allowlist (called after model init).

        This is separate from field_validator because it needs access to allow_custom_host.

        Raises:
            ValueError: If hostname not in allowlist and allow_custom_host is False
        """
        if self.allow_custom_host:
            return

        parsed = urlparse(self.api_url)
        hostname = parsed.hostname or ""

        is_allowed = any(hostname == allowed or hostname.endswith(f".{allowed}") for allowed in ALLOWED_HOSTS)

        if not is_allowed:
            raise ValueError(
                f"API URL hostname '{hostname}' not in allowlist. "
                f"Allowed: {', '.join(ALLOWED_HOSTS)}. "
                "Set CACHEKIT_ALLOW_CUSTOM_HOST=true to override."
            )

    def model_post_init(self, __context: object) -> None:
        """Validate hostname allowlist after model initialization."""
        self.validate_hostname_allowlist()

    @classmethod
    def from_env(cls) -> CachekitIOBackendConfig:
        """Create configuration from environment variables.

        Returns:
            CachekitIOBackendConfig instance loaded from environment
        """
        return cls()  # type: ignore[call-arg]
