#!/usr/bin/env bash
set -euo pipefail

# Corpus validation script for cachekit fuzzing
# Reports corpus health metrics and validates integrity

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FUZZ_DIR="$(dirname "$SCRIPT_DIR")"
CORPUS_DIR="$FUZZ_DIR/corpus"

echo "=== Cachekit Fuzzing Corpus Validator ==="
echo "Corpus directory: $CORPUS_DIR"
echo ""

# Check if corpus directory exists
if [ ! -d "$CORPUS_DIR" ]; then
    echo "ERROR: Corpus directory does not exist: $CORPUS_DIR"
    exit 1
fi

# Validate single category
validate_category() {
    local category=$1
    local category_path="$CORPUS_DIR/$category"

    if [ ! -d "$category_path" ]; then
        echo "⏭️  Skipping $category (directory not found)"
        return
    fi

    echo "📊 Category: $category"

    # Count files (excluding .gitkeep)
    local file_count=$(find "$category_path" -type f ! -name '.gitkeep' | wc -l | tr -d ' ')
    local dir_count=$(find "$category_path" -mindepth 1 -type d | wc -l | tr -d ' ')

    # Get size metrics
    local total_size=$(du -sh "$category_path" 2>/dev/null | cut -f1 || echo "0B")
    local total_bytes=$(du -sb "$category_path" 2>/dev/null | cut -f1 || echo "0")

    # Calculate average file size
    local avg_size="N/A"
    if [ "$file_count" -gt 0 ] && [ "$total_bytes" -gt 0 ]; then
        avg_size=$(awk "BEGIN {printf \"%.1f\", $total_bytes / $file_count / 1024}")
        avg_size="${avg_size}KB"
    fi

    echo "  Files: $file_count"
    echo "  Subdirectories: $dir_count"
    echo "  Total size: $total_size"
    echo "  Average file size: $avg_size"

    # Validate file integrity
    if [ "$file_count" -gt 0 ]; then
        # Check for empty files
        local empty_count=$(find "$category_path" -type f ! -name '.gitkeep' -size 0 | wc -l | tr -d ' ')
        if [ "$empty_count" -gt 0 ]; then
            echo "  ⚠️  Warning: $empty_count empty files found"
        fi

        # Check for very large files (>1MB)
        local large_files=$(find "$category_path" -type f ! -name '.gitkeep' -size +1M)
        if [ -n "$large_files" ]; then
            local large_count=$(echo "$large_files" | wc -l | tr -d ' ')
            echo "  ⚠️  Warning: $large_count files exceed 1MB"
            echo "$large_files" | while read -r file; do
                local size=$(du -sh "$file" | cut -f1)
                echo "      - $(basename "$file"): $size"
            done
        fi

        # List subdirectories with counts
        if [ "$dir_count" -gt 0 ]; then
            echo "  Subdirectory breakdown:"
            find "$category_path" -mindepth 1 -maxdepth 1 -type d | while read -r subdir; do
                local sub_count=$(find "$subdir" -type f ! -name '.gitkeep' | wc -l | tr -d ' ')
                local sub_size=$(du -sh "$subdir" 2>/dev/null | cut -f1 || echo "0B")
                echo "    - $(basename "$subdir"): $sub_count files, $sub_size"
            done
        fi
    else
        echo "  ℹ️  Empty corpus (run generate_corpus.sh to populate)"
    fi

    echo ""
}

# Main execution
main() {
    echo "=== Corpus Structure Validation ==="
    echo ""

    # Validate each major category
    validate_category "byte_storage"
    validate_category "encryption"
    validate_category "integration"

    # Overall corpus statistics
    echo "=== Overall Corpus Statistics ==="
    local total_files=$(find "$CORPUS_DIR" -type f ! -name '.gitkeep' | wc -l | tr -d ' ')
    local total_size=$(du -sh "$CORPUS_DIR" 2>/dev/null | cut -f1 || echo "0B")
    local total_mb=$(du -sm "$CORPUS_DIR" 2>/dev/null | cut -f1 || echo "0")

    echo "Total files: $total_files"
    echo "Total size: $total_size (${total_mb}MB)"
    echo ""

    # Validate against 10MB target
    if [ "$total_mb" -gt 10 ]; then
        echo "❌ VALIDATION FAILED: Corpus size (${total_mb}MB) exceeds 10MB target"
        echo "   Run minimize_corpus.sh to reduce size"
        exit 1
    elif [ "$total_mb" -gt 8 ]; then
        echo "⚠️  WARNING: Corpus size (${total_mb}MB) is approaching 10MB limit"
        echo "   Consider running minimize_corpus.sh"
    else
        echo "✅ VALIDATION PASSED: Corpus size (${total_mb}MB) is within 10MB target"
    fi

    # Check for recommended minimum corpus size
    if [ "$total_files" -lt 10 ]; then
        echo "⚠️  WARNING: Corpus has only $total_files files (recommend 50+)"
        echo "   Run generate_corpus.sh to populate corpus"
    else
        echo "✅ Corpus has sufficient samples ($total_files files)"
    fi

    echo ""
    echo "=== Validation Complete ==="
}

# Usage information
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    echo "Usage: $0"
    echo ""
    echo "Validate fuzzing corpus integrity and report health metrics."
    echo ""
    echo "Checks:"
    echo "  - Corpus size (must be < 10MB for CI)"
    echo "  - File counts per category"
    echo "  - Empty files"
    echo "  - Oversized files (> 1MB)"
    echo "  - Directory structure"
    exit 0
fi

main "$@"
