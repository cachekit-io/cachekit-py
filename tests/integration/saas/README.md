# Python SDK E2E Testing Against SaaS API

End-to-end integration tests for the Python cachekit SDK against the cachekit SaaS backend.

These tests validate that the Python SDK works correctly with the production SaaS API, including:
- Authentication and authorization
- Cache operations (GET, SET, DELETE)
- Data handling and serialization
- Error handling and recovery
- Performance characteristics
- Decorator functionality

**Note**: These are integration tests that require a running SaaS API. For unit tests of the SDK itself (without SaaS backend), see `/cachekit/tests/`.

---

## IMPORTANT: SaaS Worker Required

**⚠️ These tests REQUIRE a running SaaS Worker on localhost:8787.**

Before running tests, start the Worker locally:

```bash
# Terminal 1: Start local Worker
cd /Users/68824/code/27B/cachekit-workspace/saas/worker
make dev
# This starts wrangler dev server on http://localhost:8787

# Terminal 2: Run tests
cd /Users/68824/code/27B/cachekit-workspace/cachekit
uv run pytest tests/integration/saas/ -v
```

**Alternative**: Start Worker + Dashboard together (requires tmux):
```bash
cd /Users/68824/code/27B/cachekit-workspace/saas
make local
```

If you see `ConnectionRefusedError` or `Worker health check failed`, the Worker is not running locally.

---

## Quick Start

### 1. Prerequisites

- Python 3.10+
- uv package manager
- cachekit SDK installed locally or from PyPI
- Access to cachekit SaaS API (dev or prod)

### 2. Get API Credentials

You need an API key to run these tests:

```bash
# 1. Create account at https://app.dev.cachekit.io
# 2. Generate API key in dashboard
# 3. Export it:
export CACHEKIT_API_KEY="ck_sdk_your_key_here"
export CACHEKIT_API_URL="https://api.dev.cachekit.io"  # or your custom URL
```

### 3. Install Dependencies

```bash
cd cachekit/tests/integration/saas
uv sync --all-extras
```

### 4. Run Tests

```bash
# Run all E2E tests
uv run pytest -v

# Run specific test file
uv run pytest test_sdk_e2e.py -v

# Run with verbose output
uv run pytest -v -s

# Run in parallel (4 workers)
uv run pytest -v -n 4

# Run with timeout protection
uv run pytest -v --timeout=30
```

---

## Environment Configuration

### Required Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `CACHEKIT_API_KEY` | SDK API key (ck_sdk_...) | `ck_sdk_abc123...` |
| `CACHEKIT_API_URL` | API endpoint URL | `https://api.dev.cachekit.io` |

### Optional Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `CACHEKIT_NAMESPACE` | Cache namespace prefix | `sdk_e2e_test` |
| `CACHEKIT_DEFAULT_TTL` | Default TTL in seconds | `3600` |
| `CACHEKIT_ENABLE_COMPRESSION` | Enable LZ4 compression | `false` |

### .env Files

Create `.env.dev` or `.env.test` to set variables automatically:

```bash
# .env.dev
CACHEKIT_API_KEY=ck_sdk_your_dev_key
CACHEKIT_API_URL=https://api.dev.cachekit.io

# .env.test
CACHEKIT_API_KEY=ck_sdk_your_test_key
CACHEKIT_API_URL=https://api.test.cachekit.io
```

Load with:
```bash
set -a; source .env.dev; set +a
uv run pytest -v
```

---

## Test Files

### test_sdk_e2e.py
**Basic SDK functionality and decorator testing**

Tests:
- Decorator application on functions
- Basic GET/SET/DELETE operations
- Cache hit/miss validation
- TTL handling

Run:
```bash
uv run pytest test_sdk_e2e.py -v
```

### test_sdk_data_handling.py
**Data serialization and type handling**

Tests:
- Dict/list serialization
- Large values (>1MB)
- Special characters and Unicode
- Binary data handling
- Concurrent writes

Run:
```bash
uv run pytest test_sdk_data_handling.py -v
```

### test_sdk_error_handling.py
**Error cases and recovery**

Tests:
- Invalid API keys
- Network errors and retries
- Rate limiting behavior
- Malformed requests
- Timeout handling

Run:
```bash
uv run pytest test_sdk_error_handling.py -v
```

### test_sdk_performance.py
**Performance characteristics**

Tests:
- Decorator overhead
- Serialization speed
- Cache operation latency
- Throughput under load

Run:
```bash
uv run pytest test_sdk_performance.py -v -s
```

### conftest.py
**Shared pytest fixtures and configuration**

Provides:
- Test fixtures for cache clients
- Cleanup fixtures
- Performance tracking utilities
- Mock API responses

---

## Running Against Different Environments

### Development Environment

```bash
export CACHEKIT_API_KEY="ck_sdk_dev_key"
export CACHEKIT_API_URL="https://api.dev.cachekit.io"
uv run pytest -v
```

### Staging Environment

```bash
export CACHEKIT_API_KEY="ck_sdk_staging_key"
export CACHEKIT_API_URL="https://api.staging.cachekit.io"
uv run pytest -v
```

### Production Environment

```bash
export CACHEKIT_API_KEY="ck_sdk_prod_key"
export CACHEKIT_API_URL="https://api.cachekit.io"

# Use stricter test settings for production
uv run pytest -v --timeout=60
```

### Local Development (Worker)

```bash
# Start the worker locally
cd saas
wrangler dev --local

# In another terminal:
export CACHEKIT_API_KEY="test_key_for_local_dev"
export CACHEKIT_API_URL="http://localhost:8787"
cd cachekit/tests/integration/saas
uv run pytest -v
```

---

## Test Execution Examples

### Quick Smoke Test

```bash
# Run just the essential decorator test
uv run pytest test_sdk_e2e.py::test_cache_decorator_basic -v
```

### Full Test Suite

```bash
# Run everything with full output
uv run pytest -v --tb=short
```

### Performance Benchmark

```bash
# Run performance tests with detailed output
uv run pytest test_sdk_performance.py -v -s
```

### Error Scenario Testing

```bash
# Run only error handling tests
uv run pytest test_sdk_error_handling.py -v
```

### Parallel Execution

```bash
# Run with 4 workers for faster execution
uv run pytest -v -n 4

# Run with auto-detected worker count
uv run pytest -v -n auto
```

---

## Performance Metrics

The test suite tracks:

- **Decorator overhead**: Time added by caching decorator vs. raw function
- **Serialization speed**: Time to serialize/deserialize cached values
- **Cache operation latency**: GET/SET/DELETE operation times (p50, p95, p99)
- **Network latency**: Round-trip time to SaaS API
- **Throughput**: Operations per second

Results are stored in `perf_stats.py` and can be reviewed after test runs.

---

## Continuous Integration

### GitHub Actions Example

```yaml
name: SDK E2E Tests

on: [push, pull_request]

jobs:
  e2e-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Install uv
        uses: astral-sh/setup-uv@v1

      - name: Set Python version
        run: uv python install 3.11

      - name: Install dependencies
        run: |
          cd cachekit/tests/integration/saas
          uv sync --all-extras

      - name: Run E2E tests
        env:
          CACHEKIT_API_KEY: ${{ secrets.CACHEKIT_API_KEY_DEV }}
          CACHEKIT_API_URL: https://api.dev.cachekit.io
        run: |
          cd cachekit/tests/integration/saas
          uv run pytest -v --tb=short
```

---

## Troubleshooting

### Issue: "401 Unauthorized"

**Cause**: Invalid or expired API key

**Fix**:
```bash
# Check your API key
echo $CACHEKIT_API_KEY

# Regenerate in dashboard
# Then export and retry:
export CACHEKIT_API_KEY="ck_sdk_new_key"
uv run pytest -v
```

### Issue: "Connection refused" or "Network error"

**Cause**: API endpoint unreachable

**Fix**:
```bash
# Verify URL is correct
echo $CACHEKIT_API_URL

# Test connectivity
curl -H "Authorization: Bearer $CACHEKIT_API_KEY" \
  $CACHEKIT_API_URL/health

# For local dev, ensure worker is running:
cd saas
wrangler dev
```

### Issue: Tests timeout

**Cause**: Slow API or network

**Fix**:
```bash
# Increase timeout to 60 seconds
uv run pytest -v --timeout=60

# Or run specific fast tests first
uv run pytest test_sdk_e2e.py::test_cache_decorator_basic -v
```

### Issue: Data Integrity Errors

**Cause**: SDK serialization issue or API corruption

**Fix**:
```bash
# Run data handling tests in verbose mode
uv run pytest test_sdk_data_handling.py -v -s

# Check error messages for specific data type issues
```

### Issue: "pytest: command not found"

**Fix**:
```bash
# Use uv to run pytest
uv run pytest -v

# Or ensure virtualenv is activated
source .venv/bin/activate
pytest -v
```

---

## Best Practices

1. **Run smoke tests first** - Start with basic decorator test before full suite
2. **Test against dev first** - Always test against development API before staging/prod
3. **Set reasonable timeouts** - Use `--timeout=30` to catch hanging tests
4. **Use parallel execution** - Speed up test runs with `-n 4`
5. **Review performance metrics** - Check `perf_stats.py` for regressions
6. **Keep test data isolated** - Use unique namespace to avoid cross-test interference
7. **Clean up resources** - Tests should clean up created cache keys automatically

---

## Integration with Development Workflow

### Before Committing SDK Changes

```bash
# 1. Run local unit tests
cd cachekit
uv run pytest tests/

# 2. Run E2E tests against dev
cd tests/integration/saas
CACHEKIT_API_URL=https://api.dev.cachekit.io uv run pytest -v

# 3. If all pass, push to branch
```

### Before Deploying API Changes

```bash
# Deploy to dev first
cd saas
wrangler deploy --env development

# Run E2E tests against dev
cd ../cachekit/tests/integration/saas
CACHEKIT_API_URL=http://localhost:8787 uv run pytest -v

# If tests pass, deploy to prod
cd ../../saas
wrangler deploy --env production
```

---

## Related Documentation

- [cachekit SDK Documentation](../../../README.md)
- [SaaS API Documentation](../../../saas/README.md)
- [SDK Unit Tests](../../../tests/)
- [pytest Documentation](https://docs.pytest.org/)
- [uv Documentation](https://docs.astral.sh/uv/)

---

**Last Updated**: 2025-11-18
**Test Coverage**: ~95% of SDK public API
