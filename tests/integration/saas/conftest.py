"""Shared test fixtures for SDK + SaaS E2E integration tests.

This module provides reusable fixtures for testing the cachekit Python SDK
against the live SaaS backend (Worker API + Durable Objects).
"""

import os
from pathlib import Path
from typing import Callable

import pytest
import requests
from dotenv import load_dotenv

from .perf_stats import global_tracker

# Load .env.dev for local development (auto-loads test API key)
env_file = Path(__file__).parent / ".env.dev"
if env_file.exists():
    load_dotenv(env_file)

# ============================================================================
# Configuration Fixtures
# ============================================================================


@pytest.fixture(scope="session")
def sdk_config():
    """SDK configuration from environment variables.

    Returns:
        dict: Configuration with api_url, api_key, namespace
    """
    return {
        "api_url": os.getenv("CACHEKIT_API_URL", "http://localhost:8787"),
        "api_key": os.getenv("CACHEKIT_API_KEY", "ck_sdk_test_key_here"),
        "namespace": os.getenv("CACHEKIT_NAMESPACE", "sdk_e2e_test"),
    }


@pytest.fixture(scope="session")
def worker_health_check(sdk_config):
    """Verify Worker is running and healthy before tests start.

    Fails fast if Worker is not available.
    """
    try:
        response = requests.get(f"{sdk_config['api_url']}/health", timeout=5)
        if response.status_code != 200:
            pytest.fail(
                f"Worker health check failed: {response.status_code}\n"
                f"URL: {sdk_config['api_url']}/health\n"
                "Make sure Worker is running: cd saas && make dev"
            )
    except requests.exceptions.ConnectionError:
        pytest.fail(f"Worker not running at {sdk_config['api_url']}\nStart Worker: cd saas && make dev")
    except Exception as e:
        pytest.fail(f"Worker health check error: {e}")


@pytest.fixture(scope="session", autouse=True)
def enforce_worker_availability(worker_health_check):
    """Automatically verify Worker availability before ANY integration test.

    This fixture has autouse=True, so it runs even if not explicitly requested.
    Prevents cryptic "Failed: W..." fixture errors by failing fast with clear
    "Worker not running" messages.

    The worker_health_check fixture does the actual validation, this just
    ensures it runs automatically for all tests in this directory.
    """
    pass


# ============================================================================
# SDK Decorator Fixtures
# ============================================================================


@pytest.fixture
def cache_io_decorator(sdk_config, worker_health_check):
    """Configured @cache.io decorator.

    Sets environment variables for SDK configuration and returns
    the cache.io decorator for use in tests.

    Requires:
        - Worker running at sdk_config["api_url"]
        - Valid API key in sdk_config["api_key"]

    Returns:
        Callable: cache.io decorator
    """
    # Set SDK environment variables
    os.environ["CACHEKIT_API_URL"] = sdk_config["api_url"]
    os.environ["CACHEKIT_API_KEY"] = sdk_config["api_key"]

    # Import here to pick up environment variables
    from cachekit import cache

    return cache.io


@pytest.fixture
def test_function_factory(cache_io_decorator):
    """Factory for creating test functions decorated with @cache.io.

    Usage:
        def test_example(test_function_factory):
            test_func = test_function_factory(
                name="my_test_func",
                computation=lambda x: x * 2
            )
            result = test_func(5)
            assert result == 10

    Args:
        name: Function name (for debugging)
        computation: Callable that implements the function logic

    Returns:
        Callable: Factory function that creates decorated test functions
    """

    def create_function(name: str, computation: Callable) -> Callable:
        @cache_io_decorator
        def test_func(*args, **kwargs):
            return computation(*args, **kwargs)

        test_func.__name__ = name
        return test_func

    return create_function


# ============================================================================
# Cache Management Fixtures
# ============================================================================


@pytest.fixture
def clean_cache(unique_namespace):
    """Provide clean cache via unique namespace per test.

    Uses unique namespace instead of cleanup to ensure test isolation
    without requiring DELETE endpoint implementation.

    Each test gets a fresh namespace, preventing cache pollution from
    previous tests.
    """
    import os

    old_namespace = os.environ.get("CACHEKIT_NAMESPACE")
    os.environ["CACHEKIT_NAMESPACE"] = unique_namespace

    yield

    # Restore original namespace
    if old_namespace:
        os.environ["CACHEKIT_NAMESPACE"] = old_namespace
    else:
        os.environ.pop("CACHEKIT_NAMESPACE", None)


@pytest.fixture
def unique_namespace(sdk_config):
    """Generate unique namespace for test isolation.

    Returns:
        str: Unique namespace string
    """
    import uuid

    base_namespace = sdk_config["namespace"]
    unique_id = uuid.uuid4().hex[:8]
    return f"{base_namespace}_{unique_id}"


# ============================================================================
# Test Data Fixtures
# ============================================================================


@pytest.fixture
def sample_data():
    """Sample test data for various data type tests.

    Returns:
        dict: Dictionary of test data samples
    """
    from datetime import date, datetime
    from decimal import Decimal

    return {
        "simple": {
            "string": "Hello, World!",
            "int": 42,
            "float": 3.14159,
            "bool": True,
            "none": None,
        },
        "collections": {
            "list": [1, 2, 3, 4, 5],
            "dict": {"a": 1, "b": 2, "c": 3},
            "nested": {"level1": {"level2": {"level3": [1, 2, 3]}}},
        },
        "special_types": {
            "datetime": datetime(2025, 1, 1, 12, 0, 0),
            "date": date(2025, 1, 1),
            "decimal": Decimal("123.456"),
        },
        "unicode": {
            "emoji": "🚀🎉💯",
            "chinese": "你好世界",
            "arabic": "مرحبا بالعالم",
            "mixed": "Hello 世界 🌍",
        },
        "edge_cases": {
            "empty_string": "",
            "empty_list": [],
            "empty_dict": {},
            "large_string": "X" * 10000,
        },
    }


@pytest.fixture
def pydantic_models():
    """Pydantic model definitions for testing.

    Returns:
        dict: Dictionary of Pydantic model classes
    """
    try:
        from datetime import datetime

        from pydantic import BaseModel

        class UserProfile(BaseModel):
            user_id: int
            name: str
            email: str
            created_at: datetime

        class Product(BaseModel):
            product_id: int
            name: str
            price: float
            in_stock: bool

        return {
            "UserProfile": UserProfile,
            "Product": Product,
        }
    except ImportError:
        pytest.skip("Pydantic not installed")


# ============================================================================
# HTTP Client Fixtures (for direct API testing)
# ============================================================================


@pytest.fixture
def http_client(sdk_config):
    """HTTP client for direct API calls (bypass SDK).

    Useful for testing SDK behavior against known API responses.

    Returns:
        requests.Session: Configured HTTP session
    """
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {sdk_config['api_key']}"})
    session.base_url = sdk_config["api_url"]
    return session


# ============================================================================
# Performance Testing Fixtures
# ============================================================================


@pytest.fixture
def performance_timer():
    """Context manager for measuring execution time.

    Usage:
        def test_latency(performance_timer):
            with performance_timer() as timer:
                # Code to measure
                expensive_function()

            assert timer.elapsed_ms < 100  # Assert < 100ms

    Returns:
        Callable: Timer context manager factory
    """
    import time
    from contextlib import contextmanager

    @contextmanager
    def timer():
        class Timer:
            def __init__(self):
                self.start = None
                self.end = None
                self.elapsed_ms = None

        t = Timer()
        t.start = time.perf_counter()
        yield t
        t.end = time.perf_counter()
        t.elapsed_ms = (t.end - t.start) * 1000

    return timer


# ============================================================================
# Performance Tracking
# ============================================================================


@pytest.fixture
def perf_tracker():
    """Performance tracker for measuring latencies.

    Usage in tests:
        def test_something(perf_tracker):
            with perf_tracker.timed_operation("GET"):
                result = client.get("key")
    """
    return global_tracker


def pytest_sessionfinish(session, exitstatus):
    """Print performance summary at end of test session."""
    if global_tracker.stats:
        print("\n")
        global_tracker.print_summary()


# ============================================================================
# Markers
# ============================================================================


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "sdk_e2e: SDK + SaaS end-to-end integration tests")
    config.addinivalue_line("markers", "performance: Performance and latency tests")
    config.addinivalue_line("markers", "data_handling: Data serialization and edge case tests")
    config.addinivalue_line("markers", "error_handling: Error scenario and recovery tests")
