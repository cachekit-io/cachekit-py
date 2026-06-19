"""`auto` and `pythonic` are documented aliases for the same AutoSerializer (#167).

The handler canonicalized only ``std``/``standard`` -> ``default``, so an entry written with
``serializer="auto"`` stored ``'s':'auto'`` in the frame while a handler configured
``serializer="pythonic"`` compared against the literal ``'pythonic'`` -> serializer-mismatch ->
treated as a miss -> recompute on every read. Both aliases must canonicalize to the same name.
"""

from __future__ import annotations

import pytest

from cachekit.cache_handler import CacheSerializationHandler


@pytest.mark.unit
class TestSerializerAliasCanonicalization:
    def test_auto_written_reads_under_pythonic_alias(self) -> None:
        payload = {"value": 42, "items": [1, 2, 3]}

        wrapped = CacheSerializationHandler(serializer_name="auto").serialize_data(payload, cache_key="k")
        result = CacheSerializationHandler(serializer_name="pythonic").deserialize_data(wrapped, cache_key="k")

        assert result == payload

    def test_pythonic_written_reads_under_auto_alias(self) -> None:
        payload = {"value": 42, "items": [1, 2, 3]}

        wrapped = CacheSerializationHandler(serializer_name="pythonic").serialize_data(payload, cache_key="k")
        result = CacheSerializationHandler(serializer_name="auto").deserialize_data(wrapped, cache_key="k")

        assert result == payload

    def test_aliases_canonicalize_to_same_stored_name(self) -> None:
        # Both aliases must resolve to one canonical frame tag so entries are interchangeable.
        auto = CacheSerializationHandler(serializer_name="auto")
        pythonic = CacheSerializationHandler(serializer_name="pythonic")
        assert auto._serializer_string_name == pythonic._serializer_string_name

    def test_std_still_canonicalizes_to_default(self) -> None:
        # Guard the pre-existing canonicalization is preserved (only std/default are valid registry names).
        for name in ("std", "default"):
            assert CacheSerializationHandler(serializer_name=name)._serializer_string_name == "default"
