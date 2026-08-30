#!/usr/bin/env python3
"""Atheris fuzz target for ByteStorage (Python → Rust FFI boundary)."""

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
    from cachekit._rust_serializer import ByteStorage


_STORAGE = ByteStorage("msgpack")


def TestOneInput(data: bytes) -> None:
    """Fuzz the ByteStorage store/retrieve FFI roundtrip and hostile-envelope decode."""
    fdp = atheris.FuzzedDataProvider(data)

    if fdp.ConsumeBool():
        # Roundtrip: store must retrieve byte-identically.
        payload = bytes(fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 4096)))
        envelope = _STORAGE.store(payload, "msgpack")
        retrieved, fmt = _STORAGE.retrieve(envelope)
        assert bytes(retrieved) == payload, "ByteStorage roundtrip failed"
        assert fmt == "msgpack", f"format tag corrupted: {fmt}"
    else:
        # Attacker-controlled envelope (cache content is untrusted): must
        # raise cleanly, never crash the interpreter.
        try:
            _STORAGE.retrieve(bytes(fdp.ConsumeBytes(fdp.remaining_bytes())))
        except ValueError:
            pass


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
