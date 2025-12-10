# Contributing to cachekit

Thank you for your interest in contributing to cachekit! This document provides guidelines and instructions for contributing to the project.

## Code of Conduct

This project follows a standard code of conduct. Please be respectful and professional in all interactions.

## Getting Started

### Prerequisites

- Python 3.9 or higher
- Rust 1.80 or higher
- Redis 5.0 or higher (for testing)
- [uv](https://github.com/astral-sh/uv) (recommended for dependency management)
- pytest-redis 3.0+ (automatically installed with `uv sync`)

### Development Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/cachekit-io/cachekit-py.git
   cd cachekit-py
   ```

2. **Install dependencies**
   ```bash
   uv sync && make install
   ```

3. **Run tests to verify setup**
   ```bash
   make test
   ```

## Development Workflow

### Before You Start

1. Check [existing issues](https://github.com/cachekit-io/cachekit-py/issues) to avoid duplicate work
2. For major changes, open an issue first to discuss the approach
3. Fork the repository and create a feature branch

### Making Changes

1. **Create a feature branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes**
   - Write clear, focused commits
   - Follow the code style guidelines (see below)
   - Add tests for new functionality
   - Update documentation as needed

3. **Run quality checks**
   ```bash
   make quick-check  # format + lint + critical tests
   ```

4. **Run full test suite**
   ```bash
   make check  # format + lint + type-check + all tests
   ```

5. **Commit your changes**
   ```bash
   git add <files>
   git commit -m "feat: add new feature"
   ```

## Code Style Guidelines

### Python

- **Line length**: 129 characters maximum
- **Formatter**: Ruff (runs automatically with `make format`)
- **Linter**: Ruff (runs automatically with `make lint`)
- **Type checker**: basedpyright (standard mode, zero errors enforced)
- **Type hints**: Required for all public APIs
- **Python 3.9+ compatibility**: Use `from __future__ import annotations` for modern union syntax
- **Docstrings**: Google style for public functions/classes
- **Imports**: Absolute imports only

**Example:**
```python
from __future__ import annotations

from cachekit import cache

@cache
def example_function(param: str) -> dict[str, str]:
    """Brief description of what this does.

    Args:
        param: Description of parameter

    Returns:
        Description of return value
    """
    return {"result": param}
```

### Rust

- **Formatter**: rustfmt (standard settings)
- **Linter**: clippy
- **Documentation**: Required for public APIs

### Code Principles

Follow **KISS, DRY, YAGNI, SOLID**:
- **Keep It Simple** - Prefer simple solutions over clever ones
- **Don't Repeat Yourself** - Extract common patterns (2+ uses minimum)
- **You Aren't Gonna Need It** - Build only what's needed now
- **Single Responsibility** - Each component has one clear purpose

**Prefer:**
- Guard clauses over deep nesting
- Early returns for readability
- Explicit over implicit
- Composition over inheritance

## Testing

### Writing Tests

- **Critical tests**: Essential functionality in `tests/critical/` (must always pass)
- **Unit tests**: Fast, isolated tests in `tests/unit/`
- **Integration tests**: Tests requiring Redis in `tests/integration/`
- **Performance tests**: Benchmarks in `tests/performance/`

**Test markers:**
```python
import pytest

@pytest.mark.critical
def test_cache_basic_functionality():
    """Critical test - must pass for library to be usable"""
    pass

@pytest.mark.unit
def test_cache_configuration():
    """Test cache configuration validation"""
    pass

@pytest.mark.integration
def test_redis_connection():
    """Test actual Redis connection (requires Redis running)"""
    pass
```

**Redis Test Isolation:**
All tests requiring Redis must use pytest-redis for proper isolation:
```python
from ..utils.redis_test_helpers import RedisIsolationMixin

class TestMyCacheFeature(RedisIsolationMixin):
    def test_feature(self):
        # Test automatically gets isolated Redis instance
        pass
```

### Running Tests

```bash
# All tests
make test

# Critical tests only (fastest, must pass before commit)
make test-critical

# Specific test file
uv run pytest tests/unit/test_decorators.py -v

# Specific test function
uv run pytest tests/unit/test_decorators.py::test_cache_basic -v

# Run by marker
uv run pytest -m critical -v     # Critical tests
uv run pytest -m unit -v         # Unit tests
uv run pytest -m integration -v  # Integration tests

# With coverage
make test-cov
```

**Important**: pytest-redis is required for all Redis-dependent tests. If tests fail with "pytest-redis is required", run `uv sync` to install dependencies.

### Test Coverage

- Aim for >85% coverage for new code
- All public APIs must have tests
- Edge cases and error conditions must be tested

**Rust Test Coverage**:
- ByteStorage module: 82% coverage (measured via LLVM source-based coverage)
- Encryption modules: Integration tests only (PyO3 cdylib limitation prevents coverage measurement)
- All Rust functionality validated via Python integration tests in `tests/critical/`
- PyO3's cdylib architecture prevents LLVM coverage tracking across module boundaries
- This is a known limitation, not a code quality issue

## Pull Request Process

1. **Ensure all checks pass**
   ```bash
   make check  # Must pass before PR
   ```

2. **Update documentation**
   - Update README.md if adding user-facing features
   - Add/update docstrings
   - Update CHANGELOG.md (if exists)

3. **Write clear PR description**
   - What problem does this solve?
   - What changes were made?
   - How was it tested?
   - Any breaking changes?

4. **PR Title Format**
   ```
   feat: add distributed cache warming
   fix: resolve connection pool leak
   docs: update getting started guide
   test: add integration tests for encryption
   refactor: simplify serializer interface
   ```

5. **Review process**
   - Address reviewer feedback
   - Keep PR focused and reasonably sized
   - Rebase on main if needed

## Common Tasks

### Building Rust Extension

```bash
make build  # Standard build
make build-pgo  # Profile-Guided Optimization (5-8% faster)
```

### Running Benchmarks

```bash
make benchmark-quick  # Quick performance check
```

### Formatting Code

```bash
make format  # Auto-format Python and Rust
```

### Type Checking

```bash
make type-check  # Run basedpyright type checker (zero errors)
```

## Project Structure

```
cachekit/
├── src/cachekit/        # Python package
│   ├── decorators/      # Cache decorators
│   ├── serializers/     # Serialization engines
│   ├── reliability/     # Circuit breaker, etc.
│   └── ...
├── rust/                # Rust extensions
│   └── src/
│       ├── serialization/
│       ├── compression.rs
│       └── ...
├── tests/               # Test suite
│   ├── critical/        # Critical path tests
│   ├── unit/
│   ├── integration/
│   └── performance/
├── docs/                # Documentation
└── pyproject.toml       # Project metadata
```

## Need Help?

- **Questions**: Open a [GitHub Discussion](https://github.com/cachekit-io/cachekit-py/discussions)
- **Bug Reports**: Open an [issue](https://github.com/cachekit-io/cachekit-py/issues)
- **Security Issues**: See [SECURITY.md](SECURITY.md)

## License

By contributing to cachekit, you agree that your contributions will be licensed under the MIT License.
