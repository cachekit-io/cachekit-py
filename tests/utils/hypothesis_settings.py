"""Hypothesis settings shared by @given tests in more than one file."""

from __future__ import annotations

from hypothesis import HealthCheck, settings

# A process's first draw mines every imported module for constants; with the suite imported that can exceed too_slow's 1 s.
slow_first_draw = settings(suppress_health_check=[HealthCheck.too_slow])
