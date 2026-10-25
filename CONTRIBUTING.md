# Contributing to cachekit

Thank you for your interest in contributing to cachekit! This document provides guidelines and instructions for contributing to the project.

## Code of Conduct

This project follows a standard code of conduct. Please be respectful and professional in all interactions.

## Getting Started

### Prerequisites

- Python 3.11 or higher
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

## Documentation scope

User-facing docs (the README and `docs/`) follow CacheKit's shared rule on what belongs in them:
[What belongs in these docs](https://docs.cachekit.io/contributing/#what-belongs-in-these-docs).
`prek install` (or `pre-commit install`) sets up hooks that reject internal references in README
files, `docs/` and commit messages, and internal ticket ids, with or without the hyphen, in every
tracked file except `CHANGELOG.md` and `.github/workflows/pr-title-lint.yml`.

## Code Style Guidelines

### Python

- **Line length**: 129 characters maximum
- **Formatter**: Ruff (runs automatically with `make format`)
- **Linter**: Ruff (runs automatically with `make lint`)
- **Type checker**: basedpyright (standard mode, zero errors enforced)
- **Type hints**: Required for all public APIs
- **Annotations**: Use `from __future__ import annotations`
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

# User journeys: the built wheel in a fresh venv, nothing configured (docs/CONTRIBUTING.md)
make journeys
```

**Important**: pytest-redis is required for all Redis-dependent tests. If tests fail with "pytest-redis is required", run `uv sync` to install dependencies.

**Which Redis the tests use.** Locally, the suite starts its own `redis-server` (it must be on `PATH`) and never touches a Redis you already run on port 6379. With `REDIS_URL` set, as CI sets it, the suite uses that server instead.

> [!CAUTION]
> With `REDIS_URL` set, the tests erase **every database** on that server: pytest-redis runs `FLUSHALL` after each test. The suite uses only the URL's host and port and ignores its database number, so `/15` protects nothing. Point `REDIS_URL` only at a dedicated, disposable server that holds no data you need.

The spawned server listens on a unix socket under pytest's base temp directory, and a socket path over about 107 characters fails every Redis test with `UnixSocketTooLong`. The `make test*` targets pass a short `--basetemp` for that reason, remove it when the tests pass, and keep it, printing its path, when they fail. If your `TMPDIR` is long and you run pytest directly, pass a short base temp that no other run uses, for example `uv run pytest -n auto --basetemp=/tmp/pt-$$ && rm -rf /tmp/pt-$$`. pytest never removes a base temp you pass, so remove it yourself, as the `&& rm -rf` does after a passing run. Do not reuse one fixed path across runs: pytest empties the base temp directory when a run starts, so two runs sharing it delete each other's files.

**Testing the nothing-configured path.** Every test gets a Redis-backed provider from an autouse fixture in `tests/conftest.py`, so a decorator under test never sees what a user with nothing configured sees. Request the `nothing_configured` fixture to opt out: it installs the real default provider, clears `REDIS_URL` and every `CACHEKIT_*` variable in any casing, needs no Redis, and fails the test if the provider builds a backend. Add `no_redis_built` to fail on any Redis client construction. A promise about default behaviour also needs a fresh-interpreter test, as in `tests/critical/test_zero_config_fresh_interpreter.py`: its child process refuses every outbound connection, because CI serves Redis at `localhost:6379`, the address an unconfigured client would dial, and a test that only checks for a cache hit passes there either way.

**Executable docs are tests.** Every PR also runs the docstring examples in `src/`, the tests in `tests/docs/`, and the code blocks in `docs/`. The docstring and `docs/` examples run twice: once beside a Redis server and once with none. A behaviour change must update the affected examples in the same PR. Check them locally with `make test-doctest` (with `REDIS_URL` and `CACHEKIT_REDIS_URL` unset: the `RedisBackendConfig()` defaults example reads them), `uv run pytest tests/docs/` and `make test-docs-quick`.

**Which pandas the tests use.** The lockfile pins pandas 2.x, but a fresh install resolves the newest pandas. Every PR also runs the unit and critical suites with the newest pandas 3.x installed over the lockfile (the `Tests (pandas 3)` job), because pandas 3's copy-on-write changes what a returned frame allows. To reproduce it locally:

```bash
uv run --with "pandas>=3,<4" pytest tests/unit/ -m "not slow" -n auto
```

**The nightly run.** `.github/workflows/nightly.yml` runs the suites no PR job selects, every night at 19:17 UTC. It has three independent jobs. The first runs `tests/competitive`. The second runs `tests/performance -m "performance and not slow"`, whose wall-clock numbers are reported, not asserted, and then the serializer micro-benchmarks once, untimed. The third builds the wheel on Python 3.11 and resolves it with `--resolution lowest`, so numpy, pandas and pyarrow sit at the `data` extra's floors. It then runs `tests/unit` against that wheel, not the source tree. A failed run opens or updates the "Nightly is failing" issue. To run it on demand, use **Run workflow** on the Actions page, or `gh workflow run nightly.yml`. A PR that edits the workflow file runs it too.

**Mutation smoke.** `scripts/mutation_smoke.py` holds hand-written mutants: each is one exact string edit to a guard in `src/`, plus the tests recorded as its killers. The nightly `Mutation Smoke` workflow runs the baseline and every mutant as parallel jobs, and opens or updates a tracking issue when one fails. A mutant counts as killed only when pytest exits 1 and at least one of its recorded killers fails; a collection error, a setup or teardown error, a timeout or an unrelated failure never counts. The baseline checks that every anchor matches exactly once and that every killer passes unmutated, so a deleted or renamed killer turns the baseline red. A mutant reports `SURVIVED` only once none of its killers catch it: with two killers, weakening one leaves the other to kill it. A refactored guard reports `NOT APPLIED`. Run it locally:

```bash
uv run python scripts/mutation_smoke.py list                     # mutant names
uv run python scripts/mutation_smoke.py run baseline             # anchors apply, killers pass
uv run python scripts/mutation_smoke.py run checksum-skipped     # one mutant: exit 0 killed, 1 survived, 2 not applied
```

The runner restores the source file after each mutant, runs only the killers' test files, and runs them serially, so one Redis serves the run. To add a mutant, append a `Mutant(...)` to `MUTANTS`: a behaviour name, the file, an anchor that matches exactly once, its replacement and the killer node ids. Find the killers by running the wide suite with the mutant applied, then record the tests that check that guard itself, not ones that fail by accident. Add the name to the matrix in `.github/workflows/mutation-smoke.yml`; `tests/unit/test_mutation_smoke.py` fails until the two lists agree. When a refactor breaks an anchor, `tests/unit/test_mutation_smoke.py` fails on that PR: re-anchor the mutant on the new code so it makes the same behaviour change.

### Test Coverage

- Aim for >85% coverage for new code
- All public APIs must have tests
- Edge cases and error conditions must be tested
- CI enforces a **total-coverage floor** inside pytest on every PR
  (`--cov-fail-under`, value in `.github/workflows/ci.yml`), independent of the
  Codecov upload — a PR whose combined coverage falls below it fails the `Tests`
  job. `make test-cov` enforces the same floor locally. Codecov still reports the
  finer-grained new-code (patch) coverage on top of this.

**Rust Test Coverage**:
- ByteStorage module: 82% coverage (measured via LLVM source-based coverage)
- Encryption modules: Integration tests only (PyO3 cdylib limitation prevents coverage measurement)
- All Rust functionality validated via Python integration tests in `tests/critical/`
- PyO3's cdylib architecture prevents LLVM coverage tracking across module boundaries
- This is a known limitation, not a code quality issue

## Review Guidance

`.github/CODEOWNERS` holds one rule: changes to `/.release-please-manifest.json` request the maintainer's review. That request does not block the merge. No other path has a code owner; the guidance below lists the paths that benefit from security and maintainer review:

**Security-sensitive paths** — consider requesting review from maintainers:
- `/rust/` — memory safety, FFI boundaries, cryptography
- `/src/cachekit/serializers/` and `/src/cachekit/reliability/` — serialization and fault tolerance
- `/.github/workflows/`, `/pyproject.toml`, `/rust/Cargo.toml`, `/.pre-commit-config.yaml` — supply chain configuration
- `/tests/security/`, `/tests/fuzz/`, `/SECURITY.md` — security documentation and testing

This is a **single-maintainer org** using documentation instead of enforcement. GitHub's review rulesets on this repo require zero code-owner approvals, so the path guidance above is a routing suggestion for pull requests, not a GitHub-enforced gate.

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
```

### Running Benchmarks

```bash
make perf               # Full battery: env fingerprint + timer calibration, serializer benchmarks, GIL scaling
make perf-compare       # Regression gate: fail on >10% median serializer regression vs baseline
make perf-ir            # Instruction-count gate: fail on a >=1% per-call regression (orjson 2%; needs valgrind)
make perf-ir-update     # Ratchet the committed instruction budgets down to the measured figures

make benchmark          # Serializer benchmarks only + save a local baseline
make benchmark-compare  # Re-run and fail on >10% median regression vs that baseline
make benchmark-gil      # Serializer thread-scaling under the current interpreter (GIL)
```

All perf tests live in one folder, `tests/performance/`; the pytest-benchmark ones are
selected by `--benchmark-only` and skipped elsewhere via the `--benchmark-skip` default.
Baselines are written to `.benchmarks/` (gitignored, per-machine), so regression
comparison is a local developer tool — wall-clock benchmarks deliberately do not gate
CI. `make perf` first prints a system fingerprint + environment verdict and
self-calibrates the timer, so numbers come with the context needed to trust them.

`make perf-ir` is the deterministic one: it counts main-thread instructions per call under
valgrind against budgets committed in `tests/performance/ir_baselines.json`, so a 1% regression
is visible however loaded the machine is. Budgets belong to the build that recorded them; on a
different interpreter, extension or C library, record a baseline on `main` first. Every pull
request also runs it in CI (the `Instruction budget` jobs, CPython 3.12), against the PR's base
measured on the same runner rather than against the committed budgets. Method, budgets and the
CI job: [docs/performance.md](docs/performance.md#instruction-budgets).

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
