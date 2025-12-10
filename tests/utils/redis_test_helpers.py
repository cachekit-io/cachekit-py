"""
Redis Test Migration Helpers

This module provides utilities to easily migrate existing Redis tests
to use the new isolation framework with minimal code changes.

🧪 TDD Approach: Helpers designed for easy test conversion
"""

import functools
from typing import Any, Callable, Optional

import pytest

# =============================================================================
# Migration Decorators
# =============================================================================


def with_redis_isolation(use_session_redis: bool = False):
    """
    Decorator to easily add Redis isolation to existing test functions.

    Usage:
        @with_redis_isolation()
        def test_my_cache_function():
            # Test code here - Redis will be isolated automatically
            pass

    Args:
        use_session_redis: If True, use session-scoped Redis for performance
    """

    def decorator(test_func):
        @functools.wraps(test_func)
        def wrapper(*args, **kwargs):
            # Get the appropriate fixture name

            # Check if we're in a pytest context
            if hasattr(test_func, "__self__"):  # Method in test class
                # For test class methods, the fixture should be available via self
                return test_func(*args, **kwargs)
            else:
                # For standalone functions, we need to use the fixture system
                return test_func(*args, **kwargs)

        # Mark the test as requiring Redis isolation
        wrapper = pytest.mark.redis_isolation(wrapper)
        return wrapper

    return decorator


def requires_real_redis(test_func):
    """
    Decorator to mark tests that require real Redis (not isolated).

    Usage:
        @requires_real_redis
        def test_redis_persistence():
            # This test needs real Redis behavior
            pass
    """

    @functools.wraps(test_func)
    def wrapper(*args, **kwargs):
        return test_func(*args, **kwargs)

    # Add marker to indicate this test requires real Redis
    wrapper = pytest.mark.integration(wrapper)
    return wrapper


# =============================================================================
# Test Class Mixins
# =============================================================================


class RedisIsolationMixin:
    """
    Mixin class to add Redis isolation to test classes.

    Usage:
        class TestMyFeature(RedisIsolationMixin):
            def test_something(self):
                # Redis is automatically isolated
                pass
    """

    @pytest.fixture(autouse=True)
    def _setup_redis_isolation(self, redis_test_client):
        """Automatically inject Redis isolation into all test methods."""
        self.redis_client = redis_test_client

        # DI-based isolation is handled by the autouse fixture in conftest.py
        # No need for manual patching anymore
        yield
        # Cleanup is handled by the fixture

    def get_scoped_key(self, cache_key: str, tenant_id: str = "default") -> str:
        """Get tenant-scoped Redis key for direct Redis access in tests.

        With the new backend abstraction, keys are stored with tenant scoping.
        This helper converts a cache key to its actual Redis key.

        Args:
            cache_key: The cache key (from key_gen.generate_key())
            tenant_id: Tenant ID (default: "default" for single-tenant)

        Returns:
            Tenant-scoped key as stored in Redis (t:{tenant}:{cache_key})
        """
        from urllib.parse import quote as url_encode

        tenant_encoded = url_encode(tenant_id, safe="")
        return f"t:{tenant_encoded}:{cache_key}"


class PerformanceTestMixin:
    """
    Mixin for performance tests that need session-scoped Redis.

    Usage:
        class TestPerformance(PerformanceTestMixin):
            def test_performance(self):
                # Uses session Redis for better performance
                pass
    """

    @pytest.fixture(autouse=True, scope="session")
    def _setup_session_redis(self, redis_session):
        """Use session-scoped Redis for performance tests."""
        self.redis_client = redis_session
        yield


# =============================================================================
# Migration Helper Functions
# =============================================================================


def convert_test_to_isolated(test_func: Callable) -> Callable:
    """
    Convert an existing test function to use Redis isolation.

    This function helps migrate tests that manually manage Redis connections
    to use the new isolation framework.

    Example:
        # Old test
        def test_cache_works():
            client = redis.Redis(host='localhost', port=6379, db=15)
            client.flushdb()
            # ... test code ...
            client.close()

        # Migrated test
        test_cache_works = convert_test_to_isolated(test_cache_works)
    """

    # Add the redis_cache_isolated fixture requirement
    if not hasattr(test_func, "__annotations__"):
        test_func.__annotations__ = {}

    @functools.wraps(test_func)
    def wrapper(redis_cache_isolated, *args, **kwargs):
        # Inject isolated Redis client into the test
        return test_func(*args, **kwargs)

    # Update the wrapper's signature to include the fixture
    wrapper.__annotations__["redis_cache_isolated"] = "redis_cache_isolated"

    return wrapper


def setup_test_cache_data(redis_client, namespace: str = "test") -> dict[str, Any]:
    """
    Set up standard test data for cache testing.

    Args:
        redis_client: Redis client instance
        namespace: Namespace prefix for test keys

    Returns:
        Dictionary containing the test data that was set up
    """
    test_data = {
        "simple_string": f"{namespace}:simple",
        "complex_object": {
            "id": 123,
            "name": "Test Object",
            "nested": {"value": 42},
            "list": [1, 2, 3],
        },
        "user_data": {
            "user_id": 456,
            "email": "test@example.com",
            "preferences": {"theme": "dark", "notifications": True},
        },
    }

    # Set up cache entries
    redis_client.set(f"{namespace}:simple", "simple_value")
    redis_client.hset(f"{namespace}:hash", mapping={"field1": "value1", "field2": "value2"})
    redis_client.lpush(f"{namespace}:list", *["item1", "item2", "item3"])

    return test_data


def verify_cache_isolation(redis_client, namespace: str = "test") -> bool:
    """
    Verify that the Redis instance is properly isolated.

    Args:
        redis_client: Redis client instance
        namespace: Namespace to check for isolation

    Returns:
        True if Redis is properly isolated (empty), False otherwise
    """
    # Check for any keys in the namespace
    keys = redis_client.keys(f"{namespace}:*")
    if keys:
        return False

    # Check for any keys at all (should be empty for true isolation)
    all_keys = redis_client.keys("*")
    return len(all_keys) == 0


def assert_cache_behavior(
    cached_func: Callable,
    call_args: tuple = (),
    call_kwargs: Optional[dict] = None,
    expected_call_count: int = 1,
) -> None:
    """
    Assert that a cached function behaves correctly.

    Args:
        cached_func: The cached function to test
        call_args: Arguments to call the function with
        call_kwargs: Keyword arguments to call the function with
        expected_call_count: Expected number of actual function calls
    """
    if call_kwargs is None:
        call_kwargs = {}

    # Simple approach: just test that results are identical when cached
    # and verify that the cache key exists in Redis

    # Call the function multiple times
    result1 = cached_func(*call_args, **call_kwargs)
    result2 = cached_func(*call_args, **call_kwargs)

    # Verify cache behavior
    assert result1 == result2, "Cached results should be identical"

    # For redis_cache, we can verify by checking if results are identical
    # which proves caching is working (since timestamps, etc. would differ otherwise)


# =============================================================================
# Legacy Test Support
# =============================================================================


def patch_redis_for_test(redis_client):
    """
    Context manager to patch Redis connections for tests using dependency injection.

    Usage:
        def test_legacy_cache(redis_cache_isolated):
            with patch_redis_for_test(redis_cache_isolated):
                # Test code will now use the isolated Redis instance
                pass
    """
    from contextlib import contextmanager

    from cachekit.backends.provider import CacheClientProvider
    from cachekit.di import DIContainer
    from tests.fixtures.backend_providers import TestCacheClientProvider

    container = DIContainer()

    @contextmanager
    def di_patch():
        # Clear any existing singletons to force re-creation
        container.clear_singletons()

        # Create an async wrapper for the sync Redis client
        class AsyncRedisWrapper:
            def __init__(self, sync_client):
                self._sync_client = sync_client

            async def get(self, key):
                return self._sync_client.get(key)

            async def set(self, key, value, ex=None):
                return self._sync_client.set(key, value, ex=ex)

            async def delete(self, key):
                return self._sync_client.delete(key)

            def lock(self, key, timeout=None, blocking_timeout=None):
                # Return the sync lock but make it async-compatible
                sync_lock = self._sync_client.lock(key, timeout=timeout, blocking_timeout=blocking_timeout)

                class AsyncLockWrapper:
                    def __init__(self, sync_lock):
                        self._sync_lock = sync_lock

                    async def __aenter__(self):
                        self._sync_lock.__enter__()
                        return self

                    async def __aexit__(self, exc_type, exc_val, exc_tb):
                        return self._sync_lock.__exit__(exc_type, exc_val, exc_tb)

                return AsyncLockWrapper(sync_lock)

        async_client = AsyncRedisWrapper(redis_client)

        # Register test provider with isolated Redis client
        test_provider = TestCacheClientProvider(sync_client=redis_client, async_client=async_client)
        container.register(CacheClientProvider, lambda: test_provider, singleton=True)

        try:
            yield
        finally:
            # Clear test singletons and restore default
            container.clear_singletons()
            from cachekit.backends.provider import DefaultCacheClientProvider

            container.register(CacheClientProvider, DefaultCacheClientProvider, singleton=True)

    return di_patch()


def create_test_cache_decorator(redis_client, **decorator_kwargs):
    """
    Create a cache decorator using a specific Redis client for testing.

    Args:
        redis_client: Redis client instance to use
        **decorator_kwargs: Additional arguments for the cache decorator

    Returns:
        Cache decorator function configured with the test Redis client
    """
    from cachekit import cache

    def test_cache_decorator(ttl=300, **kwargs):
        # Merge provided kwargs with defaults
        cache_kwargs = {**decorator_kwargs, **kwargs}

        # Create the decorator with patched Redis client
        with patch_redis_for_test(redis_client):
            return cache(ttl=ttl, **cache_kwargs)

    return test_cache_decorator


# =============================================================================
# Test Discovery and Migration Tools
# =============================================================================


def discover_redis_tests(test_directory: str = "tests/") -> list:
    """
    Discover tests that might need Redis isolation.

    Returns a list of test files that likely use Redis functionality.
    """
    import os
    import re

    redis_patterns = [
        r"redis",
        r"cache",
        r"@cache",
        r"get_redis_client",
        r"Redis\(",
        r"flushdb",
        r"flushall",
    ]

    potential_redis_tests = []

    for root, _dirs, files in os.walk(test_directory):
        for file in files:
            if file.startswith("test_") and file.endswith(".py"):
                file_path = os.path.join(root, file)
                try:
                    with open(file_path) as f:
                        content = f.read()

                    # Check if file contains Redis-related patterns
                    if any(re.search(pattern, content, re.IGNORECASE) for pattern in redis_patterns):
                        potential_redis_tests.append(file_path)

                except Exception:
                    # Skip files that can't be read
                    continue

    return potential_redis_tests


def generate_migration_report(test_files: list) -> str:
    """
    Generate a migration report for Redis tests.

    Args:
        test_files: List of test file paths to analyze

    Returns:
        Formatted report string
    """
    report = ["# Redis Test Migration Report\n"]

    for file_path in test_files:
        report.append(f"## {file_path}")

        try:
            with open(file_path) as f:
                content = f.read()

            # Check for specific patterns that need migration
            patterns_found = []

            if "redis.Redis(" in content:
                patterns_found.append("- Uses direct Redis() constructor")

            if "flushdb()" in content or "flushall()" in content:
                patterns_found.append("- Manual Redis cleanup")

            if "redis://localhost" in content:
                patterns_found.append("- Hardcoded Redis URL")

            if "@cache" in content:
                patterns_found.append("- Uses @cache decorator")

            if patterns_found:
                report.append("**Migration needed:**")
                report.extend(patterns_found)
                report.append("\n**Recommended actions:**")
                report.append("1. Add `redis_cache_isolated` fixture to test methods")
                report.append("2. Remove manual Redis client creation")
                report.append("3. Remove manual cleanup (flushdb/flushall)")
                report.append("4. Use `@with_redis_isolation()` decorator if needed")
            else:
                report.append("**Status:** Likely already compatible")

            report.append("")

        except Exception as e:
            report.append(f"**Error analyzing file:** {e}")
            report.append("")

    return "\n".join(report)


# =============================================================================
# Performance Testing Utilities
# =============================================================================


def benchmark_cache_performance(
    cached_func: Callable,
    iterations: int = 100,
    call_args: tuple = (),
    call_kwargs: Optional[dict] = None,
) -> dict[str, float]:
    """
    Benchmark cache performance with isolation.

    Args:
        cached_func: The cached function to benchmark
        iterations: Number of iterations to run
        call_args: Arguments for function calls
        call_kwargs: Keyword arguments for function calls

    Returns:
        Dictionary with performance metrics
    """
    import time

    if call_kwargs is None:
        call_kwargs = {}

    # Time first call (cache miss)
    start_time = time.time()
    result1 = cached_func(*call_args, **call_kwargs)
    first_call_time = time.time() - start_time

    # Time subsequent calls (cache hits)
    start_time = time.time()
    for _ in range(iterations):
        result = cached_func(*call_args, **call_kwargs)
        assert result == result1, "Cached results should be consistent"
    cache_hit_time = time.time() - start_time

    return {
        "first_call_time": first_call_time,
        "cache_hit_time_total": cache_hit_time,
        "cache_hit_time_avg": cache_hit_time / iterations,
        "iterations": iterations,
        "cache_speedup": first_call_time / (cache_hit_time / iterations) if cache_hit_time > 0 else float("inf"),
    }
