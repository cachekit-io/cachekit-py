# Rust Fuzzing Infrastructure

Comprehensive fuzzing infrastructure for cachekit's ByteStorage and encryption modules, designed to detect security vulnerabilities before production deployment.

## Overview

This fuzzing suite provides **14 fuzz targets** covering:
- **ByteStorage attacks**: Corrupted envelopes, integer overflow, checksum collision, format injection, empty data edge cases
- **Encryption attacks**: Key derivation, nonce reuse, truncated ciphertext, AAD injection, large payloads
- **Integration testing**: Layered security (compression + encryption)

**Security Model**: Fail-closed - all malicious inputs must be safely rejected without panics or undefined behavior.

## Quick Start

### Prerequisites
```bash
# Install cargo-fuzz (libfuzzer-based)
cargo install cargo-fuzz

# Install AFL++ (optional, for mutation-based fuzzing)
cargo install cargo-afl
```

### Basic Fuzzing Workflow
```bash
# Quick smoke test (60s per target, ~14min total)
cd rust && make fuzz-quick

# Fuzz single target for development
cd rust && make fuzz-target TARGET=byte_storage_corrupted_envelope

# Deep fuzzing (8 hours per target, production validation)
cd rust && make fuzz-deep TARGET=encryption_key_derivation

# Generate coverage report
cd rust && make fuzz-coverage
```

## Fuzz Targets

### ByteStorage Targets

**byte_storage_corrupted_envelope.rs**
- Attack: Malformed MessagePack `StorageEnvelope` deserialization
- Validates: Safe rejection of corrupted serialization without panics
- Tests: Invalid MessagePack structures, truncated data, type confusion

**byte_storage_integer_overflow.rs**
- Attack: Decompression bomb via oversized `original_size` (u32::MAX, boundary cases)
- Validates: Size limit enforcement, integer overflow protection
- Tests: u32::MAX, MAX_UNCOMPRESSED_SIZE ± 1, suspicious compression ratios

**byte_storage_checksum_collision.rs**
- Attack: Data corruption with manipulated xxHash3-64 checksums
- Validates: Integrity verification detects mismatches
- Tests: Bit flips, truncation, zero checksums, partial corruption

**byte_storage_empty_data.rs**
- Attack: Inconsistent envelope state (empty data + non-zero size)
- Validates: Envelope consistency checks
- Tests: Empty compressed_data with original_size=1000, vice versa, both empty

**byte_storage_format_injection.rs**
- Attack: Malicious format identifiers (path traversal, nulls, control chars)
- Validates: Format string treated as opaque identifier
- Tests: "../../../etc/passwd", null bytes, CRLF, Unicode attacks, 10KB+ strings

### Encryption Targets

**encryption_key_derivation.rs**
- Attack: Malicious tenant IDs in key derivation
- Validates: Deterministic key derivation without crashes
- Tests: Nulls, path traversal, Unicode, empty strings, 10KB+ length

**encryption_nonce_reuse.rs**
- Attack: Nonce uniqueness verification (encrypt same plaintext 100x)
- Validates: Cryptographic randomness, no ciphertext repetition
- Tests: Multiple encryptions with same key+plaintext produce distinct outputs

**encryption_truncated_ciphertext.rs**
- Attack: Partial/incomplete ciphertext (truncated auth tags)
- Validates: Authentication failure detection
- Tests: Truncation at nonce, ciphertext body, auth tag boundaries

**encryption_aad_injection.rs**
- Attack: Additional Authenticated Data with nulls, control chars, Unicode
- Validates: AAD cryptographic binding (wrong AAD = decrypt fails)
- Tests: Null bytes, control characters, Unicode, 1-bit AAD modification

**encryption_large_payload.rs**
- Attack: Production-scale payloads (1MB, 10MB, 100MB)
- Validates: Performance and correctness at scale
- Tests: Large allocations, memory efficiency, no artificial 4KB limits

### Integration Targets

**integration_layered_security.rs**
- Attack: Corruption at multiple layers (plaintext → compression → encryption)
- Validates: Proper error attribution per layer
- Tests: Pre-compression corruption, post-compression corruption, ciphertext tampering

## Corpus Management

### Directory Structure

One committed seed directory per fuzz target, named exactly after the
`[[bin]]` stanza in `Cargo.toml`:

```
rust/fuzz/corpus/
├── CORPUS_INFO.md               # Layout contract and maintenance guide
├── byte_storage_compress/       # Raw plaintext seeds
├── byte_storage_decompress/     # Valid + corrupted StorageEnvelope bytes
├── …                            # One directory per [[bin]] target
└── integration_layered_security/
```

This is the layout `cargo fuzz run <target>` loads by default — locally and
in CI, with no corpus argument. Seeds are shaped per target's input format
(e.g. `32-byte key ++ plaintext` for encryption targets); the envelope seeds
are byte-exact against cachekit-core's wire format so their xxHash3-64
checksums verify, reaching branches random inputs essentially never hit.
See `corpus/CORPUS_INFO.md` for the full contract.

### Corpus Scripts
```bash
# (Re)generate the deterministic per-target seed set
# Needs python3 with msgpack, lz4, xxhash — or via uv:
#   uv run --no-project --with msgpack --with lz4 --with xxhash bash scripts/generate_corpus.sh
cd rust/fuzz && ./scripts/generate_corpus.sh

# Minimize corpus after growth runs (cargo fuzz cmin per target)
cd rust/fuzz && ./scripts/minimize_corpus.sh

# Validate: every Cargo.toml target has seeds, total < 10MB
cd rust/fuzz && ./scripts/validate_corpus.sh
```

**Corpus Size Limit**: Total corpus should remain under 10MB for fast CI smoke tests.

**Regression seeds**: when a crash is found and fixed, commit the minimized
reproducer into `corpus/<target>/` so it is re-tested on every future run.

## CI Integration

### Smoke Tests (PR Validation)
`.github/workflows/fuzz-smoke.yml` runs on every pull request:
- 60 seconds per target (~14 minutes total)
- Starts from the committed seeds in `corpus/<target>/` (cargo-fuzz's default
  corpus path) instead of cold-starting from random bytes
- Catches fuzzing regressions before merge
- Uploads crash artifacts on failure

```bash
# Simulate CI smoke tests locally
cd rust && make fuzz-quick
```

### Deep Fuzzing (Production Validation)
Run before releases or periodically:
```bash
# 8 hours per target (production-grade validation)
cd rust && make fuzz-deep TARGET=encryption_key_derivation
```

## Crash Triage

### Automated Crash Analysis
```bash
# Triage crashes: deduplicate, minimize, generate regression tests
cd rust/fuzz && ./scripts/triage_crashes.sh

# Output: deduplicated_crashes.txt with stack traces and minimized inputs
```

**Workflow:**
1. Find all crash artifacts in `artifacts/`
2. Extract and hash stack traces for deduplication
3. Minimize unique crashes with `cargo fuzz cmin`
4. Generate regression test templates (`#[test] #[should_panic]`)

### Manual Crash Reproduction
```bash
# Reproduce specific crash
cargo fuzz run byte_storage_corrupted_envelope artifacts/crash-xyz

# Minimize crash input
cargo fuzz cmin byte_storage_corrupted_envelope
```

## AFL++ Fuzzing (Alternative Engine)

AFL++ provides mutation-based fuzzing complementary to libfuzzer's coverage-guided approach:

```bash
# Build AFL++ target
cd rust && cargo afl build --features afl

# Run AFL++ fuzzer
cd rust && make fuzz-afl TARGET=byte_storage_corrupted_envelope
```

## Coverage Reporting

```bash
# Generate LLVM coverage report
cd rust && make fuzz-coverage

# View HTML report
open rust/fuzz/coverage/html/index.html
```

**Note**: Fuzzing coverage measures code paths explored, not test quality. Use this to identify untested paths.

## Troubleshooting

### Common Issues

**"cargo-fuzz not found"**
```bash
cargo install cargo-fuzz
```

**"Corpus too large (>10MB)"**
```bash
cd rust/fuzz && ./scripts/minimize_corpus.sh
```

**"Fuzzing too slow"**
- Reduce input size limits in targets (e.g., `if data.len() > 1024 { return; }`)
- Use `fuzz-quick` for rapid iteration
- Check for expensive operations in hot paths

**"Crash can't be reproduced"**
```bash
# Ensure same libfuzzer version
cargo fuzz run TARGET artifacts/crash-xyz -- -exact_artifact_path=artifacts/crash-xyz
```

### Performance Tips

1. **Start small**: Use `fuzz-quick` to verify targets compile and run
2. **Parallelize**: Run multiple targets simultaneously on multi-core machines
3. **Seed wisely**: Good corpus seeds improve coverage 10-100x faster
4. **Monitor coverage**: Use `fuzz-coverage` to identify unexplored paths

## Architecture Notes

### Why Separate Targets?

Each target focuses on **one specific attack vector** for maximum coverage depth:
- Deep exploration of single attack surface (vs shallow exploration of many)
- Clear failure attribution (which attack succeeded?)
- Independent corpus optimization per attack type
- Easier crash triage and minimization

### Fail-Closed Security Model

All fuzz targets enforce fail-closed behavior:
- **Malicious input** → Safe rejection (`Err(...)`, no panic)
- **Panic** = Test failure (fuzzer reports bug)
- **Undefined behavior** = Instant failure (sanitizers catch)

### Multi-Engine Strategy

- **Libfuzzer** (default): Coverage-guided, fast iteration, LLVM sanitizers
- **AFL++**: Mutation-based, finds different bug classes, mature tooling
- Both engines share corpus for cross-pollination

## Contributing

When adding new fuzz targets:
1. Follow naming convention: `<module>_<attack_vector>.rs`
2. Add target to `Cargo.toml` [[bin]] section — the Makefile, CI, and corpus
   scripts all derive the target list from these stanzas; there is no separate
   list to update
3. Add seeds for the target to `scripts/generate_corpus.sh` and run it —
   `validate_corpus.sh` fails until `corpus/<target>/` has seeds
4. Document attack vector in this README
5. Verify with `make fuzz-target TARGET=your_new_target`

## References

- [Rust Fuzz Book](https://rust-fuzz.github.io/book/)
- [libfuzzer documentation](https://llvm.org/docs/LibFuzzer.html)
- [AFL++ documentation](https://aflplus.plus/)
- [cachekit security architecture](../../SECURITY.md)
