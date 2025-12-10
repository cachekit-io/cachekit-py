"""
Comprehensive Test Configuration for PyRedis Cache Pro

This module provides:
1. Redis test isolation using pytest-redis
2. Fixtures for different test scopes and use cases
3. Backward compatibility with existing tests
4. Performance-optimized configurations

🧪 TDD Approach: Framework tested and proven via POC tests
"""

import os

import pytest

# Import existing fixtures for backward compatibility
try:
    import redis  # noqa: F401

    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

# Import pytest-redis if available
PYTEST_REDIS_AVAILABLE = False
try:
    from pytest_redis import factories
    from pytest_redis.executor import RedisExecutor  # noqa: F401

    PYTEST_REDIS_AVAILABLE = True
except ImportError:
    pass


# =============================================================================
# Configuration Management
# =============================================================================


@pytest.fixture
def redis_config_factory(monkeypatch):
    """Factory fixture for creating CachekitConfig instances with custom values.

    This fixture provides a clean way to create config instances in tests
    without polluting the global environment.

    Usage:
        def test_something(redis_config_factory):
            config = redis_config_factory(redis_url="redis://test:6379", default_ttl=7200)
    """
    from cachekit.config import CachekitConfig

    def _create_config(**kwargs):
        """Create a config with the given values by temporarily setting env vars."""
        if not kwargs:
            return CachekitConfig()

        # Map config keys to environment variables
        env_mapping = {
            "redis_url": "CACHEKIT_REDIS_URL",
            "connection_pool_size": "CACHEKIT_CONNECTION_POOL_SIZE",
            "default_ttl": "CACHEKIT_DEFAULT_TTL",
            "max_retries": "CACHEKIT_MAX_RETRIES",
            "socket_timeout": "CACHEKIT_SOCKET_TIMEOUT",
            "socket_connect_timeout": "CACHEKIT_SOCKET_CONNECT_TIMEOUT",
            "retry_on_timeout": "CACHEKIT_RETRY_ON_TIMEOUT",
            "max_chunk_size_mb": "CACHEKIT_MAX_CHUNK_SIZE_MB",
            "enable_compression": "CACHEKIT_ENABLE_COMPRESSION",
            "compression_level": "CACHEKIT_COMPRESSION_LEVEL",
            "retry_delay_ms": "CACHEKIT_RETRY_DELAY_MS",
            "early_refresh_ratio": "CACHEKIT_EARLY_REFRESH_RATIO",
            "enable_corruption_detection": "CACHEKIT_ENABLE_CORRUPTION_DETECTION",
        }

        # Use monkeypatch to set env vars temporarily for this test
        for key, value in kwargs.items():
            if key in env_mapping:
                monkeypatch.setenv(env_mapping[key], str(value))

        return CachekitConfig()

    return _create_config


class RedisTestConfig:
    """Configuration manager for Redis test environments."""

    @classmethod
    def get_redis_executable(cls):
        """Get Redis executable path from environment."""
        return os.environ.get("REDIS_EXECUTABLE", "redis-server")

    @classmethod
    def get_redis_host(cls):
        """Get Redis test host from environment."""
        return os.environ.get(
            "REDIS_HOST",
            os.environ.get("REDIS_TEST_HOST", os.environ.get("REDIS_POOL_HOST", "localhost")),
        )

    @classmethod
    def get_redis_port(cls):
        """Get Redis test port from environment."""
        # Support multiple environment variable names for flexibility
        return int(
            os.environ.get(
                "REDIS_PORT",
                os.environ.get("REDIS_TEST_PORT", os.environ.get("REDIS_POOL_PORT", "6379")),
            )
        )

    @classmethod
    def is_external_redis(cls):
        """Determine if tests should use an external Redis instance."""
        return os.environ.get("USE_EXTERNAL_REDIS", "0") == "1"

    @classmethod
    def is_pytest_redis_enabled(cls):
        """Check if pytest-redis isolation should be used."""
        return PYTEST_REDIS_AVAILABLE

    @classmethod
    def get_connection_params(cls):
        """Get connection parameters based on environment."""
        if cls.is_external_redis():
            return {
                "host": os.environ.get("REDIS_HOST", "localhost"),
                "port": cls.get_redis_port(),
                "password": os.environ.get("REDIS_PASSWORD", None),
                "db": int(os.environ.get("REDIS_POOL_DB", 15)),
            }
        return {
            "host": cls.get_redis_host(),
            "port": cls.get_redis_port(),
            "db": 0,  # pytest-redis uses db 0
        }


# =============================================================================
# pytest-redis Factory Configuration
# =============================================================================

if PYTEST_REDIS_AVAILABLE:
    # Detect if running in CI with external Redis service
    redis_url = os.environ.get("REDIS_URL")

    if redis_url:
        # CI environment - use external Redis service
        redis_noproc = factories.redis_noproc(host="localhost", port=6379)
        redis_isolated = factories.redisdb("redis_noproc")
    else:
        # Local development - spawn Redis process
        redis_proc_kwargs = {"timeout": 60}
        redis_proc = factories.redis_proc(**redis_proc_kwargs)
        redis_isolated = factories.redisdb("redis_proc")

    # Also make it available under the name the tests expect
    redisdb = redis_isolated

    # Function-scoped Redis for performance-critical tests
    @pytest.fixture(scope="function")
    def redis_session(redis_isolated):
        """Function-scoped Redis client for performance tests."""
        return redis_isolated


# =============================================================================
# Core Fixtures
# =============================================================================


@pytest.fixture
def skip_if_no_redis():
    """Skip test if Redis is not available."""
    if not REDIS_AVAILABLE:
        pytest.skip("Redis not available")


@pytest.fixture
def redis_test_client(redis_isolated):
    """
    Provide isolated Redis client using pytest-redis.

    REQUIRED: pytest-redis must be installed.
    NO FALLBACK: Tests must run with isolated Redis.
    """
    yield redis_isolated


# =============================================================================
# Backend Fixtures (for backend abstraction refactoring)
# =============================================================================


@pytest.fixture
def mock_backend():
    """Provide simple mock backend for unit tests that don't need Redis.

    This backend stores data in memory and supports basic operations.
    For tests that need real Redis behavior, use redis_backend fixture.

    NOTE: Uses regular Mock (not AsyncMock) because sync methods expect sync backend.
    For async tests, configure async methods separately with AsyncMock.
    """
    from unittest.mock import Mock

    backend = Mock()
    # Sync methods (used by StandardCacheHandler.get/set/delete)
    backend.get = Mock(return_value=None)
    backend.set = Mock(return_value=None)
    backend.delete = Mock(return_value=True)
    backend.exists = Mock(return_value=False)
    backend.get_ttl = Mock(return_value=None)
    backend.refresh_ttl = Mock(return_value=True)

    # Add connection_pool mock for timeout tests
    backend.connection_pool = Mock()
    backend.connection_pool.connection_kwargs = {"socket_timeout": 5.0}

    return backend


@pytest.fixture
def backend():
    """Provide PerRequestMockBackend for unit tests.

    This is the new standard backend fixture that uses the per-request pattern
    with tenant isolation. Implements all optional protocols.

    For tests that need real Redis, use redis_backend fixture.
    For tests that need Mock object behavior, use mock_backend fixture.
    """
    from tests.backends.mock_backend import MockBackendProvider, mock_tenant_context

    # Set default tenant for tests
    mock_tenant_context.set("test-tenant")

    provider = MockBackendProvider()
    backend = provider.get_backend()

    yield backend

    # Cleanup
    provider.clear()
    mock_tenant_context.set(None)


@pytest.fixture
def backend_provider():
    """Provide MockBackendProvider for tests that need provider pattern.

    Returns a provider instance that can create per-request backends.
    """
    from tests.backends.mock_backend import MockBackendProvider, mock_tenant_context

    # Set default tenant for tests
    mock_tenant_context.set("test-tenant")

    provider = MockBackendProvider()

    yield provider

    # Cleanup
    provider.clear()
    mock_tenant_context.set(None)


@pytest.fixture
def mock_backend_with_ttl_support():
    """Provide MockBackend that supports TTL inspection (TTLInspectableBackend).

    This backend implements the optional TTLInspectableBackend protocol.
    Use for testing TTL refresh and inspection features.
    """
    from tests.backends.mock_backend import MockBackendProvider, mock_tenant_context

    mock_tenant_context.set("test-tenant")
    provider = MockBackendProvider()
    backend = provider.get_backend()

    yield backend

    provider.clear()
    mock_tenant_context.set(None)


@pytest.fixture
def mock_backend_without_locking():
    """Provide MockBackend without locking support for testing graceful degradation.

    This backend does NOT implement LockableBackend protocol.
    Use for testing code paths that gracefully degrade when locking is unavailable.
    """
    from tests.backends.fixtures import create_backend_with_capabilities

    backend = create_backend_with_capabilities(locking_support=False, tenant_id="test-tenant")
    return backend


@pytest.fixture
def mock_backend_without_timeout():
    """Provide MockBackend without timeout configuration for testing degradation.

    This backend does NOT implement TimeoutConfigurableBackend protocol.
    Use for testing code paths that gracefully degrade when timeout config is unavailable.
    """
    from tests.backends.fixtures import create_backend_with_capabilities

    backend = create_backend_with_capabilities(timeout_support=False, tenant_id="test-tenant")
    return backend


@pytest.fixture
def redis_backend(redis_isolated):
    """Provide Redis-backed backend for integration tests.

    Uses the isolated Redis instance from pytest-redis.
    For unit tests that don't need real Redis, use backend fixture.
    """
    from cachekit.backends.redis_backend import RedisBackend

    backend = RedisBackend(redis_isolated)
    return backend


@pytest.fixture
def redis_backend_provider(redis_isolated):
    """Provide Redis backend provider for integration tests.

    Uses the isolated Redis instance from pytest-redis wrapped in TestBackendProvider.
    For unit tests that don't need real Redis, use backend_provider fixture.
    """
    from tests.fixtures.backend_providers import TestBackendProvider

    provider = TestBackendProvider(redis_client=redis_isolated)
    return provider


@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation(request):
    """
    Auto-use fixture that sets up DI for Redis test isolation.

    This runs for every test and ensures DI uses isolated Redis when available.
    """
    if not PYTEST_REDIS_AVAILABLE:
        pytest.fail("pytest-redis is required. Install with: uv pip install pytest-redis")

    # Get pytest-redis fixture (errors will propagate with clear messages)
    redis_isolated = request.getfixturevalue("redis_isolated")

    # Reset global connection pool
    from cachekit.backends.redis.client import reset_global_pool

    reset_global_pool()

    # Use dependency injection for test isolation
    from cachekit.backends.provider import (
        BackendProviderInterface,
        CacheClientProvider,
        DefaultBackendProvider,
        DefaultCacheClientProvider,
    )
    from cachekit.di import DIContainer
    from tests.fixtures.backend_providers import TestBackendProvider, TestCacheClientProvider

    # Use the global container instance
    container = DIContainer()

    # Clear any existing singletons to force re-creation
    container.clear_singletons()

    # Invalidate cached Redis client pool
    from cachekit.backends.redis.client import reset_global_pool

    reset_global_pool()

    # Register test backend provider with isolated Redis client
    test_backend_provider = TestBackendProvider(redis_client=redis_isolated)
    container.register(BackendProviderInterface, type(test_backend_provider), singleton=True)
    container._singletons[BackendProviderInterface] = test_backend_provider

    # Register legacy CacheClientProvider for backward compatibility
    test_client_provider = TestCacheClientProvider(sync_client=redis_isolated)
    container.register(CacheClientProvider, type(test_client_provider), singleton=True)
    container._singletons[CacheClientProvider] = test_client_provider

    try:
        yield
    finally:
        # Flush Redis to ensure test isolation
        # Clear L1 cache to ensure test isolation
        from cachekit.l1_cache import get_l1_cache_manager

        get_l1_cache_manager().clear_all()
        try:
            redis_isolated.flushdb()
        except Exception:
            pass  # Best effort cleanup
        # Clear test singletons and restore defaults
        container.clear_singletons()
        # Invalidate cached client pool again to ensure fresh client for next test
        reset_global_pool()

        container.register(BackendProviderInterface, DefaultBackendProvider, singleton=True)
        container.register(CacheClientProvider, DefaultCacheClientProvider, singleton=True)


# =============================================================================
# Test Helper Functions
# =============================================================================


def setup_test_data(redis_conn, key_prefix="test"):
    """Populate Redis with standard test data for testing."""
    redis_conn.set(f"{key_prefix}:string", "value")
    redis_conn.hset(f"{key_prefix}:hash", mapping={"field1": "value1", "field2": "value2"})
    redis_conn.lpush(f"{key_prefix}:list", *["item1", "item2", "item3"])
    redis_conn.sadd(f"{key_prefix}:set", "member1", "member2", "member3")
    return key_prefix


def verify_isolation(redis_conn, key_prefix="test"):
    """Verify that Redis instance is properly isolated (empty)."""
    keys = redis_conn.keys(f"{key_prefix}:*")
    return len(keys) == 0


def reset_call_counters(*mock_objects):
    """Reset any mock objects between tests."""
    for mock_obj in mock_objects:
        if mock_obj and hasattr(mock_obj, "reset_mock"):
            mock_obj.reset_mock()


# =============================================================================
# Backward Compatibility (Legacy fixtures)
# =============================================================================


# Keep existing fixtures for backward compatibility
@pytest.fixture(autouse=True)
def setup_redis_env():
    """Legacy fixture for backward compatibility."""

    # Use test database by default
    original_db = os.environ.get("REDIS_POOL_DB")
    os.environ.setdefault("REDIS_POOL_DB", "15")

    # Set up master key for encryption tests (deterministic for reproducibility)
    original_master_key = os.environ.get("CACHEKIT_MASTER_KEY")
    if original_master_key is None:
        # Use a fixed test master key (not secret, just for testing)
        test_master_key = "a" * 64  # 32 bytes in hex = 64 hex chars
        os.environ["CACHEKIT_MASTER_KEY"] = test_master_key

    yield

    # Restore original settings
    if original_db is not None:
        os.environ["REDIS_POOL_DB"] = original_db
    if original_master_key is None and "CACHEKIT_MASTER_KEY" in os.environ:
        del os.environ["CACHEKIT_MASTER_KEY"]
    elif original_master_key is not None:
        os.environ["CACHEKIT_MASTER_KEY"] = original_master_key


# =============================================================================
# Performance and Debug Utilities
# =============================================================================


@pytest.fixture
def redis_performance_monitor():
    """Monitor Redis performance during tests."""
    import time

    start_time = time.time()

    yield

    duration = time.time() - start_time
    if duration > 1.0:  # Log slow tests
        print(f"\n⚠️  Slow Redis test detected: {duration:.2f}s")


@pytest.fixture
def redis_debug_info():
    """Provide Redis debug information for troubleshooting."""
    config = RedisTestConfig()

    debug_info = {
        "pytest_redis_available": PYTEST_REDIS_AVAILABLE,
        "pytest_redis_enabled": config.is_pytest_redis_enabled(),
        "external_redis": config.is_external_redis(),
        "connection_params": config.get_connection_params(),
        "redis_executable": config.get_redis_executable(),
    }

    return debug_info


# =============================================================================
# Pytest Configuration
# =============================================================================


def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line("markers", "critical: mark test as critical path (must pass)")
    config.addinivalue_line("markers", "integration: mark test as integration test")
    config.addinivalue_line("markers", "performance: mark test as performance test")
    config.addinivalue_line("markers", "redis_isolation: mark test as requiring Redis isolation")


def pytest_collection_modifyitems(config, items):
    """Add appropriate markers to tests automatically."""
    for item in items:
        # Mark tests in critical/ as critical
        if "critical" in str(item.fspath):
            item.add_marker(pytest.mark.critical)

        # Mark tests in integration/ as integration
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)

        # Mark tests in performance/ as performance
        if "performance" in str(item.fspath):
            item.add_marker(pytest.mark.performance)

        # Auto-detect Redis isolation needs
        if any(fixture in item.fixturenames for fixture in ["redis_cache_isolated", "redis_test_client"]):
            item.add_marker(pytest.mark.redis_isolation)


# =============================================================================
# Markdown Documentation Testing
# =============================================================================


def pytest_markdown_docs_globals():
    """Provide global fixtures for markdown documentation examples.

    This hook injects common imports and mocks into all markdown code examples,
    allowing documentation to remain clean while tests remain functional.

    Returns:
        dict: Global variables available to markdown examples
    """
    import logging
    import time

    try:
        from fakeredis import FakeRedis
    except ImportError:
        # Fallback if fakeredis not installed
        FakeRedis = None  # noqa: N806

    import numpy as np
    import pandas as pd

    from cachekit import cache

    # Stub functions for documentation examples
    def do_expensive_computation():
        """Stub for expensive computation examples."""
        return {"result": "computed", "value": 42}

    def fetch_from_database(user_id):
        """Stub for database fetch examples."""
        return {"id": user_id, "name": "Alice", "email": f"user{user_id}@example.com"}

    def build_profile(user_id):
        """Stub for profile building examples."""
        return {"user_id": user_id, "profile": "data", "settings": {}}

    def fetch_user(user_id):
        """Stub for user fetch examples."""
        return {"id": user_id, "name": "Bob", "active": True}

    def process_business_logic(request_id):
        """Stub for business logic examples."""
        return {"request_id": request_id, "status": "processed", "result": "success"}

    def process_data(data):
        """Stub for data processing examples."""
        return {"processed": True, "data": data}

    def expensive_operation():
        """Stub for expensive operation examples."""
        return {"computed": True, "timestamp": time.time()}

    def compute_intensive_result():
        """Stub for compute intensive examples."""
        return {"result": "computed", "iterations": 1000}

    def process_item(item_id):
        """Stub for item processing examples."""
        return {"item_id": item_id, "processed": True}

    def important_data():
        """Stub for important data examples."""
        return {"data": "important", "priority": "high"}

    def transform(data):
        """Stub for transform examples."""
        return {"transformed": data}

    def process_tenant_request(tenant_id, request):
        """Stub for tenant request examples."""
        return {"tenant_id": tenant_id, "request": request, "result": "ok"}

    def trained_ml_model():
        """Stub for ML model examples."""
        return {"model": "trained", "accuracy": 0.95}

    def expensive_computation():
        """Stub for expensive computation examples."""
        return {"computed": True}

    # Create a logger for examples
    logger = logging.getLogger("cachekit.examples")
    logger.setLevel(logging.INFO)

    # Secret key for encryption examples (test value only)
    secret_key = "a" * 64  # 32 bytes in hex

    globals_dict = {
        "cache": cache,
        "asyncio": __import__("asyncio"),
        "time": time,
        "logging": logging,
        "logger": logger,
        "np": np,
        "pd": pd,
        # Stub functions
        "do_expensive_computation": do_expensive_computation,
        "fetch_from_database": fetch_from_database,
        "build_profile": build_profile,
        "fetch_user": fetch_user,
        "process_business_logic": process_business_logic,
        "process_data": process_data,
        "expensive_operation": expensive_operation,
        "compute_intensive_result": compute_intensive_result,
        "process_item": process_item,
        "important_data": important_data,
        "transform": transform,
        "process_tenant_request": process_tenant_request,
        "trained_ml_model": trained_ml_model,
        "expensive_computation": expensive_computation,
        "secret_key": secret_key,
    }

    # Add redis mock if fakeredis is available
    if FakeRedis is not None:
        globals_dict["redis"] = FakeRedis()

    return globals_dict
