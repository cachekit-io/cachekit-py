"""Unit test configuration - no Redis required.

Unit tests are fast, in-memory tests that don't require Redis.
This conftest overrides the root conftest's autouse fixture to
skip Redis setup for pure unit tests.
"""

import pytest


@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation(request):
    """Override root conftest's Redis isolation for pure unit tests.

    Unit tests don't need Redis - they test in-memory functionality.
    This fixture overrides the parent conftest's autouse fixture
    by having the same name.
    """
    # No-op for unit tests - just yield without Redis setup
    yield
