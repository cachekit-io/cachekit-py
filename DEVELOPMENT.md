# cachekit Development Guide

> Production-ready universal caching for Python with intelligent reliability features and pluggable backends.

---

## Table of Contents

- [Quick Start](#quick-start)
- [Design Philosophy](#design-philosophy)
- [Development Workflow](#development-workflow)
- [Codebase Architecture](#codebase-architecture)
- [Testing Strategy](#testing-strategy)
- [Security Testing](#security-testing)
- [Code Quality Standards](#code-quality-standards)
- [Build & Release](#build--release)
- [Troubleshooting](#troubleshooting)

---

## Quick Start

```bash
make install        # Install dependencies
make quick-check    # Run fast validation (< 2 min)
make test           # Run all tests
```

---

## Design Philosophy

> **"No magic. Does exactly what it says on the tin."**

cachekit prioritizes explicit, predictable behavior.

### Serializers

| Serializer | Description | Use Case |
|:-----------|:------------|:---------|
| **StandardSerializer** | Language-agnostic MessagePack | Default, works everywhere |
| **AutoSerializer** | Python-optimized (NumPy, pandas, datetime) | Named "Auto" to be transparent |
| **ArrowSerializer** | Apache Arrow for DataFrames | 60%+ faster for pandas |
| **OrjsonSerializer** | JSON via orjson | JSON compatibility |

> [!IMPORTANT]
> **NO auto-detection of business logic**: Pydantic models, SQLAlchemy ORM objects, and custom classes require explicit conversion to dict.

Error messages tell you exactly what's wrong:

```diff
- TypeError: Object of type 'User' is not JSON serializable
+ ValueError: Cannot serialize Pydantic model. Use .model_dump() to convert.
```

### Backends

- Selection is **explicit** when you care (`backend=RedisBackend(...)`)
- Auto-detected when you don't
- No hidden fallbacks or "smart" switching

<details>
<summary><strong>🎯 Why This Matters for Contributors</strong></summary>

- Add features that are **obvious**, not clever
- Error messages **must** include fix instructions
- Avoid "automagic" type detection that surprises users
- If something auto-detects, name it honestly (`AutoSerializer`, not "SmartSerializer")
- Document tradeoffs explicitly

</details>

---

## Development Workflow

### Pre-Commit Checks

```bash
make quick-check    # Format, lint, type check, critical tests (< 2 min)
```

### Full Validation

| Command | Purpose |
|:--------|:--------|
| `make check` | All checks + build |
| `make test` | All tests |
| `make test-cov` | Coverage report |

### Code Quality Tools

| Command | Tools |
|:--------|:------|
| `make format` | Ruff (Python) + rustfmt (Rust) |
| `make lint` | Ruff + clippy |
| `make type-check` | basedpyright strict mode |

### Rust-Specific

```bash
cd rust
cargo test      # Rust unit tests
cargo bench     # Benchmarks
cargo clippy    # Lints
```

---

## Codebase Architecture

### Project Structure

```
cachekit/
├── Cargo.toml                  # Cargo workspace (profiles, shared deps)
├── rust/
│   ├── Cargo.toml              # Actual Rust crate
│   ├── src/
│   │   ├── lib.rs              # PyO3 FFI boundary
│   │   └── python_bindings.rs  # Python type wrappers
│   ├── fuzz/                   # cargo-fuzz targets
│   └── supply-chain/           # cargo-vet security audits
│
├── src/cachekit/
│   ├── decorators/             # @cache, @redis_cache
│   ├── serializers/            # RawSerializer, EncryptionWrapper
│   ├── backends/               # RedisBackend, CachekitIOBackend
│   ├── reliability/            # Circuit breaker, backpressure
│   └── monitoring/             # Prometheus metrics
│
└── tests/
    ├── unit/                   # Fast, mocked (pytest.mark.unit)
    ├── critical/               # Essential functionality (pytest.mark.critical)
    ├── integration/            # Require Redis (pytest.mark.integration)
    └── fuzzing/                # Atheris PyO3 FFI targets
```

> [!NOTE]
> **Cargo workspace**: Currently overkill for a single crate, but allows expansion. Run `cargo build` from workspace root or `rust/` - both work.

### Python-Rust Boundary (PyO3)

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Python Code    │────▶│   PyO3 FFI      │────▶│   Rust Native   │
│                 │◀────│   Boundary      │◀────│   Code          │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

<details>
<summary><strong>📝 Example: ByteStorage Compression</strong></summary>

**Python side** (`src/cachekit/serializers/raw.py`):
```python
from cachekit_rust import compress_bytes  # Rust function
compressed = compress_bytes(data)          # PyO3 handles conversion
```

**Rust side** (`rust/src/python_bindings.rs`):
```rust
#[pyfunction]
fn compress_bytes(data: &[u8]) -> PyResult<Vec<u8>> {
    // Rust implementation with LZ4
}
```

</details>

**Key PyO3 concepts:**

| Concept | Behavior |
|:--------|:---------|
| Type conversion | Python → Rust automatic (`&[u8]`, `String`, `Vec<u8>`) |
| Error handling | `PyResult<T>` converts Rust errors to Python exceptions |
| GIL | Held during FFI calls |
| Panics | Rust panic → Python `RuntimeError` |

### Serialization Pipeline

```
┌─────────────┐
│  User Data  │
└──────┬──────┘
       │
       ▼
┌─────────────────────────────────┐
│  Serializer.serialize()         │  Python: msgpack/arrow/orjson
└──────┬──────────────────────────┘
       │
       ▼
┌─────────────────────────────────┐
│  ByteStorage.store()            │  Rust: LZ4 + xxHash3-64
└──────┬──────────────────────────┘
       │
       ▼
┌─────────────────────────────────┐
│  encrypt() (if @cache.secure)   │  Rust: AES-256-GCM
└──────┬──────────────────────────┘
       │
       ▼
┌─────────────────────────────────┐
│  Backend.set()                  │  Python: Redis/CachekitIO
└─────────────────────────────────┘
```

> [!TIP]
> Decoding reverses the pipeline: `get()` → `decrypt()` → `retrieve()` → `deserialize()`

**ByteStorage** (Rust layer via [cachekit-core][core-repo]):

| Component | Implementation |
|:----------|:---------------|
| Compression | LZ4 (fast, ~3:1 ratio) |
| Integrity | xxHash3-64 checksums |
| Envelope | `[version:1][format:1][checksum:8][compressed_data]` |

### Backend Architecture

**RedisBackend** (`src/cachekit/backends/redis.py`):
- Connection pooling (redis-py)
- Circuit breaker wraps all operations
- Backpressure detection via connection pool stats
- Lua scripts for atomic multi-key operations

<details>
<summary><strong>➕ Adding a New Backend</strong></summary>

1. Inherit from `BaseBackend` (`src/cachekit/backends/base.py`)
2. Implement: `get()`, `set()`, `delete()`, `exists()`, `get_ttl()`
3. Add reliability wrappers (circuit breaker, backpressure)
4. Write integration tests (mark with `@pytest.mark.integration`)

</details>

---

## Testing Strategy

### Test Organization

| Directory | Purpose | Speed | Requires Redis |
|:----------|:--------|:-----:|:--------------:|
| `tests/unit/` | Mocked dependencies | ⚡ Fast | ❌ |
| `tests/critical/` | Essential functionality | ⚡ Fast | ✅ |
| `tests/integration/` | Full system tests | 🐢 Slow | ✅ |
| `tests/fuzzing/` | Atheris FFI targets | 🐢 Slow | ❌ |

### Running Tests

```bash
pytest tests/unit/ -v           # Unit tests
pytest tests/critical/ -v       # Critical tests (requires Redis)
pytest tests/integration/ -v    # Integration tests
make fuzz-quick                 # Fuzzing (Linux/CI only, 10 min each)
```

<details>
<summary><strong>📝 Test Examples</strong></summary>

**Unit test:**
```python
import pytest
from cachekit.serializers import StandardSerializer

@pytest.mark.unit
def test_serializer_roundtrip():
    """Test serialization + deserialization."""
    serializer = StandardSerializer()
    data = {"key": "value"}
    serialized, _ = serializer.serialize(data)
    assert serializer.deserialize(serialized) == data
```

**Integration test:**
```python
import pytest
from cachekit.decorators import cache

@pytest.mark.integration
def test_cache_with_redis(redis_url):
    """Test caching decorator with real Redis."""
    @cache(redis_url=redis_url, ttl=60)
    def expensive_function(x):
        return x * 2

    result1 = expensive_function(5)  # Cache miss
    result2 = expensive_function(5)  # Cache hit
    assert result1 == result2 == 10
```

</details>

### Property-Based Testing (Hypothesis)

Used for security properties in `tests/unit/test_security_properties.py`:

| Property | Guarantee |
|:---------|:----------|
| Encryption roundtrip | `decrypt(encrypt(data)) == data` |
| Compression integrity | `decompress(compress(data)) == data` |
| Tenant isolation | Different keys for different tenants |

```python
from hypothesis import given, strategies as st

@given(st.binary(min_size=1))
def test_compression_roundtrip(data):
    """Any data should survive compression roundtrip."""
    compressed = compress_bytes(data)
    decompressed = decompress_bytes(compressed)
    assert decompressed == data
```

---

## Security Testing

### Quick Checks

```bash
make quick-check        # Includes Ruff "S" security ruleset (< 2 min)
make security-audit     # pip-audit + cargo-audit CVE scan
```

### Fuzzing

```bash
# Python-Rust boundary (Linux/CI only, 10 min each)
make fuzz-quick

# Rust fuzzing (manual)
cd rust && cargo fuzz run byte_storage_decompress
```

### Security Stack

| Layer | Tools |
|:------|:------|
| **Python** | Ruff "S" (68 checks), pip-audit, Atheris, Hypothesis, basedpyright |
| **Rust** | cargo-fuzz, cargo-deny, cargo-vet, Kani |

> [!NOTE]
> For comprehensive security documentation, see [SECURITY.md](SECURITY.md).

---

## Code Quality Standards

### Type Hints (Required)

```python
from typing import Callable
from cachekit.types import Serializer

def cache_decorator(
    redis_url: str,
    ttl: int = 3600,
    serializer: Serializer | None = None,
) -> Callable[[F], F]:
    """Decorate a function for caching."""
    ...
```

> [!WARNING]
> basedpyright strict mode is enforced. **No type hints = build fails.**

### Absolute Imports Only

```diff
+ from cachekit.decorators.main import redis_cache       # Good
+ from cachekit.serializers import StandardSerializer

- from .main import redis_cache                          # Bad (will fail)
```

### Line Length: 129 Characters

| Why 129? | Reason |
|:---------|:-------|
| Not 120 | Too arbitrary |
| Not 100 | Too restrictive for Python |
| 129 | One more complex expression per line |
| Enforcement | Ruff autoformat |

```bash
uv run ruff format src/  # Auto-fixes
```

### Rust Code Style

```bash
cargo fmt                       # Format
cargo clippy -- -D warnings     # Lint (CI fails on warnings)
```

---

## Build & Release

### Standard Build

```bash
make release-check    # Version check + full validation + build
make build            # Creates dist/cachekit-*.whl
```

### Profile-Guided Optimization (5-8% faster)

```bash
make build-pgo        # Requires workload profiling
```

<details>
<summary><strong>⚡ PGO Steps</strong></summary>

1. Build with instrumentation (`release-pgo-generate` profile)
2. Run representative workload (benchmarks)
3. Rebuild with profile data (`release-pgo-use` profile)

See `Makefile` for full details.

</details>

---

## Performance Targets

| Target | Time | Purpose |
|:-------|:----:|:--------|
| `make quick-check` | < 2 min | Fast feedback loop |
| Pre-commit hooks | < 10 sec | Don't block commits |
| `make test-critical` | < 30 sec | CI gating |
| Atheris fuzzing | 10 min/target | Nightly CI |

> [!TIP]
> Fast feedback keeps development velocity high. Slow tests go to integration suite, run less frequently.

---

## Troubleshooting

### Type Checking Errors

```bash
make type-check                 # basedpyright with detailed errors
uv run basedpyright --verbose   # Extra debug info
```

| Issue | Fix |
|:------|:----|
| Missing return type | Add explicit return type annotation |

### PyO3 Compilation Errors

```
error: could not compile `cachekit-rust`
```

**Fix**: Check Rust toolchain version:

```bash
rustc --version  # Should be 1.75+
rustup update
```

### Rust Panics from Python

```python
RuntimeError: panicked at 'assertion failed', rust/src/byte_storage.rs:42
```

> [!NOTE]
> Rust panics are caught by PyO3 and converted to Python exceptions.

**Debug**: Run Rust tests directly:

```bash
cd rust && cargo test --lib
RUST_BACKTRACE=full cargo test
```

### Redis Connection Errors

```
redis.exceptions.ConnectionError: Error -2 connecting to localhost:6379
```

**Fix**: pytest-redis automatically starts isolated Redis instances:

```bash
make test-critical  # Handles Redis lifecycle
```

### Security Lint False Positives

```
S101: Use of `assert` detected
```

| Context | Handling |
|:--------|:---------|
| In tests | Allowed (Ruff config excludes `tests/` from S101) |
| In source | Use `if not condition: raise` instead |

### cargo-vet Exemptions

```
error: Package foo@1.0.0 is not audited
```

**Fix**: Add exemption to `rust/supply-chain/config.toml`:

```toml
[[exemptions.foo]]
version = "1.0.0"
criteria = "safe-to-deploy"
notes = "Used only in tests"
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for detailed guidelines.

---

<div align="center">

**[SECURITY.md](SECURITY.md)** · **[CONTRIBUTING.md](CONTRIBUTING.md)** · **[LICENSE](LICENSE)**

*MIT License*

</div>

<!-- Reference Links -->
[core-repo]: https://github.com/cachekit-io/cachekit-core
