"""Untrusted-decode bounds (LAB-2503): protocol vectors + the SDK-local regression guard.

Why the bound exists and how it works: the ``unpackb_bounded`` docstring in
``cachekit.serializers.base`` (the canonical home). This file pins, so a
msgpack-python bump cannot silently move it:
- every reject vector is rejected on every decode path, with a bounded peak;
- every accept vector decodes on every path (the bound cannot over-tighten);
- the nesting ceiling is exactly MSGPACK_MAX_NESTING;
- the read path turns a bomb into SerializationError (a controlled miss), not a crash.

Fixture: tests/unit/protocol/fixtures/decode-bounds.json, vendored from
cachekit-io/protocol test-vectors/decode-bounds.json (sha256 pinned below).
Regenerate ONLY by re-copying from the protocol repo — never by hand.
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

from cachekit._rust_serializer import ByteStorage, check_msgpack_structure
from cachekit.cache_handler import CacheSerializationHandler
from cachekit.interop import decode_interop_value
from cachekit.serializers.auto_serializer import AutoSerializer
from cachekit.serializers.base import MSGPACK_MAX_NESTING, SerializationError, unpackb_bounded
from cachekit.serializers.standard_serializer import StandardSerializer
from cachekit.serializers.wrapper import SerializationWrapper

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "decode-bounds.json"
FIXTURE_SHA256 = "75c1204e6f58f5220581d3e40e75a68f2df605b4e3c817107b0c690cd7da5cd4"  # pragma: allowlist secret
VECTORS = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
EXPECTED_COUNTS = {"reject_vectors": 13, "accept_vectors": 2}

# Peak transient heap a rejected decode may cost: a small constant (tracemalloc + unpackb
# overhead) plus a few multiples of the input. Unguarded, the nested_array32_input_len vector
# peaks at ~8000x its input, so this discriminates by three orders of magnitude.
PEAK_BUDGET = 2 * 1024 * 1024
PEAK_PER_INPUT_BYTE = 4


def _envelope(payload: bytes) -> bytes:
    return bytes(ByteStorage("msgpack").store(payload, "msgpack"))


CACHE_KEY = "ns:decode:bounds"


@functools.lru_cache(maxsize=2)
def _frame_template(serializer: str = "default") -> tuple[dict[str, Any], str]:
    _, metadata, serializer_name = SerializationWrapper.unwrap(
        CacheSerializationHandler(serializer).serialize_data({"t": 1}, cache_key=CACHE_KEY)
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
    except (ValueError, SerializationError) as e:
        # The only rejections the read path maps to a controlled miss; any other type propagates.
        return None, e, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def _vector_ids(group: str) -> list[str]:
    return [v["name"] for v in VECTORS[group]]


def _reject_vector(name: str) -> bytes:
    return bytes.fromhex(next(v["input_hex"] for v in VECTORS["reject_vectors"] if v["name"] == name))


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
        _, err, peak = _peak_of(DECODE_PATHS[path], data)
        assert err is not None, f"{vector['name']}: {path} decoded a reject vector"
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

    def test_nesting_ceiling_is_exactly_the_pinned_constant(self) -> None:
        # The walk rejects one level past MSGPACK_MAX_NESTING; a document AT the ceiling
        # must still decode, so the constant may not exceed msgpack-python's C stack.
        at_bound = b"\x91" * MSGPACK_MAX_NESTING + b"\xc0"
        # Walked iteratively, not compared with `==`: nested-list equality recurses in CPython
        # too, and would blow the same recursion limit this test is bounding.
        value: object = unpackb_bounded(at_bound)
        depth = 0
        while isinstance(value, list):
            assert len(value) == 1
            depth, value = depth + 1, value[0]
        assert depth == MSGPACK_MAX_NESTING
        assert value is None
        with pytest.raises(ValueError, match=f"nests deeper than {MSGPACK_MAX_NESTING} levels"):
            unpackb_bounded(b"\x91" * (MSGPACK_MAX_NESTING + 1) + b"\xc0")

    def test_trailing_bytes_still_rejected(self) -> None:
        with pytest.raises(msgpack.exceptions.ExtraData):
            unpackb_bounded(b"\xc0\xc0")

    def test_mutable_exporters_are_accepted(self) -> None:
        # A bytearray (or a memoryview over one) is snapshotted so the walk and the decode see one
        # immutable document; a memoryview of bytes stays zero-copy. All three must decode.
        doc = msgpack.packb({"t": 1})
        assert unpackb_bounded(bytearray(doc), raw=False) == {"t": 1}
        assert unpackb_bounded(memoryview(bytearray(doc)), raw=False) == {"t": 1}
        assert unpackb_bounded(memoryview(doc)[0:], raw=False) == {"t": 1}

    def test_memoryview_shapes_and_formats_decode_like_bytes(self) -> None:
        # The Rust walk takes PyBuffer<u8>; msgpack takes any itemsize-1 buffer. Views are normalised
        # to a flat "B" view first so the two agree, len(data) is the byte count the caps need, and a
        # legitimate document is never rejected for the shape or format of the view it arrived in.
        doc = next(
            d
            for d in (msgpack.packb({"k": b"x" * m, "l": [1, 2, 3]}, use_bin_type=True) for m in range(1, 9))
            if len(d) % 8 == 0
        )
        expected = msgpack.unpackb(doc, raw=False)
        views = {
            "signed char": memoryview(doc).cast("b"),
            "char": memoryview(doc).cast("c"),
            "2-D bytes": memoryview(doc).cast("B", shape=[len(doc) // 8, 8]),
            "uint16": memoryview(doc).cast("H"),
        }
        for name, view in views.items():
            assert unpackb_bounded(view, raw=False) == expected, name
        # A non-contiguous view has no flat form: it is copied, then decoded like the bytes it selects.
        interleaved = bytes(b for pair in zip(doc, doc, strict=True) for b in pair)
        assert unpackb_bounded(memoryview(interleaved)[::2], raw=False) == expected

    # One exact-width document per fixed-width marker family: float32/64, uint8..64, int8..64,
    # fixext 1/2/4/8/16, ext8/16/32 (2-byte payload), str8/16/32 + fixstr, bin8/16/32.
    FIXED_WIDTH_DOCS = [
        b"\xca" + b"\x00" * 4,
        b"\xcb" + b"\x00" * 8,
        b"\xcc\x00",
        b"\xcd\x00\x00",
        b"\xce" + b"\x00" * 4,
        b"\xcf" + b"\x00" * 8,
        b"\xd0\x00",
        b"\xd1\x00\x00",
        b"\xd2" + b"\x00" * 4,
        b"\xd3" + b"\x00" * 8,
        b"\xd4\x01\x00",
        b"\xd5\x01\x00\x00",
        b"\xd6\x01" + b"\x00" * 4,
        b"\xd7\x01" + b"\x00" * 8,
        b"\xd8\x01" + b"\x00" * 16,
        b"\xc7\x02\x01\x00\x00",
        b"\xc8\x00\x02\x01\x00\x00",
        b"\xc9\x00\x00\x00\x02\x01\x00\x00",
        b"\xa1x",
        b"\xd9\x01x",
        b"\xda\x00\x01x",
        b"\xdb\x00\x00\x00\x01x",
        b"\xc4\x01x",
        b"\xc5\x00\x01x",
        b"\xc6\x00\x00\x00\x01x",
    ]

    @pytest.mark.parametrize("doc", FIXED_WIDTH_DOCS, ids=lambda d: f"0x{d[0]:02x}")
    def test_every_marker_is_walked_to_its_exact_width(self, doc: bytes) -> None:
        # Exact length passes the walk; one byte short is a truncation; a trailing byte reaches the
        # decoder as ExtraData — together they pin that the walk consumed exactly the marker's width.
        check_msgpack_structure(doc, MSGPACK_MAX_NESTING)
        with pytest.raises(ValueError, match="Unpack failed"):
            check_msgpack_structure(doc[:-1], MSGPACK_MAX_NESTING)
        with pytest.raises(msgpack.exceptions.ExtraData):
            unpackb_bounded(doc + b"\xc0")

    def test_reserved_marker_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="reserved marker 0xc1"):
            check_msgpack_structure(b"\xc1", MSGPACK_MAX_NESTING)

    def test_plain_path_miss_does_not_depend_on_numpy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Without the [data] extra (the free-threaded CI lane) a forged plain entry must still be a
        # SerializationError — not the RuntimeError a NumPy fallback raises for a missing numpy.
        monkeypatch.setattr("cachekit.serializers.auto_serializer.HAS_NUMPY", False)
        with pytest.raises(SerializationError, match="not a decodable MessagePack payload"):
            AutoSerializer(enable_integrity_checking=False).deserialize(_reject_vector("bin32_overclaim"))

    def test_validate_data_reports_a_bomb_as_invalid_within_the_peak_budget(self) -> None:
        # Python-only validate_data is a decode path too: a bomb must read as invalid (not raise),
        # and the walk must have stopped it before the decoder pre-allocated ~8000x the input.
        serializer = AutoSerializer(enable_integrity_checking=False)
        assert serializer.validate_data(msgpack.packb({"t": 1})) is True
        bomb = _reject_vector("nested_array32_input_len_depth_1100")
        valid, err, peak = _peak_of(serializer.validate_data, bomb)
        assert (valid, err) == (False, None)
        assert peak < PEAK_BUDGET + PEAK_PER_INPUT_BYTE * len(bomb), f"validate_data peaked at {peak} bytes"

    @pytest.mark.parametrize("original_type", ["dataframe", "series"])
    def test_bomb_behind_a_dataframe_or_series_frame_is_a_controlled_miss(self, original_type: str) -> None:
        # AutoSerializer's metadata routes decode outside the verified-envelope normaliser; the bound's
        # rejection must still reach the handler as SerializationError (evict + tamper hook), never a
        # bare ValueError. The message match keeps a "Serializer mismatch" error from faking a pass.
        metadata, serializer_name = _frame_template("auto")
        bomb = _reject_vector("nested_array32_input_len_depth_1100")
        frame = SerializationWrapper.wrap(_envelope(bomb), {**metadata, "original_type": original_type}, serializer_name)
        with pytest.raises(SerializationError, match=f"failed to decode as {original_type}"):
            CacheSerializationHandler("auto").deserialize_data(frame, cache_key=CACHE_KEY)
