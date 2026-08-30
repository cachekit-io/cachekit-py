#!/usr/bin/env python3
"""LAB-1140 negative proof: a crashing target must turn the job red and drop
its reproducer in tests/fuzzing/artifacts/ for the failure-only upload step.

Deliberately crashing — exists only on this proof branch, never merged.
"""

from __future__ import annotations

import sys

import atheris


def TestOneInput(data: bytes) -> None:
    """Crash unconditionally on the first input."""
    raise RuntimeError("LAB-1140 deliberate crash — this must fail the job")


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
