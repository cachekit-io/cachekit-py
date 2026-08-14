#!/usr/bin/env bash
set -euo pipefail

# Corpus validation for cachekit fuzzing.
#
# The corpus contract: one seed directory per [[bin]] target in Cargo.toml,
# at corpus/<target>/ — the layout `cargo fuzz run <target>` loads by default.
# This script fails if any target has no seeds (the drift that buried the
# original corpus: LAB-1149) or if the total exceeds the 10MB CI budget.

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

# The grep/sed above silently drops a stanza whose `name` line drifts from the
# exact expected format — and a dropped target would stop requiring seeds here
# while still fuzzing (cold) in CI. Catch that directly: every [[bin]] stanza
# must yield exactly one name. Comparing counts against fuzz_targets/*.rs
# cannot do this job — a shared helper module in that directory is not a target
# and would fail the check spuriously, while a duplicated or misspelled name
# keeps the count equal and slips through.
STANZA_COUNT=$(grep -c '^\[\[bin\]\]' "$FUZZ_DIR/Cargo.toml" || true)
NAME_COUNT=$(printf '%s\n' "$TARGETS" | grep -c . || true)
if [ "$NAME_COUNT" -ne "$STANZA_COUNT" ]; then
    echo "ERROR: Cargo.toml has $STANZA_COUNT [[bin]] stanzas but only $NAME_COUNT yielded a name"
    echo "       — a stanza's name line no longer matches 'name = \"…\"' on the line directly after [[bin]]"
    exit 1
fi

# Duplicate names would make one target silently shadow another's seed check.
DUPES=$(printf '%s\n' "$TARGETS" | sort | uniq -d)
if [ -n "$DUPES" ]; then
    echo "ERROR: duplicate [[bin]] names in Cargo.toml:"
    printf '       - %s\n' "$DUPES"
    exit 1
fi

# Each declared target must have a source file. Extra .rs files are fine —
# they are helper modules, not targets.
missing_src=""
for target in $TARGETS; do
    [ -f "$FUZZ_DIR/fuzz_targets/$target.rs" ] || missing_src="$missing_src $target"
done
if [ -n "$missing_src" ]; then
    echo "ERROR: [[bin]] targets with no fuzz_targets/<name>.rs source:"
    for t in $missing_src; do echo "       - $t"; done
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

    file_count=$(find "$target_dir" -type f | wc -l | tr -d ' ')
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
total_files=$(find "$CORPUS_DIR" -type f ! -name 'CORPUS_INFO.md' | wc -l | tr -d ' ')
total_size=$(du -sh "$CORPUS_DIR" 2>/dev/null | cut -f1 || echo "0B")
total_mb=$(du -sm "$CORPUS_DIR" 2>/dev/null | cut -f1 || echo "0")
echo "Total seeds: $total_files"
echo "Total size: $total_size (${total_mb}MB)"
echo ""

if [ "$total_mb" -gt 10 ]; then
    echo "❌ VALIDATION FAILED: corpus size (${total_mb}MB) exceeds 10MB CI budget"
    echo "   Run minimize_corpus.sh or drop oversized seeds"
    failures=$((failures + 1))
fi

if [ "$failures" -gt 0 ]; then
    echo "❌ VALIDATION FAILED: $failures problem(s) above"
    exit 1
fi

echo "✅ VALIDATION PASSED: every target has seeds, size within budget"
