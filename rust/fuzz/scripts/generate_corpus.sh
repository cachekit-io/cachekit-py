#!/usr/bin/env bash
set -euo pipefail

# Corpus generation script for cachekit fuzzing
# Extracts samples from test fixtures and generates edge cases

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FUZZ_DIR="$(dirname "$SCRIPT_DIR")"
CORPUS_DIR="$FUZZ_DIR/corpus"
PROJECT_ROOT="$(cd "$FUZZ_DIR/../.." && pwd)"

echo "=== Cachekit Fuzzing Corpus Generator ==="
echo "Corpus directory: $CORPUS_DIR"
echo "Project root: $PROJECT_ROOT"

# Cleanup function
cleanup_corpus() {
    local category=$1
    local max_size_mb=${2:-10}

    local total_size=$(du -sm "$CORPUS_DIR/$category" 2>/dev/null | cut -f1 || echo "0")
    if [ "$total_size" -gt "$max_size_mb" ]; then
        echo "WARNING: $category corpus is ${total_size}MB (limit: ${max_size_mb}MB)"
        echo "Run minimize_corpus.sh to reduce size"
    fi
}

# Generate ByteStorage corpus samples
generate_byte_storage_corpus() {
    echo ""
    echo "=== Generating ByteStorage Corpus ==="

    local valid_dir="$CORPUS_DIR/byte_storage/valid_envelopes"
    local corrupted_dir="$CORPUS_DIR/byte_storage/corrupted_envelopes"
    local size_dir="$CORPUS_DIR/byte_storage/size_edge_cases"
    local format_dir="$CORPUS_DIR/byte_storage/format_strings"

    # Create valid envelope samples using Python
    echo "Creating valid MessagePack envelopes..."
    python3 - "$valid_dir" <<'PYTHON'
import sys
import msgpack
from pathlib import Path

corpus_dir = Path(sys.argv[1])
corpus_dir.mkdir(parents=True, exist_ok=True)

# Sample 1: Minimal valid envelope
envelope = {
    "format": "msgpack",
    "original_size": 13,
    "compressed_data": b"hello, world!",
    "checksum": b"\x00" * 32,  # Placeholder Blake3 checksum
}
(corpus_dir / "minimal.msgpack").write_bytes(msgpack.packb(envelope))

# Sample 2: Larger payload
envelope = {
    "format": "json",
    "original_size": 1000,
    "compressed_data": b"x" * 1000,
    "checksum": b"\x00" * 32,
}
(corpus_dir / "large.msgpack").write_bytes(msgpack.packb(envelope))

# Sample 3: Unicode format string
envelope = {
    "format": "utf8_text_\u2728",
    "original_size": 50,
    "compressed_data": b"unicode test data" * 3,
    "checksum": b"\xff" * 32,
}
(corpus_dir / "unicode.msgpack").write_bytes(msgpack.packb(envelope))

print(f"Created 3 valid envelope samples in {corpus_dir}")
PYTHON

    # Generate corrupted envelopes
    echo "Creating corrupted envelope samples..."
    python3 - "$corrupted_dir" <<'PYTHON'
import sys
import msgpack
from pathlib import Path

corpus_dir = Path(sys.argv[1])
corpus_dir.mkdir(parents=True, exist_ok=True)

# Corrupted sample 1: Missing fields
(corpus_dir / "missing_fields.msgpack").write_bytes(msgpack.packb({"format": "msgpack"}))

# Corrupted sample 2: Wrong types
envelope = {
    "format": 12345,  # Should be string
    "original_size": "invalid",  # Should be int
    "compressed_data": "not_bytes",  # Should be bytes
    "checksum": b"\x00" * 32,
}
(corpus_dir / "wrong_types.msgpack").write_bytes(msgpack.packb(envelope))

# Corrupted sample 3: Truncated MessagePack
full_envelope = msgpack.packb({
    "format": "msgpack",
    "original_size": 100,
    "compressed_data": b"x" * 100,
    "checksum": b"\x00" * 32,
})
(corpus_dir / "truncated.msgpack").write_bytes(full_envelope[:len(full_envelope)//2])

# Corrupted sample 4: Invalid MessagePack bytes
(corpus_dir / "invalid_msgpack.bin").write_bytes(b"\xff\xff\xff\xff\xff\xff\xff\xff")

print(f"Created 4 corrupted envelope samples in {corpus_dir}")
PYTHON

    # Generate size edge cases
    echo "Creating size edge case samples..."
    python3 - "$size_dir" <<'PYTHON'
import sys
import msgpack
from pathlib import Path

corpus_dir = Path(sys.argv[1])
corpus_dir.mkdir(parents=True, exist_ok=True)

# Edge case 1: Zero size
envelope = {
    "format": "msgpack",
    "original_size": 0,
    "compressed_data": b"",
    "checksum": b"\x00" * 32,
}
(corpus_dir / "zero_size.msgpack").write_bytes(msgpack.packb(envelope))

# Edge case 2: Single byte
envelope = {
    "format": "msgpack",
    "original_size": 1,
    "compressed_data": b"x",
    "checksum": b"\x00" * 32,
}
(corpus_dir / "single_byte.msgpack").write_bytes(msgpack.packb(envelope))

# Edge case 3: u32::MAX original_size (overflow attack)
envelope = {
    "format": "msgpack",
    "original_size": 4294967295,  # u32::MAX
    "compressed_data": b"small",
    "checksum": b"\x00" * 32,
}
(corpus_dir / "u32_max.msgpack").write_bytes(msgpack.packb(envelope))

# Edge case 4: Suspicious compression ratio
envelope = {
    "format": "msgpack",
    "original_size": 10_000_000,  # 10MB claimed
    "compressed_data": b"tiny",  # 4 bytes actual
    "checksum": b"\x00" * 32,
}
(corpus_dir / "suspicious_ratio.msgpack").write_bytes(msgpack.packb(envelope))

print(f"Created 4 size edge case samples in {corpus_dir}")
PYTHON

    # Generate format string samples
    echo "Creating format string injection samples..."
    python3 - "$format_dir" <<'PYTHON'
import sys
import msgpack
from pathlib import Path

corpus_dir = Path(sys.argv[1])
corpus_dir.mkdir(parents=True, exist_ok=True)

malicious_formats = [
    "../../../etc/passwd",  # Path traversal
    "fmt\x00null",  # Null byte
    "fmt\nCRLF\r",  # Control characters
    "\u202ERTL",  # Unicode RTL override
    "x" * 10000,  # Very long string
]

for i, fmt in enumerate(malicious_formats):
    envelope = {
        "format": fmt,
        "original_size": 100,
        "compressed_data": b"x" * 100,
        "checksum": b"\x00" * 32,
    }
    (corpus_dir / f"injection_{i}.msgpack").write_bytes(msgpack.packb(envelope))

print(f"Created {len(malicious_formats)} format injection samples in {corpus_dir}")
PYTHON

    cleanup_corpus "byte_storage" 5
}

# Generate Encryption corpus samples
generate_encryption_corpus() {
    echo ""
    echo "=== Generating Encryption Corpus ==="

    local key_dir="$CORPUS_DIR/encryption/key_material"
    local tenant_dir="$CORPUS_DIR/encryption/tenant_ids"
    local aad_dir="$CORPUS_DIR/encryption/aad_patterns"
    local ciphertext_dir="$CORPUS_DIR/encryption/ciphertext_samples"

    # Generate key material samples
    echo "Creating key material samples..."
    mkdir -p "$key_dir"
    dd if=/dev/urandom of="$key_dir/valid_32byte.key" bs=32 count=1 2>/dev/null
    dd if=/dev/urandom of="$key_dir/zeros.key" bs=32 count=1 2>/dev/null </dev/zero
    dd if=/dev/urandom of="$key_dir/ones.key" bs=32 count=1 2>/dev/null | tr '\0' '\377'
    echo "Created 3 key material samples"

    # Generate tenant ID samples
    echo "Creating tenant ID samples..."
    mkdir -p "$tenant_dir"
    echo -n "customer-12345" > "$tenant_dir/normal.txt"
    echo -n "../../../admin" > "$tenant_dir/path_traversal.txt"
    printf "tenant\x00null" > "$tenant_dir/null_byte.bin"
    echo -n "tenant$(printf '\n')CRLF$(printf '\r')" > "$tenant_dir/control_chars.txt"
    python3 -c "print('\uFEFF' + 'BOM_tenant', end='')" > "$tenant_dir/unicode_bom.txt"
    python3 -c "print('x' * 10000, end='')" > "$tenant_dir/very_long.txt"
    echo "Created 6 tenant ID samples"

    # Generate AAD pattern samples
    echo "Creating AAD pattern samples..."
    mkdir -p "$aad_dir"
    echo -n "cache_key_12345" > "$aad_dir/normal.txt"
    printf "aad\x00null" > "$aad_dir/null_byte.bin"
    printf "aad\n\r\t" > "$aad_dir/control_chars.bin"
    python3 -c "print('a' * 10000, end='')" > "$aad_dir/very_long.txt"
    echo -n "" > "$aad_dir/empty.txt"
    echo "Created 5 AAD pattern samples"

    # Generate ciphertext samples (using Python)
    echo "Creating ciphertext samples..."
    python3 - "$ciphertext_dir" <<'PYTHON'
import sys
from pathlib import Path

corpus_dir = Path(sys.argv[1])
corpus_dir.mkdir(parents=True, exist_ok=True)

# Sample 1: Valid-looking ciphertext structure (12-byte nonce + data + 16-byte tag)
nonce = b"\x00" * 12
ciphertext_body = b"encrypted_data_here"
auth_tag = b"\xff" * 16
(corpus_dir / "valid_structure.bin").write_bytes(nonce + ciphertext_body + auth_tag)

# Sample 2: Truncated nonce
(corpus_dir / "truncated_nonce.bin").write_bytes(b"\x00" * 6)

# Sample 3: Missing auth tag
(corpus_dir / "missing_auth_tag.bin").write_bytes(b"\x00" * 12 + b"data")

# Sample 4: Empty ciphertext
(corpus_dir / "empty.bin").write_bytes(b"")

print(f"Created 4 ciphertext samples in {corpus_dir}")
PYTHON

    cleanup_corpus "encryption" 3
}

# Generate Integration corpus samples
generate_integration_corpus() {
    echo ""
    echo "=== Generating Integration Corpus ==="

    local layered_dir="$CORPUS_DIR/integration/layered_data"
    mkdir -p "$layered_dir"

    echo "Creating layered (compressed + encrypted) samples..."
    python3 - "$layered_dir" <<'PYTHON'
import sys
import msgpack
from pathlib import Path

corpus_dir = Path(sys.argv[1])
corpus_dir.mkdir(parents=True, exist_ok=True)

# Sample 1: Valid envelope that could be encrypted
envelope = {
    "format": "msgpack",
    "original_size": 100,
    "compressed_data": b"x" * 100,
    "checksum": b"\x00" * 32,
}
envelope_bytes = msgpack.packb(envelope)

# Simulate "encrypted" version (nonce + envelope + tag)
nonce = b"\x00" * 12
auth_tag = b"\xff" * 16
layered = nonce + envelope_bytes + auth_tag
(corpus_dir / "valid_layered.bin").write_bytes(layered)

# Sample 2: Corrupted inner envelope
corrupted_envelope = msgpack.packb({"format": "msgpack"})  # Missing fields
layered = b"\x00" * 12 + corrupted_envelope + b"\xff" * 16
(corpus_dir / "corrupted_inner.bin").write_bytes(layered)

print(f"Created 2 integration samples in {corpus_dir}")
PYTHON

    cleanup_corpus "integration" 2
}

# Main execution
main() {
    echo "Starting corpus generation..."

    # Check Python availability
    if ! command -v python3 &> /dev/null; then
        echo "ERROR: python3 is required for corpus generation"
        exit 1
    fi

    # Check msgpack availability
    if ! python3 -c "import msgpack" 2>/dev/null; then
        echo "ERROR: Python msgpack library is required"
        echo "Install with: pip install msgpack"
        exit 1
    fi

    generate_byte_storage_corpus
    generate_encryption_corpus
    generate_integration_corpus

    echo ""
    echo "=== Corpus Generation Complete ==="
    echo "Total corpus size:"
    du -sh "$CORPUS_DIR"

    echo ""
    echo "Next steps:"
    echo "  1. Review corpus: find $CORPUS_DIR -type f ! -name '.gitkeep'"
    echo "  2. Minimize corpus: ./scripts/minimize_corpus.sh"
    echo "  3. Validate corpus: ./scripts/validate_corpus.sh"
}

main "$@"
