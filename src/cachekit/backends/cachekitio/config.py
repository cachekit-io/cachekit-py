"""cachekit.io backend configuration via pydantic-settings.

Includes SSRF protection to prevent requests to private/internal networks.
"""

from __future__ import annotations

import re
import socket
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from urllib.parse import ParseResult, urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import SettingsConfigDict
from urllib3.util import parse_url

from cachekit.backends.base_config import BaseBackendConfig, inherit_config

# Allowed hostnames for API URL (SSRF protection)
ALLOWED_HOSTS: tuple[str, ...] = ("api.cachekit.io", "api.staging.cachekit.io")

# RFC 6750 §2.1 b64token: every character a bearer token may carry. Issued keys (ASCII letters, digits and
# underscores) sit inside it. Explicit ASCII ranges, not \w, so no non-ASCII letter matches.
_BEARER_TOKEN = re.compile(r"[A-Za-z0-9\-._~+/]+=*")


# Loopback, private, link-local (cloud metadata), "this network" and unspecified addresses. An IPv4-mapped IPv6
# address is checked as the IPv4 address it maps to.
_PRIVATE_NETWORKS = tuple(
    ip_network(net)
    for net in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "0.0.0.0/8",
        "::1/128",
        "::/128",
        "fe80::/10",
        "fc00::/7",
    )
)


def is_private_ip(hostname: str) -> bool:
    """Check if hostname is a private/internal IP address (SSRF protection).

    The address is read the way the platform resolver reads it, so every IPv4 spelling it accepts is checked:
    dotted, abbreviated (``127.1``), decimal (``2130706433``), hex (``0x7f.1``) and octal (``0177.0.0.1``).
    ``localhost`` and its subdomains (RFC 6761) count as loopback.

    Note:
        Does NOT perform DNS resolution to avoid network dependencies during config loading: a hostname that
        resolves to a private address passes. When allow_custom_host=True, ensure URLs come from trusted
        configuration only.

    Args:
        hostname: Hostname or IP address to check

    Returns:
        True if the hostname is a private/internal address
    """
    # A trailing dot is stripped: over-blocking a name the resolver would not read as an address is harmless.
    host = hostname.strip("[]").lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        # Anything after a % is ignored: a zone id (fe80::1%eth0) does not change which network an address is in.
        addr: IPv4Address | IPv6Address = ip_address(host.split("%", 1)[0])
    except ValueError:
        try:
            addr = IPv4Address(socket.inet_aton(host))
        except (OSError, ValueError):  # Not an address; ValueError for an embedded NUL.
            return False
    if isinstance(addr, IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in _PRIVATE_NETWORKS)


def _parse_api_url(url: str) -> tuple[str, ParseResult]:
    """The host the HTTP client connects to for ``url`` (lowercase, IPv6 without brackets), and the stdlib parse.

    The client builds its connection pool with urllib3, so the host is read with urllib3's parser, the one every
    check must see. A URL that the standard library reads with a different host, or that holds a backslash, is
    rejected: its host depends on which parser reads it. A non-ASCII host is rejected too (urllib3 reads it in its
    IDNA ``xn--`` form, the standard library does not), so a custom host must be given in that ASCII form.

    Raises:
        ValueError: If the URL cannot be parsed, holds a backslash, or the two parsers disagree on its host.
            The message never quotes the URL, whose userinfo may carry credentials (CWE-532).
    """
    if "\\" in url:
        raise ValueError("Invalid API URL: must not contain a backslash")
    # Each parser's own error can quote the whole netloc, so it is kept off the chain: raised outside the except.
    try:
        host = (parse_url(url).host or "").lower().strip("[]")
        parsed = urlparse(url)
    except ValueError:  # urllib3's LocationParseError is a ValueError
        host = parsed = None
    if host is None or parsed is None or host != (parsed.hostname or ""):
        raise ValueError("Invalid API URL: could not be parsed")
    return host, parsed


class CachekitIOBackendConfig(BaseBackendConfig):
    """Configuration for cachekit.io backend.

    Loads from environment variables with CACHEKIT_ prefix.

    Security:
        SSRF protection is enabled by default. The api_url is validated to:
        - Require HTTPS protocol
        - Reject credentials in the URL (user:password@); the API key is the only credential
        - Reject private/internal IP addresses (10.x, 172.16-31.x, 192.168.x, etc.)
        - Only allow the two API hostnames, exactly (api.cachekit.io, api.staging.cachekit.io)

        To use a custom host (e.g., for testing), set CACHEKIT_ALLOW_CUSTOM_HOST=true
    """

    model_config = SettingsConfigDict(
        **inherit_config(BaseBackendConfig),
        env_prefix="CACHEKIT_",
    )

    api_url: str = Field(
        default="https://api.cachekit.io",
        description="cachekit API endpoint URL",
    )
    api_key: SecretStr = Field(
        ...,  # Required field
        min_length=1,  # fullmatch rejects "" too, but only too_short makes the backend add its missing-key hint
        description="API key (ck_live_...) - required for authentication",
    )
    timeout: float = Field(
        default=5.0,
        gt=0,
        description="Request timeout in seconds",
    )
    # 32 = the most threads the default executor runs (min(32, cpu + 4)), which every async L2 op uses. One request
    # per HTTP/1.1 connection; more concurrent requests than this open short-lived extra connections.
    connection_pool_size: int = Field(
        default=32,
        gt=0,
        description="HTTP connections kept in the shared pool",
    )
    allow_custom_host: bool = Field(
        default=False,
        description="Allow custom API hostnames (disables SSRF hostname allowlist)",
    )

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: SecretStr) -> SecretStr:
        # A key outside the RFC 6750 b64token charset never authenticates, and it fails later with the key
        # in the error: a trailing newline on the first request (an h11 error echoing "Bearer <key>"), a
        # UTF-8 BOM or non-ASCII letter at client build (a UnicodeEncodeError whose repr holds it).
        # Reject rather than strip: never rewrite a credential. Never echo it: the message is static.
        if not _BEARER_TOKEN.fullmatch(v.get_secret_value()):
            raise ValueError(
                "is not a valid bearer token (RFC 6750 allows only A-Z a-z 0-9 - . _ ~ + / and any number of trailing =); "
                "the usual cause is a trailing newline or other whitespace, a byte-order mark from a file saved "
                "on Windows, a control character, or a non-ASCII letter"
            )
        return v

    @field_validator("api_url")
    @classmethod
    def validate_api_url(cls, v: str) -> str:
        """Validate API URL with SSRF protection.

        Raises:
            ValueError: If URL is invalid, carries credentials, uses non-HTTPS, names no host, or targets private IP
        """
        # Never echo the URL: its userinfo may carry credentials (CWE-532).
        hostname, parsed = _parse_api_url(v)

        # Userinfo never authenticates here (the client sends only the Bearer key), and a password in
        # the URL reaches any log or error that prints it (CWE-532).
        if "@" in parsed.netloc:
            raise ValueError("API URL must not contain credentials (user:password@)")

        # Enforce HTTPS protocol
        if parsed.scheme != "https":
            # No scheme echo: in "user:pw@host" urlparse reads the username as the scheme.
            raise ValueError("API URL must use HTTPS protocol")

        if not hostname:
            raise ValueError("API URL must name a host")

        # Reject private/internal IP addresses
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

        hostname = _parse_api_url(self.api_url)[0]
        if hostname not in ALLOWED_HOSTS:
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
