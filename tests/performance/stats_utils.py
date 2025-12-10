"""Statistical utilities for rigorous performance testing.

Provides tools for statistically sound performance measurements with:
- Confidence intervals (not just p95 point estimates)
- Multiple run aggregation
- GC pause detection and filtering
- JIT warmup with variance monitoring
- Effect size calculation for regression detection
"""

from __future__ import annotations

import gc
import statistics
import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class PerformanceResult:
    """Results from a performance benchmark with statistical rigor."""

    name: str
    unit: str  # "ns", "μs", "ms"
    samples: int
    runs: int
    mean: float
    median: float
    p95: float
    p99: float
    stdev: float
    ci_95_lower: float  # 95% confidence interval lower
    ci_95_upper: float  # 95% confidence interval upper
    gc_pauses_detected: int
    samples_after_gc_filter: int
    jit_stabilized: bool
    jit_warmup_samples: int

    def __str__(self) -> str:
        """Human-readable summary."""
        return (
            f"{self.name}:\n"
            f"  Mean:        {self.mean:>10.2f} {self.unit}\n"
            f"  Median:      {self.median:>10.2f} {self.unit}\n"
            f"  P95:         {self.p95:>10.2f} {self.unit}\n"
            f"  P99:         {self.p99:>10.2f} {self.unit}\n"
            f"  StdDev:      {self.stdev:>10.2f} {self.unit}\n"
            f"  95% CI:      [{self.ci_95_lower:>8.2f}, {self.ci_95_upper:>8.2f}] {self.unit}\n"
            f"  Runs:        {self.runs}\n"
            f"  Samples:     {self.samples} (after GC filtering: {self.samples_after_gc_filter})\n"
            f"  GC pauses:   {self.gc_pauses_detected}\n"
            f"  JIT stable:  {self.jit_stabilized}"
        )

    def exceeded_target(self, target: float) -> bool:
        """Check if p95 exceeds target (conservative: use p95, not mean)."""
        return self.p95 >= target


def confidence_interval_95(samples: list[float]) -> tuple[float, float]:
    """Calculate 95% confidence interval using t-distribution approximation.

    For n > 30, uses normal approximation. For n < 30, uses t-distribution.
    """
    if len(samples) < 2:
        return 0.0, 0.0

    mean = statistics.mean(samples)
    stdev = statistics.stdev(samples)
    n = len(samples)
    se = stdev / (n**0.5)  # Standard error

    # Critical values for 95% CI
    if n >= 30:
        z = 1.96  # Normal distribution
    elif n >= 20:
        z = 2.086  # t-distribution (n=20)
    elif n >= 10:
        z = 2.262  # t-distribution (n=10)
    else:
        z = 2.776  # t-distribution (n=5)

    margin = z * se
    return mean - margin, mean + margin


def detect_gc_pauses(samples: list[int], stdev: float) -> tuple[list[int], int]:
    """Detect and filter GC pauses from latency samples.

    GC pauses appear as extreme outliers (>3 stdev from mean).
    Returns filtered samples and count of detected pauses.
    """
    if len(samples) < 3:
        return samples, 0

    mean = statistics.mean(samples)
    threshold = mean + (3.0 * stdev)  # 3-sigma rule
    gc_pauses = 0
    filtered = []

    for sample in samples:
        if sample <= threshold:
            filtered.append(sample)
        else:
            gc_pauses += 1

    return filtered, gc_pauses


def measure_with_jit_warmup(
    fn: Callable[[], None],
    iterations: int,
    warmup_min_iterations: int = 5000,
    warmup_variance_threshold: float = 0.1,
) -> tuple[list[int], int]:
    """Measure function with intelligent JIT warmup.

    Warms up until either:
    1. Variance stabilizes (coefficient of variation < threshold)
    2. Minimum iterations reached
    3. 50k iterations done (safety limit)

    Returns list of nanosecond measurements and number of warmup iterations.
    """
    warmup_samples = []
    warmup_count = 0

    # Warmup phase with variance monitoring
    for i in range(min(50_000, warmup_min_iterations * 2)):
        start = time.perf_counter_ns()
        fn()
        end = time.perf_counter_ns()
        warmup_samples.append(end - start)
        warmup_count += 1

        # Check variance every 1000 iterations
        if i > 0 and i % 1000 == 0 and len(warmup_samples) > warmup_min_iterations:
            mean = statistics.mean(warmup_samples[-1000:])
            stdev = statistics.stdev(warmup_samples[-1000:]) if len(warmup_samples[-1000:]) > 1 else 0
            cv = stdev / mean if mean > 0 else 1.0  # Coefficient of variation

            if cv < warmup_variance_threshold:
                break

    # Actual measurement phase
    measurements = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        fn()
        end = time.perf_counter_ns()
        measurements.append(end - start)

    return measurements, warmup_count


def benchmark_with_gc_handling(
    name: str,
    fn: Callable[[], None],
    iterations_per_run: int = 10_000,
    runs: int = 5,
    warmup_iterations: int = 5000,
    unit: str = "ns",
) -> PerformanceResult:
    """Run benchmark multiple times with GC handling and statistical rigor.

    Args:
        name: Benchmark name
        fn: Function to benchmark
        iterations_per_run: Samples per run
        runs: Number of independent runs
        warmup_iterations: Minimum warmup iterations before measuring
        unit: Unit for reporting (ns, μs, ms)

    Returns:
        PerformanceResult with confidence intervals and GC detection
    """
    all_samples = []
    gc_pause_count = 0
    jit_warmup_samples = 0
    jit_stabilized = False

    # Multiple independent runs
    for run_num in range(runs):
        # Force GC before run to stabilize environment
        gc.collect()
        time.sleep(0.01)  # Let system settle

        # Measure with warmup
        samples, warmup_count = measure_with_jit_warmup(fn, iterations_per_run, warmup_iterations)

        if run_num == 0:
            jit_warmup_samples = warmup_count
            # Consider JIT stable if we reached variance threshold
            jit_stabilized = warmup_count <= warmup_iterations * 1.5

        # Detect GC pauses
        stdev_raw = statistics.stdev(samples) if len(samples) > 1 else 0
        filtered_samples, pauses = detect_gc_pauses(samples, stdev_raw)

        all_samples.extend(filtered_samples)
        gc_pause_count += pauses

    # Unit conversion: measurements are in nanoseconds, convert to target unit
    unit_conversions = {"ns": 1, "μs": 1000, "ms": 1_000_000}
    conversion_factor = unit_conversions.get(unit, 1)

    # Convert all samples to target unit
    converted_samples = [s / conversion_factor for s in all_samples]

    # Final statistics
    samples_after_filter = len(converted_samples)
    mean = statistics.mean(converted_samples)
    median = statistics.median(converted_samples)
    stdev = statistics.stdev(converted_samples) if len(converted_samples) > 1 else 0
    ci_lower, ci_upper = confidence_interval_95(converted_samples)

    p95 = statistics.quantiles(converted_samples, n=20)[18] if len(converted_samples) > 20 else mean
    p99 = statistics.quantiles(converted_samples, n=100)[98] if len(converted_samples) > 100 else mean

    return PerformanceResult(
        name=name,
        unit=unit,
        samples=len(all_samples) + gc_pause_count,  # Total before filtering
        runs=runs,
        mean=mean,
        median=median,
        p95=p95,
        p99=p99,
        stdev=stdev,
        ci_95_lower=ci_lower,
        ci_95_upper=ci_upper,
        gc_pauses_detected=gc_pause_count,
        samples_after_gc_filter=samples_after_filter,
        jit_stabilized=jit_stabilized,
        jit_warmup_samples=jit_warmup_samples,
    )


def effect_size_significant(baseline: PerformanceResult, current: PerformanceResult, threshold: float = 0.05) -> bool:
    """Detect if performance change is statistically significant (>5% by default).

    Uses Cohen's d effect size with conservative threshold.
    Accounts for measurement uncertainty (confidence intervals).
    """
    # If CI ranges overlap significantly, change not significant
    overlap = min(current.ci_95_upper, baseline.ci_95_upper) - max(current.ci_95_lower, baseline.ci_95_lower)
    ci_width = max(baseline.ci_95_upper - baseline.ci_95_lower, current.ci_95_upper - current.ci_95_lower)

    if overlap > ci_width * 0.5:  # >50% overlap = not significant
        return False

    # Calculate percent change
    percent_change = abs(current.mean - baseline.mean) / baseline.mean
    return percent_change >= threshold


def coefficient_of_variation(samples: list[float]) -> float:
    """Calculate coefficient of variation (CV = stdev / mean).

    CV is a normalized measure of consistency. Lower CV = more consistent.
    - CV < 0.05: Excellent consistency (highly stable)
    - CV < 0.10: Very good consistency (typical for cached L1 operations)
    - CV < 0.20: Good consistency (acceptable for network operations)
    - CV > 0.50: Poor consistency (high variance, measurement unreliable)
    """
    if len(samples) < 2:
        return 0.0

    mean = statistics.mean(samples)
    stdev = statistics.stdev(samples)

    if mean == 0:
        return 0.0

    return stdev / mean


def speedup_ratio(baseline_samples: list[float], optimized_samples: list[float]) -> tuple[float, str]:
    """Calculate speedup ratio between baseline and optimized runs.

    Returns (ratio, interpretation) where ratio = baseline_mean / optimized_mean.
    - ratio > 2.0: Significant speedup (2x faster)
    - ratio > 1.5: Good speedup (50% faster)
    - ratio > 1.0: Measurable speedup
    - ratio ≈ 1.0: No meaningful difference
    """
    baseline_mean = statistics.mean(baseline_samples)
    optimized_mean = statistics.mean(optimized_samples)

    if optimized_mean == 0:
        return 0.0, "ERROR: optimized mean is zero"

    ratio = baseline_mean / optimized_mean

    if ratio >= 2.0:
        interpretation = f"{ratio:.1f}x faster (excellent)"
    elif ratio >= 1.5:
        interpretation = f"{ratio:.1f}x faster (good)"
    elif ratio > 1.0:
        interpretation = f"{ratio:.1f}x faster (measurable)"
    else:
        interpretation = "No speedup (optimized is slower)"

    return ratio, interpretation


def validate_measurement_accuracy(
    expected_duration_ns: float, measured_samples: list[int], tolerance: float = 0.20
) -> tuple[bool, str]:
    """Validate that measured samples match expected duration within tolerance.

    This catches cases where timing overhead inflates measurements.
    For example, if we expect 1000ns but measure 5000ns, our measurement is wrong.

    Args:
        expected_duration_ns: Expected operation duration in nanoseconds
        measured_samples: List of measured latencies in nanoseconds
        tolerance: Allow ±tolerance ratio (default 20% = 0.20)

    Returns:
        (is_valid, message) tuple
    """
    if not measured_samples:
        return False, "No samples provided"

    mean = statistics.mean(measured_samples)
    min_allowed = expected_duration_ns * (1 - tolerance)
    max_allowed = expected_duration_ns * (1 + tolerance)

    is_valid = min_allowed <= mean <= max_allowed
    percent_error = ((mean - expected_duration_ns) / expected_duration_ns) * 100

    if is_valid:
        message = (
            f"✓ Measurement accuracy valid: {mean:.0f}ns (expected {expected_duration_ns:.0f}ns, error {percent_error:+.1f}%)"
        )
    else:
        message = (
            f"✗ Measurement accuracy INVALID: {mean:.0f}ns (expected {expected_duration_ns:.0f}ns, "
            f"error {percent_error:+.1f}%, tolerance ±{tolerance * 100:.0f}%)"
        )

    return is_valid, message
