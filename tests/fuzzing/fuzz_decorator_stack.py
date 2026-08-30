#!/usr/bin/env python3
"""Atheris fuzz target for the cache decorator stack (L1-only integration)."""

from __future__ import annotations

import sys

import atheris

# Pre-import third-party deps so instrument_imports() below skips them
# (already in sys.modules = not instrumented). Atheris-instrumented pydantic
# bytecode segfaults CPython 3.11 in _decorators.merge_seqs during
# pydantic_settings' CLI-provider model construction (pulled in transitively
# via cachekit.hiredis_compat) — the SIGSEGV killed every nightly target
# during startup, before a single fuzz iteration (LAB-1140/LAB-2528). We fuzz
# cachekit's code; third-party coverage is not the goal.
import pydantic  # noqa: F401
import pydantic_settings  # noqa: F401

with atheris.instrument_imports():
    from cachekit import cache


# L1-only (backend=None): no network, deterministic — fuzzes the full
# decorator/key-generation/serialization/L1 stack on every call.
@cache(backend=None)
def _cached_identity(value: bytes) -> bytes:
    return value


def TestOneInput(data: bytes) -> None:
    """Fuzz the decorator stack: miss path, then hit path, must both roundtrip."""
    fdp = atheris.FuzzedDataProvider(data)
    payload = bytes(fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 1024)))

    # First call may miss or hit L1; second call for the same args must hit.
    # Both must return the payload byte-identically.
    assert _cached_identity(payload) == payload, "Decorator roundtrip failed (first call)"
    assert _cached_identity(payload) == payload, "Decorator roundtrip failed (cached call)"


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
