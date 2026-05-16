"""Timing utilities for deterministic test coordination (no flakiness)."""

from __future__ import annotations

import threading
import time
from typing import Callable


class TimingHelper:
    """Helper class for deterministic timing operations in tests."""

    @staticmethod
    def wait_for_condition(
        condition_func: Callable[[], bool],
        timeout: float = 5.0,
        interval: float = 0.01,
        message: str = "Condition not met",
    ) -> None:
        """Wait for condition with timeout (deterministic, no flakiness).

        Args:
            condition_func: Function that returns True when condition is met.
            timeout: Maximum time to wait in seconds (default: 5.0).
            interval: Time to sleep between checks in seconds (default: 0.01).
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


class ThreadGate:
    """Named synchronization points for deterministic thread coordination.

    Replaces flaky ``time.sleep()`` ordering with explicit signals between
    threads.  Each gate is a named ``threading.Event`` created on first use.

    Example::

        gate = ThreadGate()

        def worker():
            with controller.acquire():
                gate.signal("acquired")   # tell main thread we hold the permit
                with blocking_lock:       # block until main thread releases
                    pass

        thread = threading.Thread(target=worker)
        thread.start()
        gate.wait("acquired")             # deterministic — no sleep needed

    Args:
        timeout: Default timeout for all ``wait()`` calls (seconds).
    """

    def __init__(self, timeout: float = 5.0) -> None:
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._timeout = timeout

    def _get_event(self, name: str) -> threading.Event:
        with self._lock:
            if name not in self._events:
                self._events[name] = threading.Event()
            return self._events[name]

    def signal(self, name: str) -> None:
        """Signal that a named checkpoint has been reached."""
        self._get_event(name).set()

    def wait(self, name: str, timeout: float | None = None) -> None:
        """Block until the named checkpoint is signaled.

        Args:
            name: Gate name to wait on.
            timeout: Override the default timeout (seconds).

        Raises:
            TimeoutError: If the gate is not signaled within *timeout*.
        """
        t = timeout if timeout is not None else self._timeout
        if not self._get_event(name).wait(timeout=t):
            raise TimeoutError(f"ThreadGate '{name}' not signaled within {t}s")
