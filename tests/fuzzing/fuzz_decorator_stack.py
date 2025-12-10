#!/usr/bin/env python3
"""Atheris fuzz target for decorator stack (full integration)."""

from __future__ import annotations

import sys

import atheris

with atheris.instrument_imports():
    from cachekit.decorators.main import redis_cache
    from cachekit.serializers.raw import RawSerializer


def TestOneInput(data: bytes) -> None:
    """Fuzz the complete cache decorator stack."""
    fdp = atheris.FuzzedDataProvider(data)

    try:
        # Generate test function names to avoid collision
        func_id = fdp.ConsumeIntInRange(0, 1000000)

        # Create a simple cached function with RawSerializer
        @redis_cache(
            redis_url="redis://localhost:6379",
            serializer=RawSerializer(),
            default_ttl=3600,
        )
        def cached_func(value: bytes) -> bytes:
            """Simple cached function that returns input."""
            return value

        # Test with random payload
        payload = fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 1024))

        # Attempt to call the function
        # May fail if Redis is unavailable, which is expected
        try:
            result = cached_func(payload)
            # If it works, verify roundtrip
            assert result == payload, "Decorator roundtrip failed"
        except (ConnectionError, TimeoutError, OSError):
            # Expected when Redis is unavailable
            pass

    except (ValueError, OverflowError, RuntimeError, AttributeError, TypeError):
        # Expected exceptions for malformed input or missing Redis
        pass


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
