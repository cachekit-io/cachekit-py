#!/usr/bin/env python3
"""Atheris fuzz target for RawSerializer (Python → ByteStorage boundary)."""

from __future__ import annotations

import sys

import atheris

with atheris.instrument_imports():
    from cachekit.serializers.raw import RawSerializer


def TestOneInput(data: bytes) -> None:
    """Fuzz RawSerializer serialize/deserialize with various payloads."""
    fdp = atheris.FuzzedDataProvider(data)

    try:
        serializer = RawSerializer()

        # Fuzz ByteStorage compression/decompression
        payload = fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 4096))

        # Test serialize → deserialize roundtrip
        compressed = serializer.serialize(payload)
        decompressed = serializer.deserialize(compressed)

        # Verify roundtrip
        assert decompressed == payload, "Roundtrip failed: data mismatch"
    except (ValueError, OverflowError, RuntimeError):
        # Expected exceptions for malformed input
        pass


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
