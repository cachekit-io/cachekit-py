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

When enabled via `@cache.secure`, client-side AES-256-GCM encryption ensures the server never sees plaintext values. The cache key is not encrypted ([details](docs/features/zero-knowledge-encryption.md#cleartext-cache-key-accepted-exposure)):

| Property | Guarantee |
|:---------|:----------|
| Encryption timing | **Before** data touches Redis |
| Server visibility | Opaque ciphertext values; the cache key stays cleartext |
| Key derivation | HKDF with per-tenant salts |
| Authentication | GCM tags prevent tampering |
| Compliance | May *reduce* GDPR/HIPAA/PCI DSS scope, subject to assessment — not a compliance guarantee ([details](docs/features/zero-knowledge-encryption.md#compliance-implications)) |

<details>
<summary><strong>🔐 Master Key Security</strong></summary>

| Requirement | Implementation |
|:------------|:---------------|
| Key size | Minimum 32 bytes (256 bits) |
| Configuration | `CACHEKIT_MASTER_KEY` env var |
| Activation | `@cache.secure`, or an explicit encryption option (`encryption=True` + tenant mode). The env var is a key source only, never a switch: a master key present on a cache that states no encryption intent raises `ConfigurationError` at decoration, on every preset except `@cache.secure` and `@cache.local` ([rules](docs/features/zero-knowledge-encryption.md#activation-the-master-key-is-a-source-not-a-switch)) |
| Logging | Never exposed in logs/errors |
| Derivation | HKDF with unique tenant salts |
| Single-tenant `tenant_id` | Literal `"default"` (protocol cross-SDK default) unless `deployment_uuid` / `CACHEKIT_DEPLOYMENT_UUID` is set; the same value binds HKDF and AAD |

</details>

<details>
<summary><strong>⚡ L1 Cache Behavior</strong></summary>

| Mode | L1 Storage | L2 Storage | Performance |
|:-----|:-----------|:-----------|:------------|
| `@cache`, encryption off | Plaintext | Plaintext | ~50ns L1 / ~2-7ms L2 |
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

**Implementation**: Uses `pydantic-settings` with `SecretStr` for automatic redaction. Constructing `CachekitConfig` or any backend config (such as `CachekitIOBackendConfig`) through its constructor, `from_env()` or a `model_validate*` classmethod with an invalid value raises a `pydantic.ValidationError` whose `str()` omits inputs and whose `errors()` and `json()` read `"[REDACTED]"` for every `input`. No message quotes a key or password, and neither an exception chained to it nor the validator exception in its `ctx` (kept for its message, without its traceback or chain) leads back to the raw values. Each error keeps its `type` and `loc`, so it still names the field that failed and why, and its `msg` and `ctx` unless a `ctx` exception's message came from its traceback or chain (re-rendered without them), or a custom error's message would change when formatted again with its own `ctx` (that `ctx` is dropped and the message kept verbatim). An environment value or env file that cannot be decoded at all (a list field that is not JSON, a file that is not UTF-8) raises pydantic-settings' `SettingsError` from the constructor or `from_env()`, and a `value_error` from a `model_validate*` classmethod; either names only what failed, not the value, and has no chain. When your code constructs a config class directly (constructor, `from_env()` or a `model_validate*` classmethod), the config class's own frames on the error's traceback keep no raw input in their locals, so an error tracker that captures frame locals (Sentry does by default) reads none from them. The same holds for an error raised from a cachekit entry point that takes a secret or reads one from the environment: the backend constructors (`CachekitIOBackend`, `RedisBackend`), the `@cache` decorators (`@cache`, `@cache.secure`, `@cache.io` and the other presets), the `DecoratorConfig` presets, `CacheSerializationHandler`, `EncryptionWrapper` and `validate_encryption_config`. A master key, whether you pass it or cachekit reads it from `CACHEKIT_*`, an API key you pass as a string or as bytes or cachekit reads from `CACHEKIT_API_KEY`, and a Redis URL you pass as a string, are held in cachekit's frames only as a pydantic `SecretStr` or `SecretBytes`, which renders masked, on the error's traceback and on every exception chained to it. So is a string or bytes value you pass to a `@cache` decorator or a `DecoratorConfig` preset under a keyword that no form of `@cache` takes, such as a misspelt `master_keey=`, whichever check refuses the call. `EncryptionWrapper` is the one entry point whose `master_key=` takes raw key bytes. `@cache`, `@cache.secure`, `DecoratorConfig.secure()`, `EncryptionConfig`, `CacheSerializationHandler` and `validate_encryption_config` take the key as a hex string and refuse a bytes key with `TypeError`, whatever the encryption setting; the other `@cache` forms refuse `master_key=` itself. Either way the error's frames hold a bytes key only as `SecretBytes`. An API key is an ASCII token, so `@cache.io`, `DecoratorConfig.io()` and `CachekitIOBackend` take it as `bytes`, `bytearray` or `memoryview` too, decoded as UTF-8; a key that is not UTF-8, or not an ASCII bearer token, is refused with `ConfigurationError`. `CachekitConfig` validates `master_key` the way pydantic validates any string: it decodes UTF-8 bytes to text, so bytes holding ASCII hex digits read as that hex, and it rejects bytes that are not UTF-8 with a `pydantic.ValidationError`. Your own frames still hold whatever your code passed in, and a config object you pass (such as a `RedisBackendConfig`) is shown by its own representation.

### SSRF Protection

When using `@cache.io` (CachekitIOBackend), the SDK includes built-in Server-Side Request Forgery (SSRF) protection. Custom API URLs are blocked by default - only `api.cachekit.io` and `api.staging.cachekit.io` are permitted, matched exactly (a subdomain is not).

See [SSRF Protection](docs/features/ssrf-protection.md) for full details, including custom host configuration for development environments.

### Cache Key Redaction in Logs (CWE-532)

Cache keys can embed caller-supplied tenant/user identifiers, so **the SDK's own loggers** (`cachekit.*`) never emit them verbatim ([CWE-532][cwe-532]). Every cachekit log path — decorator error handling (structured and backwards-compat), cache-operation logs, and SWR/TTL-refresh logs — replaces the key with a fixed-length blake2b digest (`<redacted:…>`), keeping log lines correlatable without leaking the key. The SWR refresh WARNINGs name the decorated function by the same digest of its `module.qualname`, because a dynamically created function's `__qualname__` can carry caller data, and SWR background threads have static names, so a `%(threadName)s` log format leaks no function name either. Error paths are covered centrally at the shared error sink (`FeatureOrchestrator.handle_cache_error` / `log_cache_operation`), so new call sites are redacted by construction. Both structured cache-operation sinks (`FeatureOrchestrator.log_cache_operation`, `StructuredLogger.cache_operation`) also sanitise an exception passed as `error=` themselves — pass the exception object, never `str(e)`, which is emitted as-is. `BackendError` redacts the key in its formatted text (`str(e)` carries `key=<redacted:…>`), while the `.key` attribute keeps the raw caller-supplied key for programmatic use — never log `e.key` (see below for the same caution applied to `e`'s traceback). Its free-form `message` is caller-supplied and third-party exception text (a redis `ResponseError` naming the key, a pymemcache illegal-input error echoing it) has unknown provenance — so **no cachekit log line renders `str(e)`**. Every logging call that mentions an exception goes through `redact_error_for_log`, which emits only the exception type plus, for `BackendError`, its `BackendErrorType` classification; the full exception stays on the object (`original_exception`, `.message`) for programmatic access. Operators lose the provider's message text in the log line and keep it on the exception, except for a CachekitIO transport failure (see below). An architecture test (`tests/unit/test_log_redaction_architecture.py`) walks every logging call in the package — `logger.*()`, `get_logger().*()`, `getattr(logger, level)()` — and fails CI if a key-shaped value reaches one unredacted in the message, `%s` arguments, or `extra=`; if an exception — any name bound by `except ... as`, a conventional name (`e`, `exc`, `err`, `error`, `*_err`), or an attribute of one — reaches one outside `redact_error_for_log`; or if a call emits a traceback (`logger.exception`, `exc_info=`). The guarantee does not depend on the next contributor remembering it. It is flow-insensitive: build log lines inline, not via a pre-formatted variable, and bind exceptions with `except ... as` or a conventional name (an `Exception`-typed parameter called `failure` is invisible to it), or the guard cannot see them.

**Scope — application-rendered tracebacks are not covered.** `BackendError.original_exception` (and the `from exc` chain that sets `__cause__`) deliberately keeps the original provider exception for programmatic access, and that exception's own text can embed the raw key — a pymemcache `MemcacheIllegalInputError` echoing an oversized key, or a redis `ResponseError` naming it. The CachekitIO backend keeps no urllib3 exception. A request that fails in transport raises a `BackendError` whose `original_exception` and `__cause__` are an `HTTPTransportError` naming only urllib3's exception class, with no traceback. It is raised outside the `except` block, so its `__context__` is never urllib3's exception. urllib3's exception is dropped because its traceback runs through urllib3's request frames, whose locals hold the request headers, the `Authorization: Bearer` API key included, and an error tracker that captures frame locals would send them. A request answered with a status other than 2xx keeps an `HTTPStatusError` holding the response, which reaches no request frame either. So no local in a frame on a failed CachekitIO request's `BackendError`, or on any exception its `__cause__`, `__context__` or `original_exception` reaches, holds the API key as an error tracker's frame-locals capture serialises it: strings and bytes by content, dicts, lists, tuples and sets item by item, and any other object by its `repr` (`tests/unit/backends/test_cachekitio_transport_error_frames.py`). cachekit's own frames on it still hold the cache key, as below. cachekit itself never renders a provider exception's text: no `cachekit.*` log line calls `logger.exception()` or passes `exc_info=`, so the SDK never prints a traceback, which is the same architecture test enforcing the guarantee above. That guarantee scopes to cachekit's own logging *calls*, not to the `JsonFormatter` cachekit ships (`cachekit.logging.JsonFormatter`): that formatter renders whatever `record.exc_info` a caller supplies via `traceback.format_exception`, so wiring it into your application's logging and emitting a `BackendError` with `exc_info` set renders the chained cause and its raw key exactly like the application paths below. The remaining path is your own logging code: if application code catches a `BackendError` and calls `logger.exception(e)`, sets `exc_info=True`, calls `traceback.format_exc()`, or hands the exception to an APM/error-tracking SDK, the rendered traceback includes the chained cause and its raw key. Log `redact_error_for_log(e)` (`from cachekit.hash_utils import redact_error_for_log`) or `type(e).__name__` instead of the traceback for a cachekit exception. If you must hand the exception itself to an error/APM tracker, pass it a freshly constructed exception carrying only `redact_error_for_log(e)`, handed over explicitly (`capture_exception(RuntimeError(redact_error_for_log(e)))`) rather than raised inside the `except` block: raising it there sets its `__context__` to `e` and re-links the whole chain, and `raise … from None` does not undo that — it only sets `__suppress_context__`, which hides the chain from `traceback` but not from an SDK that walks exception attributes. Failing that, clear **all three** references to the provider exception on `e` first — `__cause__`, `__context__`, and `BackendError.original_exception` — **and `e.__traceback__` and `e.key` with them**. The other backends raise the classified `BackendError` from inside the `except` block that caught the provider exception, so Python sets `__context__` as well as `__cause__`; clearing `__cause__` alone hides the provider text from `traceback` but leaves it reachable to an SDK that walks exception attributes, and `original_exception` is a third reference. `__traceback__` leaks by a different route than the other three: they carry the provider exception's *text*, whereas the traceback carries the backend *frame* that raised — and that frame's locals still hold the raw key (`MemcachedBackend.get` raises `classify_memcached_error(exc, operation="get", key=key) from exc`), so a tracker that captures frame locals reads the key off the frame even with all three references cleared. `e.key` is the raw caller-supplied key itself (only `str(e)` is redacted), so a tracker that serialises exception attributes reads it directly. On Python 3.11+, clearing `e.__traceback__` also clears what `sys.exc_info()` reports for that exception. On Python 3.10 it does not: `sys.exc_info()` keeps its own reference to the original traceback until the `except` block exits, so a no-argument `capture_exception()` inside the block still reads the backend frame — always pass the exception to the tracker explicitly.

**Scope — transport logs are not covered.** The CachekitIO backend addresses entries by key in the request path (`GET /v1/cache/{key}`), and urllib3 logs every request line — method, path, status — at `DEBUG` on its `urllib3.connectionpool` logger. An application that enables `DEBUG` globally (`logging.basicConfig(level=logging.DEBUG)`) will therefore see raw keys in *urllib3's* output on every operation, exactly as it would see any REST resource path. urllib3 logs no headers, so neither the `Authorization` API key nor the `X-CacheKit-Lock-Id` lock token reaches that output (`tests/unit/backends/test_cachekitio_transport_logging.py`), and the line never carries a password: an API URL with credentials (`user:password@`) is rejected at construction. cachekit leaves the `urllib3` logger to you, because that line carries identifiers, not credentials. If your keys carry identifiers, silence or raise the level of that logger in your logging config:

```python
import logging

logging.getLogger("urllib3").setLevel(logging.INFO)
```

The same applies to any HTTP-layer capture between the SDK and `api.cachekit.io` — see the lock-token paragraph below for why path/query content is treated as logged.

**Digest strength.** The redaction digest is *unkeyed* blake2b, so it is exactly as hard to reverse as the key material is to guess — and the key material is deterministic from the call: `[ns:{ns}:]func:{mod.fn}:args:{blake2b(args)}` for generated keys, or whatever you return from `@cache(key=...)`. Namespace and function name are static application config, so a cache on `get_user(user_id)` is enumerable from its digest by iterating plausible IDs, whether the key was generated (hash the candidate args) or hand-built (`default:user:1234`). A per-installation secret was considered and rejected for a public library (unset it is theatre; set it breaks cross-process log correlation, the property the digest exists for). Treat the digest as a correlation ID, never as a secret: if a log reader must not be able to confirm *which* user an entry belongs to, do not grant that reader the logs.

### Lock Token Transport (CWE-532)

The distributed-lock capability token (`lock_id`) is sent in the `X-CacheKit-Lock-Id` request header when releasing a lock (`DELETE /v1/cache/{key}/lock`), **never** in the URL query string. Query strings are routinely captured by access logs, proxy/CDN logs, and OpenTelemetry `http.url` spans ([CWE-532][cwe-532]); a leaked token could be replayed to release a lock within its short TTL. The CacheKit SaaS backend dual-reads the header and the legacy `?lock_id=` query during migration, preferring the header (removed in protocol 2.0).

### Cache-Key Path Encoding (CWE-22)

Custom `@cache(key=...)` values are percent-encoded before they reach the CachekitIO request path, so a key can only ever address `/v1/cache/{key}` and never a different `api.cachekit.io` endpoint. Without encoding, `?`/`#` would be split into a query/fragment and a `/`-bearing key would introduce extra path segments, both escaping the cache namespace with the application's bearer token; an HTTP client splits these off client-side *before the request leaves the process* ([CWE-22][cwe-22]), so the SaaS-side key validator never sees them. `quote(key, safe="")` encodes every reserved character (`/` → `%2F`, `?` → `%3F`, `#` → `%23`, `%` → `%25`), collapsing the whole key into one inert path segment.

Six keys cannot be encoded safely at all, so they are **rejected client-side** with a `PERMANENT` `BackendError` before any request is made: the empty key, `.`, `..`, `health`, `ttl` and `lock` ([protocol § Cache-Key Path Encoding](https://github.com/cachekit-io/protocol/blob/main/spec/saas-api.md#cache-key-path-encoding), rule 2). RFC-3986 marks `.` as *unreserved*, so `quote` leaves a key of exactly `.` or `..` raw, and that is a live dot-segment: `..` → `GET /v1`, `../ttl` → `GET /v1/ttl`, reaching a *different* route with the bearer token. Percent-encoding the dots does not fix it. The client sends `%2E%2E` intact, but the SaaS parses the request URL under the WHATWG URL Standard, which treats `%2e` / `%2e%2e` (any case) as dot segments and collapses them server-side, so the request still never reaches the key validator. `health`, `ttl` and `lock` are route tokens at the same level: `/v1/cache/health` is the health endpoint, and a final `ttl` or `lock` segment selects a sub-resource. The empty key is an empty segment: `/v1/cache/{key}` becomes `/v1/cache/` and `/v1/cache/{key}/ttl` becomes `/v1/cache//ttl`, neither of which addresses a stored entry. Only an exact match is reserved (`a:..`, `..a` and `x..y` are sent as-is), and canonical keys always contain `:`, so they never match.

Encode-once matches the SaaS validator's single decode, so a canonical key round-trips byte-for-byte. Python's `quote(key, safe="")` is byte-identical to cachekit-rs `urlencoding::encode`, and resolves to the same server-side key as cachekit-ts `encodeURIComponent` after that single decode, so cross-SDK cache lookups still coincide.

### Invalidation Channel (Redis Pub/Sub)

On the tenant-scoped Redis backend, every successful invalidation is announced on the Redis pub/sub channel `cachekit:py:invalidate:v1` ([Invalidation announcements](docs/features/l1-invalidation.md#whole-function-invalidation)). Redis delivers pub/sub messages to every subscriber whatever its database number, and ACL channel rights are granted apart from key patterns, so the channel is a trust boundary of its own.

**What a subscriber learns.** Each message names a function by its namespace and a hash of its `module.qualname`. For `invalidate_cache(args)` on a generated or fast-mode key it also carries the key, which spells out the namespace and the function and holds an unkeyed hash of the arguments: guessable arguments can be recovered from it, exactly as **Digest strength** describes above. A custom `key=` key, which can embed caller identifiers, is never sent, and neither is the tenant prefix. Grant `subscribe` on the channel only to your application's own Redis users; with Redis 7+ ACLs, give every other user `resetchannels`.

**What a publisher can do.** A process that sets `CACHEKIT_INVALIDATION_LISTENER_ENABLED` treats each message as untrusted input. It is size-checked (4096 bytes) before MessagePack decoding, which builds only a small map of short strings, so a forged message can neither run code nor stop the listener; at most it evicts L1 entries in every listening process. A publisher can also spend listeners' resources. Redis delivers a message whole before cachekit sees its size, so a large message costs each listener its size in memory, and one beyond Redis's pub/sub output-buffer limit (32 MB by default) makes Redis drop every listener's connection until it reconnects. A whole-function event costs time in proportion to the keys the process recorded for the function. Malformed messages cannot flood the logs: each process logs at most one WARNING a minute for them, `Invalidation event dropped (drops since the last warning: N)`, with the count of drops since the last one, and each drop in between at DEBUG. Grant `publish` on the channel only to your application's own Redis users.

**The listener's connection** is a clone of the backend's connection pool that keeps its connection class, so the listener of a `rediss://` backend uses TLS too, never plaintext.

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
[core-decompress]: https://github.com/cachekit-io/cachekit-core/blob/main/SECURITY.md#decompression-limits
[rustsec]: https://rustsec.org/
[cwe-502]: https://cwe.mitre.org/data/definitions/502.html
[cwe-532]: https://cwe.mitre.org/data/definitions/532.html
[cwe-22]: https://cwe.mitre.org/data/definitions/22.html
