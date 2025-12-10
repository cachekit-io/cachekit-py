"""Timing utilities for deterministic test polling (no flakiness)."""

from __future__ import annotations

import time
from typing import Callable


class TimingHelper:
    """Helper class for deterministic timing operations in tests."""

    @staticmethod
    def wait_for_condition(
        condition_func: Callable[[], bool],
        timeout: float = 5.0,
        interval: float = 0.1,
        message: str = "Condition not met",
    ) -> None:
        """Wait for condition with timeout (deterministic, no flakiness).

        Args:
            condition_func: Function that returns True when condition is met.
            timeout: Maximum time to wait in seconds (default: 5.0).
            interval: Time to sleep between checks in seconds (default: 0.1).
            message: Error message to raise on timeout.

        Raises:
            TimeoutError: If condition is not met within timeout.
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            if condition_func():
                return
            time.sleep(interval)
        raise TimeoutError(f"{message} after {timeout}s")
