"""The perf harness's statistics (tests/performance/stats_utils.py), on synthetic data: no timing.

Runs are drawn the way a shared host behaves: each run gets its own multiplicative shift (sd 3%),
and samples inside a run vary around it (sd 5%). Seeds are fixed, so every rate here is exact.
"""

from __future__ import annotations

import random

import pytest

from tests.performance.stats_utils import PerformanceResult, effect_size_significant, noise_floor, summarize

BASE = 1000.0
OUTLIER = 10 * BASE


def _clean_runs(rng: random.Random, runs: int = 5, n: int = 10_000) -> list[list[float]]:
    return [[rng.gauss(BASE, 50) for _ in range(n)] for _ in range(runs)]


def _with_outliers(runs: list[list[float]], share: float, rng: random.Random) -> list[list[float]]:
    dirty = [list(r) for r in runs]
    for r in dirty:
        for i in rng.sample(range(len(r)), int(share * len(r))):
            r[i] = OUTLIER
    return dirty


def _arm(rng: random.Random, shift: float = 1.0, runs: int = 5, n: int = 200) -> PerformanceResult:
    per_run = []
    for _ in range(runs):
        level = BASE * shift * rng.gauss(1.0, 0.03)
        per_run.append([rng.gauss(level, 0.05 * level) for _ in range(n)])
    return summarize("arm", per_run)


def test_raw_p95_keeps_a_five_percent_tail() -> None:
    rng = random.Random(5)
    clean = _clean_runs(rng)
    dirty = summarize("dirty", _with_outliers(clean, 0.05, rng))
    assert dirty.p95 >= 5 * summarize("clean", clean).p95


def test_raw_p99_reflects_a_one_percent_tail() -> None:
    # At exactly 1% the p99 sits on the boundary between the clean body and the outliers,
    # so it interpolates between the two; it must not collapse back onto the clean value.
    rng = random.Random(1)
    clean = _clean_runs(rng)
    clean_p99 = summarize("clean", clean).p99
    dirty = summarize("dirty", _with_outliers(clean, 0.01, rng))
    assert clean_p99 < dirty.p99 < OUTLIER
    assert dirty.p99 > 5 * clean_p99


def test_outliers_are_counted_not_filtered() -> None:
    rng = random.Random(2)
    clean = _clean_runs(rng, n=2_000)
    result = summarize("dirty", _with_outliers(clean, 0.02, rng))
    assert result.samples == 5 * 2_000
    assert result.outlier_count >= 5 * 40  # every injected outlier, plus any 3-sigma clean draws
    assert result.p99 == OUTLIER


def test_a_a_with_three_percent_run_drift_rarely_calls_a_change() -> None:
    rng = random.Random(3)
    false_positives = sum(effect_size_significant(_arm(rng), _arm(rng)) for _ in range(200))
    assert false_positives / 200 <= 0.05


def test_a_ten_percent_shift_is_detected() -> None:
    rng = random.Random(4)
    detected = sum(effect_size_significant(_arm(rng), _arm(rng, shift=1.10)) for _ in range(200))
    assert detected / 200 >= 0.95


def test_the_floor_is_the_threshold_or_the_wider_band() -> None:
    rng = random.Random(6)
    a, b = _arm(rng), _arm(rng)
    assert noise_floor(a, b) == max(0.05 * a.center, a.band, b.band)
    assert noise_floor(a, b, threshold=0.0) == max(a.band, b.band)


def test_inference_needs_five_runs() -> None:
    rng = random.Random(7)
    with pytest.raises(ValueError, match="needs >= 5"):
        effect_size_significant(_arm(rng, runs=4), _arm(rng))


def test_band_is_a_t_interval_over_run_medians() -> None:
    result = summarize("t", [[1.0, 1.0], [2.0, 2.0], [3.0, 3.0], [4.0, 4.0], [5.0, 5.0]])
    # run medians 1..5: mean 3, sd 1.5811; t(df=4) = 2.776
    assert result.center == 3.0
    assert result.band == pytest.approx(2.776 * 1.5811388 / 5**0.5)
    assert (result.ci_95_lower, result.ci_95_upper) == pytest.approx((3.0 - result.band, 3.0 + result.band))


def test_tails_below_the_floor_are_inconclusive() -> None:
    rng = random.Random(8)
    result = summarize("thin", [[rng.gauss(BASE, 50) for _ in range(100)] for _ in range(5)])
    assert result.p95_ci is None and result.p99_ci is None
    assert "P95:  inconclusive at n=500, runs=5" in str(result)


def test_tails_at_the_floor_carry_a_whole_run_bootstrap_ci() -> None:
    rng = random.Random(9)
    result = summarize("thick", [[rng.gauss(BASE, 50) for _ in range(40)] for _ in range(10)])
    assert result.p95_ci is not None and result.p99_ci is None
    assert result.p95_ci[0] <= result.p95 <= result.p95_ci[1]
    assert "bootstrap over 10 whole runs" in str(result)
