**[Home](../README.md)** › **Features** › **SSRF Protection**

# SSRF Protection

Server-Side Request Forgery (SSRF) protection for the CachekitIO backend.

## Overview

When using `CachekitIOBackend` to connect to the cachekit.io SaaS, the SDK includes built-in SSRF protection to prevent attackers from using your application to make requests to internal networks or cloud metadata endpoints.

## What is SSRF?

SSRF (Server-Side Request Forgery) occurs when an attacker can control the destination of server-side HTTP requests. Common attack targets include:

- **Cloud metadata endpoints**: `169.254.169.254` (AWS, GCP, Azure) - can leak IAM credentials
- **Internal services**: `10.x.x.x`, `192.168.x.x`, `172.16-31.x.x` - can access internal APIs
- **Localhost**: `127.0.0.1`, `localhost` - can access local services

## Protection Layers

The SDK implements three layers of SSRF protection:

### 1. HTTPS Enforcement

All API URLs must use HTTPS. HTTP is rejected:

```python notest
from cachekit.backends.cachekitio import CachekitIOBackendConfig

# ❌ Raises ValueError: API URL must use HTTPS protocol
config = CachekitIOBackendConfig(
    api_key="ck_live_xxx",
    api_url="http://api.cachekit.io"  # HTTP not allowed
)
```

### 2. Private IP Blocking

Requests to private/internal IP addresses are blocked:

```python notest
from cachekit.backends.cachekitio import CachekitIOBackendConfig

# ❌ All of these raise ValueError: private/internal IP address

# Cloud metadata (AWS, GCP, Azure)
config = CachekitIOBackendConfig(api_key="...", api_url="https://169.254.169.254")

# Private networks
config = CachekitIOBackendConfig(api_key="...", api_url="https://10.0.0.1")
config = CachekitIOBackendConfig(api_key="...", api_url="https://192.168.1.1")
config = CachekitIOBackendConfig(api_key="...", api_url="https://172.16.0.1")

# Localhost
config = CachekitIOBackendConfig(api_key="...", api_url="https://127.0.0.1")
config = CachekitIOBackendConfig(api_key="...", api_url="https://localhost")

# IPv6 equivalents
config = CachekitIOBackendConfig(api_key="...", api_url="https://[::1]")
config = CachekitIOBackendConfig(api_key="...", api_url="https://[::ffff:127.0.0.1]")
```

**Blocked IP ranges:**

| Range | Description |
|-------|-------------|
| `127.0.0.0/8` | Loopback |
| `10.0.0.0/8` | Private (Class A) |
| `172.16.0.0/12` | Private (Class B) |
| `192.168.0.0/16` | Private (Class C) |
| `169.254.0.0/16` | Link-local / Cloud metadata |
| `0.0.0.0/8` | Current network |
| `::1` | IPv6 loopback |
| `fe80::/10` | IPv6 link-local |
| `fc00::/7` | IPv6 unique local |
| `::ffff:x.x.x.x` | IPv4-mapped IPv6 (checked recursively) |

### 3. Hostname Allowlist

Only known cachekit.io hostnames are allowed by default:

```python notest
from cachekit.backends.cachekitio import CachekitIOBackendConfig

# ✅ Allowed (default)
config = CachekitIOBackendConfig(api_key="...", api_url="https://api.cachekit.io")

# ✅ Allowed (staging)
config = CachekitIOBackendConfig(api_key="...", api_url="https://api.staging.cachekit.io")

# ✅ Allowed (subdomains)
config = CachekitIOBackendConfig(api_key="...", api_url="https://v2.api.cachekit.io")

# ❌ Raises ValueError: hostname not in allowlist
config = CachekitIOBackendConfig(api_key="...", api_url="https://evil.com")
```

## Custom Hosts (Testing/Development)

For testing or self-hosted deployments, you can allow custom hostnames:

```python
from cachekit.backends.cachekitio import CachekitIOBackendConfig

# Via constructor
config = CachekitIOBackendConfig(
    api_key="ck_test_xxx",
    api_url="https://cache.internal.company.com",
    allow_custom_host=True
)

# Via environment variable
# CACHEKIT_ALLOW_CUSTOM_HOST=true
# CACHEKIT_API_URL=https://cache.internal.company.com
```

**Important**: Even with `allow_custom_host=True`, private IP addresses are still blocked. The allowlist bypass only affects hostname validation.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `CACHEKIT_API_URL` | API endpoint URL | `https://api.cachekit.io` |
| `CACHEKIT_ALLOW_CUSTOM_HOST` | Allow non-cachekit.io hostnames | `false` |

## Cross-SDK Consistency

This SSRF protection is implemented consistently across all CacheKit SDKs:

| SDK | Implementation |
|-----|----------------|
| Python | `cachekit.backends.cachekitio.config.is_private_ip()` |
| TypeScript | `@cachekit-io/cachekit` - `config.ts:isPrivateIP()` |

Both implementations block identical IP ranges and enforce the same hostname allowlist.

## Security Considerations

1. **Hostname allowlist is the primary defense**: The IP blocking is defense-in-depth for obvious patterns. The hostname allowlist (`api.cachekit.io`, `api.staging.cachekit.io`) is the primary security control.

2. **DNS rebinding**: The SDK checks hostnames at configuration time, not at request time. DNS rebinding attacks that resolve to private IPs after validation are mitigated by the hostname allowlist (attackers can't control `*.cachekit.io` DNS).

3. **IPv4-mapped IPv6**: The SDK recursively checks `::ffff:x.x.x.x` addresses to prevent bypasses via IPv4-mapped IPv6 notation.

4. **Custom hosts require trust**: When using `allow_custom_host=True`, the hostname allowlist is bypassed. This setting should only be used in controlled environments (development, testing, self-hosted). The IP blocking still applies but uses string pattern matching only.

## Known Limitations

When `allow_custom_host=True`, the IP blocking uses **string pattern matching** (not DNS resolution). This means:

- **Blocked**: `https://127.0.0.1`, `https://10.0.0.1`, `https://[::1]`
- **Not blocked**: Alternative IP encodings like hex (`0x7f000001`), decimal (`2130706433`), or abbreviated (`127.1`)

This is intentional to avoid network dependencies during configuration loading. If you use `allow_custom_host=True`:

1. Only use it in trusted environments
2. Ensure the URL comes from trusted configuration, not user input
3. Consider additional network-level controls (firewall rules, egress filtering)

---

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

*Last Updated: 2026-01-21*
