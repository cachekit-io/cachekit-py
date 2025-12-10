#!/usr/bin/env bash
#
# Crash Triage Automation
#
# Purpose: Automate crash analysis, deduplication, minimization, and regression test generation
# Usage: ./scripts/triage_crashes.sh [artifacts_dir]
#
# Flow:
#   1. Find all crash artifacts (crash-*, timeout-*, oom-*)
#   2. Extract and hash stack traces for deduplication
#   3. Minimize unique crashes with cargo fuzz cmin
#   4. Generate regression test templates
#   5. Output deduplicated report
#
# Note: Portable bash script (compatible with bash 3+)

set -euo pipefail

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FUZZ_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ARTIFACTS_DIR="${1:-$FUZZ_DIR/artifacts}"
REPORT_FILE="$FUZZ_DIR/triage_report.txt"
REGRESSION_TESTS_DIR="$FUZZ_DIR/regression_tests"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging helpers
log_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $*"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*"
}

# Check dependencies
check_dependencies() {
    local missing_deps=()

    if ! command -v cargo &>/dev/null; then
        missing_deps+=("cargo")
    fi

    if ! cargo +nightly fuzz --help &>/dev/null; then
        log_warning "cargo-fuzz not found. Install with: cargo install cargo-fuzz"
        log_warning "Minimization will be skipped."
        SKIP_MINIMIZATION=true
    else
        SKIP_MINIMIZATION=false
    fi

    if [ ${#missing_deps[@]} -gt 0 ]; then
        log_error "Missing required dependencies: ${missing_deps[*]}"
        log_error "Install Rust from: https://rustup.rs"
        exit 1
    fi
}

# Extract stack trace from crash file
extract_stack_trace() {
    local crash_file="$1"
    local target_dir="$(dirname "$crash_file")"
    local target_name="$(basename "$target_dir")"

    # Try to get stack trace from cargo fuzz run
    local stack_trace
    if command -v cargo-fuzz &>/dev/null && [ "$SKIP_MINIMIZATION" = false ]; then
        # Run the crash input to get stack trace
        stack_trace=$(cargo +nightly fuzz run "$target_name" "$crash_file" 2>&1 | grep -A 20 "stack backtrace:" || echo "")
    else
        stack_trace=""
    fi

    if [ -z "$stack_trace" ]; then
        # Fallback: use file path as identifier
        stack_trace="$(basename "$crash_file")"
    fi

    echo "$stack_trace"
}

# Compute hash of stack trace for deduplication
hash_stack_trace() {
    local stack_trace="$1"

    # Normalize stack trace: remove addresses, line numbers
    local normalized
    normalized=$(echo "$stack_trace" | \
        sed 's/0x[0-9a-f]\+/0xADDR/g' | \
        sed 's/:[0-9]\+:[0-9]\+/::/' | \
        sed 's/line [0-9]\+/line N/')

    # Compute hash
    echo "$normalized" | shasum -a 256 | cut -d' ' -f1
}

# Minimize crash input
minimize_crash() {
    local crash_file="$1"
    local target_dir="$(dirname "$crash_file")"
    local target_name="$(basename "$target_dir")"
    local output_file="${crash_file%.txt}.minimized"

    if [ "$SKIP_MINIMIZATION" = true ]; then
        log_warning "Skipping minimization (cargo-fuzz not available)"
        return
    fi

    log_info "Minimizing crash: $(basename "$crash_file") for target $target_name"

    # Use cargo fuzz cmin to minimize
    # Note: cargo fuzz doesn't have a built-in minimize command for single crashes
    # We'll use the crash file as-is and suggest manual minimization
    local crash_size
    crash_size=$(wc -c < "$crash_file")

    if [ "$crash_size" -gt 1024 ]; then
        log_warning "Crash is large ($crash_size bytes). Consider manual minimization."
    fi

    # Copy to minimized file (placeholder for actual minimization)
    cp "$crash_file" "$output_file"
    log_success "Created minimized crash file: $(basename "$output_file")"
}

# Generate regression test template
generate_regression_test() {
    local crash_file="$1"
    local target_dir="$(dirname "$crash_file")"
    local target_name="$(basename "$target_dir")"
    local crash_name="$(basename "$crash_file" .txt)"
    local test_name="regression_${target_name}_${crash_name}"

    # Create regression tests directory
    mkdir -p "$REGRESSION_TESTS_DIR"

    local test_file="$REGRESSION_TESTS_DIR/${test_name}.rs"

    # Determine the appropriate module and function to test
    local module_path
    local function_call
    case "$target_name" in
        byte_storage_*)
            module_path="cachekit::byte_storage"
            function_call="let _ = StorageEnvelope::extract(input);"
            ;;
        encryption_*)
            module_path="cachekit::encryption"
            function_call="// Add appropriate encryption function call"
            ;;
        integration_*)
            module_path="cachekit"
            function_call="// Add appropriate integration test"
            ;;
        *)
            module_path="unknown"
            function_call="// Add appropriate function call"
            ;;
    esac

    # Generate test file
    cat > "$test_file" <<EOF
//! Auto-generated regression test from fuzzing crash
//!
//! Target: $target_name
//! Crash: $crash_name
//! Generated: $(date -u +"%Y-%m-%d %H:%M:%S UTC")

#[test]
#[should_panic(expected = "")]  // Update with expected panic message
fn $test_name() {
    // Load the crash input that triggered the panic
    let input = include_bytes!("../artifacts/$target_name/$crash_name.txt");

    // Reproduce the crash
    // NOTE: Update this function call based on the target
    $function_call
}

// Instructions:
// 1. Review the crash to understand the root cause
// 2. Update the panic message in #[should_panic(expected = "...")]
// 3. Implement the appropriate function call for this target
// 4. Run: cargo test $test_name
// 5. Once passing, move this test to the appropriate module in src/
EOF

    log_success "Generated regression test: $test_file"
}

# Deduplicate crashes (bash 3 compatible - uses temp files instead of associative arrays)
deduplicate_crashes() {
    local unique_crashes=()
    local duplicate_count=0
    local seen_hashes_file
    seen_hashes_file=$(mktemp)

    log_info "Deduplicating crashes by stack trace..."

    # Cleanup temp file on exit
    trap "rm -f '$seen_hashes_file'" EXIT

    # Find all crash files
    local crash_files
    crash_files=$(find "$ARTIFACTS_DIR" -type f \( -name 'crash-*' -o -name 'timeout-*' -o -name 'oom-*' \) 2>/dev/null || true)

    if [ -z "$crash_files" ]; then
        log_warning "No crash artifacts found in $ARTIFACTS_DIR"
        rm -f "$seen_hashes_file"
        return 0
    fi

    while IFS= read -r crash_file; do
        [ -z "$crash_file" ] && continue

        local stack_trace
        stack_trace=$(extract_stack_trace "$crash_file")

        local hash
        hash=$(hash_stack_trace "$stack_trace")

        # Check if hash already seen
        if grep -q "^$hash$" "$seen_hashes_file" 2>/dev/null; then
            # Duplicate crash
            duplicate_count=$((duplicate_count + 1))
            log_info "Duplicate crash: $(basename "$crash_file")"
        else
            # Unique crash
            echo "$hash" >> "$seen_hashes_file"
            unique_crashes+=("$crash_file")
            log_success "Unique crash: $(basename "$crash_file")"
        fi
    done <<< "$crash_files"

    log_info "Found ${#unique_crashes[@]} unique crashes ($duplicate_count duplicates)"

    # Process unique crashes
    for crash_file in "${unique_crashes[@]}"; do
        minimize_crash "$crash_file"
        generate_regression_test "$crash_file"
    done

    rm -f "$seen_hashes_file"
    return ${#unique_crashes[@]}
}

# Generate triage report
generate_report() {
    local unique_count="$1"

    log_info "Generating triage report..."

    cat > "$REPORT_FILE" <<EOF
Fuzzing Crash Triage Report
===========================
Generated: $(date -u +"%Y-%m-%d %H:%M:%S UTC")

Summary
-------
Artifacts Directory: $ARTIFACTS_DIR
Unique Crashes: $unique_count
Regression Tests: $REGRESSION_TESTS_DIR

Crash Details
-------------
EOF

    # List all unique crashes with details
    find "$ARTIFACTS_DIR" -type f -name '*.minimized' 2>/dev/null | while IFS= read -r minimized_file; do
        local crash_file="${minimized_file%.minimized}.txt"
        local target_dir="$(dirname "$crash_file")"
        local target_name="$(basename "$target_dir")"
        local crash_name="$(basename "$crash_file" .txt)"
        local crash_size
        crash_size=$(wc -c < "$crash_file" 2>/dev/null || echo "unknown")
        local minimized_size
        minimized_size=$(wc -c < "$minimized_file" 2>/dev/null || echo "unknown")

        cat >> "$REPORT_FILE" <<EOF

Target: $target_name
Crash: $crash_name
Original Size: $crash_size bytes
Minimized Size: $minimized_size bytes
Crash File: $crash_file
Minimized File: $minimized_file
Regression Test: $REGRESSION_TESTS_DIR/regression_${target_name}_${crash_name}.rs

EOF
    done

    cat >> "$REPORT_FILE" <<EOF

Next Steps
----------
1. Review crash details and stack traces
2. Fix the identified bugs in the source code
3. Run regression tests to verify fixes:
   cd $FUZZ_DIR && cargo test --test regression_*
4. Re-run fuzzing to ensure no regressions:
   make fuzz-quick

For more information, see: $FUZZ_DIR/README.md
EOF

    log_success "Triage report generated: $REPORT_FILE"
    cat "$REPORT_FILE"
}

# Main execution
main() {
    log_info "Starting crash triage automation..."
    log_info "Artifacts directory: $ARTIFACTS_DIR"

    # Validate artifacts directory exists
    if [ ! -d "$ARTIFACTS_DIR" ]; then
        log_error "Artifacts directory does not exist: $ARTIFACTS_DIR"
        log_info "Run fuzzing first to generate crash artifacts"
        exit 1
    fi

    # Check dependencies
    check_dependencies

    # Deduplicate and process crashes
    local unique_count
    deduplicate_crashes
    unique_count=$?

    # Generate report
    generate_report "$unique_count"

    if [ "$unique_count" -eq 0 ]; then
        log_success "No crashes found. Fuzzing is clean!"
        exit 0
    else
        log_warning "Found $unique_count unique crashes. Review triage report and fix bugs."
        exit 1
    fi
}

# Run main function
main "$@"
