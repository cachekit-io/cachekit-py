"""Untrusted-decode bounds (LAB-2503): protocol vectors + the SDK-local regression guard.

Every cache read is a MessagePack decode of bytes the backend controls. A
collection header costs 1-5 bytes but may declare up to 2**32-1 elements, and
msgpack-python's C unpacker pre-allocates the container before decoding the
children, so nested headers stack allocations depth-first. Before this guard the
decoder was bounded only by library defaults, and those defaults still allowed
~8 x 1024 x len(data) bytes of transient heap (measured 10 KB -> 67 MB; the
"82 MB hard ceiling" once reported was an artifact of the array16(10000) probe).

Fixture: tests/unit/protocol/fixtures/decode-bounds.json, vendored from
cachekit-io/protocol test-vectors/decode-bounds.json
(sha256 864b7126986e9a2bd0dd50358018eda34fe2f70bca06ae9763e8ce6321f34b0a).
Regenerate ONLY by re-copying from the protocol repo — never by hand.

What is pinned here, so a msgpack-python bump cannot silently move it:
- every reject vector is rejected on every decode path, with a bounded peak;
- every accept vector decodes on every path (the bound cannot over-tighten);
- the nesting ceiling is exactly MSGPACK_MAX_NESTING;
- the read path turns a bomb into SerializationError (a controlled miss), not a crash.
"""

from __future__ import annotations

import functools
import hashlib
import json
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any

import msgpack
import pytest

from cachekit._rust_serializer import ByteStorage
from cachekit.cache_handler import CacheSerializationHandler
from cachekit.interop import decode_interop_value
from cachekit.serializers.auto_serializer import AutoSerializer
from cachekit.serializers.base import MSGPACK_MAX_NESTING, SerializationError, unpackb_bounded
from cachekit.serializers.standard_serializer import StandardSerializer
from cachekit.serializers.wrapper import SerializationWrapper

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "decode-bounds.json"
FIXTURE_SHA256 = "864b7126986e9a2bd0dd50358018eda34fe2f70bca06ae9763e8ce6321f34b0a"  # pragma: allowlist secret
VECTORS = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
EXPECTED_COUNTS = {"reject_vectors": 10, "accept_vectors": 2}

# Peak transient heap a rejected decode may cost: a small constant (Unpacker buffer)
# plus a few multiples of the input. The unguarded decoder peaks at ~5000x for the
# 15 KB bomb below, so this discriminates by three orders of magnitude.
PEAK_BUDGET = 2 * 1024 * 1024
PEAK_PER_INPUT_BYTE = 4


def _envelope(payload: bytes) -> bytes:
    return bytes(ByteStorage("msgpack").store(payload, "msgpack"))


CACHE_KEY = "ns:decode:bounds"


@functools.lru_cache(maxsize=1)
def _frame_template() -> tuple[dict[str, Any], str]:
    _, metadata, serializer_name = SerializationWrapper.unwrap(
        CacheSerializationHandler().serialize_data({"t": 1}, cache_key=CACHE_KEY)
    )
    return metadata, serializer_name


def _forged_entry(payload: bytes) -> bytes:
    """A genuine CK v3 frame with its payload swapped — the backend-write attacker's move."""
    metadata, serializer_name = _frame_template()
    return SerializationWrapper.wrap(_envelope(payload), metadata, serializer_name)


# Every path that decodes backend-supplied MessagePack. Each must reach unpackb_bounded.
DECODE_PATHS: dict[str, Callable[[bytes], Any]] = {
    "unpackb_bounded": lambda b: unpackb_bounded(b, raw=False),
    "interop": decode_interop_value,
    "standard/plain": StandardSerializer(enable_integrity_checking=False).deserialize,
    "standard/envelope": lambda b: StandardSerializer().deserialize(_envelope(b)),
    "auto/plain": AutoSerializer(enable_integrity_checking=False).deserialize,
    "auto/envelope": lambda b: AutoSerializer().deserialize(_envelope(b)),
    "handler.deserialize_data": lambda b: CacheSerializationHandler().deserialize_data(_forged_entry(b), cache_key=CACHE_KEY),
}


def _peak_of(fn: Callable[..., Any], *args: Any) -> tuple[Any, BaseException | None, int]:
    tracemalloc.start()
    try:
        return fn(*args), None, tracemalloc.get_traced_memory()[1]
    except Exception as e:  # noqa: BLE001 — the exception type is what we assert on
        return None, e, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def _vector_ids(group: str) -> list[str]:
    return [v["name"] for v in VECTORS[group]]


class TestFixtureIsTheVendoredProtocolFile:
    def test_sha256_and_counts(self) -> None:
        assert hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest() == FIXTURE_SHA256
        assert {g: len(VECTORS[g]) for g in EXPECTED_COUNTS} == EXPECTED_COUNTS
        assert VECTORS["spec"] == "spec/interop-mode.md#decode-bounds"


@pytest.mark.parametrize("path", DECODE_PATHS)
class TestProtocolVectors:
    @pytest.mark.parametrize("vector", VECTORS["reject_vectors"], ids=_vector_ids("reject_vectors"))
    def test_reject_vector_is_rejected_with_bounded_peak(self, path: str, vector: dict[str, Any]) -> None:
        data = bytes.fromhex(vector["input_hex"])
        assert len(data) == vector["input_len"]
        _, err, peak = _peak_of(DECODE_PATHS[path], data)
        assert err is not None, f"{vector['name']}: {path} decoded a reject vector"
        # Every rejection is a controlled error the read path maps to a cache miss.
        assert isinstance(err, (ValueError, SerializationError)), f"{vector['name']}: {path} raised {type(err).__name__}"
        assert peak < PEAK_BUDGET + PEAK_PER_INPUT_BYTE * len(data), f"{vector['name']}: {path} peaked at {peak} bytes"

    @pytest.mark.parametrize("vector", VECTORS["accept_vectors"], ids=_vector_ids("accept_vectors"))
    def test_accept_vector_decodes(self, path: str, vector: dict[str, Any]) -> None:
        data = bytes.fromhex(vector["input_hex"])
        value = DECODE_PATHS[path](data)
        depth = 0
        while isinstance(value, list):
            depth, value = depth + 1, value[0] if value else None
        assert depth == vector["nesting_depth"]


class TestOwnedBounds:
    """SDK-local guards that go beyond the shared vectors."""

    @pytest.mark.parametrize("path", DECODE_PATHS)
    def test_worst_case_bombs_stay_bounded(self, path: str) -> None:
        # (a) the LAB-2487 probe: 5000 x array16(10000) = 15 KB, 82 MB unguarded.
        # (b) array32 headers claiming exactly len(data): defeats a per-collection cap of
        #     len(data); 10 KB -> 67 MB unguarded (depth x len x 8).
        a16 = b"\xdc\x27\x10" * 5000
        a32 = (b"\xdd" + (8192).to_bytes(4, "big")) * 2048
        for bomb in (a16, a32):
            _, err, peak = _peak_of(DECODE_PATHS[path], bomb)
            assert isinstance(err, (ValueError, SerializationError)), f"{path}: {err!r}"
            assert peak < PEAK_BUDGET + PEAK_PER_INPUT_BYTE * len(bomb), f"{path}: peak {peak}"

    def test_nesting_ceiling_is_exactly_the_pinned_constant(self) -> None:
        # msgpack-python exposes no depth option; the C unpacker's fixed stack is the
        # bound. If a release moves it, this fails and the constant + protocol note
        # (spec/interop-mode.md → Decode bounds, 32 <= bound <= 1024) must be revisited.
        assert MSGPACK_MAX_NESTING == 1024
        at_bound = b"\x91" * MSGPACK_MAX_NESTING + b"\xc0"
        assert unpackb_bounded(at_bound) == json.loads("[" * MSGPACK_MAX_NESTING + "null" + "]" * MSGPACK_MAX_NESTING)
        with pytest.raises(msgpack.exceptions.StackError):
            unpackb_bounded(b"\x91" * (MSGPACK_MAX_NESTING + 1) + b"\xc0")

    def test_collection_caps_are_explicit_not_defaults(self) -> None:
        # A header may not declare more than the input can back — even when the walk is
        # bypassed by a structurally complete but oversize claim, unpackb's explicit caps hold.
        with pytest.raises(ValueError, match="max_array_len"):
            msgpack.unpackb(b"\xdc\x27\x10" + b"\xc0" * 3, max_array_len=6)
        # and the real thing: over-claim is caught by the walk before any allocation
        _, err, peak = _peak_of(unpackb_bounded, b"\xdc\x27\x10" + b"\xc0" * 3)
        assert isinstance(err, ValueError) and "more elements/bytes than the input can back" in str(err)
        assert peak < PEAK_BUDGET

    def test_legitimate_payloads_round_trip_unchanged(self) -> None:
        # The bound must not touch real values: large flat collections, long str/bin,
        # nested maps, and the columnar shapes the DataFrame path emits.
        big = {
            "s": "x" * 200_000,
            "b": b"y" * 200_000,
            "l": list(range(100_000)),
            "m": {str(i): [i, {"i": i}] for i in range(1000)},
        }
        packed = msgpack.packb(big, use_bin_type=True)
        assert unpackb_bounded(packed, raw=False) == big
        assert StandardSerializer().deserialize(StandardSerializer().serialize(big)[0]) == big
        assert AutoSerializer().deserialize(AutoSerializer().serialize(big)[0]) == big

    def test_trailing_bytes_still_rejected(self) -> None:
        with pytest.raises(msgpack.exceptions.ExtraData):
            unpackb_bounded(b"\xc0\xc0")
