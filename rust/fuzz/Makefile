# cachekit/rust/fuzz - Fuzzing Makefile
# Handles all fuzzing and corpus management targets
SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c

.PHONY: help quick deep target coverage corpus-generate corpus-minimize corpus-validate triage

.DEFAULT_GOAL := help

# Colors for output (using printf for cross-platform compatibility)
BLUE := $(shell printf '\033[36m')
GREEN := $(shell printf '\033[32m')
YELLOW := $(shell printf '\033[33m')
RESET := $(shell printf '\033[0m')

# Log directories (inherited from parent, but with defaults for standalone use)
LOG_DIR ?= ../../logs
LOG_FUZZ_DIR ?= $(LOG_DIR)/fuzz

# Ensure log directories exist
$(shell mkdir -p $(LOG_FUZZ_DIR))

# Timestamp for log files (inherited from parent, or generated here)
TIMESTAMP ?= $(shell date +%Y%m%d_%H%M%S)

# All fuzz targets (complete list)
FUZZ_TARGETS := byte_storage_compress byte_storage_decompress encryption_roundtrip \
	byte_storage_corrupted_envelope byte_storage_integer_overflow byte_storage_checksum_collision \
	byte_storage_empty_data byte_storage_format_injection encryption_key_derivation \
	encryption_nonce_reuse encryption_truncated_ciphertext encryption_aad_injection \
	encryption_large_payload integration_layered_security

# Helper function to check if a binary exists
define require_binary
	@command -v $(1) >/dev/null 2>&1 || { echo "$(YELLOW)❌ $(1) not found. $(2)$(RESET)"; exit 1; }
endef

define warn_if_missing
	@command -v $(1) >/dev/null 2>&1 || echo "$(YELLOW)⚠️  $(1) not found. $(2)$(RESET)"
endef

help: ## Show fuzzing-specific commands
	@echo "$(BLUE)cachekit/rust/fuzz - Fuzzing Commands$(RESET)"
	@echo ""
	@echo "$(GREEN)Quick Fuzzing:$(RESET)"
	@echo "  $(YELLOW)make quick$(RESET)      Run quick fuzzing smoke test (60s per target)"
	@echo ""
	@echo "$(GREEN)Deep Fuzzing:$(RESET)"
	@echo "  $(YELLOW)make deep$(RESET)       Run deep fuzzing (8 hours per target)"
	@echo "  $(YELLOW)make target$(RESET)     Run single fuzz target (TARGET=<name> [TIME=seconds])"
	@echo ""
	@echo "$(GREEN)Corpus Management:$(RESET)"
	@echo "  $(YELLOW)make corpus-generate$(RESET)   Generate initial fuzzing corpus"
	@echo "  $(YELLOW)make corpus-minimize$(RESET)   Minimize fuzzing corpus"
	@echo "  $(YELLOW)make corpus-validate$(RESET)   Validate fuzzing corpus"
	@echo ""
	@echo "$(GREEN)Analysis:$(RESET)"
	@echo "  $(YELLOW)make coverage$(RESET)   Generate fuzzing coverage report"
	@echo "  $(YELLOW)make triage$(RESET)     Triage fuzzing crashes"
	@echo ""

# ═══════════════════════════════════════════════════════════════════════════════
# 🧬 FUZZING TARGETS
# ═══════════════════════════════════════════════════════════════════════════════

quick: ## Run quick fuzzing smoke test (60s per target, ~14min total)
	@echo "$(BLUE)Running quick fuzzing smoke test (60s per target)...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_FUZZ_DIR)/quick_$(TIMESTAMP).log$(RESET)"
	$(call require_binary,cargo,Install Rust: https://rustup.rs)
	$(call warn_if_missing,cargo-fuzz,Install: cargo install cargo-fuzz)
	@{ \
		for target in $(FUZZ_TARGETS); do \
			echo "$(YELLOW)Fuzzing $$target (60s)...$(RESET)"; \
			cargo +nightly fuzz run $$target -- -max_total_time=60 || true; \
		done && \
		echo "$(YELLOW)Checking for crashes...$(RESET)" && \
		CRASHES=$$(find artifacts -name "crash-*" 2>/dev/null | wc -l) && \
		if [ "$$CRASHES" -gt 0 ]; then \
			echo "$(YELLOW)❌ Found $$CRASHES crashes$(RESET)"; \
			exit 1; \
		fi && \
		echo "$(GREEN)✓ No crashes found$(RESET)"; \
	} 2>&1 | tee $(LOG_FUZZ_DIR)/quick_$(TIMESTAMP).log
	@echo "$(GREEN)✓ Quick fuzzing smoke test completed$(RESET)"

deep: ## Run deep fuzzing (8 hours per target, production-grade)
	@echo "$(BLUE)Running deep fuzzing (8 hours per target)...$(RESET)"
	@echo "$(YELLOW)This will take ~112 hours total. Use target TARGET=<name> for single targets.$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_FUZZ_DIR)/deep_$(TIMESTAMP).log$(RESET)"
	$(call require_binary,cargo,Install Rust: https://rustup.rs)
	$(call warn_if_missing,cargo-fuzz,Install: cargo install cargo-fuzz)
	@{ \
		for target in $(FUZZ_TARGETS); do \
			echo "$(YELLOW)Deep fuzzing $$target (8 hours)...$(RESET)"; \
			cargo +nightly fuzz run $$target -- -max_total_time=28800 || true; \
		done && \
		echo "$(YELLOW)Checking for crashes...$(RESET)" && \
		CRASHES=$$(find artifacts -name "crash-*" 2>/dev/null | wc -l) && \
		if [ "$$CRASHES" -gt 0 ]; then \
			echo "$(YELLOW)❌ Found $$CRASHES crashes$(RESET)"; \
			echo "$(YELLOW)Run scripts/triage_crashes.sh to analyze$(RESET)"; \
			exit 1; \
		fi && \
		echo "$(GREEN)✓ No crashes found$(RESET)"; \
	} 2>&1 | tee $(LOG_FUZZ_DIR)/deep_$(TIMESTAMP).log
	@echo "$(GREEN)✓ Deep fuzzing completed$(RESET)"

target: ## Run single fuzz target (TARGET=<name>, TIME=60)
	@if [ -z "$(TARGET)" ]; then \
		echo "$(YELLOW)Usage: make target TARGET=<name> [TIME=seconds]$(RESET)"; \
		echo "Available targets:"; \
		for target in $(FUZZ_TARGETS); do \
			echo "  - $$target"; \
		done; \
		exit 1; \
	fi
	@TIME=$${TIME:-60}; \
	echo "$(BLUE)Fuzzing $(TARGET) for $$TIME seconds...$(RESET)"; \
	echo "$(YELLOW)Logging to $(LOG_FUZZ_DIR)/target_$(TARGET)_$(TIMESTAMP).log$(RESET)"; \
	$(call require_binary,cargo,Install Rust: https://rustup.rs); \
	$(call warn_if_missing,cargo-fuzz,Install: cargo install cargo-fuzz); \
	{ \
		cargo +nightly fuzz run $(TARGET) -- -max_total_time=$$TIME; \
	} 2>&1 | tee $(LOG_FUZZ_DIR)/target_$(TARGET)_$(TIMESTAMP).log
	@echo "$(GREEN)✓ Fuzzing completed for $(TARGET)$(RESET)"

# ═══════════════════════════════════════════════════════════════════════════════
# 📊 CORPUS MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

corpus-generate: ## Generate initial fuzzing corpus
	@echo "$(BLUE)Generating fuzzing corpus...$(RESET)"
	@bash scripts/generate_corpus.sh
	@echo "$(GREEN)✓ Corpus generated$(RESET)"

corpus-minimize: ## Minimize fuzzing corpus
	@echo "$(BLUE)Minimizing fuzzing corpus...$(RESET)"
	@bash scripts/minimize_corpus.sh
	@echo "$(GREEN)✓ Corpus minimized$(RESET)"

corpus-validate: ## Validate fuzzing corpus
	@echo "$(BLUE)Validating fuzzing corpus...$(RESET)"
	@bash scripts/validate_corpus.sh
	@echo "$(GREEN)✓ Corpus validated$(RESET)"

# ═══════════════════════════════════════════════════════════════════════════════
# 📈 ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

coverage: ## Generate fuzzing coverage report
	@echo "$(BLUE)Generating fuzzing coverage report...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_FUZZ_DIR)/coverage_$(TIMESTAMP).log$(RESET)"
	$(call require_binary,cargo,Install Rust: https://rustup.rs)
	$(call warn_if_missing,cargo-fuzz,Install: cargo install cargo-fuzz)
	$(call warn_if_missing,cargo-llvm-cov,Install: cargo install cargo-llvm-cov)
	@{ \
		echo "$(YELLOW)Running coverage for all fuzz targets...$(RESET)" && \
		cargo +nightly fuzz coverage --all && \
		echo "$(YELLOW)Generating HTML report...$(RESET)" && \
		cargo llvm-cov report --html --output-dir coverage/html 2>/dev/null || \
		echo "$(YELLOW)⚠️  cargo-llvm-cov not available, coverage HTML not generated$(RESET)"; \
	} 2>&1 | tee $(LOG_FUZZ_DIR)/coverage_$(TIMESTAMP).log
	@echo "$(GREEN)✓ Coverage report: coverage/html/index.html$(RESET)"

triage: ## Triage fuzzing crashes
	@echo "$(BLUE)Triaging fuzzing crashes...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_FUZZ_DIR)/triage_$(TIMESTAMP).log$(RESET)"
	@{ \
		bash scripts/triage_crashes.sh; \
	} 2>&1 | tee $(LOG_FUZZ_DIR)/triage_$(TIMESTAMP).log
	@echo "$(GREEN)✓ Crash triage completed$(RESET)"
