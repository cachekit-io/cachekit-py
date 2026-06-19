"""Measurement integrity for the perf suite: reproducible fingerprint, environment
pre-flight gating, and a timer self-calibration check.

Benchmark numbers are only trustworthy if you can tell (a) which machine produced
them — `system_fingerprint` + `fingerprint_hash`; (b) that the machine was not
throttled or loaded while measuring — `check_measurement_environment`; and (c) that
the timer itself measures what you think — `calibrate_timer`. `conftest.py` wires
the first two into a session banner; `test_measurement_calibration.py` asserts the
third. Salvaged (minimally) from the pyredis-cache-pro prototype's measurement layer.
"""

from __future__ import annotations

import hashlib
import os
import platform
import statistics
import sys
import time
from dataclasses import dataclass, field

import psutil

from .stats_utils import coefficient_of_variation


def system_fingerprint() -> dict[str, str | int | float]:
    """Capture machine characteristics for cross-run comparability."""
    vm = psutil.virtual_memory()
    return {
        "cpu_model": platform.processor() or "unknown",
        "cpu_cores_physical": psutil.cpu_count(logical=False) or 0,
        "cpu_cores_logical": psutil.cpu_count(logical=True) or 0,
        "ram_total_gb": round(vm.total / (1024**3), 1),
        "platform": platform.system(),
        "platform_machine": platform.machine(),
        "python_version": platform.python_version(),
        "python_minor": ".".join(platform.python_version_tuple()[:2]),
        "python_impl": platform.python_implementation(),
        "gil_enabled": int(getattr(sys, "_is_gil_enabled", lambda: True)()),
    }


# Deterministic fields only: two runs on the same machine + interpreter build hash
# identically, so results are compared only when they share a hash. Excludes the full
# patch version (3.13.x bumps do not change the hardware) and anything volatile
# (current freq, load) — the prototype's stable-fingerprint trick. python_minor and
# gil_enabled ARE included: 3.12-vs-3.13 and GIL-vs-free-threaded change performance.
_HASH_FIELDS = (
    "cpu_model",
    "cpu_cores_physical",
    "cpu_cores_logical",
    "ram_total_gb",
    "platform",
    "platform_machine",
    "python_minor",
    "python_impl",
    "gil_enabled",
)


def fingerprint_hash(fp: dict[str, str | int | float]) -> str:
    """Stable 12-char hash over the deterministic fingerprint fields."""
    payload = "|".join(f"{k}={fp.get(k)}" for k in _HASH_FIELDS)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


@dataclass
class EnvVerdict:
    """Result of the pre-flight environment check."""

    trustworthy: bool
    warnings: list[str] = field(default_factory=list)


def check_measurement_environment() -> EnvVerdict:
    """Pre-flight: is this machine fit to produce trustworthy timings right now?

    Warns (does not raise) on thermal throttling or load — a high-variance run on a
    hot or busy box yields numbers you cannot compare. Thresholds match the prototype:
    cpu_freq < 85% of max, CPU > 80%, memory > 85%, load average > 1.5x cores.
    """
    warnings: list[str] = []

    cpu_pct = psutil.cpu_percent(interval=0.1)
    if cpu_pct > 80.0:
        warnings.append(f"high CPU load: {cpu_pct:.0f}%")

    # Thermal throttling only matters when the CPU is actually working: at idle the
    # current frequency sits far below the boost max (healthy, not throttling), so a
    # bare current/max ratio cries wolf on every idle machine. Flag a depressed
    # frequency only UNDER load.
    try:
        freq = psutil.cpu_freq()
        if freq and freq.max and cpu_pct > 50.0 and (freq.current / freq.max) < 0.85:
            warnings.append(
                f"CPU may be throttled under load: {freq.current:.0f}/{freq.max:.0f} MHz ({freq.current / freq.max:.0%} of max)"
            )
    except (AttributeError, NotImplementedError, OSError):
        pass  # cpu_freq() is unavailable on some platforms / VMs

    mem_pct = psutil.virtual_memory().percent
    if mem_pct > 85.0:
        warnings.append(f"high memory usage: {mem_pct:.0f}%")

    if hasattr(os, "getloadavg"):
        cores = psutil.cpu_count(logical=True) or 1
        load1 = os.getloadavg()[0]
        if (load1 / cores) > 1.5:
            warnings.append(f"high load average: {load1:.1f} over {cores} cores")

    return EnvVerdict(trustworthy=not warnings, warnings=warnings)


def calibrate_timer(sleep_ms: float = 1.0, samples: int = 50) -> tuple[bool, str]:
    """Self-check the measurement instrument: time a known sleep, confirm sanity.

    A 1 ms sleep that measures ~1 ms means the `perf_counter_ns` loop captures the
    operation and nothing else. If it measured 10-30 ms (the classic timing-scope-
    pollution bug, where setup leaks into the timed region) or near-zero, the timer is
    lying and no benchmark on this machine can be trusted. The window is deliberately
    generous on the high side to absorb OS scheduling overshoot — it catches only gross
    errors, not precision. Reuses `coefficient_of_variation` for the stability report;
    see `validate_measurement_accuracy` for the per-operation (symmetric) analog.
    """
    expected_ns = sleep_ms * 1_000_000.0
    time.sleep(sleep_ms / 1000.0)  # one untimed warmup sleep
    measured: list[float] = []
    for _ in range(samples):
        start = time.perf_counter_ns()
        time.sleep(sleep_ms / 1000.0)
        measured.append(float(time.perf_counter_ns() - start))

    median_ns = statistics.median(measured)
    cv = coefficient_of_variation(measured)
    trustworthy = (expected_ns * 0.8) <= median_ns <= (expected_ns * 3.0)
    status = "✓" if trustworthy else "✗"
    message = (
        f"{status} timer calibration: {sleep_ms} ms sleep measured median "
        f"{median_ns / 1_000_000:.3f} ms (CV {cv:.2f}); "
        f"honest window [{sleep_ms * 0.8:.2f}, {sleep_ms * 3.0:.2f}] ms"
    )
    return trustworthy, message
