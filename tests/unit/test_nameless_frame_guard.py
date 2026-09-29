"""A cached value that records no serializer name is a serializer mismatch (LAB-4432).

protocol spec/cache-key-format.md: a reader whose container records a serializer name MUST
reject on mismatch, and "a value that records no serializer name is a mismatch". The writer
always records one, so a nameless entry is malformed or tampered and must never reach the
reader's serializer. Asserted at the caller boundary: the planted value is never returned,
the function recomputes, and the entry is evicted and re-stored (miss + evict).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable

import pytest

from cachekit import cache
from cachekit.cache_handler import CacheSerializationHandler
from cachekit.serializers.wrapper import SerializationWrapper


class _DictBackend:
    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl=None):
        self.store[key] = value

    def delete(self, key: str) -> bool:
        self.deleted.append(key)
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self):
        return True, {"backend_type": "dict_test"}


def _v3_frame_without_name(frame: bytes) -> bytes:
    """Re-frame with the header's `s` stripped; metadata and payload stay decodable."""
    payload, metadata, _ = SerializationWrapper.unwrap(frame)
    header = json.dumps({"m": metadata, "v": "2.0"}, separators=(",", ":")).encode("utf-8")
    return b"CK\x03" + len(header).to_bytes(4, "big") + header + bytes(payload)


def _legacy_envelope_without_name(frame: bytes) -> bytes:
    """The pre-v3 base64+JSON envelope with its `serializer` key omitted."""
    payload, metadata, _ = SerializationWrapper.unwrap(frame)
    envelope = {"data": base64.b64encode(bytes(payload)).decode("ascii"), "metadata": metadata, "version": "2.0"}
    return json.dumps(envelope).encode("utf-8")


@pytest.mark.parametrize("strip", [_v3_frame_without_name, _legacy_envelope_without_name], ids=["v3-frame", "legacy"])
def test_nameless_entry_is_miss_and_evict(strip: Callable[[bytes], bytes]):
    backend = _DictBackend()
    calls: list[int] = []

    @cache(backend=backend, ttl=300, l1_enabled=False)
    def get_value(x: int) -> dict:
        calls.append(x)
        return {"result": x}

    get_value(1)
    (key,) = backend.store
    # A decodable planted value under the real key: only the missing name should stop it.
    planted = CacheSerializationHandler().serialize_data({"result": "planted"}, cache_key=key)
    backend.store[key] = strip(planted)
    calls.clear()

    assert get_value(1) == {"result": 1}, "a nameless entry must never be decoded and returned"
    assert calls == [1], "a nameless entry must be a miss that recomputes"
    assert key in backend.deleted, "a nameless entry must be evicted"
    assert SerializationWrapper.unwrap(backend.store[key])[2] == "default", "the recompute re-stores a named frame"
