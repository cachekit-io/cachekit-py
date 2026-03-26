"""Pytest configuration for critical path tests.

Override autouse fixtures that aren't needed for non-Redis backend tests.
"""


def pytest_runtest_setup(item):
    """Skip redis setup for non-Redis backend critical tests."""
    skip_redis = (
        "file_backend" in item.nodeid
        or "cachekitio_metrics" in item.nodeid
        or "memcached_backend" in item.nodeid
        or "secure_env_fallback" in item.nodeid
    )
    if skip_redis:
        # Remove autouse redis fixtures for tests that don't need Redis
        item.fixturenames = [f for f in item.fixturenames if f not in ("setup_di_for_redis_isolation", "setup_redis_env")]
