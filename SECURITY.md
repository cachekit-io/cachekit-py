# Security Policy

> Comprehensive security documentation for the cachekit Python SDK.

---

## Table of Contents

- [Supported Versions](#supported-versions)
- [Reporting a Vulnerability](#reporting-a-vulnerability)
- [Architecture Overview](#architecture-overview)
- [Python SDK Security Features](#python-sdk-security-features)
- [FFI Boundary Security](#ffi-boundary-security)
- [Supply Chain Security](#supply-chain-security)
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

### Bounded Decompression (ByteStorage envelopes)

The default read path is `decrypt → ByteStorage.retrieve (LZ4 + xxHash3) →
MessagePack decode`. This SDK does **not** implement LZ4 — it delegates to
cachekit-core's bounded `extract()`, which caps the decompressed output at
`min(512 MiB, 1000 × compressed_len)` before decompressing rather than trusting
the envelope's self-declared `original_size`. The xxHash3-64 checksum is
unkeyed, so it detects corruption and does not gate a forging attacker. See
[cachekit-core Decompression limits][core-decompress] for the numbers and the
constrained-runtime caveat.

The MessagePack size caps sit on the already-decompressed bytes, so they are
downstream of that bound.

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

## Supply Chain Security

### Rust Dependencies

| Tool | Purpose | Config |
|:-----|:--------|:-------|
| **cargo-deny** | License + vulnerability scanning | `rust/deny.toml` |
| **cargo-vet** | Supply chain auditing | `rust/supply-chain/config.toml` |

<details>
<summary><strong>📋 Policy Details</strong></summary>

**Allowed licenses**: MIT, Apache-2.0, BSD-3-Clause

**Denied licenses**: GPL (all variants)

**Vulnerability scanning**: [RustSec Advisory Database][rustsec]

**Audit status**: In progress (Q1 2026 target for full coverage)

</details>

> [!NOTE]
> Core dependencies (ring, lz4_flex, blake3) are audited in cachekit-core. See [cachekit-core supply chain docs][core-supply-chain].

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

### Arrow IPC Decompression Is Unbounded

> [!WARNING]
> `ArrowSerializer` (`serializer="arrow"`, requires the `[data]` extra) does not
> read through cachekit-core's bounded `extract()`. `deserialize()` hands the
> body to `pa.ipc.open_file(...).read_all()`, which decompresses with no size or
> ratio limit. Measured: a 2,570-byte envelope expands to 64 MiB (26,112:1), and
> 8,714 bytes to 256 MiB (30,805:1) — ratios cachekit-core rejects at 1000:1.
> Tracked in LAB-2730.

Neither existing control covers it:

- **The `[8-byte xxHash3-64][Arrow IPC]` prefix is not authentication.** It is
  unkeyed, so a backend-write attacker recomputes it — and they need not
  bother, because `deserialize()` also accepts raw `ARROW1` bodies with no
  checksum at all (the legacy integrity-off branch).
- **`max_value_size` is enforced on the write path only** (`cache_handler.py`),
  so it is a producer-side quota, not a check on bytes coming back off the wire.

**Exposure**: non-secure Arrow caches on a backend an attacker can write to.
`arrow_compression` defaults to `"zstd"`, so compression is on by default *once
Arrow is selected*; Arrow itself is opt-in. Secure (`@cache.secure`) caches
authenticate via AES-256-GCM before the reader sees anything, so they are not
exposed.

**A sound bound exists, and it costs the compression feature.** Uncompressed
Arrow IPC allocates in proportion to its own length (measured ratio 1.000), so
refusing bodies that declare `BodyCompression` on read makes `len(body)` a
genuine pre-decompression bound. That requires writing `compression="none"` too,
or every read of our own entries fails — which is a wire-size and L1-footprint
decision, not a drive-by fix. Keeping compression instead means summing each
buffer's uncompressed-length prefix before decompressing; `pa.ipc.read_message`
exposes the first buffer's prefix but not the rest, so that needs a
bounds-checked walk of the record-batch Flatbuffers metadata. LAB-2730 carries
both options.

Approaches that do **not** work, so nobody re-derives them: pyarrow exposes no
read-side size limit and no allocation-limiting memory pool; accumulating
`batch.nbytes` across `reader.get_batch(i)` is defeated because a forged
envelope declares one batch (our writer chunks to ~8 MiB, an attacker does not);
and a `table.nbytes` check after `read_all()` runs after the allocation it is
meant to prevent.

**Mitigations available now**: use `@cache.secure` for Arrow caches on
untrusted backends, or run with an enforced process memory limit. Setting
`compression=None` on the serializer does **not** mitigate — `deserialize()`
decompresses according to the stored stream's own metadata and never consults
that setting.

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
| Q1 2026 | Complete cargo-vet audits for all dependencies |
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
[core-supply-chain]: https://github.com/cachekit-io/cachekit-core/blob/main/SECURITY.md#supply-chain-security
[core-kani]: https://github.com/cachekit-io/cachekit-core/blob/main/SECURITY.md#kani-verification
[core-decompress]: https://github.com/cachekit-io/cachekit-core/blob/main/SECURITY.md#decompression-limits
[rustsec]: https://rustsec.org/
[cwe-502]: https://cwe.mitre.org/data/definitions/502.html
[cwe-532]: https://cwe.mitre.org/data/definitions/532.html
[cwe-22]: https://cwe.mitre.org/data/definitions/22.html
