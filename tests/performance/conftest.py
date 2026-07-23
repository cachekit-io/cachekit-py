"""Performance-suite fixtures: surface measurement integrity at session start.

The autouse fixture prints the system fingerprint (+ stable hash) and an environment
pre-flight verdict once per perf session. It is WARN-ONLY: a non-trustworthy
environment is reported, never gated — wall-clock benchmarks deliberately do not fail
the build (see .github/workflows/ci.yml). Tests that want a hard gate can request the
`measurement_env` fixture and inspect the returned verdict.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from .measurement_env import (
    EnvVerdict,
    check_measurement_environment,
    fingerprint_hash,
    system_fingerprint,
)


@pytest.fixture(scope="session", autouse=True)
def measurement_env() -> Iterator[EnvVerdict]:
    """Print the fingerprint + environment verdict once; yield it for optional gating."""
    fp = system_fingerprint()
    verdict = check_measurement_environment()

    print(f"\n{'=' * 70}")
    print(f"Measurement environment  [fingerprint {fingerprint_hash(fp)}]")
    print(f"{'=' * 70}")
    for key, value in fp.items():
        print(f"  {key:<20} {value}")
    if verdict.trustworthy:
        print("  environment          ✓ clean (no throttling / load warnings)")
    else:
        for warning in verdict.warnings:
            print(f"  environment          ⚠ {warning}")
    print(f"{'=' * 70}\n")

    yield verdict
