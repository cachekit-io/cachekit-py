# CacheKit Test Suite

## Test Directory Structure

```
tests/
├── critical/      # Must-pass tests for release (@pytest.mark.critical)
├── unit/          # Fast, mocked tests (@pytest.mark.unit)
├── integration/   # Real Redis tests (@pytest.mark.integration)
├── performance/   # Benchmarks (@pytest.mark.performance)
└── fuzzing/       # Atheris/cargo-fuzz targets
```

## Test Placement Decision Tree

1. Does it require Redis? → `integration/`
2. Is it security-critical? → `critical/`
3. Is it a benchmark? → `performance/`
4. Is it a fuzzer? → `fuzzing/`
5. Otherwise → `unit/`

## Naming Conventions

- `test_<module>.py` - Tests for a specific module
- `test_<feature>_<type>.py` - Tests for a feature with type suffix

## Markers

- `@pytest.mark.unit` - Fast, mocked (< 1s)
- `@pytest.mark.integration` - Requires Redis
- `@pytest.mark.critical` - Must pass for release
- `@pytest.mark.performance` - Benchmarks
- `@pytest.mark.slow` - Slow tests (> 5s)

## Quality Rules

1. **No empty assertions** - Every test must validate behavior
2. **Skipped tests need reasons** - Format: `@pytest.mark.skip(reason="TODO: description")`
3. **No test theatre** - Don't mock the system under test

## Running Tests

```bash
make quick-check           # Full validation
uv run pytest tests/critical/  # Critical only
uv run pytest -m unit      # Unit tests only
uv run pytest -m integration   # Integration tests
```
