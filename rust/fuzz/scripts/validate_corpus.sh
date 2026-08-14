#!/usr/bin/env bash
set -euo pipefail

# Corpus validation for cachekit fuzzing.
#
# The corpus contract: one seed directory per [[bin]] target in Cargo.toml,
# at corpus/<target>/ — the layout `cargo fuzz run <target>` loads by default.
# This script fails if any target has no seeds (the drift that buried the
# original corpus: LAB-1149) or if the total exceeds the 10MB CI budget.

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    echo "Usage: $0"
    echo ""
    echo "Validate the per-target fuzzing corpus."
    echo ""
    echo "Checks:"
    echo "  - Every [[bin]] target in Cargo.toml has a non-empty corpus/<target>/"
    echo "  - Total corpus size within the 10MB CI budget"
    echo "  - Flags individual seeds over 1MB"
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FUZZ_DIR="$(dirname "$SCRIPT_DIR")"
CORPUS_DIR="$FUZZ_DIR/corpus"

echo "=== Cachekit Fuzzing Corpus Validator ==="
echo "Corpus directory: $CORPUS_DIR"
echo ""

if [ ! -d "$CORPUS_DIR" ]; then
    echo "ERROR: Corpus directory does not exist: $CORPUS_DIR"
    exit 1
fi

# Same target derivation as the Makefile and CI: the [[bin]] stanzas are the
# single source of truth, so a new target automatically becomes a required
# corpus directory here.
TARGETS=$(grep -A1 '^\[\[bin\]\]' "$FUZZ_DIR/Cargo.toml" | sed -n 's/^name = "\(.*\)"/\1/p')
if [ -z "$TARGETS" ]; then
    echo "ERROR: no [[bin]] targets found in $FUZZ_DIR/Cargo.toml"
    exit 1
fi

failures=0

for target in $TARGETS; do
    target_dir="$CORPUS_DIR/$target"

    if [ ! -d "$target_dir" ]; then
        echo "❌ $target: corpus/$target/ missing"
        echo "   Run scripts/generate_corpus.sh (and add seeds for new targets to it)"
        failures=$((failures + 1))
        continue
    fi

    file_count=$(find "$target_dir" -type f ! -name '.gitkeep' | wc -l | tr -d ' ')
    if [ "$file_count" -eq 0 ]; then
        echo "❌ $target: corpus/$target/ has no seeds"
        failures=$((failures + 1))
        continue
    fi

    size=$(du -sh "$target_dir" 2>/dev/null | cut -f1 || echo "0B")
    echo "✅ $target: $file_count seeds, $size"

    # Oversized seeds slow every fuzz run that loads them
    large_files=$(find "$target_dir" -type f -size +1M)
    if [ -n "$large_files" ]; then
        echo "   ⚠️  seeds over 1MB:"
        echo "$large_files" | while read -r f; do
            echo "      - $(basename "$f"): $(du -sh "$f" | cut -f1)"
        done
    fi
done

echo ""
echo "=== Overall Corpus Statistics ==="
total_files=$(find "$CORPUS_DIR" -type f ! -name '.gitkeep' ! -name 'CORPUS_INFO.md' | wc -l | tr -d ' ')
total_size=$(du -sh "$CORPUS_DIR" 2>/dev/null | cut -f1 || echo "0B")
total_mb=$(du -sm "$CORPUS_DIR" 2>/dev/null | cut -f1 || echo "0")
echo "Total seeds: $total_files"
echo "Total size: $total_size (${total_mb}MB)"
echo ""

if [ "$total_mb" -gt 10 ]; then
    echo "❌ VALIDATION FAILED: corpus size (${total_mb}MB) exceeds 10MB CI budget"
    echo "   Run minimize_corpus.sh or drop oversized seeds"
    failures=$((failures + 1))
elif [ "$total_mb" -gt 8 ]; then
    echo "⚠️  WARNING: corpus size (${total_mb}MB) is approaching the 10MB budget"
fi

if [ "$failures" -gt 0 ]; then
    echo "❌ VALIDATION FAILED: $failures problem(s) above"
    exit 1
fi

echo "✅ VALIDATION PASSED: every target has seeds, size within budget"
