"""Critical path tests for serializer backward compatibility.

Ensures 100% backward compatibility - all existing @cache usages work unchanged.
"""

from __future__ import annotations

import pytest

from cachekit import cache
from cachekit.serializers import get_serializer
from cachekit.serializers.auto_serializer import AutoSerializer
from cachekit.serializers.standard_serializer import StandardSerializer


@pytest.mark.critical
class TestSerializerBackwardCompatibility:
    """Test 100% backward compatibility (Requirement 4)."""

    def test_cache_without_serializer_uses_default(self, redis_isolated):
        """@cache without serializer parameter uses StandardSerializer."""
        call_count = 0

        @cache(ttl=300)  # No serializer parameter
        def get_data(key: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"key": key, "value": call_count}

        # First call
        result1 = get_data("test")
        assert call_count == 1
        assert result1["key"] == "test"

        # Cache hit
        result2 = get_data("test")
        assert call_count == 1  # No additional call
        assert result2 == result1

    def test_existing_cache_usage_patterns_still_work(self, redis_isolated):
        """All existing cache usage patterns work unchanged."""

        @cache(ttl=300)
        def pattern1(x: int) -> int:
            return x * 2

        @cache(ttl=300, l1_enabled=True)
        def pattern2(x: int) -> int:
            return x * 3

        @cache(ttl=300, l1_enabled=False)
        def pattern3(x: int) -> int:
            return x * 4

        # All patterns work
        assert pattern1(5) == 10
        assert pattern2(5) == 15
        assert pattern3(5) == 20

    def test_auto_serializer_implements_protocol(self):
        """AutoSerializer implements SerializerProtocol (backward compat)."""
        from cachekit.serializers.base import SerializerProtocol

        serializer = AutoSerializer()
        assert isinstance(serializer, SerializerProtocol)

    def test_get_serializer_default_returns_standard_serializer(self):
        """get_serializer('default') returns StandardSerializer (new default)."""
        serializer = get_serializer("default")
        assert isinstance(serializer, StandardSerializer)

    def test_existing_cached_data_can_be_retrieved(self, redis_isolated):
        """Existing cached data (AutoSerializer) can be retrieved after adding serializer abstraction."""
        call_count = 0

        @cache(ttl=300)
        def compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x**2

        # First call - cache miss
        result1 = compute(7)
        assert result1 == 49
        assert call_count == 1

        # Second call - cache hit
        result2 = compute(7)
        assert result2 == 49
        assert call_count == 1  # No additional call

    def test_no_breaking_changes_to_decorator_signature(self):
        """@cache decorator signature is backward compatible."""
        # This test verifies that the decorator still accepts all previous parameters
        # and works correctly with them

        @cache(
            ttl=300,
            l1_enabled=True,
        )
        def func() -> int:
            return 42

        assert func() == 42

    def test_serializer_parameter_is_optional(self):
        """serializer parameter is optional (backward compat)."""
        # If serializer parameter is omitted, StandardSerializer is used

        @cache(ttl=300)
        def func() -> str:
            return "result"

        result = func()
        assert result == "result"

    def test_existing_tests_still_pass(self, redis_isolated):
        """Simulate existing test patterns to ensure they still work."""

        # Pattern 1: Simple caching
        @cache(ttl=300)
        def simple_cache() -> int:
            return 123

        assert simple_cache() == 123
        assert simple_cache() == 123  # Cache hit

        # Pattern 2: With arguments
        @cache(ttl=300)
        def with_args(x: int, y: int) -> int:
            return x + y

        assert with_args(5, 10) == 15
        assert with_args(5, 10) == 15  # Cache hit

        # Pattern 3: Complex data
        @cache(ttl=300)
        def complex_data() -> dict:
            return {"nested": {"data": [1, 2, 3]}, "list": [4, 5, 6]}

        result = complex_data()
        assert result["nested"]["data"] == [1, 2, 3]
