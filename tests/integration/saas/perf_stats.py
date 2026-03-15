"""Performance statistics collection for SaaS API tests.

Tracks and reports latency statistics for cache operations against the SaaS backend.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LatencyStats:
    """Statistics for a single operation type."""

    operation: str
    samples: list[float] = field(default_factory=list)

    def record(self, latency_ms: float) -> None:
        """Record a latency measurement in milliseconds."""
        self.samples.append(latency_ms)

    @property
    def count(self) -> int:
        """Number of samples recorded."""
        return len(self.samples)

    @property
    def mean(self) -> float:
        """Mean latency in milliseconds."""
        return statistics.mean(self.samples) if self.samples else 0.0

    @property
    def median(self) -> float:
        """Median latency in milliseconds."""
        return statistics.median(self.samples) if self.samples else 0.0

    @property
    def p95(self) -> float:
        """95th percentile latency in milliseconds."""
        if len(self.samples) < 20:
            return self.mean
        return statistics.quantiles(self.samples, n=20)[18]

    @property
    def p99(self) -> float:
        """99th percentile latency in milliseconds."""
        if len(self.samples) < 100:
            return self.mean
        return statistics.quantiles(self.samples, n=100)[98]

    @property
    def min(self) -> float:
        """Minimum latency in milliseconds."""
        return min(self.samples) if self.samples else 0.0

    @property
    def max(self) -> float:
        """Maximum latency in milliseconds."""
        return max(self.samples) if self.samples else 0.0

    @property
    def stdev(self) -> float:
        """Standard deviation in milliseconds."""
        return statistics.stdev(self.samples) if len(self.samples) > 1 else 0.0

    def __str__(self) -> str:
        """Human-readable summary."""
        if not self.samples:
            return f"{self.operation}: No data"

        return (
            f"{self.operation}:\n"
            f"  Count:   {self.count:>8}\n"
            f"  Mean:    {self.mean:>8.2f}ms\n"
            f"  Median:  {self.median:>8.2f}ms\n"
            f"  P95:     {self.p95:>8.2f}ms\n"
            f"  P99:     {self.p99:>8.2f}ms\n"
            f"  Min:     {self.min:>8.2f}ms\n"
            f"  Max:     {self.max:>8.2f}ms\n"
            f"  StdDev:  {self.stdev:>8.2f}ms"
        )


class PerformanceTracker:
    """Tracks performance statistics for cache operations."""

    def __init__(self) -> None:
        """Initialize performance tracker."""
        self.stats: dict[str, LatencyStats] = {}

    def record(self, operation: str, latency_ms: float) -> None:
        """Record a latency measurement."""
        if operation not in self.stats:
            self.stats[operation] = LatencyStats(operation=operation)
        self.stats[operation].record(latency_ms)

    def timed_operation(self, operation: str):
        """Context manager for timing operations.

        Usage:
            with tracker.timed_operation("GET"):
                result = cache_client.get("key")
        """
        return TimedOperation(self, operation)

    def get_stats(self, operation: str) -> LatencyStats | None:
        """Get statistics for a specific operation."""
        return self.stats.get(operation)

    def print_summary(self) -> None:
        """Print performance summary to console."""
        if not self.stats:
            print("No performance data collected")
            return

        print("\n" + "=" * 80)
        print("PERFORMANCE STATISTICS")
        print("=" * 80)

        for op_name in sorted(self.stats.keys()):
            print(self.stats[op_name])
            print()

        # Overall statistics
        all_samples = []
        total_ops = 0
        for stat in self.stats.values():
            all_samples.extend(stat.samples)
            total_ops += stat.count

        if all_samples:
            print("=" * 80)
            print("OVERALL")
            print("=" * 80)
            print(f"  Total operations: {total_ops:>8}")
            print(f"  Mean latency:     {statistics.mean(all_samples):>8.2f}ms")
            print(f"  Median latency:   {statistics.median(all_samples):>8.2f}ms")
            if len(all_samples) >= 20:
                print(f"  P95 latency:      {statistics.quantiles(all_samples, n=20)[18]:>8.2f}ms")
            if len(all_samples) >= 100:
                print(f"  P99 latency:      {statistics.quantiles(all_samples, n=100)[98]:>8.2f}ms")
            print("=" * 80 + "\n")


class TimedOperation:
    """Context manager for timing operations."""

    def __init__(self, tracker: PerformanceTracker, operation: str):
        """Initialize timed operation context."""
        self.tracker = tracker
        self.operation = operation
        self.start_time: float = 0.0

    def __enter__(self) -> TimedOperation:
        """Start timing."""
        self.start_time = time.perf_counter()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Stop timing and record latency."""
        end_time = time.perf_counter()
        latency_ms = (end_time - self.start_time) * 1000
        self.tracker.record(self.operation, latency_ms)


# Global tracker instance for pytest fixtures
global_tracker = PerformanceTracker()
