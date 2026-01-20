# SDK + SaaS End-to-End Testing

Comprehensive integration testing for the cachekit Python SDK (@cache.io decorator) with live SaaS backend.

---

## Quick Start

### Prerequisites

1. **Worker running**:
   ```bash
   cd saas
   make dev  # Starts Worker at http://localhost:8787
   ```

2. **API Key**:
   - Via Dashboard: http://localhost:5173 → API Keys → Generate SDK Key
   - Or use test key from D1: `ck_sdk_dev_test_key`

3. **SDK installed**:
   ```bash
   cd cachekit
   make install  # Install cachekit library
   ```

4. **Test dependencies**:
   ```bash
   cd saas/tests/validation
   pip install -r requirements.txt
   ```

### Run Tests

```bash
# Set environment variables
export CACHEKITIO_API_URL="http://localhost:8787"
export CACHEKITIO_API_TOKEN="ck_sdk_your_key_here"

# Run all E2E tests
pytest test_sdk_e2e.py -v

# Run specific test
pytest test_sdk_e2e.py::test_basic_decorator_usage -v

# Run with coverage
pytest test_sdk_*.py --cov=cachekit --cov-report=html
```

---

## Test Organization

### Current Files

- **test_cache_integrity.py** (Existing) - HTTP API tests (15 tests)
  - CRUD operations, data integrity, rate limiting
  - 3 basic SDK tests (optional)

- **test_sdk_e2e.py** (NEW) - Core SDK E2E tests (15 tests)
  - @cache.io decorator functionality
  - L1/L2 cache interaction
  - cache_info() statistics
  - TTL, namespaces, authentication

- **conftest.py** (NEW) - Shared fixtures
  - SDK configuration
  - Decorator fixtures
  - Test data generators
  - Performance timers

### Planned Files

- **test_sdk_data_handling.py** - Serialization & edge cases (12 tests)
- **test_sdk_error_handling.py** - Error scenarios (10 tests)
- **test_sdk_performance.py** - Performance validation (8 tests)
- **test_sdk_advanced.py** - Advanced features (5 tests, optional)

**See**: `saas/tests/SDK_E2E_TESTING_SPEC.md` for complete implementation plan.

---

## Test Coverage Status

| Category | Tests | Status | Priority |
|----------|-------|--------|----------|
| Core functionality | 15 | ✅ Implemented | P0 |
| Data handling | 12 | ⏳ Planned | P0/P1 |
| Error handling | 10 | ⏳ Planned | P0/P1 |
| Performance | 8 | ⏳ Planned | P1 |
| Advanced features | 5 | ⏳ Planned | P2/P3 |
| **Total** | **50** | **30% complete** | - |

---

## Writing New Tests

### Basic Pattern

```python
import pytest
from cachekit import cache

@pytest.mark.sdk_e2e
def test_my_feature(cache_io_decorator, clean_cache):
    """Test description."""

    # Create decorated function
    @cache_io_decorator
    def my_function(x: int) -> int:
        return x * 2

    # Test behavior
    result = my_function(5)
    assert result == 10

    # Verify caching
    info = my_function.cache_info()
    assert info.currsize == 1
```

### Using Fixtures

```python
def test_with_factory(test_function_factory, clean_cache):
    """Test using function factory."""

    # Create test function
    compute = test_function_factory(
        name="compute",
        computation=lambda x: x ** 2
    )

    # Test
    assert compute(5) == 25
```

### Testing Error Scenarios

```python
def test_timeout_error(cache_io_decorator, clean_cache):
    """Test timeout handling."""

    import os
    os.environ["CACHEKITIO_TIMEOUT"] = "0.001"  # 1ms

    @cache.io
    def slow_function(x: int) -> int:
        return x * 2

    with pytest.raises(Exception) as exc_info:
        slow_function(5)

    assert "timeout" in str(exc_info.value).lower()
```

---

## Available Fixtures

### Configuration

- `sdk_config` - SDK configuration dict (api_url, api_token, namespace)
- `worker_health_check` - Ensures Worker is running before tests start

### Decorators

- `cache_io_decorator` - Configured @cache.io decorator
- `test_function_factory` - Factory for creating test functions

### Cache Management

- `clean_cache` - Cleans test namespace before/after test
- `unique_namespace` - Generates unique namespace for isolation

### Test Data

- `sample_data` - Sample data for various data types
- `pydantic_models` - Pydantic model classes for testing

### Utilities

- `http_client` - HTTP client for direct API calls
- `performance_timer` - Context manager for measuring latency

**See**: `conftest.py` for complete fixture documentation.

---

## Test Markers

Run tests by marker:

```bash
# SDK E2E tests only
pytest -m sdk_e2e -v

# Performance tests only
pytest -m performance -v

# Data handling tests only
pytest -m data_handling -v

# Error handling tests only
pytest -m error_handling -v
```

**Available markers**:
- `@pytest.mark.sdk_e2e` - SDK + SaaS integration tests
- `@pytest.mark.performance` - Performance/latency tests
- `@pytest.mark.data_handling` - Serialization/edge case tests
- `@pytest.mark.error_handling` - Error scenario tests

---

## CI/CD Integration

### GitHub Actions

Tests run automatically on PR and push to `main`:

```yaml
- name: Run SDK E2E tests
  env:
    CACHEKITIO_API_URL: http://localhost:8787
    CACHEKITIO_API_TOKEN: ${{ secrets.SDK_TEST_API_KEY }}
  run: |
    pytest saas/tests/validation/test_sdk_*.py -v
```

### Pre-Deployment Gate

**All P0 tests must pass** before deploying to production:

```bash
# Run critical tests
pytest saas/tests/validation/test_sdk_e2e.py -v

# Check exit code
if [ $? -ne 0 ]; then
  echo "❌ E2E tests failed - blocking deployment"
  exit 1
fi
```

---

## Troubleshooting

### "Connection refused" error

**Problem**: Worker not running

**Fix**:
```bash
cd saas
make dev
# Wait for "Ready on http://localhost:8787"
```

### "401 Unauthorized" errors

**Problem**: Invalid or missing API key

**Fix**:
```bash
# Check environment variable
echo $CACHEKITIO_API_TOKEN

# Generate new key via dashboard
open http://localhost:5173

# Or use test key
export CACHEKITIO_API_TOKEN="ck_sdk_dev_test_key"
```

### "Module not found: cachekit"

**Problem**: SDK not installed

**Fix**:
```bash
cd cachekit
make install  # Or: uv sync
```

### Tests hanging/timing out

**Problem**: Worker slow or unresponsive

**Fix**:
```bash
# Check Worker logs
cd saas
wrangler tail --env development

# Restart Worker
# Ctrl+C to stop, then:
make dev
```

### Import errors for Pydantic/other libraries

**Problem**: Test dependencies not installed

**Fix**:
```bash
cd saas/tests/validation
pip install -r requirements.txt
```

---

## Performance Expectations

### Latency Targets

- **L1 cache hit**: < 1ms (in-memory lookup)
- **L2 cache hit**: < 50ms (HTTP roundtrip to Worker)
- **Cache miss**: < 100ms (function execution + cache storage)

### Test Execution Time

- **Core tests** (test_sdk_e2e.py): ~30 seconds
- **All E2E tests** (when complete): ~5 minutes
- **Full suite** (HTTP API + SDK): ~7 minutes

---

## Contributing

### Adding New Tests

1. **Create test file** in `saas/tests/validation/`
2. **Import fixtures** from `conftest.py`
3. **Add marker**: `@pytest.mark.sdk_e2e`
4. **Document**: Add docstring with test purpose
5. **Run**: Verify test passes locally
6. **Update**: Add to coverage matrix in SDK_E2E_TESTING_SPEC.md

### Code Quality

- **Type hints**: Required for all test functions
- **Docstrings**: Required for all tests
- **Assertions**: Use clear, specific assertions
- **Cleanup**: Use `clean_cache` fixture to avoid test pollution

---

## Related Documentation

- **Specification**: `saas/tests/SDK_E2E_TESTING_SPEC.md` (complete implementation plan)
- **HTTP API tests**: `test_cache_integrity.py` (existing validation tests)
- **SDK source**: `cachekit/src/cachekit/`
- **SaaS Worker**: `saas/src/index.ts`
- **Backend**: `cachekit/src/cachekit/backends/cachekitio/`

---

**Last Updated**: 2025-11-18
**Status**: Phase 1 implemented (15 core tests), Phase 2-5 planned
**Next**: Implement data handling tests (test_sdk_data_handling.py)
