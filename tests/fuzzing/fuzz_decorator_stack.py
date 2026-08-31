#!/usr/bin/env python3
"""Atheris fuzz target for the cache decorator stack (L1-only integration)."""

from __future__ import annotations

import contextlib
import importlib
import sys

import atheris

# Pre-import third-party deps so instrument_imports() below skips them
# (already in sys.modules = not instrumented) — we fuzz cachekit's code;
# third-party coverage is not the goal, and atheris-instrumented third-party
# bytecode is a proven startup-crash class: instrumented pydantic segfaults
# CPython 3.11 in _decorators.merge_seqs during pydantic_settings'
# CLI-provider model construction (pulled in transitively via
# cachekit.hiredis_compat). That SIGSEGV killed every nightly target during
# startup, before a single fuzz iteration (LAB-1140/LAB-2528). Optional deps
# use suppress: absent is fine, instrumented is the trap.
for _mod in (
    "pydantic",
    "pydantic_settings",
    "numpy",
    "pandas",
    "pyarrow",
    "redis",
    "msgpack",
    "xxhash",
    "prometheus_client",
):
    with contextlib.suppress(ImportError):
        importlib.import_module(_mod)

with atheris.instrument_imports():
    from cachekit import cache
    from cachekit.config import DecoratorConfig
    from cachekit.config.nested import L1CacheConfig


# L1-only (backend=None): no network, deterministic — exercises the
# decorator / key-generation / ObjectCache (L1) stack on every call. In this
# mode values are stored as raw Python objects (no serializer runs).
#
# max_size_mb=8: L1's byte accounting counts only getsizeof(value); the
# ~450 B of real per-entry overhead (key string + entry bookkeeping) is
# uncounted, so the default 100 MB budget reaches ~1.5 GB real RSS over a
# 600 s run — inside libFuzzer's default -rss_limit_mb=2048 OOM kill. 8 MB
# accounted keeps real RSS comfortably bounded.
@cache(config=DecoratorConfig(backend=None, l1=L1CacheConfig(max_size_mb=8, swr_enabled=False)))
def _cached_identity(value: bytes) -> bytes:
    return value


def TestOneInput(data: bytes) -> None:
    """Fuzz the decorator stack: both calls must roundtrip byte-identically."""
    fdp = atheris.FuzzedDataProvider(data)
    payload = bytes(fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 1024)))

    # First call stores (or hits L1); the repeat exercises the hit path when
    # the entry survived eviction. Both must return the payload unchanged.
    if _cached_identity(payload) != payload:
        raise AssertionError("Decorator roundtrip failed (first call)")
    if _cached_identity(payload) != payload:
        raise AssertionError("Decorator roundtrip failed (repeat call)")


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
