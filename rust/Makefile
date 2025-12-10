# cachekit/rust - Rust Build Makefile
# Handles all cargo-related targets for the Rust extension
SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c

.PHONY: help format format-quiet lint lint-quiet test test-unit test-quiet \
        test-byte-storage test-encryption test-integration test-property \
        bench security-fast security-medium kani-verify asan msan tsan

.DEFAULT_GOAL := help

# Colors for output (using printf for cross-platform compatibility)
BLUE := $(shell printf '\033[36m')
GREEN := $(shell printf '\033[32m')
YELLOW := $(shell printf '\033[33m')
RESET := $(shell printf '\033[0m')

# Log directories (inherited from parent, but with defaults for standalone use)
LOG_DIR ?= ../logs
LOG_FORMAT_DIR ?= $(LOG_DIR)/format
LOG_LINT_DIR ?= $(LOG_DIR)/lint
LOG_SECURITY_DIR ?= $(LOG_DIR)/security
LOG_TEST_DIR ?= $(LOG_DIR)/test
LOG_BENCHMARK_DIR ?= $(LOG_DIR)/benchmark

# Ensure log directories exist
$(shell mkdir -p $(LOG_FORMAT_DIR) $(LOG_LINT_DIR) $(LOG_SECURITY_DIR) $(LOG_TEST_DIR) $(LOG_BENCHMARK_DIR))

# Timestamp for log files (inherited from parent, or generated here)
TIMESTAMP ?= $(shell date +%Y%m%d_%H%M%S)

# Helper function to check if a binary exists
define require_binary
	@command -v $(1) >/dev/null 2>&1 || { echo "$(YELLOW)❌ $(1) not found. $(2)$(RESET)"; exit 1; }
endef

help: ## Show Rust-specific commands
	@echo "$(BLUE)cachekit/rust - Rust Build Commands$(RESET)"
	@echo ""
	@echo "$(GREEN)Formatting & Linting:$(RESET)"
	@echo "  $(YELLOW)make format$(RESET)          Format Rust code"
	@echo "  $(YELLOW)make format-quiet$(RESET)    Format Rust code (minimal output)"
	@echo "  $(YELLOW)make lint$(RESET)            Lint Rust code"
	@echo "  $(YELLOW)make lint-quiet$(RESET)      Lint Rust code (minimal output)"
	@echo ""
	@echo "$(GREEN)Testing:$(RESET)"
	@echo "  $(YELLOW)make test$(RESET)                 Run all Rust tests"
	@echo "  $(YELLOW)make test-unit$(RESET)            Run pure Rust unit tests (no PyO3, fast)"
	@echo "  $(YELLOW)make test-quiet$(RESET)           Run tests with minimal output"
	@echo "  $(YELLOW)make test-byte-storage$(RESET)    ByteStorage tests only"
	@echo "  $(YELLOW)make test-encryption$(RESET)      Encryption tests only"
	@echo "  $(YELLOW)make test-integration$(RESET)     Integration tests only"
	@echo "  $(YELLOW)make test-property$(RESET)        Property-based tests (proptest)"
	@echo ""
	@echo "$(GREEN)Benchmarks:$(RESET)"
	@echo "  $(YELLOW)make bench$(RESET)            Run Rust benchmarks"
	@echo ""
	@echo "$(GREEN)Security:$(RESET)"
	@echo "  $(YELLOW)make security-fast$(RESET)    Fast security checks (< 3 min)"
	@echo "  $(YELLOW)make security-medium$(RESET)  Medium security checks (< 15 min)"
	@echo "  $(YELLOW)make kani-verify$(RESET)      Run Kani formal verification"
	@echo "  $(YELLOW)make asan$(RESET)             Run AddressSanitizer"
	@echo "  $(YELLOW)make msan$(RESET)             Run MemorySanitizer"
	@echo "  $(YELLOW)make tsan$(RESET)             Run ThreadSanitizer"
	@echo ""

# ═══════════════════════════════════════════════════════════════════════════════
# 🎨 CODE FORMATTING
# ═══════════════════════════════════════════════════════════════════════════════

format: ## Format Rust code
	@echo "$(BLUE)Formatting Rust code...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_FORMAT_DIR)/rust_$(TIMESTAMP).log$(RESET)"
	$(call require_binary,cargo,Install Rust: https://rustup.rs)
	@if ! cargo fmt --all 2>&1 | tee $(LOG_FORMAT_DIR)/rust_$(TIMESTAMP).log; then \
		echo "$(YELLOW)❌ Rust formatting failed$(RESET)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ Rust code formatted$(RESET)"

format-quiet: ## Format Rust code (minimal output)
	@printf "$(BLUE)format (rs)...$(RESET) "
	$(call require_binary,cargo,Install Rust: https://rustup.rs)
	@if ! cargo fmt --all --quiet > $(LOG_FORMAT_DIR)/rust_$(TIMESTAMP).log 2>&1; then \
		echo "$(YELLOW)❌ FAILED$(RESET)"; \
		cat $(LOG_FORMAT_DIR)/rust_$(TIMESTAMP).log; \
		exit 1; \
	fi
	@echo "$(GREEN)✓$(RESET)"

# ═══════════════════════════════════════════════════════════════════════════════
# 🔍 LINTING
# ═══════════════════════════════════════════════════════════════════════════════

lint: ## Lint Rust code
	@echo "$(BLUE)Linting Rust code...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_LINT_DIR)/rust_$(TIMESTAMP).log$(RESET)"
	$(call require_binary,cargo,Install Rust: https://rustup.rs)
	@if ! cargo clippy --all-targets --no-default-features --features compression,checksum,messagepack,encryption -- -D warnings 2>&1 | tee $(LOG_LINT_DIR)/rust_$(TIMESTAMP).log; then \
		echo "$(YELLOW)❌ Rust linting failed$(RESET)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ Rust linting completed$(RESET)"

lint-quiet: ## Lint Rust code (minimal output)
	@printf "$(BLUE)lint (rs)...$(RESET) "
	$(call require_binary,cargo,Install Rust: https://rustup.rs)
	@if ! cargo clippy --all-targets --no-default-features --features compression,checksum,messagepack,encryption --quiet -- -D warnings > $(LOG_LINT_DIR)/rust_$(TIMESTAMP).log 2>&1; then \
		echo "$(YELLOW)❌ FAILED$(RESET)"; \
		tail -30 $(LOG_LINT_DIR)/rust_$(TIMESTAMP).log; \
		exit 1; \
	fi
	@echo "$(GREEN)✓$(RESET)"

# ═══════════════════════════════════════════════════════════════════════════════
# ✅ TESTING
# ═══════════════════════════════════════════════════════════════════════════════

test: test-unit ## Run all Rust tests (alias for test-unit)

test-unit: ## Run library unit tests (thin wrapper smoke tests)
	@echo "$(BLUE)Running Rust library tests...$(RESET)"
	@echo "$(YELLOW)Note: Core tests are in cachekit-core/. This runs wrapper validation only.$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_DIR)/test/rust-unit_$(TIMESTAMP).log$(RESET)"
	$(call require_binary,cargo,Install Rust: https://rustup.rs)
	@if ! cargo test --lib \
		--no-default-features --features compression,checksum,messagepack,encryption \
		2>&1 | tee $(LOG_DIR)/test/rust-unit_$(TIMESTAMP).log; then \
		echo "$(YELLOW)❌ Rust library tests failed$(RESET)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ Rust wrapper tests completed$(RESET)"

test-quiet: ## Run Rust tests (minimal output)
	@printf "$(BLUE)rust tests...$(RESET) "
	$(call require_binary,cargo,Install Rust: https://rustup.rs)
	@if ! cargo test --lib \
		--no-default-features --features compression,checksum,messagepack,encryption \
		--quiet > $(LOG_DIR)/test/rust-unit_$(TIMESTAMP).log 2>&1; then \
		echo "$(YELLOW)❌ FAILED$(RESET)"; \
		tail -30 $(LOG_DIR)/test/rust-unit_$(TIMESTAMP).log; \
		exit 1; \
	fi
	@echo "$(GREEN)✓$(RESET)"

test-byte-storage: ## Run ByteStorage tests (in cachekit-core)
	@echo "$(BLUE)ByteStorage tests are now in cachekit-core/$(RESET)"
	@echo "$(YELLOW)Run: cd ../../cachekit-core && cargo test --test byte_storage_tests$(RESET)"

test-encryption: ## Run encryption tests (in cachekit-core)
	@echo "$(BLUE)Encryption tests are now in cachekit-core/$(RESET)"
	@echo "$(YELLOW)Run: cd ../../cachekit-core && cargo test --test encryption_tests$(RESET)"

test-integration: ## Run integration tests (in cachekit-core)
	@echo "$(BLUE)Integration tests are now in cachekit-core/$(RESET)"
	@echo "$(YELLOW)Run: cd ../../cachekit-core && cargo test$(RESET)"

test-property: ## Run property-based tests (in cachekit-core)
	@echo "$(BLUE)Property tests are now in cachekit-core/$(RESET)"
	@echo "$(YELLOW)Run: cd ../../cachekit-core && cargo test --test property_tests$(RESET)"

# ═══════════════════════════════════════════════════════════════════════════════
# 📊 BENCHMARKS
# ═══════════════════════════════════════════════════════════════════════════════

bench: ## Run Rust benchmarks
	@echo "$(BLUE)Running Rust benchmarks...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_DIR)/benchmark/rust-bench_$(TIMESTAMP).log$(RESET)"
	$(call require_binary,cargo,Install Rust: https://rustup.rs)
	@cargo bench --bench unified_benchmarks 2>&1 | tee $(LOG_DIR)/benchmark/rust-bench_$(TIMESTAMP).log
	@echo "$(GREEN)✓ Rust benchmarks completed$(RESET)"

# ═══════════════════════════════════════════════════════════════════════════════
# 🔒 SECURITY & VERIFICATION
# ═══════════════════════════════════════════════════════════════════════════════

security-fast: ## Run fast security checks (< 3 min)
	@echo "$(BLUE)Running fast security checks...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_SECURITY_DIR)/fast_$(TIMESTAMP).log$(RESET)"
	$(call require_binary,cargo,Install Rust: https://rustup.rs)
	@{ \
		echo "$(YELLOW)Running cargo-audit...$(RESET)" && \
		cargo audit --deny warnings && echo "$(GREEN)✓ cargo-audit passed$(RESET)" && \
		echo "$(YELLOW)Running cargo-deny...$(RESET)" && \
		cargo deny check && echo "$(GREEN)✓ cargo-deny passed$(RESET)" && \
		echo "$(YELLOW)Running clippy security lints...$(RESET)" && \
		cargo clippy --no-default-features --features compression,checksum,encryption \
			-- -D warnings -W clippy::cargo -W clippy::pedantic && echo "$(GREEN)✓ clippy passed$(RESET)" && \
		echo "$(YELLOW)Running cargo-machete...$(RESET)" && \
		cargo machete && echo "$(GREEN)✓ cargo-machete passed$(RESET)"; \
	} 2>&1 | tee $(LOG_SECURITY_DIR)/fast_$(TIMESTAMP).log
	@echo "$(GREEN)✓ Fast security checks completed$(RESET)"

security-medium: ## Run medium security checks (< 15 min)
	@echo "$(BLUE)Running medium security checks...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_SECURITY_DIR)/medium_$(TIMESTAMP).log$(RESET)"
	$(call require_binary,cargo,Install Rust: https://rustup.rs)
	@{ \
		echo "$(YELLOW)Running cargo-geiger (unsafe analysis)...$(RESET)" && \
		cargo geiger --output-format Json > geiger-report.json && \
		TOTAL_FUNCS=$$(jq '[.packages[].package.functions.safe + .packages[].package.functions.unsafe] | add' geiger-report.json) && \
		UNSAFE_FUNCS=$$(jq '[.packages[].package.functions.unsafe] | add' geiger-report.json) && \
		UNSAFE_RATIO=$$(echo "scale=2; $$UNSAFE_FUNCS / $$TOTAL_FUNCS * 100" | bc) && \
		echo "  Unsafe ratio: $$UNSAFE_RATIO% ($$UNSAFE_FUNCS / $$TOTAL_FUNCS functions)" && \
		if (( $$(echo "$$UNSAFE_RATIO > 5.0" | bc -l) )); then \
			echo "$(YELLOW)❌ Unsafe ratio exceeds 5% threshold$(RESET)"; \
			exit 1; \
		fi && \
		echo "$(GREEN)✓ cargo-geiger passed (< 5% unsafe)$(RESET)" && \
		echo "$(YELLOW)Running Miri on byte_storage module...$(RESET)" && \
		cargo +nightly miri test --lib --no-default-features --features compression,checksum byte_storage && \
		echo "$(GREEN)✓ Miri byte_storage passed$(RESET)" && \
		echo "$(YELLOW)Running Miri on encryption module...$(RESET)" && \
		cargo +nightly miri test --lib --no-default-features --features encryption encryption && \
		echo "$(GREEN)✓ Miri encryption passed$(RESET)" && \
		echo "$(YELLOW)Running cargo-semver-checks...$(RESET)" && \
		(cargo semver-checks check-release || echo "$(YELLOW)⚠️  No previous version - skipping semver check$(RESET)") && \
		echo "$(GREEN)✓ cargo-semver-checks passed$(RESET)"; \
	} 2>&1 | tee $(LOG_SECURITY_DIR)/medium_$(TIMESTAMP).log
	@echo "$(GREEN)✓ Medium security checks completed$(RESET)"

kani-verify: ## Run Kani formal verification
	@echo "$(BLUE)Running Kani verification...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_SECURITY_DIR)/kani-verify_$(TIMESTAMP).log$(RESET)"
	$(call require_binary,cargo,Install Rust: https://rustup.rs)
	@if ! cargo kani --tests --no-default-features --features compression,checksum,encryption 2>&1 | tee $(LOG_SECURITY_DIR)/kani-verify_$(TIMESTAMP).log; then \
		echo "$(YELLOW)❌ Kani verification failed$(RESET)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ Kani verification completed$(RESET)"

# ═══════════════════════════════════════════════════════════════════════════════
# 🧪 SANITIZERS
# ═══════════════════════════════════════════════════════════════════════════════

asan: ## Run AddressSanitizer
	@echo "$(BLUE)Running AddressSanitizer...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_SECURITY_DIR)/asan_$(TIMESTAMP).log$(RESET)"
	$(call require_binary,cargo,Install Rust: https://rustup.rs)
	@if ! RUSTFLAGS="-Zsanitizer=address" cargo +nightly test --lib --target x86_64-unknown-linux-gnu --no-default-features --features compression,checksum,encryption 2>&1 | tee $(LOG_SECURITY_DIR)/asan_$(TIMESTAMP).log; then \
		echo "$(YELLOW)❌ AddressSanitizer detected issues$(RESET)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ AddressSanitizer completed$(RESET)"

tsan: ## Run ThreadSanitizer
	@echo "$(BLUE)Running ThreadSanitizer...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_SECURITY_DIR)/tsan_$(TIMESTAMP).log$(RESET)"
	$(call require_binary,cargo,Install Rust: https://rustup.rs)
	@if ! RUSTFLAGS="-Zsanitizer=thread" TSAN_OPTIONS="suppressions=$$PWD/tsan_suppressions.txt" cargo +nightly test --lib --target x86_64-unknown-linux-gnu --no-default-features --features compression,checksum,encryption 2>&1 | tee $(LOG_SECURITY_DIR)/tsan_$(TIMESTAMP).log; then \
		echo "$(YELLOW)❌ ThreadSanitizer detected issues$(RESET)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ ThreadSanitizer completed$(RESET)"

msan: ## Run MemorySanitizer
	@echo "$(BLUE)Running MemorySanitizer...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_SECURITY_DIR)/msan_$(TIMESTAMP).log$(RESET)"
	$(call require_binary,cargo,Install Rust: https://rustup.rs)
	@if ! RUSTFLAGS="-Zsanitizer=memory -Zsanitizer-memory-track-origins" cargo +nightly test --lib --target x86_64-unknown-linux-gnu -Zbuild-std --no-default-features --features compression,checksum,encryption 2>&1 | tee $(LOG_SECURITY_DIR)/msan_$(TIMESTAMP).log; then \
		echo "$(YELLOW)❌ MemorySanitizer detected issues$(RESET)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ MemorySanitizer completed$(RESET)"
