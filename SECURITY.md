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
| 0.1.x   | ✅        |

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
[rustsec]: https://rustsec.org/
[cwe-502]: https://cwe.mitre.org/data/definitions/502.html
