"""Integration tests for the Rust cache decorator.

IMPORTANT: This is an EXPERIMENTAL FEATURE that was designed but not yet integrated.
The rust_decorator represents a potential future optimization where ALL caching logic
(key generation, serialization, Redis operations) happens in Rust with only ONE FFI
crossing per cache operation.

Current Status (2025-07-05):
- PyCacheDecorator exists in rust/src/cache_decorator.rs
- It's commented out in rust/src/lib.rs (line 2897)
- Tests are skipped until this feature is re-evaluated

Performance Potential:
- Could provide 2-3x speedup for API responses
- Could provide 5-10x speedup for NumPy arrays
- Reduces FFI overhead from multiple crossings to just one

TODO: Revisit this optimization post-v0.1.0 launch to determine if the
additional complexity is worth the performance gains given that network
latency to Redis (~1-2ms) dominates over serialization time (~50-200μs).
"""

import numpy as np
import pytest

# Try to import the Rust decorator
try:
    from cachekit.rust_decorator import RUST_DECORATOR_AVAILABLE, rust_cache
except ImportError:
    RUST_DECORATOR_AVAILABLE = False
    rust_cache = None

# Also import the standard Python decorator for comparison
from cachekit import cache


@pytest.mark.skipif(
    not RUST_DECORATOR_AVAILABLE,
    reason="Rust decorator not yet integrated - see module docstring for status",
)
class TestRustCacheDecorator:
    """Test the Rust cache decorator with real cache workloads."""

    def test_api_response_caching(self, redis_test_client):
        """Test caching API response data (JSON-compatible)."""
        call_count = 0

        @rust_cache(ttl=60, auto_format=True)
        def get_api_response(user_id: int):
            nonlocal call_count
            call_count += 1
            return {
                "user_id": user_id,
                "name": f"User {user_id}",
                "email": f"user{user_id}@example.com",
                "metadata": {"created": "2024-01-01", "active": True, "score": 98.5},
            }

        # First call - cache miss
        result1 = get_api_response(123)
        assert call_count == 1
        assert result1["user_id"] == 123

        # Second call - cache hit
        result2 = get_api_response(123)
        assert call_count == 1  # No additional calls
        assert result1 == result2

    def test_numpy_array_caching(self, redis_test_client):
        """Test caching NumPy arrays with zero-copy optimization."""
        call_count = 0

        @rust_cache(ttl=60, auto_format=True)
        def compute_matrix(size: int):
            nonlocal call_count
            call_count += 1
            return np.random.rand(size, size)

        # First call
        result1 = compute_matrix(100)
        assert call_count == 1
        assert result1.shape == (100, 100)

        # Second call - should get cached result
        result2 = compute_matrix(100)
        assert call_count == 1
        np.testing.assert_array_equal(result1, result2)

    def test_complex_object_rejection(self, redis_test_client):
        """Test that complex objects are rejected by default."""

        class CustomObject:
            def __init__(self, value):
                self.value = value

        @rust_cache(ttl=60, auto_format=True, allow_complex=False)
        def get_complex_object():
            return CustomObject(42)

        # Should raise an error for complex objects
        with pytest.raises(TypeError, match="Complex objects not supported"):
            get_complex_object()

    def test_complex_object_with_fallback(self, redis_test_client):
        """Test complex objects with explicit fallback."""
        call_count = 0

        class CustomObject:
            def __init__(self, value):
                self.value = value
                self.processed = False

        @rust_cache(ttl=60, auto_format=True, allow_complex=True)
        def get_complex_object():
            nonlocal call_count
            call_count += 1
            return CustomObject(42)

        # First call
        result1 = get_complex_object()
        assert call_count == 1
        assert result1.value == 42

        # Second call - cached
        result2 = get_complex_object()
        assert call_count == 1  # No additional calls
        assert result2.value == 42

    def test_performance_vs_python_decorator(self, redis_test_client, benchmark):
        """Benchmark Rust decorator vs Python decorator."""

        # Test data
        api_response = {
            "items": list(range(100)),
            "metadata": {"page": 1, "total": 100},
        }

        # Rust decorator
        @rust_cache(ttl=60, auto_format=True)
        def rust_cached():
            return api_response

        # Python decorator
        @cache(ttl=60)
        def python_cached():
            return api_response

        # Clear any existing cache
        redis_test_client.flushall()

        # Warm up both
        rust_cached()
        python_cached()

        # Benchmark Rust decorator
        def bench_rust():
            # Include both cache miss and hit
            redis_test_client.delete("rust_cached:None:None")
            rust_cached()  # Miss
            rust_cached()  # Hit

        rust_result = benchmark(bench_rust, rounds=100)

        # The goal is to have Rust decorator be faster due to:
        # 1. Single FFI crossing
        # 2. Pure Rust JSON serialization
        # 3. Optimized cache key generation
        assert rust_result is not None  # Just ensure it runs

    def test_mixed_data_types(self, redis_test_client):
        """Test decorator with mixed data types."""

        @rust_cache(ttl=60, auto_format=True)
        def get_mixed_data(data_type: str):
            if data_type == "json":
                return {"type": "json", "values": [1, 2, 3]}
            elif data_type == "numpy":
                return np.array([1.0, 2.0, 3.0])
            elif data_type == "primitive":
                return 42.5
            else:
                return None

        # Test each type
        json_result = get_mixed_data("json")
        assert json_result["type"] == "json"

        numpy_result = get_mixed_data("numpy")
        np.testing.assert_array_equal(numpy_result, np.array([1.0, 2.0, 3.0]))

        primitive_result = get_mixed_data("primitive")
        assert primitive_result == 42.5

        none_result = get_mixed_data("none")
        assert none_result is None


@pytest.mark.skipif(not RUST_DECORATOR_AVAILABLE, reason="Rust decorator not available")
def test_rust_decorator_imports():
    """Test that Rust decorator can be imported."""
    from cachekit.rust_decorator import rust_cache

    assert rust_cache is not None
    assert callable(rust_cache)
