"""Run a tracemalloc measurement in a fresh interpreter that has exactly one thread.

tracemalloc.start()/stop() swap the process-wide allocator hooks without synchronising with
threads that are allocating at that moment, so on a free-threaded build a concurrent
allocation can crash the process: https://github.com/python/cpython/issues/143143. The pytest
process cannot be relied on to be single-threaded: under ``-n`` (as CI runs it) pytest-xdist adds
its I/O thread, and a test that builds an L1 cache leaves its cleanup thread running. So the unit
and critical suites start and stop tracemalloc only through here;
tests/performance/test_large_object_memory.py still does it in-process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any

import pytest


def _run_alone(measure: Callable[[], Any]) -> Any:
    """Subprocess side: measure with no other thread alive."""
    assert threading.active_count() == 1, f"tracemalloc must not start or stop beside {threading.enumerate()}"
    result = measure()
    assert threading.active_count() == 1, f"a thread started while measuring: {threading.enumerate()}"
    return result


def measure_in_subprocess(measure: Callable[[], Any], timeout: float = 300) -> Any:
    """Return ``measure()`` as computed in a fresh, single-threaded interpreter.

    ``measure`` must be a module-level, zero-argument function returning JSON-serialisable
    data. The subprocess imports it from its own module, with this process's sys.path.
    """
    script = (
        "import json, sys\n"
        f"from {measure.__module__} import {measure.__qualname__} as measure\n"
        f"from {__name__} import _run_alone\n"
        "json.dump(_run_alone(measure), sys.stdout)\n"
    )
    proc = subprocess.run(  # noqa: S603 (trusted: sys.executable + a script naming our own functions)
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    if proc.returncode != 0:
        pytest.fail(f"{measure.__qualname__} subprocess exited {proc.returncode}:\n{proc.stderr}")
    return json.loads(proc.stdout)
