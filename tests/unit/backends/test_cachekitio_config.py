"""Tests for CachekitIO backend configuration with SSRF protection."""

import pytest
from pydantic import SecretStr, ValidationError

from cachekit.backends.cachekitio.config import (
    ALLOWED_HOSTS,
    CachekitIOBackendConfig,
    is_private_ip,
)

pytestmark = pytest.mark.unit


class TestIsPrivateIP:
    """Tests for is_private_ip function."""

    @pytest.mark.parametrize(
        "ip",
        [
            # Localhost variants
            "localhost",
            "127.0.0.1",
            "127.0.0.255",
            "127.255.255.255",
            "::1",
            # Private IPv4 ranges
            "10.0.0.1",
            "10.255.255.255",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.0.1",
            "192.168.255.255",
            # Link-local (cloud metadata)
            "169.254.169.254",
            "169.254.0.1",
            # Current network
            "0.0.0.0",  # noqa: S104 - test data, not a bind()
            "0.0.0.1",
            # IPv6 private
            "fe80::1",
            "fe80:0000:0000:0000:0000:0000:0000:0001",
            "fc00::1",
            "fd00::1",
            "fdff::1",
            # IPv4-mapped IPv6
            "::ffff:127.0.0.1",
            "::ffff:10.0.0.1",
            "::ffff:169.254.169.254",
            "::ffff:192.168.1.1",
            # Bracketed IPv6
            "[::1]",
            "[fe80::1]",
        ],
    )
    def test_private_ips_detected(self, ip: str) -> None:
        """Private/internal IPs should be detected."""
        assert is_private_ip(ip) is True, f"{ip} should be detected as private"

    @pytest.mark.parametrize(
        "ip",
        [
            # Public IPv4
            "8.8.8.8",
            "1.1.1.1",
            "93.184.216.34",
            "203.0.113.1",
            # Edge cases that are NOT private
            "172.15.255.255",  # Just below 172.16.0.0/12
            "172.32.0.0",  # Just above 172.16.0.0/12
            "192.167.255.255",  # Just below 192.168.0.0/16
            "192.169.0.0",  # Just above 192.168.0.0/16
            # Hostnames (not IPs)
            "api.cachekit.io",
            "example.com",
            "google.com",
        ],
    )
    def test_public_ips_not_detected(self, ip: str) -> None:
        """Public IPs and hostnames should NOT be detected as private."""
        assert is_private_ip(ip) is False, f"{ip} should NOT be detected as private"


class TestCachekitIOBackendConfig:
    """Tests for CachekitIOBackendConfig SSRF protection."""

    def test_default_url_allowed(self) -> None:
        """Default API URL should be allowed."""
        config = CachekitIOBackendConfig(api_key=SecretStr("ck_test_123"))
        assert config.api_url == "https://api.cachekit.io"

    def test_staging_url_allowed(self) -> None:
        """Staging API URL should be allowed."""
        config = CachekitIOBackendConfig(
            api_key=SecretStr("ck_test_123"),
            api_url="https://api.staging.cachekit.io",
        )
        assert config.api_url == "https://api.staging.cachekit.io"

    def test_subdomain_of_allowed_host(self) -> None:
        """Subdomains of allowed hosts should be allowed."""
        config = CachekitIOBackendConfig(
            api_key=SecretStr("ck_test_123"),
            api_url="https://v2.api.cachekit.io",
        )
        assert config.api_url == "https://v2.api.cachekit.io"

    @pytest.mark.parametrize(
        "url",
        [
            "http://api.cachekit.io",  # HTTP not allowed
            "http://localhost:8080",
            "http://127.0.0.1:3000",
        ],
    )
    def test_http_rejected(self, url: str) -> None:
        """HTTP protocol should be rejected."""
        with pytest.raises(ValidationError, match="must use HTTPS"):
            CachekitIOBackendConfig(api_key=SecretStr("ck_test_123"), api_url=url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://127.0.0.1",
            "https://localhost",
            "https://10.0.0.1",
            "https://192.168.1.1",
            "https://172.16.0.1",
            "https://169.254.169.254",  # AWS metadata
        ],
    )
    def test_private_ip_rejected(self, url: str) -> None:
        """Private/internal IPs should be rejected."""
        with pytest.raises(ValidationError, match="private/internal IP"):
            CachekitIOBackendConfig(api_key=SecretStr("ck_test_123"), api_url=url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.com",
            "https://attacker.io",
            "https://cachekit.io.evil.com",  # Subdomain attack
            "https://not-cachekit.io",
        ],
    )
    def test_unknown_host_rejected(self, url: str) -> None:
        """Unknown hosts should be rejected without allow_custom_host."""
        with pytest.raises(ValidationError, match="not in allowlist"):
            CachekitIOBackendConfig(api_key=SecretStr("ck_test_123"), api_url=url)

    def test_custom_host_allowed_with_override(self) -> None:
        """Custom hosts should be allowed when allow_custom_host=True."""
        config = CachekitIOBackendConfig(
            api_key=SecretStr("ck_test_123"),
            api_url="https://custom-cache.internal.company.com",
            allow_custom_host=True,
        )
        assert config.api_url == "https://custom-cache.internal.company.com"

    def test_private_ip_still_rejected_with_custom_host(self) -> None:
        """Private IPs should still be rejected even with allow_custom_host."""
        # allow_custom_host only bypasses hostname allowlist, not IP check
        with pytest.raises(ValidationError, match="private/internal IP"):
            CachekitIOBackendConfig(
                api_key=SecretStr("ck_test_123"),
                api_url="https://10.0.0.1",
                allow_custom_host=True,
            )

    def test_allowed_hosts_constant(self) -> None:
        """Verify ALLOWED_HOSTS contains expected values."""
        assert "api.cachekit.io" in ALLOWED_HOSTS
        assert "api.staging.cachekit.io" in ALLOWED_HOSTS

    def test_from_env_uses_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """from_env should use default URL if not specified."""
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_test_123")
        config = CachekitIOBackendConfig.from_env()
        assert config.api_url == "https://api.cachekit.io"

    def test_env_allow_custom_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CACHEKIT_ALLOW_CUSTOM_HOST env var should enable custom hosts."""
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_test_123")
        monkeypatch.setenv("CACHEKIT_API_URL", "https://custom.example.com")
        monkeypatch.setenv("CACHEKIT_ALLOW_CUSTOM_HOST", "true")
        config = CachekitIOBackendConfig.from_env()
        assert config.api_url == "https://custom.example.com"


class TestSSRFBypassAttempts:
    """Test various SSRF bypass attempts."""

    @pytest.mark.parametrize(
        "url",
        [
            # DNS rebinding doesn't help - we check hostname, not resolved IP
            # These are just hostname checks
            "https://[::ffff:127.0.0.1]",  # IPv4-mapped IPv6
            "https://[::1]",  # IPv6 loopback
            "https://0x7f000001",  # Hex encoding (won't parse as IP)
        ],
    )
    def test_bypass_attempts_blocked(self, url: str) -> None:
        """Various SSRF bypass attempts should be blocked."""
        with pytest.raises(ValidationError):
            CachekitIOBackendConfig(api_key=SecretStr("ck_test_123"), api_url=url)

    def test_ipv4_mapped_ipv6_blocked(self) -> None:
        """IPv4-mapped IPv6 addresses should be blocked."""
        # This tests the is_private_ip recursive check
        assert is_private_ip("::ffff:127.0.0.1") is True
        assert is_private_ip("::ffff:10.0.0.1") is True
        assert is_private_ip("::ffff:169.254.169.254") is True
