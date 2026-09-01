# Fuzzing Corpus

Committed seed inputs for the fuzz targets in this crate.

## Layout: one directory per target

```text
corpus/<target-name>/
```

`<target-name>` is the `[[bin]]` name in `Cargo.toml`, because that is the
directory `cargo fuzz run <target>` loads (and grows) by default — no corpus
argument needed, locally or in CI. Any other layout is invisible to the
fuzzer: an earlier category tree (`byte_storage/`, `encryption/`, …) sat here
for months without a single target ever reading it (LAB-1149).

No file counts or sizes are recorded here — a written-down count is stale the
day someone adds a seed. For live numbers run, from the repository root (the
scripts resolve their paths relative to `rust/fuzz/`, one level up from this
file):

```bash
cd rust/fuzz
./scripts/validate_corpus.sh
```

It fails if any `[[bin]]` target lacks seeds or the corpus exceeds the 10MB
CI budget.

## Where seeds come from

- **Generated**: `scripts/generate_corpus.sh` writes a deterministic seed set
  per target, shaped to each target's input format (e.g. `32-byte key ++
  plaintext` for encryption targets). Valid `StorageEnvelope` seeds are
  byte-exact against cachekit-core's wire format — raw LZ4 block, xxHash3-64
  big-endian checksum, rmp-serde array-form MessagePack — so they reach
  `extract()`'s post-decompression branches that random inputs essentially
  never hit (a matching 64-bit checksum is a 2^-64 event).
- **Grown**: local fuzz runs write hash-named discoveries into these
  directories. Commit keepers after `scripts/minimize_corpus.sh`, or discard.
- **Regressions**: when a crash is found and fixed, commit the minimized
  reproducer into the crashing target's directory so the input is re-tested
  on every future run.

## Maintenance

All three below run from the repository root — the `cd rust/fuzz` is part of
the recipe, since the scripts resolve their paths relative to `rust/fuzz/`:

```bash
cd rust/fuzz

# Regenerate the deterministic seed set. Byte-identical under the dependency
# versions pinned at the top of the script; see its header for the caveat.
./scripts/generate_corpus.sh

# Deduplicate / shrink after growth runs (needs a nightly toolchain)
./scripts/minimize_corpus.sh

# Check layout and size budget
./scripts/validate_corpus.sh
```

Keep the total under 10MB so CI seed loading stays cheap.
