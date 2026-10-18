"""A cached value that records no serializer name is a serializer mismatch (LAB-4432).

The protocol cache-key spec requires a reader that records serializer names to compare the
recorded name with its own before decoding, and states that "an entry that records no
serializer name is a mismatch". Every cachekit-py writer records one, so a nameless entry is
malformed and must never reach the reader's serializer. Asserted at the caller boundary: the
planted value is never returned, the function recomputes, and the entry is evicted and
re-stored (miss + evict).
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


def _reframe(frame: bytes, **name: str) -> bytes:
    """Re-frame a genuine entry with header `s` set to `name`, or omitted; payload stays decodable."""
    payload, metadata, _ = SerializationWrapper.unwrap(frame)
    header = json.dumps({"m": metadata, **name, "v": "2.0"}, separators=(",", ":")).encode("utf-8")
    return b"CK\x03" + len(header).to_bytes(4, "big") + header + bytes(payload)


def _v3_frame_without_name(frame: bytes) -> bytes:
    return _reframe(frame)


def _v3_frame_named_unknown(frame: bytes) -> bytes:
    """The sentinel the reader once defaulted a missing name to, and the guard waved through."""
    return _reframe(frame, s="unknown")


def _legacy_envelope_without_name(frame: bytes) -> bytes:
    """The pre-v3 base64+JSON envelope with its `serializer` key omitted."""
    payload, metadata, _ = SerializationWrapper.unwrap(frame)
    envelope = {"data": base64.b64encode(bytes(payload)).decode("ascii"), "metadata": metadata, "version": "2.0"}
    return json.dumps(envelope).encode("utf-8")


def _plant(strip: Callable[[bytes], bytes]):
    """Cache get_value(1), then overwrite its entry with a decodable planted value run through strip."""
    backend = _DictBackend()
    calls: list[int] = []

    @cache(backend=backend, ttl=300, l1_enabled=False)
    def get_value(x: int) -> dict:
        calls.append(x)
        return {"result": x}

    get_value(1)
    (key,) = backend.store
    planted = CacheSerializationHandler().serialize_data({"result": "planted"}, cache_key=key)
    backend.store[key] = strip(planted)
    calls.clear()
    backend.deleted.clear()
    return backend, calls, get_value, key


def test_control_named_planted_entry_is_returned():
    """Positive control: with its name intact the planted entry is a hit, so the name alone stops it below."""
    backend, calls, get_value, key = _plant(lambda frame: _reframe(frame, s="default"))
    assert get_value(1) == {"result": "planted"}
    assert calls == []
    assert key not in backend.deleted


@pytest.mark.parametrize(
    "strip",
    [_v3_frame_without_name, _v3_frame_named_unknown, _legacy_envelope_without_name],
    ids=["v3-frame", "v3-frame-unknown", "legacy"],
)
def test_nameless_entry_is_miss_and_evict(strip: Callable[[bytes], bytes]):
    backend, calls, get_value, key = _plant(strip)

    assert get_value(1) == {"result": 1}, "a nameless entry must never be decoded and returned"
    assert calls == [1], "a nameless entry must be a miss that recomputes"
    assert key in backend.deleted, "a nameless entry must be evicted"
    assert SerializationWrapper.unwrap(backend.store[key])[2] == "default", "the recompute re-stores a named frame"
