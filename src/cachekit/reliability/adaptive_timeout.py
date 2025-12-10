"""Adaptive timeout management for cachekit.

This module provides adaptive timeout calculation capabilities that learn from
actual Redis performance and adjust timeouts dynamically. This prevents both
premature timeouts when Redis is slower than usual and excessive waiting
when Redis is truly unresponsive.

Key Features:
- AdaptiveTimeout: General timeout calculator based on operation history
- AdaptiveTimeoutManager: Specialized timeout management for lock operations
- Sliding window performance tracking using collections.deque
- Thread-safe operations for high-concurrency environments
- Load factor calculation for dynamic timeout adjustment

Architecture:
The adaptive timeout system uses percentile-based calculations on historical
operation durations to determine appropriate timeout values. The sliding window
approach ensures recent performance data has the most influence on timeout
decisions while maintaining bounded memory usage.
"""

import logging
import threading
from collections import deque
from typing import Optional

# Set up module logger
logger = logging.getLogger(__name__)

# Adaptive timeout constants
MIN_SAMPLES_FOR_CALCULATION = 10  # Minimum samples needed for timeout calculation
MIN_SAMPLES_FOR_LOAD_FACTOR = 5  # Minimum samples for load factor calculation
SIGNIFICANT_TIMEOUT_CHANGE_THRESHOLD = 2.0  # Threshold for logging timeout changes


class AdaptiveTimeout:
    """Adaptive timeout calculator based on operation history.

    Instead of using fixed timeouts, this class learns from actual Redis
    performance and adjusts timeouts dynamically. This prevents both:
    - Premature timeouts when Redis is slower than usual
    - Excessive waiting when Redis is truly unresponsive

    The algorithm uses a sliding window of recent operations and calculates
    the Nth percentile (typically P95) to set appropriate timeouts.

    Example:
        timeout_calc = AdaptiveTimeout(percentile=95.0)

        # Record actual durations
        start = time.time()
        redis_client.get("key")
        timeout_calc.record_duration(time.time() - start)

        # Get adaptive timeout for next operation
        timeout = timeout_calc.get_timeout()  # e.g., 0.15s if P95 is 100ms

    Examples:
        Create with defaults:

        >>> timeout = AdaptiveTimeout()
        >>> timeout.min_timeout
        1.0
        >>> timeout.max_timeout
        30.0

        Get timeout without enough samples (returns 2x min):

        >>> timeout = AdaptiveTimeout(min_timeout=1.0)
        >>> timeout.get_timeout()
        2.0

        Record durations and get adaptive timeout:

        >>> timeout = AdaptiveTimeout(min_timeout=0.1, max_timeout=5.0)
        >>> for _ in range(15):  # Need >= 10 samples
        ...     timeout.record_duration(0.05)
        >>> t = timeout.get_timeout()
        >>> 0.1 <= t <= 5.0  # Within bounds
        True
    """

    def __init__(
        self,
        window_size: int = 1000,  # Keep last 1000 operations
        percentile: float = 95.0,  # Use 95th percentile
        min_timeout: float = 1.0,  # Never timeout before 1s
        max_timeout: float = 30.0,  # Never wait more than 30s
    ):
        self.window_size = window_size
        self.percentile = percentile
        self.min_timeout = min_timeout
        self.max_timeout = max_timeout
        self._durations = deque(maxlen=window_size)  # Circular buffer
        self._lock = threading.Lock()  # Thread safety

    def record_duration(self, duration: float):
        """Record operation duration."""
        with self._lock:
            self._durations.append(duration)

    def get_timeout(self) -> float:
        """Calculate adaptive timeout based on recent performance.

        Algorithm:
        1. If insufficient data (<10 samples), use conservative default
        2. Calculate the specified percentile of recent durations
        3. Add 50% buffer to handle variance
        4. Apply min/max bounds for safety

        Returns:
            Timeout in seconds, guaranteed to be between min_timeout and max_timeout
        """
        with self._lock:
            if len(self._durations) < MIN_SAMPLES_FOR_CALCULATION:
                # Not enough data - use conservative default (2x minimum)
                return self.min_timeout * 2

            # Calculate percentile from sorted durations
            sorted_durations = sorted(self._durations)
            index = int(len(sorted_durations) * self.percentile / 100)
            p_value = sorted_durations[index]

            # Add 50% buffer for safety margin
            # Example: If P95 is 100ms, timeout will be 150ms
            timeout = p_value * 1.5

            # Apply bounds to prevent extreme values
            # min_timeout prevents premature timeouts
            # max_timeout prevents hanging forever
            return max(self.min_timeout, min(timeout, self.max_timeout))


class AdaptiveTimeoutManager:
    """Adaptive timeout management specifically for lock operations.

    This class extends the general AdaptiveTimeout pattern to provide
    specialized timeout management for Redis lock operations. It calculates
    dynamic timeout values based on current system load and performance,
    optimizing lock timeouts under varying conditions.

    Key Features:
    - Load factor calculation based on current system performance
    - Dynamic adjustment of both lock timeout and blocking timeout
    - Integration with existing performance tracking infrastructure
    - Thread-safe operations for high-concurrency environments

    Unlike the general AdaptiveTimeout class that focuses on operation
    duration percentiles, this manager specifically considers:
    - Lock contention patterns
    - System load indicators
    - Redis connection pool utilization
    - Cache hit/miss ratios affecting lock frequency

    Example:
        timeout_manager = AdaptiveTimeoutManager()

        # Record lock operation performance
        timeout_manager.record_lock_operation(duration=0.05, contention_factor=0.3)

        # Get adaptive timeouts for lock operations
        lock_timeout, blocking_timeout = timeout_manager.get_lock_timeouts()

        redis_lock = redis_client.lock(
            lock_key,
            timeout=lock_timeout,
            blocking_timeout=blocking_timeout
        )

    Examples:
        Create manager with defaults:

        >>> manager = AdaptiveTimeoutManager()
        >>> manager.base_lock_timeout
        10.0
        >>> manager.base_blocking_timeout
        5.0

        Get initial lock timeouts (before learning):

        >>> lock_t, blocking_t = manager.get_lock_timeouts()
        >>> lock_t == manager.base_lock_timeout
        True

        Get load factor without samples (default 1.0):

        >>> manager.get_load_factor()
        1.0

        Record operations and check stats:

        >>> manager.record_lock_operation(duration=0.05, success=True)
        >>> stats = manager.get_stats()
        >>> stats["total_operations"]
        1
        >>> stats["data_points"]
        1

        Reset manager:

        >>> manager.reset()
        >>> manager.get_stats()["total_operations"]
        0
    """

    def __init__(
        self,
        base_lock_timeout: float = 10.0,  # Base lock expiration timeout
        base_blocking_timeout: float = 5.0,  # Base blocking acquisition timeout
        min_lock_timeout: float = 2.0,  # Minimum lock timeout
        max_lock_timeout: float = 60.0,  # Maximum lock timeout
        min_blocking_timeout: float = 1.0,  # Minimum blocking timeout
        max_blocking_timeout: float = 30.0,  # Maximum blocking timeout
        load_factor_window: int = 100,  # Operations to track for load calculation
        adaptation_rate: float = 0.1,  # How quickly to adapt (0.0-1.0)
    ):
        """Initialize the adaptive timeout manager.

        Args:
            base_lock_timeout: Default lock expiration timeout in seconds
            base_blocking_timeout: Default blocking acquisition timeout in seconds
            min_lock_timeout: Minimum allowed lock timeout
            max_lock_timeout: Maximum allowed lock timeout
            min_blocking_timeout: Minimum allowed blocking timeout
            max_blocking_timeout: Maximum allowed blocking timeout
            load_factor_window: Number of recent operations to consider for load calculation
            adaptation_rate: Speed of adaptation (0.0=no adaptation, 1.0=immediate)
        """
        self.base_lock_timeout = base_lock_timeout
        self.base_blocking_timeout = base_blocking_timeout
        self.min_lock_timeout = min_lock_timeout
        self.max_lock_timeout = max_lock_timeout
        self.min_blocking_timeout = min_blocking_timeout
        self.max_blocking_timeout = max_blocking_timeout
        self.adaptation_rate = adaptation_rate

        # Performance tracking for load factor calculation
        self._operation_durations = deque(maxlen=load_factor_window)
        self._contention_factors = deque(maxlen=load_factor_window)
        self._success_rates = deque(maxlen=load_factor_window)

        # Current adaptive values
        self._current_lock_timeout = base_lock_timeout
        self._current_blocking_timeout = base_blocking_timeout

        # Thread safety
        self._lock = threading.Lock()

        # Performance metrics
        self._total_operations = 0
        self._successful_operations = 0

        logger.debug(
            "AdaptiveTimeoutManager initialized: lock_timeout=%.1fs, blocking_timeout=%.1fs",
            base_lock_timeout,
            base_blocking_timeout,
        )

    def record_lock_operation(self, duration: float, success: bool = True, contention_factor: Optional[float] = None):
        """Record the results of a lock operation for adaptive learning.

        Args:
            duration: Time taken for the lock operation in seconds
            success: Whether the lock operation succeeded
            contention_factor: Optional measure of lock contention (0.0-1.0)
                             Higher values indicate more contention
        """
        with self._lock:
            self._operation_durations.append(duration)
            self._success_rates.append(1.0 if success else 0.0)

            # Estimate contention factor if not provided
            if contention_factor is None:
                # Use duration as a proxy for contention
                # Longer durations typically indicate more contention
                contention_factor = min(duration / self.base_blocking_timeout, 1.0)

            self._contention_factors.append(contention_factor)

            self._total_operations += 1
            if success:
                self._successful_operations += 1

            # Update adaptive timeouts based on new data
            self._update_adaptive_timeouts()

    def _calculate_load_factor(self) -> float:
        """Calculate current system load factor based on recent operations.

        The load factor combines multiple indicators:
        - Average operation duration relative to baseline
        - Lock contention levels
        - Success rate trends

        Returns:
            Load factor between 0.0 (light load) and 2.0+ (heavy load)
        """
        if len(self._operation_durations) < MIN_SAMPLES_FOR_LOAD_FACTOR:
            return 1.0  # Default load factor with insufficient data

        # Duration factor: how slow are operations relative to expected?
        avg_duration = sum(self._operation_durations) / len(self._operation_durations)
        expected_duration = 0.1  # Expected lock operation duration (100ms)
        duration_factor = avg_duration / expected_duration

        # Contention factor: how much lock contention are we seeing?
        avg_contention = sum(self._contention_factors) / len(self._contention_factors)

        # Success rate factor: are operations failing more often?
        avg_success_rate = sum(self._success_rates) / len(self._success_rates)
        failure_factor = 2.0 - avg_success_rate  # 1.0 for 100% success, 2.0 for 0% success

        # Combine factors with weights
        # Duration and contention are primary indicators
        # Failure rate is secondary but important
        load_factor = (
            duration_factor * 0.4  # 40% weight on duration
            + (1.0 + avg_contention) * 0.4  # 40% weight on contention
            + failure_factor * 0.2  # 20% weight on failure rate
        )

        # Clamp to reasonable range
        return max(0.5, min(load_factor, 3.0))

    def _update_adaptive_timeouts(self):
        """Update current timeout values based on system load.

        Called automatically when new operation data is recorded.
        Uses exponential smoothing to gradually adapt timeouts.
        """
        load_factor = self._calculate_load_factor()

        # Calculate target timeouts based on load
        # Higher load = longer timeouts to handle slower Redis
        target_lock_timeout = self.base_lock_timeout * load_factor
        target_blocking_timeout = self.base_blocking_timeout * load_factor

        # Apply bounds
        target_lock_timeout = max(self.min_lock_timeout, min(target_lock_timeout, self.max_lock_timeout))
        target_blocking_timeout = max(self.min_blocking_timeout, min(target_blocking_timeout, self.max_blocking_timeout))

        # Exponential smoothing for gradual adaptation
        self._current_lock_timeout += self.adaptation_rate * (target_lock_timeout - self._current_lock_timeout)
        self._current_blocking_timeout += self.adaptation_rate * (target_blocking_timeout - self._current_blocking_timeout)

        # Log significant changes
        if abs(target_lock_timeout - self.base_lock_timeout) > SIGNIFICANT_TIMEOUT_CHANGE_THRESHOLD:
            logger.debug(
                "Adaptive timeouts adjusted: load_factor=%.2f, lock_timeout=%.1fs, blocking_timeout=%.1fs",
                load_factor,
                self._current_lock_timeout,
                self._current_blocking_timeout,
            )

    def get_lock_timeouts(self) -> tuple[float, float]:
        """Get current adaptive timeout values for lock operations.

        Returns:
            Tuple of (lock_timeout, blocking_timeout) in seconds

        Example:
            lock_timeout, blocking_timeout = manager.get_lock_timeouts()
            redis_lock = redis_client.lock(
                lock_key,
                timeout=lock_timeout,
                blocking_timeout=blocking_timeout
            )
        """
        with self._lock:
            return (self._current_lock_timeout, self._current_blocking_timeout)

    def get_load_factor(self) -> float:
        """Get current system load factor.

        Returns:
            Current load factor (1.0 = normal load, >1.0 = higher load)
        """
        with self._lock:
            return self._calculate_load_factor()

    def get_stats(self) -> dict:
        """Get statistics about timeout management and system load.

        Returns:
            Dictionary containing performance statistics and current settings
        """
        with self._lock:
            load_factor = self._calculate_load_factor()
            success_rate = self._successful_operations / self._total_operations if self._total_operations > 0 else 1.0

            return {
                "current_lock_timeout": self._current_lock_timeout,
                "current_blocking_timeout": self._current_blocking_timeout,
                "base_lock_timeout": self.base_lock_timeout,
                "base_blocking_timeout": self.base_blocking_timeout,
                "load_factor": load_factor,
                "success_rate": success_rate,
                "total_operations": self._total_operations,
                "data_points": len(self._operation_durations),
                "avg_duration": (
                    sum(self._operation_durations) / len(self._operation_durations) if self._operation_durations else 0.0
                ),
                "avg_contention": (
                    sum(self._contention_factors) / len(self._contention_factors) if self._contention_factors else 0.0
                ),
            }

    def reset(self):
        """Reset adaptive timeouts to base values and clear performance history.

        Useful for testing or when system conditions change dramatically.
        """
        with self._lock:
            self._operation_durations.clear()
            self._contention_factors.clear()
            self._success_rates.clear()
            self._current_lock_timeout = self.base_lock_timeout
            self._current_blocking_timeout = self.base_blocking_timeout
            self._total_operations = 0
            self._successful_operations = 0

        logger.info("AdaptiveTimeoutManager reset to base values")
