"""Pytest configuration for critical path tests.

Override autouse fixtures that aren't needed for FileBackend tests.
"""


def pytest_runtest_setup(item):
    """Skip redis setup for file backend tests."""
    if "file_backend" in item.nodeid:
        # Remove the autouse redis isolation fixture for this test
        item.fixturenames = [f for f in item.fixturenames if f != "setup_di_for_redis_isolation"]
