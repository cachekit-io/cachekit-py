#!/usr/bin/env bash
set -euo pipefail

# Corpus generation for cachekit fuzzing.
#
# Writes seeds into corpus/<target>/ — one directory per [[bin]] name in
# Cargo.toml, because that is the layout `cargo fuzz run <target>` loads by
# default. (A previous version wrote a category tree, corpus/byte_storage/…,
# which no target ever read: LAB-1149.)
#
# Deterministic: same script version → byte-identical seeds, so re-runs
# produce clean git diffs. Valid StorageEnvelope seeds are byte-exact against
# cachekit-core 0.4.0's wire format (raw LZ4 block + xxHash3-64 big-endian
# checksum + rmp-serde array-form msgpack), verified against the crate's
# pinned empty-input checksum test vector.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FUZZ_DIR="$(dirname "$SCRIPT_DIR")"
CORPUS_DIR="$FUZZ_DIR/corpus"

echo "=== Cachekit Fuzzing Corpus Generator ==="
echo "Corpus directory: $CORPUS_DIR"

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 is required for corpus generation"
    exit 1
fi

# msgpack for envelope framing; lz4 + xxhash to mint envelopes whose checksum
# actually verifies — those are the seeds that reach extract()'s deepest
# branch (checksum check runs AFTER decompression; random bytes never get
# there). With uv: uv run --no-project --with lz4 --with xxhash --with msgpack …
for mod in msgpack lz4.block xxhash; do
    if ! python3 -c "import $mod" 2>/dev/null; then
        echo "ERROR: Python module '$mod' is required"
        echo "Install with: pip install msgpack lz4 xxhash"
        echo "Or run via uv: uv run --no-project --with msgpack --with lz4 --with xxhash bash scripts/generate_corpus.sh"
        exit 1
    fi
done

python3 - "$CORPUS_DIR" <<'PYTHON'
import random
import sys
from pathlib import Path

import lz4.block
import msgpack
import xxhash

corpus = Path(sys.argv[1])
# Deterministic PRNG: reproducible corpus, clean diffs on regeneration.
rng = random.Random(0xCAC4E)

written = {}


def seed(target: str, name: str, data: bytes) -> None:
    d = corpus / target
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    written[target] = written.get(target, 0) + 1


def envelope(data: bytes, fmt: str = "msgpack") -> bytes:
    """Byte-exact StorageEnvelope: what ByteStorage::store() emits for `data`.

    rmp-serde array form: [bin(compressed), [8 checksum ints], u32 size, str].
    compressed_data is a raw LZ4 block (lz4_flex::compress — no size prefix);
    checksum is xxh3_64(data).to_be_bytes().
    """
    compressed = lz4.block.compress(data, store_size=False)
    checksum = list(xxhash.xxh3_64(data).digest())  # digest() is big-endian
    return msgpack.packb([compressed, checksum, len(data), fmt], use_bin_type=True)


# ── Shared payloads ─────────────────────────────────────────────────────────
text = b"Hello, cachekit! MessagePack envelope roundtrip seed."
compressible = b"a" * 2048
gradient = bytes(range(256)) * 4
random_1k = rng.randbytes(1024)
key32 = rng.randbytes(32)

# ── byte_storage_compress: raw plaintext in, store()/retrieve() roundtrip ──
seed("byte_storage_compress", "empty.bin", b"")
seed("byte_storage_compress", "single_byte.bin", b"a")
seed("byte_storage_compress", "text.bin", text)
seed("byte_storage_compress", "compressible.bin", compressible)
seed("byte_storage_compress", "gradient.bin", gradient)
seed("byte_storage_compress", "random_1k.bin", random_1k)

# ── byte_storage_decompress + byte_storage_corrupted_envelope:
#    both parse envelope bytes (retrieve() / rmp_serde::from_slice) ─────────
valid_text = envelope(text)
valid_comp = envelope(compressible)

corrupted_payload = bytearray(valid_comp)
corrupted_payload[len(corrupted_payload) // 2] ^= 0xFF  # checksum mismatch

env_samples = {
    "valid_text.bin": valid_text,
    "valid_compressible.bin": valid_comp,
    "valid_gradient.bin": envelope(gradient),
    "valid_random.bin": envelope(random_1k),
    "valid_unicode_format.bin": envelope(text, fmt="utf8_✨"),
    "corrupted_payload.bin": bytes(corrupted_payload),
    "truncated.bin": valid_text[: len(valid_text) // 2],
    "wrong_checksum.bin": msgpack.packb(
        [lz4.block.compress(text, store_size=False), [0] * 8, len(text), "msgpack"],
        use_bin_type=True,
    ),
    "size_mismatch.bin": msgpack.packb(
        [
            lz4.block.compress(text, store_size=False),
            list(xxhash.xxh3_64(text).digest()),
            len(text) + 1,
            "msgpack",
        ],
        use_bin_type=True,
    ),
    "decompression_bomb.bin": msgpack.packb(
        [b"\x00\x01\x02\x03", [0] * 8, 4_294_967_295, "msgpack"],
        use_bin_type=True,
    ),
    "empty_compressed.bin": msgpack.packb(
        [b"", [0] * 8, 100, "msgpack"], use_bin_type=True
    ),
    "missing_fields.bin": msgpack.packb([b"only_one_field"], use_bin_type=True),
    "map_form.bin": msgpack.packb(
        {
            "compressed_data": lz4.block.compress(text, store_size=False),
            "checksum": list(xxhash.xxh3_64(text).digest()),
            "original_size": len(text),
            "format": "msgpack",
        },
        use_bin_type=True,
    ),
}
for name, data in env_samples.items():
    seed("byte_storage_decompress", name, data)
    seed("byte_storage_corrupted_envelope", name, data)

# ── byte_storage_format_injection: raw bytes become the format string ──────
seed("byte_storage_format_injection", "normal.bin", b"msgpack")
seed("byte_storage_format_injection", "path_traversal.bin", b"../../../etc/passwd")
seed("byte_storage_format_injection", "null_byte.bin", b"fmt\x00null")
seed("byte_storage_format_injection", "control_chars.bin", b"fmt\nCRLF\r")
seed("byte_storage_format_injection", "rtl_override.bin", "‮rtl".encode())
seed("byte_storage_format_injection", "bom.bin", "﻿bom".encode())
seed("byte_storage_format_injection", "long_10k.bin", b"x" * 10_000)

# ── Arbitrary-derived targets: seeds are raw bytes consumed by the derive.
#    Exact field mapping is an implementation detail of `arbitrary`, so these
#    are varied starting points, not precision-crafted structs. ─────────────
# byte_storage_integer_overflow: (u32, u8, [u8;8], u8) ≈ 14 bytes
seed("byte_storage_integer_overflow", "zeros.bin", b"\x00" * 14)
seed("byte_storage_integer_overflow", "u32_max.bin", b"\xff\xff\xff\xff" + b"\x10" + b"\xaa" * 8 + b"\x04")
seed("byte_storage_integer_overflow", "boundary_512mb.bin", b"\x00\x00\x00\x20" + b"\xff" + b"\x55" * 8 + b"\x08")
seed("byte_storage_integer_overflow", "random_a.bin", rng.randbytes(14))
seed("byte_storage_integer_overflow", "random_b.bin", rng.randbytes(16))

# byte_storage_checksum_collision: (Vec<u8>, u32, u8)
seed("byte_storage_checksum_collision", "small.bin", rng.randbytes(24))
seed("byte_storage_checksum_collision", "medium.bin", rng.randbytes(256))
seed("byte_storage_checksum_collision", "large.bin", rng.randbytes(1024))
seed("byte_storage_checksum_collision", "compressible.bin", b"a" * 512 + rng.randbytes(8))

# byte_storage_empty_data: (u32, u8, [u8;8]) ≈ 13 bytes
seed("byte_storage_empty_data", "zeros.bin", b"\x00" * 13)
seed("byte_storage_empty_data", "size_no_data.bin", b"\xe8\x03\x00\x00" + b"\x00" + b"\x00" * 8)
seed("byte_storage_empty_data", "data_no_size.bin", b"\x00\x00\x00\x00" + b"\x40" + b"\x11" * 8)
seed("byte_storage_empty_data", "random.bin", rng.randbytes(13))

# ── Encryption targets: input = 32-byte key ++ plaintext ────────────────────
seed("encryption_roundtrip", "key_only.bin", key32)
seed("encryption_roundtrip", "key_text.bin", key32 + text)
seed("encryption_roundtrip", "key_compressible.bin", key32 + compressible)
seed("encryption_roundtrip", "key_random.bin", key32 + rng.randbytes(256))

# encryption_key_derivation: 16-byte master key ++ tenant salt
mk16 = rng.randbytes(16)
seed("encryption_key_derivation", "normal_tenant.bin", mk16 + b"customer-12345")
seed("encryption_key_derivation", "long_tenant.bin", mk16 + b"x" * 4096)

# encryption_nonce_reuse: small plaintexts → 100-iteration uniqueness loop
seed("encryption_nonce_reuse", "key_small.bin", key32 + b"nonce_reuse_seed")
seed("encryption_nonce_reuse", "key_64.bin", key32 + rng.randbytes(64))

# encryption_truncated_ciphertext: total must be in 32..=1024
seed("encryption_truncated_ciphertext", "key_min.bin", key32 + b"tc")
seed("encryption_truncated_ciphertext", "key_200.bin", key32 + rng.randbytes(200))
seed("encryption_truncated_ciphertext", "key_512.bin", key32 + rng.randbytes(512))

# encryption_aad_injection: needs 32-byte key + ≥100 bytes (split plaintext/AAD)
seed("encryption_aad_injection", "key_128.bin", key32 + b"p" * 64 + b"aad_normal" + b"q" * 54)
seed("encryption_aad_injection", "key_nulls.bin", key32 + b"p" * 60 + b"aad\x00null\x00" + b"q" * 50)
seed("encryption_aad_injection", "key_random.bin", key32 + rng.randbytes(1000))

# encryption_large_payload: sizes libFuzzer is slow to grow into on its own
seed("encryption_large_payload", "key_16k_compressible.bin", key32 + b"a" * 16_384)
seed("encryption_large_payload", "key_64k.bin", key32 + rng.randbytes(65_536))
seed("encryption_large_payload", "key_256k_compressible.bin", key32 + b"ab" * 131_072)

# integration_layered_security: 32-byte key + plaintext (total ≥ 64, ≤ 4096+32)
seed("integration_layered_security", "key_text.bin", key32 + text + b" layered." * 4)
seed("integration_layered_security", "key_compressible.bin", key32 + b"a" * 512)
seed("integration_layered_security", "key_random.bin", key32 + rng.randbytes(128))

# Per-target breakdown lives in validate_corpus.sh — the single count surface.
print(f"Wrote {sum(written.values())} seeds across {len(written)} target directories")
PYTHON

echo "=== Corpus Generation Complete ==="
du -sh "$CORPUS_DIR"
