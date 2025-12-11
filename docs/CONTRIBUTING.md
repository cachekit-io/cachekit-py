# Contributing to cachekit Documentation

This guide helps you write testable, executable documentation examples that stay synchronized with the codebase.

## Writing Testable Examples

All Python code examples in markdown files are automatically tested using `pytest-markdown-docs`. This ensures examples remain accurate as the code evolves.

### ✅ Working Example

```python
from cachekit import cache

@cache(ttl=3600)
def get_user(user_id):
    return fetch_from_database(user_id)

# Use the cached function
user = get_user(123)
```

This example will execute successfully because:
- `cache` is imported from the public API
- `fetch_from_database()` is provided as a test stub
- All variables are defined

### Available Test Fixtures

The following variables and functions are automatically available in all documentation examples (no imports needed):

#### Core Library
- `cache` - The main caching decorator from cachekit
- `redis` - FakeRedis instance (in-memory Redis mock)

#### Standard Library
- `time` - Time module for delays and timestamps
- `asyncio` - Async/await utilities
- `logging` - Logging module
- `logger` - Pre-configured logger instance

#### Data Science
- `np` - NumPy (as `numpy`)
- `pd` - Pandas (as `pandas`)

#### Stub Functions

These functions are provided for documentation examples and return mock data:

- `do_expensive_computation()` - Returns computed result
- `fetch_from_database(user_id)` - Returns user data
- `build_profile(user_id)` - Returns profile data
- `fetch_user(user_id)` - Returns user object
- `process_business_logic(request_id)` - Returns processed result
- `process_data(data)` - Returns processed data
- `expensive_operation()` - Returns operation result
- `compute_intensive_result()` - Returns computation result
- `process_item(item_id)` - Returns processed item
- `important_data()` - Returns important data
- `transform(data)` - Returns transformed data
- `process_tenant_request(tenant_id, request)` - Returns tenant result

#### Configuration
- `secret_key` - Test encryption key (value: `"a" * 64`)
- `CACHEKIT_MASTER_KEY` - Environment variable set to `secret_key` (enables `@cache.secure` examples)

## Skipping Examples with `notest`

Some examples can't be tested automatically (e.g., they require external services). Mark these with `notest`:

````markdown
```python notest
# This example requires a production Redis instance
import redis
client = redis.Redis(host='production-redis.example.com', port=6379)
```
````

**When to use `notest`:**
- Examples requiring real external services (databases, APIs)
- Examples showing configuration file contents
- Examples demonstrating error scenarios
- Pseudocode or conceptual examples

**Always include a comment** explaining why the example is skipped:

````markdown
```python notest
# Real Redis connection required - not available in test environment
@cache(backend=redis_client)
def production_function():
    return fetch_from_production_db()
```
````

## Testing Locally

Before submitting documentation changes, test your examples:

### Quick Test (Quiet Mode)
```bash
pytest --markdown-docs docs/ -q
```

### Verbose Test (Detailed Output)
```bash
pytest --markdown-docs docs/ -v
```

### Test Specific File
```bash
pytest --markdown-docs docs/getting-started.md -v
```

### Using Make Targets
```bash
# Quick validation
make test-docs-quick

# Detailed output
make test-docs-examples
```

## Common Issues and Solutions

| Issue | Solution |
|-------|----------|
| `NameError: name 'xyz' is not defined` | Use available fixtures or mark example `notest` |
| `ImportError: No module named 'xyz'` | Import from cachekit public API or mark `notest` |
| `AssertionError` in example | Remove assertions or use variables that work with stubs |
| Example needs real Redis | Mark with `notest` and explain why |

## Best Practices

### ✅ DO

- **Import from public API**: `from cachekit import cache`
- **Use available fixtures**: Leverage `cache`, `redis`, stub functions
- **Test before submitting**: Run `make test-docs-examples`
- **Explain `notest` usage**: Add comment when skipping tests
- **Keep examples simple**: Focus on demonstrating one concept

### ❌ DON'T

- **Use internal imports**: Avoid `from cachekit.internal import X`
- **Reference undefined variables**: Either use fixtures or define in example
- **Leave broken examples**: Fix or mark `notest` with explanation
- **Add FIXME/TODO**: Complete examples or remove from docs
- **Skip local testing**: Always validate before PR

## Example Patterns

### Pattern 1: Basic Usage
```python
from cachekit import cache

@cache(ttl=60)
def simple_example():
    return do_expensive_computation()

result = simple_example()
```

### Pattern 2: With Configuration
```python
from cachekit import cache

@cache(
    ttl=3600,
    namespace="users"
)
def configured_example(user_id):
    return fetch_from_database(user_id)
```

### Pattern 3: Integration Example (notest)
````markdown
```python notest
# Requires external Redis - configuration example only
from cachekit import cache
import redis

# Your production setup
redis_client = redis.Redis(host='prod-redis', port=6379)

@cache(backend=redis_client)
def production_function():
    return fetch_from_production()
```
````

## Continuous Integration

All documentation examples are tested in CI on every pull request. Your PR will fail if:

- Examples have syntax errors
- Examples reference undefined variables
- Examples import missing modules
- Any example fails without `notest` marker

**Fix CI failures** by:
1. Running tests locally: `make test-docs-examples`
2. Fixing broken examples or adding `notest`
3. Pushing updated changes

## Questions?

- **Documentation issues**: [Open an issue](https://github.com/cachekit-io/cachekit-py/issues)
- **Example not working**: Check available fixtures above
- **Need new fixture**: Propose in issue or PR

---

**Remember**: Good documentation examples work out of the box. If users can copy-paste and run your example successfully, you've created excellent documentation.
