# cachekit - Development Makefile
SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c

.PHONY: help install test test-cov lint format check clean build build-multiarch-linux publish publish-test release release-check sbom
.DEFAULT_GOAL := help

# Colors for output (using printf for cross-platform compatibility)
BLUE := $(shell printf '\033[36m')
GREEN := $(shell printf '\033[32m')
YELLOW := $(shell printf '\033[33m')
RESET := $(shell printf '\033[0m')

# Project configuration
PACKAGE_NAME := src/cachekit
TEST_DIR := tests
LOG_DIR := logs

# Log subdirectories by category
LOG_TEST_DIR := $(LOG_DIR)/test
LOG_DOCS_EXAMPLES_DIR := $(LOG_DIR)/docs-examples
LOG_FORMAT_DIR := $(LOG_DIR)/format
LOG_LINT_DIR := $(LOG_DIR)/lint
LOG_TYPE_DIR := $(LOG_DIR)/type-check
LOG_SECURITY_DIR := $(LOG_DIR)/security
LOG_FUZZ_DIR := $(LOG_DIR)/fuzz
LOG_BUILD_DIR := $(LOG_DIR)/build
LOG_BENCHMARK_DIR := $(LOG_DIR)/benchmark
LOG_CI_DIR := $(LOG_DIR)/ci

# Binary checks
PYRIGHT_COMMAND := basedpyright

# Timestamp for log files
TIMESTAMP := $(shell date +%Y%m%d_%H%M%S)

# Unique test directory (timestamp + random to prevent collisions)
TEST_BASETEMP := /tmp/pt-$(TIMESTAMP)-$(shell echo $$RANDOM)

# Helper function to check if a binary exists
define require_binary
	@command -v $(1) >/dev/null 2>&1 || { echo "$(YELLOW)❌ $(1) not found. $(2)$(RESET)"; exit 1; }
endef

define warn_if_missing
	@command -v $(1) >/dev/null 2>&1 || echo "$(YELLOW)⚠️  $(1) not found. $(2)$(RESET)"
endef

help: ## Show available commands
	@echo "$(BLUE)cachekit - Development Commands$(RESET)"
	@echo ""
	@echo "$(GREEN)Release Workflow:$(RESET)"
	@echo "  $(YELLOW)make release$(RESET)                 Full release: check, build all platforms, publish to Test PyPI"
	@echo ""
	@echo "$(GREEN)Common Workflows:$(RESET)"
	@echo "  $(YELLOW)make quick-check$(RESET)              Fast validation (minimal output, logs to logs/)"
	@echo "  $(YELLOW)make check$(RESET)                   Full validation (verbose output)"
	@echo "  $(YELLOW)make test$(RESET)                    Run all tests (Python + Rust)"
	@echo "  $(YELLOW)make build-multiarch-linux$(RESET)   Build Linux wheels (amd64 + arm64)"
	@echo "  $(YELLOW)make publish-test$(RESET)            Publish wheels to Test PyPI"
	@echo "  $(YELLOW)make publish$(RESET)                 Publish wheels to PyPI (production)"
	@echo ""
	@echo "$(GREEN)Code Quality:$(RESET)"
	@echo "  $(YELLOW)make format$(RESET)          Format all code (Python + Rust)"
	@echo "  $(YELLOW)make format-python$(RESET)   Format Python only"
	@echo "  $(YELLOW)make format-rust$(RESET)     Format Rust only"
	@echo "  $(YELLOW)make lint$(RESET)            Lint all code (Python + Rust)"
	@echo "  $(YELLOW)make lint-python$(RESET)     Lint Python only"
	@echo "  $(YELLOW)make lint-rust$(RESET)       Lint Rust only"
	@echo ""
	@echo "$(GREEN)Testing:$(RESET)"
	@echo "  $(YELLOW)make test$(RESET)                  All tests (Python + Rust)"
	@echo "  $(YELLOW)make test-python$(RESET)           Python tests (642+ tests)"
	@echo "  $(YELLOW)make test-doctest$(RESET)          Doctest validation (code examples)"
	@echo "  $(YELLOW)make test-docs-examples$(RESET)    Markdown docs validation (verbose)"
	@echo "  $(YELLOW)make test-docs-quick$(RESET)       Markdown docs validation (quiet)"
	@echo "  $(YELLOW)make test-rust-unit$(RESET)        Rust tests (72 tests, <1s)"
	@echo "  $(YELLOW)make test-rust-byte-storage$(RESET) ByteStorage tests only"
	@echo "  $(YELLOW)make test-rust-encryption$(RESET)  Encryption tests only"
	@echo "  $(YELLOW)make test-rust-integration$(RESET) Integration tests only"
	@echo "  $(YELLOW)make test-rust-property$(RESET)    Property-based tests (proptest)"
	@echo ""
	@echo "$(GREEN)All Commands:$(RESET)"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  $(YELLOW)%-20s$(RESET) %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""

check-deps: ## Check for required development tools
	@echo "$(BLUE)Checking for required tools...$(RESET)"
	$(call require_binary,uv,Install: https://github.com/astral-sh/uv)
	$(call warn_if_missing,cargo,Rust commands will not work. Install: https://rustup.rs)
	$(call warn_if_missing,git,Version control features limited.)
	@echo "$(GREEN)✓ Core dependencies available$(RESET)"

setup-logs: ## Create logs directory structure
	@mkdir -p $(LOG_TEST_DIR) $(LOG_DOCS_EXAMPLES_DIR) $(LOG_FORMAT_DIR) $(LOG_LINT_DIR) $(LOG_TYPE_DIR) \
		$(LOG_SECURITY_DIR) $(LOG_FUZZ_DIR) $(LOG_BUILD_DIR) $(LOG_BENCHMARK_DIR) $(LOG_CI_DIR)

# ═══════════════════════════════════════════════════════════════════════════════
# 🚀 ESSENTIAL COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

install: ## Install dependencies
	@echo "$(BLUE)Installing dependencies...$(RESET)"
	$(call require_binary,uv,Install: https://github.com/astral-sh/uv)
	@uv sync
	@echo "$(GREEN)✓ Dependencies installed$(RESET)"

test: test-python test-rust-unit ## Run all tests (Python + Rust)

test-python: setup-logs ## Run Python tests only
	@echo "$(BLUE)Running Python tests...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_TEST_DIR)/python_$(TIMESTAMP).log$(RESET)"
	@if ! uv run pytest $(TEST_DIR) --ignore=$(TEST_DIR)/fuzzing -q --basetemp=$(TEST_BASETEMP) --tb=short 2>&1 | tee $(LOG_TEST_DIR)/python_$(TIMESTAMP).log; then \
		echo "$(YELLOW)❌ Python tests failed$(RESET)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ Python tests completed$(RESET)"

test-quick: setup-logs ## Run fast tests (skip slow tests)
	@echo "$(BLUE)Running fast tests (excluding slow tests)...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_TEST_DIR)/quick_$(TIMESTAMP).log$(RESET)"
	@if ! uv run pytest $(TEST_DIR) -q --tb=short -m "not slow" --basetemp=$(TEST_BASETEMP) 2>&1 | tee $(LOG_TEST_DIR)/quick_$(TIMESTAMP).log; then \
		echo "$(YELLOW)❌ Fast tests failed$(RESET)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ Fast tests completed$(RESET)"

test-cov: setup-logs ## Run tests with coverage
	@echo "$(BLUE)Running tests with coverage...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_TEST_DIR)/cov_$(TIMESTAMP).log$(RESET)"
	@if ! uv run pytest $(TEST_DIR) \
		--cov=$(PACKAGE_NAME) \
		--cov-report=term-missing \
		--cov-report=html:reports/htmlcov \
		--cov-fail-under=60 \
		-q \
		--tb=short \
		--basetemp=$(TEST_BASETEMP) 2>&1 | tee $(LOG_TEST_DIR)/cov_$(TIMESTAMP).log; then \
		echo "$(YELLOW)❌ Coverage tests failed$(RESET)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ Coverage report: reports/htmlcov/index.html$(RESET)"

test-critical: test-critical-quick

test-critical-quick: setup-logs ## Run critical tests (skip slow ones)
	@echo "$(BLUE)Running critical tests (excluding slow tests)...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_TEST_DIR)/critical-quick_$(TIMESTAMP).log$(RESET)"
	@if ! uv run pytest tests/critical/ -q -m "not slow" --basetemp=$(TEST_BASETEMP) 2>&1 | tee $(LOG_TEST_DIR)/critical-quick_$(TIMESTAMP).log; then \
		echo "$(YELLOW)❌ Critical fast tests failed$(RESET)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ Critical fast tests completed$(RESET)"

test-critical-quiet: setup-logs ## Run critical tests (minimal output)
	@printf "$(BLUE)critical tests...$(RESET) "
	@if ! uv run pytest tests/critical/ -q -m "not slow" --basetemp=$(TEST_BASETEMP) --tb=no -p no:warnings > $(LOG_TEST_DIR)/critical-quick_$(TIMESTAMP).log 2>&1; then \
		echo "$(YELLOW)❌ FAILED$(RESET)"; \
		echo "$(YELLOW)See $(LOG_TEST_DIR)/critical-quick_$(TIMESTAMP).log$(RESET)"; \
		tail -20 $(LOG_TEST_DIR)/critical-quick_$(TIMESTAMP).log; \
		exit 1; \
	fi
	@echo "$(GREEN)✓$(RESET)"

security-audit: setup-logs ## Scan dependencies for CVEs
	@echo "$(BLUE)Scanning dependencies for vulnerabilities...$(RESET)"
	@uv run pip-audit --desc --format json --output $(LOG_DIR)/pip-audit_$(TIMESTAMP).json || \
		(echo "$(YELLOW)⚠️  Vulnerabilities found. See $(LOG_DIR)/pip-audit_$(TIMESTAMP).json$(RESET)" && exit 1)
	@echo "$(GREEN)✓ No vulnerabilities detected$(RESET)"

test-doctest: setup-logs ## Run doctest validation on code examples
	@echo "$(BLUE)Running doctest validation...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_TEST_DIR)/doctest_$(TIMESTAMP).log$(RESET)"
	@if ! uv run pytest --doctest-modules $(PACKAGE_NAME) -v --ignore=$(PACKAGE_NAME)/_rust_serializer.py 2>&1 | tee $(LOG_TEST_DIR)/doctest_$(TIMESTAMP).log; then \
		echo "$(YELLOW)❌ Doctest validation failed$(RESET)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ Doctest validation completed$(RESET)"

test-doctest-quiet: setup-logs ## Run doctest validation (minimal output)
	@printf "$(BLUE)doctest...$(RESET) "
	@if ! uv run pytest --doctest-modules $(PACKAGE_NAME) -q --tb=line -p no:warnings --ignore=$(PACKAGE_NAME)/_rust_serializer.py > $(LOG_TEST_DIR)/doctest_$(TIMESTAMP).log 2>&1; then \
		echo "$(YELLOW)❌ FAILED$(RESET)"; \
		echo "$(YELLOW)See $(LOG_TEST_DIR)/doctest_$(TIMESTAMP).log$(RESET)"; \
		grep -A 5 "FAILED" $(LOG_TEST_DIR)/doctest_$(TIMESTAMP).log || tail -20 $(LOG_TEST_DIR)/doctest_$(TIMESTAMP).log; \
		exit 1; \
	fi
	@echo "$(GREEN)✓$(RESET)"

test-docs-examples: setup-logs ## Run markdown documentation examples (verbose)
	@echo "$(BLUE)Running markdown documentation examples...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_DOCS_EXAMPLES_DIR)/verbose_$(TIMESTAMP).log$(RESET)"
	@if ! uv run pytest --markdown-docs docs/ -v 2>&1 | tee $(LOG_DOCS_EXAMPLES_DIR)/verbose_$(TIMESTAMP).log; then \
		echo "$(YELLOW)❌ Markdown docs validation failed$(RESET)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ Markdown documentation examples passed$(RESET)"

test-docs-quick: setup-logs ## Run markdown documentation examples (quiet)
	@printf "$(BLUE)markdown docs...$(RESET) "
	@if ! uv run pytest --markdown-docs docs/ -q --tb=line -p no:warnings > $(LOG_DOCS_EXAMPLES_DIR)/quiet_$(TIMESTAMP).log 2>&1; then \
		echo "$(YELLOW)❌ FAILED$(RESET)"; \
		echo "$(YELLOW)See $(LOG_DOCS_EXAMPLES_DIR)/quiet_$(TIMESTAMP).log$(RESET)"; \
		tail -20 $(LOG_DOCS_EXAMPLES_DIR)/quiet_$(TIMESTAMP).log; \
		exit 1; \
	fi
	@echo "$(GREEN)✓$(RESET)"

test-docs-quiet: setup-logs ## Run documentation validation tests (minimal output)
	@printf "$(BLUE)docs tests...$(RESET) "
	@if ! uv run pytest tests/docs/ -q -m critical --tb=no -p no:warnings > $(LOG_TEST_DIR)/docs_$(TIMESTAMP).log 2>&1; then \
		echo "$(YELLOW)❌ FAILED$(RESET)"; \
		echo "$(YELLOW)See $(LOG_TEST_DIR)/docs_$(TIMESTAMP).log$(RESET)"; \
		tail -20 $(LOG_TEST_DIR)/docs_$(TIMESTAMP).log; \
		exit 1; \
	fi
	@echo "$(GREEN)✓$(RESET)"

format: format-python format-rust ## Format all code (Python + Rust)

format-python: setup-logs ## Format Python code only
	@echo "$(BLUE)Formatting Python code...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_FORMAT_DIR)/python_$(TIMESTAMP).log$(RESET)"
	@if ! uv run ruff format $(PACKAGE_NAME) $(TEST_DIR) 2>&1 | tee $(LOG_FORMAT_DIR)/python_$(TIMESTAMP).log; then \
		echo "$(YELLOW)❌ Python formatting failed$(RESET)"; \
		exit 1; \
	fi
	@if ! uv run ruff check $(PACKAGE_NAME) $(TEST_DIR) --fix 2>&1 | tee -a $(LOG_FORMAT_DIR)/python_$(TIMESTAMP).log; then \
		echo "$(YELLOW)❌ Python auto-fix failed$(RESET)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ Python code formatted$(RESET)"

format-python-quiet: setup-logs ## Format Python code (minimal output)
	@printf "$(BLUE)format (py)...$(RESET) "
	@if ! uv run ruff format $(PACKAGE_NAME) $(TEST_DIR) --quiet > $(LOG_FORMAT_DIR)/python_$(TIMESTAMP).log 2>&1; then \
		echo "$(YELLOW)❌ FAILED$(RESET)"; \
		cat $(LOG_FORMAT_DIR)/python_$(TIMESTAMP).log; \
		exit 1; \
	fi
	@if ! uv run ruff check $(PACKAGE_NAME) $(TEST_DIR) --fix --quiet >> $(LOG_FORMAT_DIR)/python_$(TIMESTAMP).log 2>&1; then \
		echo "$(YELLOW)❌ FAILED$(RESET)"; \
		cat $(LOG_FORMAT_DIR)/python_$(TIMESTAMP).log; \
		exit 1; \
	fi
	@echo "$(GREEN)✓$(RESET)"

format-rust: ## Format Rust code only
	@$(MAKE) -C rust format

format-rust-quiet: setup-logs ## Format Rust code (minimal output)
	@$(MAKE) -C rust format-quiet LOG_FORMAT_DIR=$(LOG_FORMAT_DIR) TIMESTAMP=$(TIMESTAMP)

lint: lint-python lint-rust ## Lint all code (Python + Rust)

lint-python: setup-logs ## Lint Python code only
	@echo "$(BLUE)Linting Python code...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_LINT_DIR)/python_$(TIMESTAMP).log$(RESET)"
	@if ! uv run ruff check $(PACKAGE_NAME) $(TEST_DIR) --fix 2>&1 | tee $(LOG_LINT_DIR)/python_$(TIMESTAMP).log; then \
		echo "$(YELLOW)❌ Python linting failed$(RESET)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ Python linting completed$(RESET)"

lint-python-quiet: setup-logs ## Lint Python code (minimal output)
	@printf "$(BLUE)lint (py)...$(RESET) "
	@if ! uv run ruff check $(PACKAGE_NAME) $(TEST_DIR) --fix --quiet > $(LOG_LINT_DIR)/python_$(TIMESTAMP).log 2>&1; then \
		echo "$(YELLOW)❌ FAILED$(RESET)"; \
		uv run ruff check $(PACKAGE_NAME) $(TEST_DIR); \
		exit 1; \
	fi
	@echo "$(GREEN)✓$(RESET)"

lint-rust: ## Lint Rust code only
	@$(MAKE) -C rust lint

lint-rust-quiet: setup-logs ## Lint Rust code (minimal output)
	@$(MAKE) -C rust lint-quiet LOG_LINT_DIR=$(LOG_LINT_DIR) TIMESTAMP=$(TIMESTAMP)

type-check: setup-logs ## Run type checking with basedpyright
	@echo "$(BLUE)Running type checking with basedpyright...$(RESET)"
	@echo "$(YELLOW)Note: Using standard mode for gradual migration$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_TYPE_DIR)/check_$(TIMESTAMP).log$(RESET)"
	$(call require_binary,uv,Install: https://github.com/astral-sh/uv)
	@uv run $(PYRIGHT_COMMAND) 2>&1 | tee $(LOG_TYPE_DIR)/check_$(TIMESTAMP).log || echo "$(YELLOW)⚠️  Type checking warnings present$(RESET)"
	@echo "$(GREEN)✓ Type checking completed$(RESET)"

type-check-quick: setup-logs ## Run type checking with basedpyright (quick)
	@echo "$(BLUE)Running type checking with basedpyright (quick)...$(RESET)"
	@echo "$(YELLOW)Note: Using standard mode for gradual migration$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_TYPE_DIR)/check-quick_$(TIMESTAMP).log$(RESET)"
	$(call require_binary,uv,Install: https://github.com/astral-sh/uv)
	@if ! uv run $(PYRIGHT_COMMAND) --level error 2>&1 | tee $(LOG_TYPE_DIR)/check-quick_$(TIMESTAMP).log; then \
		echo "$(YELLOW)❌ Type checking failed$(RESET)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ Type checking completed$(RESET)"

type-check-quiet: setup-logs ## Run type checking (minimal output)
	@printf "$(BLUE)type check...$(RESET) "
	$(call require_binary,uv,Install: https://github.com/astral-sh/uv)
	@if ! uv run $(PYRIGHT_COMMAND) --level error > $(LOG_TYPE_DIR)/check-quick_$(TIMESTAMP).log 2>&1; then \
		echo "$(YELLOW)❌ FAILED$(RESET)"; \
		grep "error:" $(LOG_TYPE_DIR)/check-quick_$(TIMESTAMP).log || tail -20 $(LOG_TYPE_DIR)/check-quick_$(TIMESTAMP).log; \
		exit 1; \
	fi
	@echo "$(GREEN)✓$(RESET)"

check: format lint type-check test version-check ## Run all quality checks (full validation)

quick-check: format-python-quiet format-rust-quiet lint-python-quiet lint-rust-quiet type-check-quiet test-critical-quiet test-docs-quiet test-doctest-quiet test-rust-quiet ## Quick development check (minimal output)
	@echo ""
	@echo "$(GREEN)━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━$(RESET)"
	@echo "$(GREEN)✓ All checks passed$(RESET)"
	@echo "$(GREEN)━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━$(RESET)"

version-check: ## Check version consistency across files
	@echo "$(BLUE)Checking version consistency...$(RESET)"
	@PYTHON_VERSION=$$(grep -E "^version = " pyproject.toml | head -1 | cut -d'"' -f2); \
	RUST_VERSION=$$(grep -E "^version = " rust/Cargo.toml | head -1 | cut -d'"' -f2); \
	echo "  Python version: $$PYTHON_VERSION"; \
	echo "  Rust version:   $$RUST_VERSION"; \
	if [ "$$PYTHON_VERSION" != "$$RUST_VERSION" ]; then \
		echo "$(YELLOW)❌ Version mismatch! Python and Rust versions must match.$(RESET)"; \
		exit 1; \
	else \
		echo "$(GREEN)✓ Versions match$(RESET)"; \
	fi

# ═══════════════════════════════════════════════════════════════════════════════
# 🦀 RUST COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

test-rust: ## Run all Rust tests (including PyO3 library tests)
	@$(MAKE) -C rust test

test-rust-unit: ## Run pure Rust unit tests (no PyO3, fast)
	@$(MAKE) -C rust test-unit

test-rust-quiet: ## Run Rust tests (minimal output)
	@$(MAKE) -C rust test-quiet

test-rust-byte-storage: ## Run ByteStorage tests only
	@$(MAKE) -C rust test-byte-storage

test-rust-encryption: ## Run encryption tests only
	@$(MAKE) -C rust test-encryption

test-rust-integration: ## Run integration tests only
	@$(MAKE) -C rust test-integration

test-rust-property: ## Run property-based tests (proptest)
	@$(MAKE) -C rust test-property

rust-bench: ## Run Rust benchmarks
	@$(MAKE) -C rust bench

# ═══════════════════════════════════════════════════════════════════════════════
# 📊 BENCHMARKS
# ═══════════════════════════════════════════════════════════════════════════════

benchmark: setup-logs ## Run quick benchmarks
	@echo "$(BLUE)Running quick benchmarks...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_BENCHMARK_DIR)/quick_$(TIMESTAMP).log$(RESET)"
	@uv run python -m benchmarks.cli quick 2>&1 | tee $(LOG_BENCHMARK_DIR)/quick_$(TIMESTAMP).log
	@echo "$(GREEN)✓ Benchmarks completed$(RESET)"

benchmark-full: setup-logs ## Run comprehensive benchmarks
	@echo "$(BLUE)Running comprehensive benchmarks...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_BENCHMARK_DIR)/full_$(TIMESTAMP).log$(RESET)"
	@uv run python -m benchmarks.cli performance --comprehensive 2>&1 | tee $(LOG_BENCHMARK_DIR)/full_$(TIMESTAMP).log
	@echo "$(GREEN)✓ Comprehensive benchmarks completed$(RESET)"

# ═══════════════════════════════════════════════════════════════════════════════
# 🔧 BUILD & RELEASE
# ═══════════════════════════════════════════════════════════════════════════════

build: setup-logs ## Build package
	@echo "$(BLUE)Building package...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_BUILD_DIR)/build_$(TIMESTAMP).log$(RESET)"
	@if ! uv build 2>&1 | tee $(LOG_BUILD_DIR)/build_$(TIMESTAMP).log; then \
		echo "$(YELLOW)❌ Build failed$(RESET)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ Package built in dist/$(RESET)"

build-pgo: setup-logs ## Build with Profile-Guided Optimization (5-8% faster)
	@echo "$(BLUE)Building with Profile-Guided Optimization...$(RESET)"
	@echo "$(YELLOW)This will take several minutes but results in 5-8% performance improvement$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_BUILD_DIR)/build-pgo_$(TIMESTAMP).log$(RESET)"
	@if [ -f "scripts/build_with_pgo.sh" ]; then \
		if ! bash scripts/build_with_pgo.sh 2>&1 | tee $(LOG_BUILD_DIR)/build-pgo_$(TIMESTAMP).log; then \
			echo "$(YELLOW)❌ PGO build failed$(RESET)"; \
			exit 1; \
		fi; \
	else \
		echo "$(YELLOW)⚠️  scripts/build_with_pgo.sh not found, running standard build$(RESET)"; \
		if ! uv build 2>&1 | tee $(LOG_BUILD_DIR)/build-pgo_$(TIMESTAMP).log; then \
			echo "$(YELLOW)❌ Build failed$(RESET)"; \
			exit 1; \
		fi; \
	fi
	@echo "$(GREEN)✓ Build complete$(RESET)"

build-multiarch-linux: ## Build Linux wheels for amd64 + arm64 using Docker (local Mac)
	@echo "$(BLUE)Building multi-arch Linux wheels...$(RESET)"
	$(call require_binary,docker,Install Docker: https://www.docker.com/products/docker-desktop)
	@echo "$(YELLOW)Setting up multi-platform builder...$(RESET)"
	@if ! docker buildx ls | grep -q "cachekit-builder"; then \
		echo "$(YELLOW)Creating buildx builder for multi-platform support...$(RESET)"; \
		docker buildx create --name cachekit-builder --platform linux/amd64,linux/arm64 --use || \
		docker buildx use cachekit-builder; \
	else \
		echo "$(GREEN)✓ Using existing cachekit-builder$(RESET)"; \
		docker buildx use cachekit-builder; \
	fi
	@echo "$(YELLOW)Building for linux/amd64 and linux/arm64...$(RESET)"
	@mkdir -p $(LOG_BUILD_DIR) dist .dist-linux-build
	@docker buildx build --platform linux/amd64,linux/arm64 \
		--output type=local,dest=./.dist-linux-build \
		--file Dockerfile . 2>&1 | tee $(LOG_BUILD_DIR)/multiarch_$(TIMESTAMP).log || \
		(echo "$(YELLOW)Build failed. Check $(LOG_BUILD_DIR)/multiarch_$(TIMESTAMP).log$(RESET)" && exit 1)
	@echo "$(YELLOW)Extracting wheels to dist/...$(RESET)"
	@cp .dist-linux-build/linux_amd64/cachekit-*.whl dist/ 2>/dev/null || true
	@cp .dist-linux-build/linux_arm64/cachekit-*.whl dist/ 2>/dev/null || true
	@echo "$(GREEN)✓ Multi-arch wheels built$(RESET)"
	@ls -lh dist/cachekit-*linux*.whl 2>/dev/null || (echo "$(YELLOW)⚠️  No Linux wheels found. Check $(LOG_BUILD_DIR)/multiarch_$(TIMESTAMP).log$(RESET)" && exit 1)

publish-test: ## Publish all wheels to Test PyPI
	@echo "$(BLUE)Publishing all wheels to Test PyPI...$(RESET)"
	$(call require_binary,twine,Install twine: pip install twine)
	@if [ -z "$$(ls dist/cachekit-*.whl 2>/dev/null)" ]; then \
		echo "$(YELLOW)❌ No wheels in dist/. Run 'make build' and 'make build-multiarch-linux' first$(RESET)"; \
		exit 1; \
	fi
	@twine upload --repository testpypi dist/cachekit-*.whl
	@echo "$(GREEN)✓ Published to Test PyPI$(RESET)"
	@echo "$(YELLOW)Install with: pip install -i https://test.pypi.org/simple/ cachekit$(RESET)"

publish: ## Publish all wheels to PyPI (production)
	@echo "$(BLUE)Publishing all wheels to PyPI...$(RESET)"
	$(call require_binary,twine,Install twine: pip install twine)
	@if [ -z "$$(ls dist/cachekit-*.whl 2>/dev/null)" ]; then \
		echo "$(YELLOW)❌ No wheels in dist/. Run 'make build' and 'make build-multiarch-linux' first$(RESET)"; \
		exit 1; \
	fi
	@twine upload dist/cachekit-*.whl
	@echo "$(GREEN)✓ Published to PyPI$(RESET)"

release-check: ## Check release readiness
	@echo "$(BLUE)Checking release readiness...$(RESET)"
	@echo "$(YELLOW)Checking version consistency...$(RESET)"
	@$(MAKE) version-check
	@echo ""
	@echo "$(YELLOW)Checking uncommitted changes...$(RESET)"
	$(call warn_if_missing,git,Git not available - skipping uncommitted changes check)
	@if command -v git >/dev/null 2>&1 && [ -n "$$(git status --porcelain 2>/dev/null)" ]; then \
		echo "$(YELLOW)⚠️  Uncommitted changes detected:$(RESET)"; \
		git status --porcelain | head -10; \
		echo "$(YELLOW)   Consider committing or stashing changes before release.$(RESET)"; \
	elif command -v git >/dev/null 2>&1; then \
		echo "$(GREEN)✓ Working directory clean$(RESET)"; \
	fi
	@echo ""
	@echo "$(YELLOW)Running quality checks (format, lint, type-check)...$(RESET)"
	@$(MAKE) format lint type-check-quick
	@echo ""
	@echo "$(YELLOW)Running critical tests...$(RESET)"
	@$(MAKE) test-critical
	@echo ""
	@echo "$(YELLOW)Building package...$(RESET)"
	@$(MAKE) build
	@echo "$(GREEN)✓ Release check completed$(RESET)"

release: release-check build-multiarch-linux publish-test ## Full release: check, build all platforms, publish to Test PyPI
	@echo ""
	@echo "$(GREEN)════════════════════════════════════════════════════════════$(RESET)"
	@echo "$(GREEN)✓ Release complete!$(RESET)"
	@echo "$(GREEN)════════════════════════════════════════════════════════════$(RESET)"
	@echo ""
	@echo "$(YELLOW)Published wheels:$(RESET)"
	@ls -lh dist/cachekit-*.whl 2>/dev/null | awk '{print "  " $$9 " (" $$5 ")"}'
	@echo ""
	@echo "$(YELLOW)Next steps:$(RESET)"
	@echo "  1. Distribute wheels to testers"
	@echo "  2. Tag release: git tag v0.1.0"
	@echo "  3. Push to GitHub: git push origin main --tags"

ci: setup-logs ## Run CI pipeline locally
	@echo "$(BLUE)Running CI pipeline...$(RESET)"
	@echo "$(YELLOW)Logging to $(LOG_CI_DIR)/ci_$(TIMESTAMP).log$(RESET)"
	@if ! $(MAKE) clean install check test-cov build 2>&1 | tee $(LOG_CI_DIR)/ci_$(TIMESTAMP).log; then \
		echo "$(YELLOW)❌ CI pipeline failed$(RESET)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ CI pipeline completed$(RESET)"

# ═══════════════════════════════════════════════════════════════════════════════
# 🔒 SECURITY TOOLCHAIN
# ═══════════════════════════════════════════════════════════════════════════════

security-install: ## Install all security tools (cargo-audit, deny, geiger, etc.)
	@echo "$(BLUE)Installing security tools...$(RESET)"
	$(call require_binary,cargo,Install Rust: https://rustup.rs)
	@echo "$(YELLOW)Installing cargo-audit...$(RESET)"
	@if command -v cargo-audit >/dev/null 2>&1; then \
		echo "  $(GREEN)✓ cargo-audit already installed$(RESET)"; \
	else \
		cargo install --locked cargo-audit && echo "  $(GREEN)✓ cargo-audit installed$(RESET)"; \
	fi
	@echo "$(YELLOW)Installing cargo-deny...$(RESET)"
	@if command -v cargo-deny >/dev/null 2>&1; then \
		echo "  $(GREEN)✓ cargo-deny already installed$(RESET)"; \
	else \
		cargo install --locked cargo-deny && echo "  $(GREEN)✓ cargo-deny installed$(RESET)"; \
	fi
	@echo "$(YELLOW)Installing cargo-geiger...$(RESET)"
	@if command -v cargo-geiger >/dev/null 2>&1; then \
		echo "  $(GREEN)✓ cargo-geiger already installed$(RESET)"; \
	else \
		cargo install --locked cargo-geiger && echo "  $(GREEN)✓ cargo-geiger installed$(RESET)"; \
	fi
	@echo "$(YELLOW)Installing cargo-semver-checks...$(RESET)"
	@if command -v cargo-semver-checks >/dev/null 2>&1; then \
		echo "  $(GREEN)✓ cargo-semver-checks already installed$(RESET)"; \
	else \
		cargo install --locked cargo-semver-checks && echo "  $(GREEN)✓ cargo-semver-checks installed$(RESET)"; \
	fi
	@echo "$(YELLOW)Installing cargo-machete...$(RESET)"
	@if command -v cargo-machete >/dev/null 2>&1; then \
		echo "  $(GREEN)✓ cargo-machete already installed$(RESET)"; \
	else \
		cargo install --locked cargo-machete && echo "  $(GREEN)✓ cargo-machete installed$(RESET)"; \
	fi
	@echo "$(YELLOW)Installing kani-verifier...$(RESET)"
	@if command -v cargo-kani >/dev/null 2>&1; then \
		echo "  $(GREEN)✓ kani-verifier already installed$(RESET)"; \
	else \
		cargo install --locked kani-verifier && cargo kani setup && echo "  $(GREEN)✓ kani-verifier installed$(RESET)"; \
	fi
	@echo "$(YELLOW)Installing cargo-fuzz...$(RESET)"
	@if command -v cargo-fuzz >/dev/null 2>&1; then \
		echo "  $(GREEN)✓ cargo-fuzz already installed$(RESET)"; \
	else \
		cargo install --locked cargo-fuzz && echo "  $(GREEN)✓ cargo-fuzz installed$(RESET)"; \
	fi
	@echo "$(YELLOW)Installing cargo-sbom...$(RESET)"
	@if command -v cargo-sbom >/dev/null 2>&1; then \
		echo "  $(GREEN)✓ cargo-sbom already installed$(RESET)"; \
	else \
		cargo install --locked cargo-sbom && echo "  $(GREEN)✓ cargo-sbom installed$(RESET)"; \
	fi
	@echo "$(YELLOW)Installing nightly toolchain with Miri...$(RESET)"
	@if rustup toolchain list | grep -q nightly; then \
		echo "  $(GREEN)✓ nightly toolchain already installed$(RESET)"; \
		rustup component add miri --toolchain nightly 2>/dev/null || echo "  $(GREEN)✓ miri already installed$(RESET)"; \
	else \
		rustup toolchain install nightly --component miri && echo "  $(GREEN)✓ nightly + miri installed$(RESET)"; \
	fi
	@echo "$(GREEN)✓ All security tools installed$(RESET)"

security-fast: ## Run fast security checks (< 3 min)
	@$(MAKE) -C rust security-fast

security-medium: ## Run medium security checks (< 15 min)
	@$(MAKE) -C rust security-medium

sbom: setup-logs ## Generate Software Bill of Materials (CycloneDX format)
	@echo "$(BLUE)Generating SBOM...$(RESET)"
	$(call require_binary,cargo-sbom,Install: cargo install cargo-sbom)
	@mkdir -p dist
	@cd rust && cargo sbom --output-format cyclonedx_json_1_4 > ../dist/cachekit-sbom.json 2>/dev/null || \
		cargo sbom > ../dist/cachekit-sbom.json
	@echo "$(GREEN)✓ SBOM generated: dist/cachekit-sbom.json$(RESET)"
	@echo "$(YELLOW)Contains: $$(jq '.components | length' dist/cachekit-sbom.json 2>/dev/null || echo '?') dependencies$(RESET)"

kani-verify: ## Run Kani formal verification
	@$(MAKE) -C rust kani-verify

fuzz-quick: ## Run Atheris Python fuzzing (10 min per target) + Rust fuzzing
	@if command -v python &> /dev/null && python -c "import atheris" 2>/dev/null; then \
		echo "$(BLUE)Running Atheris fuzzing...$(RESET)"; \
		for fuzz_target in tests/fuzzing/fuzz_*.py; do \
			if [ -f "$$fuzz_target" ]; then \
				echo "$(YELLOW)Fuzzing $$fuzz_target...$(RESET)"; \
				timeout 10m uv run python "$$fuzz_target" -max_total_time=600 || true; \
			fi; \
		done; \
	else \
		echo "$(YELLOW)⚠️  Atheris not available (macOS limitation - libFuzzer not in Apple Clang)$(RESET)"; \
		echo "$(YELLOW)   Atheris fuzzing will run in CI on Linux$(RESET)"; \
	fi
	@echo "$(BLUE)Running Rust fuzzing...$(RESET)"
	@$(MAKE) -C rust/fuzz quick

fuzz-deep: ## Run deep fuzzing (8 hours per target, production-grade)
	@$(MAKE) -C rust/fuzz deep

fuzz-target: ## Run single fuzz target (TARGET=<name>, TIME=60)
	@$(MAKE) -C rust/fuzz target TARGET=$(TARGET) TIME=$(TIME)

fuzz-coverage: ## Generate fuzzing coverage report
	@$(MAKE) -C rust/fuzz coverage

fuzz-corpus-generate: ## Generate initial fuzzing corpus
	@$(MAKE) -C rust/fuzz corpus-generate

fuzz-corpus-minimize: ## Minimize fuzzing corpus
	@$(MAKE) -C rust/fuzz corpus-minimize

fuzz-corpus-validate: ## Validate fuzzing corpus
	@$(MAKE) -C rust/fuzz corpus-validate

fuzz-triage: ## Triage fuzzing crashes
	@$(MAKE) -C rust/fuzz triage

asan: ## Run AddressSanitizer
	@$(MAKE) -C rust asan

tsan: ## Run ThreadSanitizer
	@$(MAKE) -C rust tsan

msan: ## Run MemorySanitizer
	@$(MAKE) -C rust msan

security-report: setup-logs ## Generate comprehensive security report
	@echo "$(BLUE)Generating security report...$(RESET)"
	@mkdir -p reports/security
	@REPORT_FILE="$$(pwd)/reports/security/report_$(TIMESTAMP).md"; \
	echo "# Security Report" > "$$REPORT_FILE"; \
	echo "Generated: $$(date -u +"%Y-%m-%d %H:%M:%S UTC")" >> "$$REPORT_FILE"; \
	echo "" >> "$$REPORT_FILE"; \
	echo "## Summary" >> "$$REPORT_FILE"; \
	echo "" >> "$$REPORT_FILE"; \
	echo "### Vulnerability Scan (cargo-audit)" >> "$$REPORT_FILE"; \
	(cd rust && cargo audit --json 2>/dev/null | jq -r '.vulnerabilities.count // 0' | xargs -I {} echo "- Vulnerabilities found: {}") >> "$$REPORT_FILE" || echo "- cargo-audit not run" >> "$$REPORT_FILE"; \
	echo "" >> "$$REPORT_FILE"; \
	echo "### License Compliance (cargo-deny)" >> "$$REPORT_FILE"; \
	(cd rust && cargo deny check licenses --format json 2>/dev/null | jq -r '.advisories | length' | xargs -I {} echo "- License issues: {}") >> "$$REPORT_FILE" || echo "- cargo-deny not run" >> "$$REPORT_FILE"; \
	echo "" >> "$$REPORT_FILE"; \
	echo "### Unsafe Code Analysis (cargo-geiger)" >> "$$REPORT_FILE"; \
	if [ -f rust/geiger-report.json ]; then \
		TOTAL=$$(jq '[.packages[].package.functions.safe + .packages[].package.functions.unsafe] | add' rust/geiger-report.json); \
		UNSAFE=$$(jq '[.packages[].package.functions.unsafe] | add' rust/geiger-report.json); \
		RATIO=$$(echo "scale=2; $$UNSAFE / $$TOTAL * 100" | bc); \
		echo "- Unsafe ratio: $$RATIO% ($$UNSAFE / $$TOTAL functions)" >> "$$REPORT_FILE"; \
	else \
		echo "- Geiger report not available" >> "$$REPORT_FILE"; \
	fi; \
	echo "" >> "$$REPORT_FILE"; \
	echo "## Details" >> "$$REPORT_FILE"; \
	echo "" >> "$$REPORT_FILE"; \
	echo "See individual tool outputs in logs/ directory for detailed findings." >> "$$REPORT_FILE"; \
	echo "" >> "$$REPORT_FILE"; \
	echo "Report saved to: $$REPORT_FILE"; \
	cat "$$REPORT_FILE"
	@echo "$(GREEN)✓ Security report generated$(RESET)"

security-deep: kani-verify fuzz-deep security-report ## Run deep security analysis (master target)

# ═══════════════════════════════════════════════════════════════════════════════
# 🧹 CLEANUP
# ═══════════════════════════════════════════════════════════════════════════════

clean: ## Clean temporary files
	@echo "$(BLUE)Cleaning temporary files...$(RESET)"
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf build/ dist/ .pytest_cache/ reports/ .DS_Store
	@echo "$(GREEN)✓ Cleaned temporary files$(RESET)"

clean-logs: ## Clean log files
	@echo "$(BLUE)Cleaning log files...$(RESET)"
	rm -rf $(LOG_DIR)
	@echo "$(GREEN)✓ Cleaned log files$(RESET)"

clean-all: clean clean-logs ## Clean everything including virtual environment
	@echo "$(BLUE)Cleaning everything...$(RESET)"
	rm -rf .venv/ .pyright_cache/ .ruff_cache/
	@echo "$(GREEN)✓ Cleaned everything$(RESET)"
