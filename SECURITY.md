# Security Policy

> Comprehensive security documentation for the cachekit Python SDK.

---

## Table of Contents

- [Supported Versions](#supported-versions)
- [Reporting a Vulnerability](#reporting-a-vulnerability)
- [Architecture Overview](#architecture-overview)
- [Python SDK Security Features](#python-sdk-security-features)
- [FFI Boundary Security](#ffi-boundary-security)
- [Dependency Security](#dependency-security)
- [CI/CD Security](#cicd-security)
- [Known Limitations](#known-limitations)
- [Security Roadmap](#security-roadmap)

---

## Supported Versions

| Version | Supported |
|:--------|:---------:|
| 0.4.x   | ✅        |
| 0.3.x   | ✅        |
| < 0.3   | ❌        |

> [!NOTE]
> As a young project, we maintain security support for the latest release only. Once we reach 1.0.0, we will establish a longer-term LTS policy.

---

## Reporting a Vulnerability

> [!IMPORTANT]
> **We take security seriously.** If you discover a security vulnerability, please report it responsibly.

### Reporting Channels

| Channel | Use Case |
|:--------|:---------|
| **[security@cachekit.io](mailto:security@cachekit.io)** | Preferred for sensitive issues |
| **[GitHub Security Advisory][gh-advisory]** | Public vulnerability reports |

### What to Include

- Description of the vulnerability
- Steps to reproduce
- Affected versions
- Potential impact
- Suggested fix (if available)

### Response Timeline

| Stage | Timeline |
|:------|:--------:|
| Initial Response | 48 hours |
| Status Update | 7 days |
| Fix Timeline | Varies by severity |

<details>
<summary><strong>📋 Disclosure Policy</strong></summary>

We follow coordinated disclosure:

1. Acknowledge receipt within 48 hours
2. Confirm vulnerability and determine severity
3. Develop and test fix
4. Release security patch
5. Public disclosure after patch availability (coordinated with reporter)

</details>

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     cachekit Python SDK                         │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │   @cache     │  │   @cache     │  │   Redis/CachekitIO    │  │
│  │  Decorator   │  │   .secure    │  │      Backend          │  │
│  └──────┬───────┘  └──────┬───────┘  └───────────┬───────────┘  │
│         │                 │                      │              │
│         └────────┬────────┴──────────────────────┘              │
│                  │                                              │
│         ┌────────▼────────┐                                     │
│         │   PyO3 FFI      │  ◄── This repo                      │
│         │   Wrapper       │                                     │
│         └────────┬────────┘                                     │
└──────────────────┼──────────────────────────────────────────────┘
                   │
         ┌─────────▼─────────┐
         │   cachekit-core   │  ◄── Separate crate
         │  ┌─────────────┐  │
         │  │ AES-256-GCM │  │
         │  │ LZ4 Compress│  │
         │  │ xxHash3     │  │
         │  │ HKDF        │  │
         │  └─────────────┘  │
         └───────────────────┘
```

| Component | Responsibility |
|:----------|:---------------|
| **[cachekit-core][core-repo]** (Rust) | Compression, checksums, encryption, formal verification |
| **cachekit SDK** (this repo) | PyO3 FFI wrapper, decorators, Redis backend, configuration |

> [!TIP]
> For comprehensive security details about core cryptographic operations, see **[cachekit-core SECURITY.md][core-security]**.

This document focuses on **Python SDK-specific security**: FFI boundary, configuration, and Python-layer tooling.

---

## Python SDK Security Features

### No Untrusted Deserialization

> [!CAUTION]
> cachekit **NEVER** uses Python's `pickle` module due to arbitrary code execution risks ([CWE-502][cwe-502]).

We use MessagePack (safe binary serialization) with type preservation via schema metadata.

```diff
- import pickle  # NEVER - arbitrary code execution
+ import msgpack  # Safe binary serialization
```

### Zero-Knowledge Encryption

When enabled via `@cache.secure`, client-side AES-256-GCM encryption ensures the server never sees plaintext:

| Property | Guarantee |
|:---------|:----------|
| Encryption timing | **Before** data touches Redis |
| Server visibility | Opaque ciphertext only |
| Key derivation | HKDF with per-tenant salts |
| Authentication | GCM tags prevent tampering |
| Compliance | GDPR/HIPAA/PCI-DSS ready |

<details>
<summary><strong>🔐 Master Key Security</strong></summary>

| Requirement | Implementation |
|:------------|:---------------|
| Key size | Minimum 32 bytes (256 bits) |
| Configuration | `CACHEKIT_MASTER_KEY` env var |
| Logging | Never exposed in logs/errors |
| Derivation | HKDF with unique tenant salts |

</details>

<details>
<summary><strong>⚡ L1 Cache Behavior</strong></summary>

| Mode | L1 Storage | L2 Storage | Performance |
|:-----|:-----------|:-----------|:------------|
| `@cache` | Plaintext | Plaintext | ~50ns L1 / ~2-7ms L2 |
| `@cache.secure` | **Encrypted** | **Encrypted** | ~50ns L1 / ~2-7ms L2 |

Both tiers store encrypted bytes when encryption is enabled (encrypt-at-rest everywhere). Decryption happens at read time only, minimizing plaintext exposure.

</details>

> [!NOTE]
> All cryptographic operations are implemented in cachekit-core. See [cachekit-core SECURITY.md][core-security] for AES-256-GCM, HKDF, and formal verification details.

### Sensitive Configuration Masking

All sensitive values are automatically masked:

| Context | Masked |
|:--------|:------:|
| Structured logs | ✅ |
| Error messages | ✅ |
| Health endpoints | ✅ |
| Monitoring output | ✅ |

**Implementation**: Uses `pydantic-settings` with `SecretStr` for automatic redaction.

### SSRF Protection

When using `@cache.io` (CachekitIOBackend), the SDK includes built-in Server-Side Request Forgery (SSRF) protection. Custom API URLs are blocked by default - only `api.cachekit.io` and its subdomains are permitted.

See [SSRF Protection](docs/features/ssrf-protection.md) for full details, including custom host configuration for development environments.

### Cache Key Redaction in Logs (CWE-532)

Cache keys can embed caller-supplied tenant/user identifiers, so **the SDK's own loggers** (`cachekit.*`) never emit them verbatim ([CWE-532][cwe-532]). Every cachekit log path — decorator error handling (structured and backwards-compat), cache-operation logs, and SWR/TTL-refresh debug logs — replaces the key with a fixed-length blake2b digest (`<redacted:…>`), keeping log lines correlatable without leaking the key. Error paths are covered centrally at the shared error sink (`FeatureOrchestrator.handle_cache_error` / `log_cache_operation`), so new call sites are redacted by construction. Both structured cache-operation sinks (`FeatureOrchestrator.log_cache_operation`, `UltraOptimizedStructuredLogger.cache_operation`) also sanitise an exception passed as `error=` themselves — pass the exception object, never `str(e)`, which is emitted as-is. `BackendError` redacts the key in its formatted text (`str(e)` carries `key=<redacted:…>`), while the `.key` attribute keeps the raw caller-supplied key for programmatic use — never log `e.key` (see below for the same caution applied to `e`'s traceback). Its free-form `message` is caller-supplied and third-party exception text (a redis `ResponseError` naming the key, a pymemcache illegal-input error echoing it) has unknown provenance — so **no cachekit log line renders `str(e)`**. Every logging call that mentions an exception goes through `redact_error_for_log`, which emits only the exception type plus, for `BackendError`, its `BackendErrorType` classification; the full exception stays on the object (`original_exception`, `.message`) for programmatic access. Operators lose the provider's message text in the log line and keep it on the exception. An architecture test (`tests/unit/test_log_redaction_architecture.py`) walks every logging call in the package — `logger.*()`, `get_logger().*()`, `getattr(logger, level)()` — and fails CI if a key-shaped value reaches one unredacted in the message, `%s` arguments, or `extra=`; if an exception — any name bound by `except ... as`, a conventional name (`e`, `exc`, `err`, `error`, `*_err`), or an attribute of one — reaches one outside `redact_error_for_log`; or if a call emits a traceback (`logger.exception`, `exc_info=`). The guarantee does not depend on the next contributor remembering it. It is flow-insensitive: build log lines inline, not via a pre-formatted variable, and bind exceptions with `except ... as` or a conventional name (an `Exception`-typed parameter called `failure` is invisible to it), or the guard cannot see them.

**Scope — application-rendered tracebacks are not covered.** `BackendError.original_exception` (and the `from exc` chain that sets `__cause__`) deliberately keeps the original provider exception for programmatic access, and that exception's own text can embed the raw key — a pymemcache `MemcacheIllegalInputError` echoing an oversized key, a redis `ResponseError` naming it, or an httpx error string carrying the CachekitIO request path. cachekit itself never renders that text: no `cachekit.*` log line calls `logger.exception()` or passes `exc_info=`, so the SDK never prints a traceback, which is the same architecture test enforcing the guarantee above. That guarantee scopes to cachekit's own logging *calls*, not to the `JsonFormatter` cachekit ships (`cachekit.logging.JsonFormatter`): that formatter renders whatever `record.exc_info` a caller supplies via `traceback.format_exception`, so wiring it into your application's logging and emitting a `BackendError` with `exc_info` set renders the chained cause and its raw key exactly like the application paths below. The remaining path is your own logging code: if application code catches a `BackendError` and calls `logger.exception(e)`, sets `exc_info=True`, calls `traceback.format_exc()`, or hands the exception to an APM/error-tracking SDK, the rendered traceback includes the chained cause and its raw key. Log `redact_error_for_log(e)` (`from cachekit.hash_utils import redact_error_for_log`) or `type(e).__name__` instead of the traceback for a cachekit exception. If you must hand the exception itself to an error/APM tracker, submit a freshly constructed exception carrying only `redact_error_for_log(e)`; failing that, clear **all three** references to the provider exception on `e` first — `__cause__`, `__context__`, and `BackendError.original_exception` — **and `e.__traceback__` with them**. The backends raise the classified `BackendError` from inside the `except` block that caught the provider exception, so Python sets `__context__` as well as `__cause__`; clearing `__cause__` alone hides the provider text from `traceback` but leaves it reachable to an SDK that walks exception attributes, and `original_exception` is a third reference. `__traceback__` leaks by a different route than the other three: they carry the provider exception's *text*, whereas the traceback carries the backend *frame* that raised — and that frame's locals still hold the raw key (`MemcachedBackend.get` raises `classify_memcached_error(exc, operation="get", key=key) from exc`), so a tracker that captures frame locals reads the key off the frame even with all three references cleared. Clearing `e.__traceback__` also clears what `sys.exc_info()` reports for that exception, so it closes the `capture_exception()`-style path as well as the rendered one.

**Scope — transport logs are not covered.** The CachekitIO backend addresses entries by key in the request path (`GET /v1/cache/{key}`), and `httpx` logs every request line — method, full URL, status — at `INFO` on its own `httpx` logger. An application that enables `INFO` globally (`logging.basicConfig(level=logging.INFO)`) will therefore see raw keys in *httpx's* output on every operation, exactly as it would see any REST resource path. cachekit does not mute a third-party logger on your behalf; if your keys carry identifiers, silence or raise the level of that logger in your logging config:

```python
import logging

logging.getLogger("httpx").setLevel(logging.WARNING)
```

The same applies to any HTTP-layer capture between the SDK and `api.cachekit.io` — see the lock-token paragraph below for why path/query content is treated as logged.

**Digest strength.** The redaction digest is *unkeyed* blake2b, so it is exactly as hard to reverse as the key material is to guess — and the key material is deterministic from the call: `[ns:{ns}:]func:{mod.fn}:args:{blake2b(args)}` for generated keys, or whatever you return from `@cache(key=...)`. Namespace and function name are static application config, so a cache on `get_user(user_id)` is enumerable from its digest by iterating plausible IDs, whether the key was generated (hash the candidate args) or hand-built (`default:user:1234`). A per-installation secret was considered and rejected for a public library (unset it is theatre; set it breaks cross-process log correlation, the property the digest exists for). Treat the digest as a correlation ID, never as a secret: if a log reader must not be able to confirm *which* user an entry belongs to, do not grant that reader the logs.

### Lock Token Transport (CWE-532)

The distributed-lock capability token (`lock_id`) is sent in the `X-CacheKit-Lock-Id` request header when releasing a lock (`DELETE /v1/cache/{key}/lock`), **never** in the URL query string. Query strings are routinely captured by access logs, proxy/CDN logs, and OpenTelemetry `http.url` spans ([CWE-532][cwe-532]); a leaked token could be replayed to release a lock within its short TTL. The CacheKit SaaS backend dual-reads the header and the legacy `?lock_id=` query during migration, preferring the header (removed in protocol 2.0).

### Cache-Key Path Encoding (CWE-22)

Custom `@cache(key=...)` values are percent-encoded before they reach the CachekitIO request path, so a key can only ever address `/v1/cache/{key}` and never a different `api.cachekit.io` endpoint. Without encoding, `?`/`#` would be split into a query/fragment and a `/`-bearing key would introduce extra path segments, both escaping the cache namespace with the application's bearer token; httpx normalizes these client-side *before the request leaves the process* ([CWE-22][cwe-22]), so the SaaS-side key validator never sees them. `quote(key, safe="")` encodes every reserved character (`/` → `%2F`, `?` → `%3F`, `#` → `%23`, `%` → `%25`), collapsing the whole key into one inert path segment.

RFC-3986 marks `.` as *unreserved*, so `quote` (like cachekit-ts `encodeURIComponent` and cachekit-rs `urlencoding::encode`) leaves it raw — but a key of exactly `.` or `..` is still a live dot-segment that httpx collapses: `..` → `GET /v1`, and on the sub-resource routes `../ttl` → `GET /v1/ttl`, `../lock` → `GET /v1/lock`, reaching a *different* route with the bearer token. The encoder special-cases an all-dot segment (`..` → `%2E%2E`) so it can no longer collapse; only a segment that is *entirely* dots is affected (`a:..` is untouched), so canonical keys are unchanged.

Encode-once matches the SaaS validator's single decode, so a canonical key round-trips byte-for-byte. Python's `quote(key, safe="")` is byte-identical to cachekit-rs `urlencoding::encode`, and resolves to the same server-side key as cachekit-ts `encodeURIComponent` after that single decode, so cross-SDK cache lookups still coincide.

---

## FFI Boundary Security

> [!IMPORTANT]
> The PyO3 FFI boundary between Python and Rust is security-critical.

### Memory Safety

| Guarantee | Mechanism |
|:----------|:----------|
| Type safety | PyO3's compile-time type system |
| No unsafe serialization | MessagePack only (no `pickle`) |
| Buffer validation | Inputs validated before Rust calls |
| Panic handling | Rust panics → Python exceptions |

### Thread Safety

| Guarantee | Mechanism |
|:----------|:----------|
| GIL protection | All FFI calls acquire GIL |
| Rust synchronization | `Send`/`Sync` guarantees in cachekit-core |
| TSan validation | PyO3 false positives documented |

> [!WARNING]
> TSan suppressions in `rust/tsan_suppressions.txt` only cover PyO3/Python runtime false positives. Any data races in cachekit code are **real bugs** and must be fixed.

---

## Dependency Security

### Rust Dependencies

| Tool | Purpose | Config |
|:-----|:--------|:-------|
| **cargo-deny** | License + vulnerability scanning | `deny.toml` |
| **cargo-audit** | CVE scanning against RustSec Advisory Database | `.github/workflows/security-fast.yml` (inline ignore list) |

<details>
<summary><strong>📋 Policy Details</strong></summary>

**Allowed licenses**: MIT, Apache-2.0, BSD-3-Clause

**Denied licenses**: GPL (all variants)

**Vulnerability scanning**: [RustSec Advisory Database][rustsec]

</details>

> [!NOTE]
> Core dependencies (`ring` / `aes-gcm` for AES-256-GCM, `lz4_flex`, `xxhash-rust`, `rmp-serde`, `hkdf`, `sha2`) are audited in cachekit-core. See [cachekit-core dependency docs][core-deps]. `blake3` is not a cachekit-core dependency: it is a cachekit-py (Python) dependency used for cache-key hashing in `src/cachekit/hash_utils.py`, audited in this repo's own Python dependencies below.

### Python Dependencies

| Tool | Purpose | Command |
|:-----|:--------|:--------|
| **pip-audit** | CVE scanning | `make security-audit` |

---

## CI/CD Security

### Tiered Security Checks

| Tier | Timing | Trigger | Checks |
|:-----|:------:|:--------|:-------|
| **Fast** | < 3 min | Every PR | cargo-audit, cargo-deny, clippy, machete, pip-audit |
| **Medium** | < 15 min | Post-merge | cargo-geiger (<5% unsafe), semver-checks |
| **Deep** | < 2 hr | Nightly | Sanitizers (ASan, TSan, MSan), security report |

<details>
<summary><strong>📁 Workflow Files</strong></summary>

| Tier | Workflow |
|:-----|:---------|
| Fast | `.github/workflows/security-fast.yml` |
| Medium | `.github/workflows/security-medium.yml` |
| Deep | `.github/workflows/security-deep.yml` |

</details>

> [!TIP]
> Kani formal verification and cargo-fuzz run in cachekit-core CI. This SDK relies on cachekit-core's verification results.

### Local Development

```bash
# One-time setup
make security-install

# Quick checks (< 3 min)
make security-fast

# Comprehensive (< 15 min)
make security-medium

# Python dependencies
make security-audit

# Generate report
make security-report
```

Reports are archived in `reports/security/` for compliance and audit trails.

---

## Known Limitations

### Cryptographic Security

> [!NOTE]
> This SDK does not implement cryptography directly. All cryptographic operations are in [cachekit-core][core-repo].

**SDK Responsibilities**:
- Safely calling cachekit-core via FFI
- Protecting master keys in memory (`SecretStr`)
- Preventing key leakage in logs/errors
- Validating inputs before FFI calls

**For cryptographic guarantees**, see:
- [cachekit-core Cryptographic Security][core-security]
- [cachekit-core Kani Verification][core-kani]

### CI Workflow Validation

<details>
<summary><strong>⚠️ Validation Status</strong></summary>

**Validated**:
- Workflow syntax
- Job structure and dependencies
- Tool installation procedures
- Trigger configuration

**Requires validation on first PR**:
- Actual timing (fast < 3min, medium < 15min, deep < 2h)
- Sanitizer execution on Linux runners
- Caching effectiveness
- Resource limits and timeouts

</details>

---

## Version Policy

| Release Type | Scope | Breaking Changes |
|:-------------|:------|:----------------:|
| Patch (0.1.x) | Security fixes | ❌ |
| Minor (0.x.0) | New features | ❌ |
| Major (x.0.0) | Breaking changes | ✅ |

> [!NOTE]
> Pre-1.0: Minor versions may include breaking changes.

Security patches are backported to the latest supported version.

---

## Security Roadmap

| Quarter | Milestone |
|:--------|:----------|
| Q2 2026 | Add Hypothesis fuzzing for Python layer |
| Q3 2026 | Third-party security audit (SDK + FFI boundary) |
| Q4 2026 | SLSA Level 3 compliance |

---

## Contact

| Purpose | Channel |
|:--------|:--------|
| Security issues | [security@cachekit.io](mailto:security@cachekit.io) |
| General issues | [GitHub Issues][gh-issues] |
| Maintainers | [GitHub Repository][gh-repo] |

---

## Acknowledgments

We appreciate responsible disclosure from the security community. Security researchers who report valid vulnerabilities will be acknowledged in release notes (with permission).

---

<div align="center">

**[Report Vulnerability][gh-advisory]** · **[cachekit-core Security][core-security]** · **[GitHub][gh-repo]**

*Last Updated: 2025-12-09*

</div>

<!-- Reference Links -->
[gh-advisory]: https://github.com/cachekit-io/cachekit-py/security/advisories/new
[gh-issues]: https://github.com/cachekit-io/cachekit-py/issues
[gh-repo]: https://github.com/cachekit-io/cachekit-py
[core-repo]: https://github.com/cachekit-io/cachekit-core
[core-security]: https://github.com/cachekit-io/cachekit-core/blob/main/SECURITY.md
[core-deps]: https://github.com/cachekit-io/cachekit-core/blob/main/SECURITY.md#dependencies
[core-kani]: https://github.com/cachekit-io/cachekit-core/blob/main/SECURITY.md#kani-verification
[rustsec]: https://rustsec.org/
[cwe-502]: https://cwe.mitre.org/data/definitions/502.html
[cwe-532]: https://cwe.mitre.org/data/definitions/532.html
[cwe-22]: https://cwe.mitre.org/data/definitions/22.html
