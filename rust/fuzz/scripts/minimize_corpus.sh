#!/usr/bin/env bash
set -euo pipefail

# Corpus minimization script for cachekit fuzzing
# Runs cargo fuzz cmin to deduplicate and reduce corpus size

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FUZZ_DIR="$(dirname "$SCRIPT_DIR")"
CORPUS_DIR="$FUZZ_DIR/corpus"

echo "=== Cachekit Fuzzing Corpus Minimizer ==="

# Check cargo-fuzz availability
if ! command -v cargo-fuzz &> /dev/null; then
    echo "ERROR: cargo-fuzz is not installed"
    echo "Install with: cargo install cargo-fuzz"
    exit 1
fi

# All fuzz targets (must match Cargo.toml)
FUZZ_TARGETS=(
    "byte_storage_compress"
    "byte_storage_decompress"
    "encryption_roundtrip"
    "byte_storage_corrupted_envelope"
    "byte_storage_integer_overflow"
    "byte_storage_checksum_collision"
    "byte_storage_empty_data"
    "byte_storage_format_injection"
    "encryption_key_derivation"
    "encryption_nonce_reuse"
    "encryption_truncated_ciphertext"
    "encryption_aad_injection"
    "encryption_large_payload"
    "integration_layered_security"
)

# Function to minimize corpus for a single target
minimize_target() {
    local target=$1
    local corpus_path="$CORPUS_DIR/$target"

    # Check if corpus exists
    if [ ! -d "$corpus_path" ]; then
        echo "⏭️  Skipping $target (no corpus directory)"
        return
    fi

    # Count files before minimization
    local before_count=$(find "$corpus_path" -type f ! -name '.gitkeep' | wc -l | tr -d ' ')
    local before_size=$(du -sh "$corpus_path" 2>/dev/null | cut -f1 || echo "0B")

    if [ "$before_count" -eq 0 ]; then
        echo "⏭️  Skipping $target (empty corpus)"
        return
    fi

    echo ""
    echo "Minimizing $target corpus (${before_count} files, ${before_size})..."

    # Run corpus minimization
    cd "$FUZZ_DIR"
    if cargo fuzz cmin "$target" 2>&1 | grep -E "(Minimizing|files|testcases)"; then
        # Count files after minimization
        local after_count=$(find "$corpus_path" -type f ! -name '.gitkeep' | wc -l | tr -d ' ')
        local after_size=$(du -sh "$corpus_path" 2>/dev/null | cut -f1 || echo "0B")

        local removed=$((before_count - after_count))
        echo "✅ Minimized $target: ${before_count} → ${after_count} files (-${removed}), ${before_size} → ${after_size}"
    else
        echo "⚠️  Minimization failed for $target (target may not exist yet)"
    fi
}

# Main execution
main() {
    local target_filter="${1:-}"

    echo "Corpus directory: $CORPUS_DIR"
    echo ""

    if [ -n "$target_filter" ]; then
        echo "Minimizing single target: $target_filter"
        minimize_target "$target_filter"
    else
        echo "Minimizing all targets..."
        for target in "${FUZZ_TARGETS[@]}"; do
            minimize_target "$target"
        done
    fi

    echo ""
    echo "=== Corpus Minimization Complete ==="
    echo "Total corpus size:"
    du -sh "$CORPUS_DIR"

    # Check if corpus exceeds 10MB
    local total_size_mb=$(du -sm "$CORPUS_DIR" 2>/dev/null | cut -f1 || echo "0")
    if [ "$total_size_mb" -gt 10 ]; then
        echo ""
        echo "⚠️  WARNING: Corpus size (${total_size_mb}MB) exceeds 10MB target"
        echo "Consider removing large files or further minimizing corpus"
    else
        echo "✅ Corpus size (${total_size_mb}MB) is within 10MB target"
    fi
}

# Usage information
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    echo "Usage: $0 [TARGET]"
    echo ""
    echo "Minimize fuzzing corpus to remove redundant test cases."
    echo ""
    echo "Arguments:"
    echo "  TARGET    Optional: minimize specific target only"
    echo ""
    echo "Examples:"
    echo "  $0                                    # Minimize all targets"
    echo "  $0 byte_storage_corrupted_envelope   # Minimize single target"
    exit 0
fi

main "$@"
