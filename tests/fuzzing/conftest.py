"""Pytest configuration for fuzzing tests.

Atheris fuzzing tests require libFuzzer which is only available on Linux.
Skip collection of fuzzing tests on macOS and Windows.
"""

from __future__ import annotations

import platform

import pytest


def pytest_ignore_collect(collection_path, config):
    """Skip collection of fuzzing tests on non-Linux platforms.

    Atheris requires libFuzzer which is unavailable on macOS (Apple Clang)
    and Windows. These tests are designed to run in CI on Linux only.
    """
    if platform.system() != "Linux":
        return True
    return False


# Also provide a marker for explicit skip in case collection happens
pytestmark = pytest.mark.skipif(
    platform.system() != "Linux",
    reason="Atheris fuzzing requires libFuzzer (Linux-only)",
)
