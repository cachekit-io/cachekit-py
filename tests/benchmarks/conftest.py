"""Benchmark suite configuration - no Redis required.

These benchmarks instantiate serializers directly and never touch a backend,
so the root conftest's autouse Redis-isolation fixture (which would spawn a
local redis-server binary when REDIS_URL is unset) is overridden to a no-op
here, the same way tests/unit/conftest.py does for unit tests.
"""

import pytest


@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation():
    """Override root conftest's Redis isolation - benchmarks need no backend.

    Shadows the parent autouse fixture by having the same name (nearest
    conftest wins), so serializer benchmarks run without Redis.
    """
    # No-op for benchmarks - just yield without Redis setup
    yield
