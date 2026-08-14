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

# Targets derived from the [[bin]] stanzas in Cargo.toml — the same source
# `cargo fuzz list`, the Makefile, and validate_corpus.sh read. A
# hand-maintained copy here is the drift hazard that buried the original
# corpus (LAB-1149).
mapfile -t FUZZ_TARGETS < <(grep -A1 '^\[\[bin\]\]' "$FUZZ_DIR/Cargo.toml" | sed -n 's/^name = "\(.*\)"/\1/p')
if [ "${#FUZZ_TARGETS[@]}" -eq 0 ]; then
    echo "ERROR: no [[bin]] targets found in $FUZZ_DIR/Cargo.toml"
    exit 1
fi

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

    # Run corpus minimization — a failed cmin fails the script (targets are
    # derived from Cargo.toml, so "target may not exist" is a real error).
    cd "$FUZZ_DIR"
    if ! cargo fuzz cmin "$target"; then
        echo "❌ Minimization failed for $target"
        exit 1
    fi

    local after_count=$(find "$corpus_path" -type f ! -name '.gitkeep' | wc -l | tr -d ' ')
    local after_size=$(du -sh "$corpus_path" 2>/dev/null | cut -f1 || echo "0B")
    local removed=$((before_count - after_count))
    echo "✅ Minimized $target: ${before_count} → ${after_count} files (-${removed}), ${before_size} → ${after_size}"
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
