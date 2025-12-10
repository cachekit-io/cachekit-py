"""Backward compatibility stub for legacy CacheSerializer tests.

IMPORTANT: CacheSerializer has been removed from PyRedis Cache Pro.
These tests are kept for historical reference but test obsolete functionality.
The new architecture uses UniversalSerializer which doesn't have compression
thresholds or pattern detection - it automatically handles all Python types.
"""

import pytest

# Mark these tests as legacy/obsolete
CACHE_SERIALIZER_AVAILABLE = False


# Provide a stub class to prevent import errors
class CacheSerializer:
    """Stub class - CacheSerializer no longer exists."""

    pass


# Mark all tests using this as skipped
pytest.skip("CacheSerializer has been removed. These tests are for obsolete functionality.", allow_module_level=True)
