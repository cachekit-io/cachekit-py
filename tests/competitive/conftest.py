"""Pytest configuration for competitive tests.

Override autouse Redis fixtures — competitive tests use backend=None (L1-only).
"""

import pytest


@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation():
    """Override root conftest's Redis isolation — competitive tests don't need Redis."""
    yield
