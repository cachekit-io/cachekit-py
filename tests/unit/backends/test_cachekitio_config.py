"""Tests for CachekitIO backend configuration with SSRF protection."""

import pytest
from pydantic import SecretStr, ValidationError

from cachekit.backends.cachekitio.config import (
    ALLOWED_HOSTS,
    CachekitIOBackendConfig,
    _parse_api_url,
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
            # Unspecified, scoped, and IPv4-mapped in hex
            "::",
            "fe80::1%eth0",
            "[::ffff:7f00:1]",
            "::ffff:a9fe:a9fe",
            # IPv4 spellings the resolver reads as these addresses
            "127.1",
            "2130706433",
            "0x7f.1",
            "0x7f000001",
            "0177.0.0.1",
            "10.1",
            "167772161",
            "0xa9.0xfe.0xa9.0xfe",
            "0",
            # Trailing dot, and RFC 6761 localhost names
            "127.0.0.1.",
            "localhost.",
            "LOCALHOST",
            "app.localhost",
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
            # Public IPv4 in other spellings
            "8.8",
            "0x8.0x8.0x8.0x8",
            "134744072",
            "[::ffff:8.8.8.8]",
            # Hostnames (not IPs)
            "api.cachekit.io",
            "example.com",
            "google.com",
            "localhost.example.com",
            "notlocalhost",
            "127.0.0.1.example.com",
            "",
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

    @pytest.mark.parametrize(
        "url",
        [
            "https://API.CACHEKIT.IO",
            "https://api.cachekit.io:443",
            "https://api.cachekit.io/v1/",
            "https://api.staging.cachekit.io/v1?x=1#frag",
        ],
    )
    def test_allowed_host_spellings(self, url: str) -> None:
        """Case, an explicit port, a path, a query and a fragment do not change the host."""
        assert CachekitIOBackendConfig(api_key=SecretStr("ck_test_123"), api_url=url).api_url == url

    @pytest.mark.parametrize(
        "url",
        [
            "https://v2.api.cachekit.io",
            "https://x.api.staging.cachekit.io",
            "https://api.cachekit.io.",
            "https://cachekit.io",
        ],
    )
    def test_allowlist_is_exact(self, url: str) -> None:
        """Only the two API hostnames themselves are allowed; a subdomain needs allow_custom_host."""
        with pytest.raises(ValidationError, match="not in allowlist"):
            CachekitIOBackendConfig(api_key=SecretStr("ck_test_123"), api_url=url)

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
            "https://127.1",
            "https://2130706433",
            "https://0x7f.1",
            "https://0177.0.0.1",
            "https://[::ffff:7f00:1]",
            "https://[::]",
            "https://10.1:8443/v1",
            "https://app.localhost",
            "https://127.0.0.1%25x",  # anything after % is ignored
        ],
    )
    def test_private_ip_spellings_rejected_with_custom_host(self, url: str) -> None:
        """Every spelling of a private address is rejected, with or without allow_custom_host."""
        for allow in (False, True):
            with pytest.raises(ValidationError, match="private/internal IP"):
                CachekitIOBackendConfig(api_key=SecretStr("ck_test_123"), api_url=url, allow_custom_host=allow)

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
        # Checked as the IPv4 address each one maps to
        assert is_private_ip("::ffff:127.0.0.1") is True
        assert is_private_ip("::ffff:10.0.0.1") is True
        assert is_private_ip("::ffff:169.254.169.254") is True


class TestParseApiUrl:
    """The host checked is the host the HTTP client connects to."""

    @pytest.mark.parametrize(
        ("url", "host"),
        [
            ("https://api.cachekit.io", "api.cachekit.io"),
            ("https://API.cachekit.io:8443/v1", "api.cachekit.io"),
            ("https://[::1]:443", "::1"),
            ("https://[FE80::1]", "fe80::1"),
            ("https://xn--mnchen-3ya.example", "xn--mnchen-3ya.example"),
        ],
    )
    def test_host(self, url: str, host: str) -> None:
        assert _parse_api_url(url)[0] == host

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com\\.api.cachekit.io/..",
            "https://169.254.169.254\\.api.cachekit.io/..",
            "https://api.cachekit.io\\@example.com",
            "https://api.cachekit.io/\\",
            "https://ex%61mple.com",
            "https://[fe80::1%25eth0]",
            "https://api.cachekit.io:99999",
            "https://exa mple.com",
            "https://münchen.example",  # a custom host must use its ASCII xn-- form
        ],
    )
    def test_ambiguous_or_unparseable_url_rejected(self, url: str) -> None:
        """A URL whose host is not the same to both parsers, or that neither can parse, is rejected at config load."""
        with pytest.raises(ValueError, match="Invalid API URL"):
            _parse_api_url(url)
        for allow in (False, True):
            with pytest.raises(ValidationError, match="Invalid API URL"):
                CachekitIOBackendConfig(api_key=SecretStr("ck_test_123"), api_url=url, allow_custom_host=allow)

    def test_error_never_quotes_the_url(self) -> None:
        """Both parsers' own errors quote this userinfo; neither reaches the raised error or its chain."""
        url = "https://user:hunter2\uff20example.com"  # fullwidth @  # pragma: allowlist secret
        with pytest.raises(ValueError) as excinfo:
            _parse_api_url(url)
        assert "hunter2" not in str(excinfo.value)
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__context__ is None

    def test_url_without_host_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must name a host"):
            CachekitIOBackendConfig(api_key=SecretStr("ck_test_123"), api_url="https://:443", allow_custom_host=True)
