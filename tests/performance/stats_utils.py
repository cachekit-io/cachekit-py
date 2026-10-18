"""Statistical utilities for performance tests.

The run, not the sample, is the unit of inference. Samples inside one run share the host's
state at that moment (clock frequency, caches, neighbours), so pooling tens of thousands of them
as if they were independent gives a confidence interval a few ns wide that says nothing about
the next run. So:

- p50, p95 and p99 are computed over every raw sample. Nothing is trimmed: a mean + 3-sigma cut
  takes its sigma from the contaminated sample, and it removes exactly the tail a p99 exists to
  show. The outlier count is reported as a diagnostic only.
- The estimate is the mean of the per-run medians, with a t-interval at df = runs - 1 as its
  band. Each run median already shrugs off in-run spikes; one bad run widens the band, so it can
  only make a comparison more cautious.
- ``effect_size_significant`` calls a change only when it clears both a practical threshold and
  the 95% bands: each side's, and Welch's t on the difference of the run medians.
- A tail percentile is printed only with independent tail data: p95 at n >= 400 and p99 at
  n >= 2,000, from at least 10 runs, with a bootstrap interval over whole runs. Below that the
  report says "inconclusive at n". That interval is not calibrated: at 10 runs of 40 it covered
  the true p95 in 82-91% of simulated trials, not 95%, so it is never labelled a 95% CI.
"""

from __future__ import annotations

import gc
import math
import random
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import cached_property

# Two-sided 95% Student-t critical values, indexed by degrees of freedom (df = runs - 1).
_T95 = (
    12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)  # fmt: skip

MIN_RUNS_FOR_INFERENCE = 5
# A tail claim needs about 20 samples beyond its quantile, and those spread over >= 10 runs.
TAIL_FLOORS = {95: 400, 99: 2_000}
MIN_TAIL_RUNS = 10
BOOTSTRAP_RESAMPLES = 2_000
# Diagnostic outlier cut: median + 3 robust sigmas, where a robust sigma is 1.4826 * MAD.
_OUTLIER_SIGMAS = 3.0
_MAD_TO_SIGMA = 1.4826


def t_critical_95(df: int) -> float:
    """Two-sided 95% t critical value. Past df = 30 it stays at the df = 30 value, which is conservative.

    >>> t_critical_95(4)
    2.776
    >>> t_critical_95(60)
    2.042
    """
    if df < 1:
        raise ValueError(f"df must be >= 1, got {df}")
    return _T95[min(df, len(_T95)) - 1]


def percentile(samples: Sequence[float], p: int) -> float:
    """The p-th percentile of all samples (``statistics.quantiles``, exclusive method)."""
    return statistics.quantiles(samples, n=100)[p - 1]


def outlier_count(samples: Sequence[float]) -> int:
    """Samples above median + 3 robust sigmas. A diagnostic: nothing is ever filtered on it."""
    median = statistics.median(samples)
    mad = statistics.median(abs(s - median) for s in samples)
    threshold = median + _OUTLIER_SIGMAS * _MAD_TO_SIGMA * mad
    return sum(1 for s in samples if s > threshold)


def tail_ci(runs: Sequence[Sequence[float]], p: int, seed: int = 0) -> tuple[float, float] | None:
    """2.5-97.5% bootstrap interval of the p-th percentile over whole runs; None below the tail floors.

    Uncalibrated: it under-covers at the floor (see the module docstring).

    Costs BOOTSTRAP_RESAMPLES sorts of all samples, so it only runs once the floors are met.
    """
    if sum(len(r) for r in runs) < TAIL_FLOORS[p] or len(runs) < MIN_TAIL_RUNS:
        return None
    rng = random.Random(seed)
    estimates = sorted(percentile([s for r in rng.choices(runs, k=len(runs)) for s in r], p) for _ in range(BOOTSTRAP_RESAMPLES))
    return estimates[int(0.025 * BOOTSTRAP_RESAMPLES)], estimates[int(0.975 * BOOTSTRAP_RESAMPLES) - 1]


def format_tail(p: int, value: float, ci: tuple[float, float] | None, n: int, runs: int, unit: str) -> str:
    """One report line for a tail percentile: the value with its CI, or why it is no claim yet.

    >>> format_tail(95, 9.0, None, 100, 5, "ms")
    'P95:  inconclusive at n=100, runs=5 (a claim needs n >= 400 from >= 10 runs)'
    """
    if ci is None:
        return f"P{p}:  inconclusive at n={n}, runs={runs} (a claim needs n >= {TAIL_FLOORS[p]} from >= {MIN_TAIL_RUNS} runs)"
    return f"P{p}:  {value:.2f} {unit}, bootstrap 2.5-97.5% [{ci[0]:.2f}, {ci[1]:.2f}] over {runs} whole runs (uncalibrated)"


@dataclass
class PerformanceResult:
    """Results from a multi-run benchmark. Every percentile covers every raw sample."""

    name: str
    unit: str  # "ns", "μs", "ms"
    samples: int
    runs: int
    mean: float
    median: float
    p95: float  # all raw samples: a guard may assert on it; a claim needs p95_ci
    p99: float
    stdev: float
    run_medians: list[float]
    center: float  # mean of the per-run medians: what effect_size_significant compares
    band: float  # 95% t half-width of center at df = runs - 1; 0.0 for a single run
    outlier_count: int  # diagnostic: samples above median + 3 robust sigmas; never filtered
    per_run: list[list[float]] = field(repr=False)  # the raw samples, kept for the tail intervals
    jit_stabilized: bool = False
    jit_warmup_samples: int = 0

    def __str__(self) -> str:
        """Human-readable summary."""
        return (
            f"{self.name}:\n"
            f"  Mean:        {self.mean:>10.2f} {self.unit}\n"
            f"  Median:      {self.median:>10.2f} {self.unit}\n"
            f"  {format_tail(95, self.p95, self.p95_ci, self.samples, self.runs, self.unit)}\n"
            f"  {format_tail(99, self.p99, self.p99_ci, self.samples, self.runs, self.unit)}\n"
            f"  StdDev:      {self.stdev:>10.2f} {self.unit}\n"
            f"  Run median:  {self.center:>10.2f} ± {self.band:.2f} {self.unit} (95% t, {self.runs} runs)\n"
            f"  Samples:     {self.samples} (none filtered)\n"
            f"  Outliers:    {self.outlier_count} above median + 3 robust sigmas (diagnostic)\n"
            f"  JIT stable:  {self.jit_stabilized}"
        )

    @cached_property
    def p95_ci(self) -> tuple[float, float] | None:
        """Whole-run bootstrap interval of p95; None below the tail floors. Computed on first use."""
        return tail_ci(self.per_run, 95)

    @cached_property
    def p99_ci(self) -> tuple[float, float] | None:
        """Whole-run bootstrap interval of p99; None below the tail floors. Computed on first use."""
        return tail_ci(self.per_run, 99)

    @property
    def ci_95_lower(self) -> float:
        """Lower end of the 95% t-interval on center."""
        return self.center - self.band

    @property
    def ci_95_upper(self) -> float:
        """Upper end of the 95% t-interval on center."""
        return self.center + self.band

    def exceeded_target(self, target: float) -> bool:
        """Check if p95 over all raw samples reaches the target (conservative: p95, not mean)."""
        return self.p95 >= target


def summarize(name: str, runs: Sequence[Sequence[float]], unit: str = "ns") -> PerformanceResult:
    """Summarize per-run samples, already in ``unit``. Pure (no timing), so unit tests drive it.

    >>> r = summarize("demo", [[1.0, 2.0, 3.0]] * 5)
    >>> (r.center, r.band, r.p95_ci)
    (2.0, 0.0, None)
    """
    if not runs or any(len(r) < 2 for r in runs):
        raise ValueError("summarize needs at least one run, each with at least 2 samples")
    every = [s for r in runs for s in r]
    run_medians = [statistics.median(r) for r in runs]
    center = statistics.mean(run_medians)
    band = t_critical_95(len(runs) - 1) * statistics.stdev(run_medians) / len(runs) ** 0.5 if len(runs) > 1 else 0.0
    return PerformanceResult(
        name=name,
        unit=unit,
        samples=len(every),
        runs=len(runs),
        mean=statistics.mean(every),
        median=statistics.median(every),
        p95=percentile(every, 95),
        p99=percentile(every, 99),
        stdev=statistics.stdev(every),
        run_medians=run_medians,
        center=center,
        band=band,
        outlier_count=sum(outlier_count(r) for r in runs),
        per_run=[list(r) for r in runs],
    )


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
    """Run a benchmark as several independent runs, each after a forced collection, and summarize.

    Args:
        name: Benchmark name
        fn: Function to benchmark
        iterations_per_run: Samples per run
        runs: Number of independent runs (>= 5 for effect_size_significant)
        warmup_iterations: Minimum warmup iterations before measuring
        unit: Unit for reporting (ns, μs, ms)

    Returns:
        PerformanceResult over every raw sample, with the run-level band
    """
    per_run: list[list[float]] = []
    jit_warmup_samples = 0
    jit_stabilized = False
    conversion_factor = {"ns": 1, "μs": 1000, "ms": 1_000_000}.get(unit, 1)

    for run_num in range(runs):
        # Collect before each run, so no run inherits the previous run's garbage.
        gc.collect()
        time.sleep(0.01)  # Let system settle

        samples, warmup_count = measure_with_jit_warmup(fn, iterations_per_run, warmup_iterations)
        if run_num == 0:
            jit_warmup_samples = warmup_count
            jit_stabilized = warmup_count <= warmup_iterations * 1.5
        per_run.append([s / conversion_factor for s in samples])

    result = summarize(name, per_run, unit)
    result.jit_stabilized = jit_stabilized
    result.jit_warmup_samples = jit_warmup_samples
    return result


def balanced_order(runs_per_arm: int, rng: random.Random, arms: str = "AB") -> str:
    """A random run order with ``runs_per_arm`` runs of each arm, for interleaving A/B or A/A runs.

    Random, not a fixed pattern like ABBA: any periodic latency on the host or service that lines
    up with a fixed pattern loads one arm with the slow runs every time (a period-4 cycle against
    ABBAABBAAB called a change in every trial; a shuffled order, in about 1%).

    >>> sorted(balanced_order(5, random.Random(0)))
    ['A', 'A', 'A', 'A', 'A', 'B', 'B', 'B', 'B', 'B']
    """
    order = list(arms * runs_per_arm)
    rng.shuffle(order)
    return "".join(order)


def difference_band(baseline: PerformanceResult, current: PerformanceResult) -> float:
    """95% half-width on ``current.center - baseline.center``: Welch's t over the two sets of run medians."""
    va = statistics.variance(baseline.run_medians) / baseline.runs
    vb = statistics.variance(current.run_medians) / current.runs
    if va + vb == 0:
        return 0.0
    df = (va + vb) ** 2 / (va**2 / (baseline.runs - 1) + vb**2 / (current.runs - 1))
    return t_critical_95(max(1, int(df))) * math.sqrt(va + vb)


def noise_floor(baseline: PerformanceResult, current: PerformanceResult, threshold: float = 0.05) -> float:
    """The smallest change in ``center`` that effect_size_significant calls, in the result's unit.

    The largest of: the practical threshold (a fraction of the baseline), either side's band, and
    the band on the difference itself. The difference band is what holds the A/A false-positive
    rate near 5% at any run count; the wider of the two side bands alone let it climb to 9% at
    10 runs and 12% at 20.
    """
    return max(threshold * baseline.center, baseline.band, current.band, difference_band(baseline, current))


def effect_size_significant(baseline: PerformanceResult, current: PerformanceResult, threshold: float = 0.05) -> bool:
    """Call a change only when the run-level estimates differ by more than ``noise_floor``.

    Compares ``center`` (the mean of per-run medians) and requires the difference to exceed both
    ``threshold`` x baseline and the 95% bands (each side's, and Welch's on the difference), so
    neither a small drift nor run-to-run noise reads as a change. A difference exactly at the floor is not called. Both sides
    need at least MIN_RUNS_FOR_INFERENCE runs. Alternate
    baseline and candidate runs in one process where you can, so slow host drift hits both.

    No Cohen's d: per-run medians are few, and the question is "beyond run-to-run noise", not
    "how many pooled standard deviations".
    """
    for side in (baseline, current):
        if side.runs < MIN_RUNS_FOR_INFERENCE:
            raise ValueError(f"{side.name}: {side.runs} runs; inference needs >= {MIN_RUNS_FOR_INFERENCE}")
    return abs(current.center - baseline.center) > noise_floor(baseline, current, threshold)


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
