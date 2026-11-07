"""AutoSerializer's and interop's Rust decodes (``unpackb_auto``, ``unpackb_interop``) against the decodes they replaced.

Every AutoSerializer read used to run ``unpackb_bounded(data, raw=False, use_list=True, object_hook=_auto_object_hook)``,
and every interop read ``unpackb_bounded(bytes(data), raw=False, strict_map_key=False, object_pairs_hook=_build_map)``
behind the CK-frame check: cachekit-core's structure walk, then msgpack-python. The Rust extension now does each in one
pass, in the decoder StandardSerializer's reads already use (``tests/unit/test_standard_decode.py``). These tests hold
both to the decode they replaced, input for input:

- every input the reference accepts decodes to an equal value of the same types (``repr`` tells 1 from 1.0 from True,
  a tuple from its marker map, bytes from str); every input it refuses raises the same exception class, a bound with
  the walk's own message, and an interop read the same message with the same cause;
- AutoSerializer's hook runs only on maps holding one of its seven marker keys. Interop's ``_revive_sentinels`` runs
  only on maps holding a temporal marker key and ``_build_map`` only on maps holding a key neither str nor bytes
  (IOP-18), which it still counts before any key is hashed;
- no path leaks an object it allocates or a reference to its input, a heap amplification payload costs no more than
  it did, and a decode is safe to run from many threads.

The inputs are values packed as each writer packs them, documents whose maps are keyed by every MessagePack type, and
both after random byte edits, so the bounds, truncation, reserved-marker and trailing-byte paths are reached too.
"""

from __future__ import annotations

import gc
import sys
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID

import msgpack
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import cachekit.interop as interop
from cachekit._rust_serializer import unpackb_auto, unpackb_interop
from cachekit.interop import InteropDecodeError, decode_interop_value, encode_interop_value
from cachekit.serializers.auto_serializer import _auto_default, _auto_object_hook
from cachekit.serializers.base import MSGPACK_MAX_NESTING, PAYLOAD_DECODE_ERRORS, SerializationError, unpackb_bounded
from tests.unit.test_standard_decode import CASES, GUARD_ERROR_PREFIX
from tests.utils.tracemalloc_isolation import measure_in_subprocess

pytestmark = pytest.mark.unit

# The diagnostic decode_interop_value raised, before this change, for a stored value starting with the CK v3 frame magic.
_CK_DIAGNOSTIC = (
    "stored value is a Python-SDK-internal auto-mode entry (CK v3 frame), not an "
    "interop value. An auto-mode writer and an interop reader are sharing a key — "
    "check that every writer for this key uses @cache(interop=...)."
)


def auto_reference(data: bytes) -> Any:
    """The decode every AutoSerializer read ran before this one."""
    return unpackb_bounded(data, raw=False, use_list=True, object_hook=_auto_object_hook)


def auto_rust(data: bytes | bytearray | memoryview) -> Any:
    return unpackb_auto(data, MSGPACK_MAX_NESTING, _auto_object_hook)


def interop_reference(data: bytes) -> Any:
    """``decode_interop_value`` as every interop read ran it before this one."""
    raw = bytes(data)
    if raw[:2] == b"CK":
        raise InteropDecodeError(_CK_DIAGNOSTIC)
    try:
        return unpackb_bounded(raw, raw=False, strict_map_key=False, object_pairs_hook=interop._build_map)
    except InteropDecodeError:
        raise
    except Exception as e:
        raise InteropDecodeError(f"stored value is not a single well-formed MessagePack document: {e}") from e


def interop_rust(data: bytes | bytearray | memoryview) -> Any:
    return unpackb_interop(data, MSGPACK_MAX_NESTING, interop._revive_sentinels, interop._build_map)


def interop_rust_reference(data: bytes) -> Any:
    """What ``unpackb_interop`` replaced, without decode_interop_value's frame check and wrapping."""
    return unpackb_bounded(data, raw=False, strict_map_key=False, object_pairs_hook=interop._build_map)


def outcome(decode: Callable[[bytes], Any], data: bytes, *, message: bool = False) -> tuple[Any, ...]:
    """What ``decode`` returned, as a repr, or the class it raised and its cause's. A bound's message is kept: it names
    the bound. ``message`` keeps every message: interop's InteropDecodeError quotes the error it wraps."""
    try:
        return ("value", repr(decode(data)))
    except msgpack.ExtraData as e:
        return ("extra", repr(e.unpacked), e.extra)
    except Exception as e:  # noqa: BLE001 - the outcome under comparison
        text = str(e)
        kept = text if message or text.startswith(GUARD_ERROR_PREFIX) else None
        return ("raised", type(e), kept, type(e.__cause__))


def assert_auto_same(data: bytes) -> None:
    assert outcome(auto_rust, data) == outcome(auto_reference, data), data.hex()


def assert_interop_same(data: bytes) -> None:
    assert outcome(interop_rust, data, message=True) == outcome(interop_rust_reference, data, message=True), data.hex()
    assert outcome(decode_interop_value, data, message=True) == outcome(interop_reference, data, message=True), data.hex()


# ── generated values ──────────────────────────────────────────────────────────────────────────

_AUTO_MARKER_KEYS = ("__datetime__", "__date__", "__time__", "__uuid__", "__tuple__", "__set__", "__ndarray__")
_TEMPORAL_MARKER_KEYS = ("__datetime__", "__date__", "__time__")
_ZONES = st.sampled_from([None, UTC, timezone(timedelta(hours=5, minutes=30)), timezone(-timedelta(hours=8))])
_TEMPORAL = st.datetimes(timezones=_ZONES) | st.dates() | st.times(timezones=_ZONES)
_ISO = _TEMPORAL.map(lambda t: t.isoformat())
_FLAGS = st.sampled_from([True, False, 1, 0, 0.0, float("nan"), "yes", "", b"", [], [0], {}, None])
_MARKER_VALUES = (
    _ISO
    | st.uuids().map(str)
    | st.text(max_size=12)
    | st.none()
    | st.integers(-(2**63), 2**64 - 1)
    | st.binary(max_size=4)
    | st.lists(st.integers(-3, 3), max_size=3)
)


def _mimics(keys: tuple[str, ...]) -> st.SearchStrategy[dict[Any, Any]]:
    """Maps a caller wrote that look like markers: a sentinel, truthy or not, with a value that parses, does not parse,
    is not a str, or is missing; maps of another size that carry a sentinel; and a marker under a bin key."""
    k = st.sampled_from(keys)
    return (
        st.builds(lambda k, f, v: {k: f, "value": v}, k, _FLAGS, _MARKER_VALUES)
        | st.builds(lambda k, f, o: {k: f, o: 1}, k, _FLAGS, st.text(max_size=6))
        | st.builds(lambda k, v: {k: True, "value": v, "extra": 1}, k, _MARKER_VALUES)
        | st.builds(lambda k, v, f: {k: True, "value": v, "frozen": f}, k, st.lists(st.integers(-3, 3), max_size=3), _FLAGS)
        | st.builds(lambda k: {k: True}, k)
        | st.builds(lambda k, v: {k.encode(): True, "value": v}, k, _ISO)  # bin key
        | st.builds(
            lambda d, s, t: {"__ndarray__": True, "data": d, "shape": s, "dtype": t},
            st.binary(max_size=16),
            st.lists(st.integers(-1, 4), max_size=2),
            st.sampled_from(["int8", "<i4", "float64", "object", "O", "nope", "V3"]),
        )
    )


_EXT = st.builds(msgpack.ExtType, st.integers(0, 127), st.binary(max_size=20)) | st.builds(
    msgpack.Timestamp, st.integers(-(2**40), 2**40), st.integers(0, 999_999_999)
)
_HASHABLE = (
    st.none() | st.booleans() | st.integers(-(2**63), 2**64 - 1) | st.floats() | st.text(max_size=8) | st.binary(max_size=4)
)
_KEYS = st.text(max_size=8) | st.binary(max_size=4) | st.sampled_from((*_AUTO_MARKER_KEYS, "value")) | st.integers(-3, 3)
_AUTO_LEAVES = (
    _HASHABLE
    | st.text()
    | st.binary()
    | _TEMPORAL
    | st.uuids()
    | st.frozensets(_HASHABLE, max_size=4)
    | st.sets(st.integers(-3, 3) | st.text(max_size=3), max_size=4)
    | _mimics(_AUTO_MARKER_KEYS)
    | _EXT
    | st.sampled_from(_AUTO_MARKER_KEYS)
)
# Tuples go through the packer's default, which marks them; so do UUIDs, sets and temporals.
AUTO_VALUES = st.recursive(
    _AUTO_LEAVES,
    lambda inner: st.lists(inner, max_size=6) | st.dictionaries(_KEYS, inner, max_size=6) | st.tuples(inner, inner),
    max_leaves=40,
)


def _auto_pack(value: Any, use_bin_type: bool) -> bytes:
    """As AutoSerializer packs a value (strict types, so tuples and builtin subclasses go to its default).
    use_bin_type=False packs bytes as str, so invalid UTF-8 reaches the str path."""
    return msgpack.packb(value, use_bin_type=use_bin_type, strict_types=True, default=_auto_default)


@settings(max_examples=1500, deadline=None)
@given(value=AUTO_VALUES, use_bin_type=st.booleans())
def test_every_auto_packed_value_decodes_as_before(value: Any, use_bin_type: bool) -> None:
    assert_auto_same(_auto_pack(value, use_bin_type))


# Raw documents, so a map key can be any document: every scalar type, an array (unhashable), a map (unhashable unless
# it revives to a temporal), repeated keys, and maps on either side of interop's bound of 32 keys neither str nor bytes.


def _array(items: list[bytes]) -> bytes:
    n = len(items)
    return (bytes([0x90 | n]) if n < 16 else b"\xdc" + n.to_bytes(2, "big")) + b"".join(items)


def _map(pairs: list[tuple[bytes, bytes]]) -> bytes:
    n = len(pairs)
    return (bytes([0x80 | n]) if n < 16 else b"\xde" + n.to_bytes(2, "big")) + b"".join(k + v for k, v in pairs)


def _packed(value: Any) -> bytes:
    return msgpack.packb(value, use_bin_type=True)


_SENTINELS = st.builds(lambda k, v: {k: True, "value": v}, st.sampled_from(_TEMPORAL_MARKER_KEYS), _ISO | st.text(max_size=6))
_LEAF_DOCS = (
    (_HASHABLE | _EXT | _SENTINELS | _mimics(_AUTO_MARKER_KEYS) | st.text(max_size=40)).map(_packed)
    | st.binary(min_size=1, max_size=3).map(lambda b: bytes([0xA0 | len(b)]) + b)  # str of arbitrary bytes
)
_NON_STR_KEY_DOCS = (st.integers(-(2**63), 2**64 - 1) | st.floats() | st.booleans() | st.none() | _EXT).map(_packed)


def _wide_map(inner: st.SearchStrategy[bytes]) -> st.SearchStrategy[bytes]:
    """A map of 28 to 40 entries, its keys mostly neither str nor bytes: either side of the bound, in any order."""
    keys = _NON_STR_KEY_DOCS | st.text(max_size=4).map(_packed) | st.binary(max_size=3).map(_packed)
    return st.lists(st.tuples(st.one_of(_NON_STR_KEY_DOCS, keys), inner), min_size=28, max_size=40).map(_map)


DOCS = st.recursive(
    _LEAF_DOCS,
    lambda inner: (
        st.lists(inner, max_size=5).map(_array) | st.lists(st.tuples(inner, inner), max_size=5).map(_map) | _wide_map(_LEAF_DOCS)
    ),
    max_leaves=40,
)


@settings(max_examples=1500, deadline=None)
@given(doc=DOCS)
def test_every_document_decodes_as_before(doc: bytes) -> None:
    assert_auto_same(doc)
    assert_interop_same(doc)


INTEROP_VALUES = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-(2**63), 2**64 - 1)
    | st.floats(allow_nan=False, allow_infinity=False)  # interop refuses both
    | st.text()
    | st.binary()
    | _TEMPORAL,
    lambda inner: st.lists(inner, max_size=6) | st.dictionaries(st.text(max_size=8), inner, max_size=6),
    max_leaves=40,
)


@settings(max_examples=500, deadline=None)
@given(value=INTEROP_VALUES)
def test_every_interop_written_value_decodes_as_before(value: Any) -> None:
    assert_interop_same(encode_interop_value(value))


_EDITS = st.lists(
    st.tuples(st.sampled_from(["set", "insert", "delete"]), st.integers(0, 2**16), st.integers(0, 255)), max_size=4
)


def _edited(data: bytes, edits: list[tuple[str, int, int]], cut: int, tail: bytes) -> bytes:
    out = bytearray(data)
    for op, at, byte in edits:
        at %= len(out) + 1
        if op == "insert":
            out.insert(at, byte)
        elif at < len(out):
            if op == "set":
                out[at] = byte
            else:
                del out[at]
    return bytes(out[: max(len(out) - cut, 0)]) + tail


@settings(max_examples=1500, deadline=None)
@given(value=AUTO_VALUES, use_bin_type=st.booleans(), edits=_EDITS, cut=st.integers(0, 3), tail=st.binary(max_size=3))
def test_every_edited_auto_value_decodes_or_refuses_as_before(
    value: Any, use_bin_type: bool, edits: list[tuple[str, int, int]], cut: int, tail: bytes
) -> None:
    assert_auto_same(_edited(_auto_pack(value, use_bin_type), edits, cut, tail))


@settings(max_examples=1500, deadline=None)
@given(doc=DOCS, edits=_EDITS, cut=st.integers(0, 3), tail=st.binary(max_size=3))
def test_every_edited_document_decodes_or_refuses_as_before(
    doc: bytes, edits: list[tuple[str, int, int]], cut: int, tail: bytes
) -> None:
    edited = _edited(doc, edits, cut, tail)
    assert_auto_same(edited)
    assert_interop_same(edited)


@settings(max_examples=3000, deadline=None)
@given(data=st.binary(max_size=48))
def test_arbitrary_bytes_decode_or_refuse_as_before(data: bytes) -> None:
    assert_auto_same(data)
    assert_interop_same(data)


# ── explicit cases ────────────────────────────────────────────────────────────────────────────


def _repeat_map(pairs: list[tuple[Any, Any]]) -> bytes:
    """A map16 of the given pairs in order, repeats kept (packb would merge them)."""
    return _map([(_packed(k), _packed(v)) for k, v in pairs])


_SENTINEL = _packed({"__datetime__": True, "value": "2024-01-01T00:00:00+00:00"})
INTEROP_CASES = {
    "CK frame magic": b"CK\x03\x00\x00\x00\x02{}\xc0",
    "CK alone": b"CK",
    "C alone (fixint 67)": b"C",
    "int key": _packed({1: 42}),
    "every scalar key type": _repeat_map(
        [("a", 1), (msgpack.ExtType(1, b"*"), 2), (b"k", 3), (None, 4), (1.5, 5), (True, 6), (msgpack.Timestamp(1, 2), 7)]
    ),
    "str keys, then an int key": _repeat_map([*((f"k{i}", i) for i in range(20)), (7, "int")]),
    "repeated str keys, then an int key": _repeat_map([("a", 1), ("b", 2), ("a", 3), (0, 4), ("b", 5)]),
    "an int key, then repeats of it": _repeat_map([(1, "x"), ("s", 1), (1, "y")]),
    "32 int keys": _repeat_map([(i, i) for i in range(32)]),
    "33 int keys": _repeat_map([(i, i) for i in range(33)]),
    "33 repeats of one int key": _repeat_map([(1, i) for i in range(33)]),
    "str keys around 33 int keys": _repeat_map([("a", 0), *((i, i) for i in range(33)), ("b", 0)]),
    "33 int keys, then invalid UTF-8": _repeat_map([(i, i) for i in range(33)])[:-1] + b"\xa2\xff\xfe",
    "a temporal sentinel as a key": b"\x81" + _SENTINEL + b"\x01",
    "a temporal sentinel key, then a str key": b"\x82" + _SENTINEL + b"\x01\xa1a\x02",
    "a plain map as a key": b"\x81\x81\xa1a\x01\x02",
    "an array as a key": b"\x81\x91\x01\x01",
    "an array key, then invalid UTF-8": b"\x82\x91\x01\x01\xa1a\xa2\xff\xfe",
    "an unparseable sentinel": _packed({"__date__": True, "value": "yesterday"}),
    "a sentinel with an int key beside it": _repeat_map([("__date__", True), ("value", "2024-01-15"), (1, 2)]),
    "a sentinel with a non-str value": _packed({"__time__": True, "value": 5}),
    "an Auto marker is not an interop one": _packed({"__tuple__": True, "value": [1, 2]}),
    "timestamp bad length": b"\xc7\x05\xff" + b"\x00" * 5,
    "timestamp bad nanoseconds 64": b"\xd7\xff" + (0x3FFFFFFF << 34 | 1).to_bytes(8, "big"),
    "timestamp bad nanoseconds 96": b"\xc7\x0c\xff" + (10**9).to_bytes(4, "big") + (5).to_bytes(8, "big"),
    "timestamp 96, negative seconds": b"\xc7\x0c\xff" + (7).to_bytes(4, "big") + (-(2**62)).to_bytes(8, "big", signed=True),
}
AUTO_CASES = {
    "tuple": _auto_pack((1, "a", (2,)), True),
    "set and frozenset": _auto_pack([{1, 2}, frozenset({"x"})], True),
    "uuid": _auto_pack(UUID(int=7), True),
    "temporals": _auto_pack([datetime(2024, 1, 15, 1, 2, 3, tzinfo=UTC), date(2024, 1, 15), time(12, 30)], True),
    "tuple marker missing its value": _packed({"__tuple__": True}),
    "tuple marker with a str value": _packed({"__tuple__": True, "value": "ab"}),
    "set marker with a dict value": _packed({"__set__": True, "value": {}}),
    "set marker of unhashables": _packed({"__set__": True, "value": [[1]]}),
    "uuid marker with an unparseable value": _packed({"__uuid__": True, "value": "x" * 300}),
    "uuid marker with an int value": _packed({"__uuid__": True, "value": 7}),
    "ndarray marker missing its dtype": _packed({"__ndarray__": True, "data": b"", "shape": [0]}),
    "ndarray marker of an object dtype": _packed({"__ndarray__": True, "data": b"\x00" * 8, "shape": [1], "dtype": "O"}),
    "two markers": _packed({"__uuid__": True, "__tuple__": True, "value": [1]}),
    "a marker under a bin key": _packed({b"__tuple__": True, "value": [1]}),
    "a tuple marker as a key": b"\x81" + _auto_pack((1, 2), True) + b"\x01",
    "a marker as a str value": _packed({"k": "__set__"}),
}


@pytest.mark.parametrize("data", [*CASES.values(), *INTEROP_CASES.values(), *AUTO_CASES.values()])
def test_explicit_case_decodes_or_refuses_as_before(data: bytes) -> None:
    assert_auto_same(data)
    assert_interop_same(data)


def test_a_malformed_timestamp_raises_msgpacks_own_message() -> None:
    """msgpack-python's C unpacker words both refusals differently from Timestamp.from_bytes, and interop quotes them."""
    with pytest.raises(InteropDecodeError, match=r"invalid timestamp data \(length 5\)$"):
        decode_interop_value(INTEROP_CASES["timestamp bad length"])
    with pytest.raises(InteropDecodeError, match=r"not greater than 999999999\.$"):
        decode_interop_value(INTEROP_CASES["timestamp bad nanoseconds 64"])


def test_the_hook_errors_are_the_serializers_own() -> None:
    # Not only the same class as before: the class the read paths already turn into a miss.
    with pytest.raises(SerializationError, match="Invalid tuple format: missing 'value' field"):
        auto_rust(AUTO_CASES["tuple marker missing its value"])


@pytest.mark.parametrize("decode", [auto_rust, interop_rust], ids=["auto", "interop"])
@pytest.mark.parametrize("header", [b"\x91", b"\x81\xa0"], ids=["array", "map"])
def test_the_nesting_ceiling_is_the_walks(decode: Callable[[bytes], Any], header: bytes) -> None:
    """At the ceiling it decodes; one level past it, or an empty container one level past it, the walk's error."""
    value, depth = decode(header * MSGPACK_MAX_NESTING + b"\xc0"), 0
    while isinstance(value, (list, dict)):  # iteratively: nested == recurses past the recursion limit
        depth, value = depth + 1, next(iter(value.values()) if isinstance(value, dict) else iter(value))
    assert (depth, value) == (MSGPACK_MAX_NESTING, None)
    empty = b"\x90" if header == b"\x91" else b"\x80"
    for past in (header * (MSGPACK_MAX_NESTING + 1) + b"\xc0", header * MSGPACK_MAX_NESTING + empty):
        with pytest.raises(ValueError, match=f"^{GUARD_ERROR_PREFIX}nests deeper than {MSGPACK_MAX_NESTING} levels$"):
            decode(past)


def test_buffer_inputs_decode_like_bytes() -> None:
    auto_doc = msgpack.packb({"k": [1, 2, b"x"]}, use_bin_type=True)
    interop_doc = msgpack.packb({"k": [1, 2, b"x"], 1: [2]}, use_bin_type=True)
    for wrap in (bytearray, memoryview, lambda d: memoryview(bytearray(d)), lambda d: memoryview(d).cast("c")):
        assert auto_rust(wrap(auto_doc)) == {"k": [1, 2, b"x"]}
        assert interop_rust(wrap(interop_doc)) == {"k": [1, 2, b"x"], 1: [2]}
        assert decode_interop_value(wrap(interop_doc)) == {"k": [1, 2, b"x"], 1: [2]}


def test_decode_interop_value_reads_any_buffer_its_reference_read() -> None:
    """Any buffer bytes() copied before: a signed or wide view, a non-contiguous one, or a mutable one."""
    doc = next(
        d for d in (msgpack.packb({"k": b"x" * m, "l": [1, 2, 3]}, use_bin_type=True) for m in range(1, 9)) if len(d) % 8 == 0
    )
    expected = interop_reference(doc)
    interleaved = bytes(b for pair in zip(doc, doc, strict=True) for b in pair)
    views = {
        "signed char": memoryview(doc).cast("b"),
        "2-D bytes": memoryview(doc).cast("B", shape=[len(doc) // 8, 8]),
        "uint16": memoryview(doc).cast("H"),
        "non-contiguous": memoryview(interleaved)[::2],
        "bytearray": bytearray(doc),
    }
    for name, view in views.items():
        assert decode_interop_value(view) == expected, name
    with pytest.raises(InteropDecodeError, match="CK v3 frame"):
        decode_interop_value(memoryview(b"CK\x03").cast("b"))


# ── the hook rules ────────────────────────────────────────────────────────────────────────────


def test_the_auto_hook_runs_only_on_maps_holding_a_marker_key() -> None:
    """The reference ran the hook on every map; it returns any map without a marker str key unchanged, because each
    marker test reads its key with dict.get. So skipping those maps changes no value."""
    seen: list[dict[Any, Any]] = []

    def hook(obj: dict[Any, Any]) -> Any:
        seen.append(dict(obj))
        return _auto_object_hook(obj)

    value = {
        "plain": {"a": 1},
        "tuple": (1, 2),
        "uuid": UUID(int=1),
        "set": {3},
        "look-alike": {"__set__": False, "value": 1},
        "bin key": {b"__tuple__": True, "value": [1]},
        "as a value": {"k": "__uuid__"},
        "near miss": {"__tuple": True, "__uuid___": True, "_set__": True},
    }
    unpackb_auto(_auto_pack(value, True), MSGPACK_MAX_NESTING, hook)
    assert seen == [
        {"__tuple__": True, "value": [1, 2]},
        {"__uuid__": True, "value": "00000000-0000-0000-0000-000000000001"},
        {"__set__": True, "value": [3], "frozen": False},
        {"__set__": False, "value": 1},
    ]


def test_every_marker_the_auto_writer_emits_is_revived() -> None:
    """A marker key missing from the Rust decoder's set would decode as its plain map: every one the writer emits must
    come back as the value written, nested and at the top."""
    numpy = pytest.importorskip("numpy")
    values = [
        datetime(2024, 1, 15, 1, 2, 3, tzinfo=UTC),
        date(2024, 1, 15),
        time(12, 30),
        UUID(int=9),
        (1, "a"),
        {1, 2},
        frozenset({"x"}),
    ]
    for value in values:
        for doc in (value, [value], {"k": value}):
            assert auto_rust(_auto_pack(doc, True)) == doc
    array = auto_rust(_auto_pack({"a": numpy.arange(4, dtype="int32")}, True))["a"]
    assert array.tolist() == [0, 1, 2, 3] and str(array.dtype) == "int32"
    marker_keys = {key for value in values for key in _auto_default(value) if key.startswith("__")}
    marker_keys |= {key for key in _auto_default(numpy.arange(1)) if key.startswith("__")}
    assert marker_keys == set(_AUTO_MARKER_KEYS)


def test_interop_hooks_run_only_where_the_reference_needed_them() -> None:
    """_revive_sentinels only on a map of str and bytes keys holding a temporal marker; _build_map only on a map holding
    a key neither str nor bytes, handed the pairs msgpack-python handed it."""
    revived: list[dict[Any, Any]] = []
    built: list[list[tuple[Any, Any]]] = []

    def revive(obj: dict[Any, Any]) -> Any:
        revived.append(dict(obj))
        return interop._revive_sentinels(obj)

    def build(pairs: list[tuple[Any, Any]]) -> Any:
        built.append(list(pairs))
        return interop._build_map(pairs)

    sentinel = {"__date__": True, "value": "2024-01-15"}
    doc = _map(
        [
            (_packed("plain"), _packed({"a": 1, b"b": 2})),
            (_packed("dated"), _packed(sentinel)),
            (_packed("auto marker"), _packed({"__tuple__": True, "value": [1]})),
            (_packed("int keyed"), _repeat_map([("s", 1), (1, 2), ("t", sentinel)])),
            (_packed("dated int keyed"), _repeat_map([("__date__", True), ("value", "2024-01-15"), (0, 0)])),
        ]
    )
    reference_pairs: list[list[tuple[Any, Any]]] = []

    def reference_build(pairs: Any) -> Any:
        reference_pairs.append(list(pairs))
        return interop._build_map(reference_pairs[-1])

    expected = unpackb_bounded(doc, raw=False, strict_map_key=False, object_pairs_hook=reference_build)
    assert unpackb_interop(doc, MSGPACK_MAX_NESTING, revive, build) == expected
    assert revived == [sentinel, sentinel]  # the top-level dated value, and the one inside the int-keyed map
    assert built == [p for p in reference_pairs if any(type(k) not in (str, bytes) for k, _ in p)]
    assert len(built) == 2


def test_a_hash_flood_is_refused_before_a_key_is_hashed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A map's str keys go straight into its dict. From its first key of another type the decode only holds its pairs
    for _build_map, which counts them before it hashes any, so a forged map never pays for its quadratic build."""
    hashed = 0
    timestamp_hash = msgpack.Timestamp.__hash__

    def counting_hash(self: msgpack.Timestamp) -> int:
        nonlocal hashed
        hashed += 1
        return timestamp_hash(self)

    monkeypatch.setattr(msgpack.Timestamp, "__hash__", counting_hash)
    with pytest.raises(InteropDecodeError, match="more than 32 entries keyed by neither str nor bytes"):
        decode_interop_value(_repeat_map([("a", 0), ("b", 1), *((msgpack.Timestamp(i, 0), i) for i in range(40))]))
    assert hashed == 0
    decode_interop_value(_repeat_map([("a", 0), *((msgpack.Timestamp(i, 0), i) for i in range(32))]))
    assert hashed == 32  # under the bound the map is built, which hashes each key once


# ── heap, references, threads ─────────────────────────────────────────────────────────────────

# Legal payloads that materialise many objects per byte: N maps keyed by an int (each made by _build_map from its
# pairs), N maps of a str key then an int key (each holds its pairs from the str key on), and N short marker maps.
_N = 100_000
AMPLIFIERS = {
    "int-keyed maps": b"\xdd" + _N.to_bytes(4, "big") + b"\x81\x01\xc0" * _N,
    "str then int keyed maps": b"\xdd" + _N.to_bytes(4, "big") + b"\x82\xa1a\xc0\x01\xc0" * _N,
    "one wide map, then an int key": b"\xdf"
    + (_N + 1).to_bytes(4, "big")
    + b"".join(b"\xa5" + b"%05d" % i + b"\xc0" for i in range(_N))
    + b"\x01\xc0",
    "short tuple markers": b"\xdd" + _N.to_bytes(4, "big") + _auto_pack((1,), True) * _N,
}


def _measure_amplifier_peaks() -> dict[str, list[int]]:
    import tracemalloc

    peaks = {}
    for name, data in AMPLIFIERS.items():
        pair = []
        decoders = (auto_reference, auto_rust) if "marker" in name else (interop_rust_reference, interop_rust)
        for decode in decoders:
            # Collection paused: on a free-threaded build a cycle collection that lands mid-decode allocates a
            # worklist (one pointer per tracked object) inside the traced window, and where it lands differs by decoder.
            gc.collect()
            gc.disable()
            tracemalloc.start()
            value = decode(data)
            pair.append(tracemalloc.get_traced_memory()[1])
            tracemalloc.stop()
            gc.enable()
            del value
        peaks[name] = pair
    return peaks


def test_an_amplifying_payload_costs_no_more_heap_than_before() -> None:
    for name, (before, now) in measure_in_subprocess(_measure_amplifier_peaks).items():
        assert now <= before, f"{name}: {now} bytes at peak, {before} before"


_ALL_CASES = {**CASES, **INTEROP_CASES, **AUTO_CASES}


def _decode_or_refuse(data: bytes | bytearray | memoryview) -> None:
    """Decode ``data`` with both decoders, swallowing only the refusals a read path turns into a miss (a forged Auto
    marker can raise AttributeError from its hook); any other error fails the caller."""
    for decode in (auto_rust, interop_rust, decode_interop_value):
        try:
            decode(data)
        except (*PAYLOAD_DECODE_ERRORS, SerializationError):  # noqa: S110 - the refusal is the expected outcome
            pass


def _measure_leaks() -> list[int]:
    import tracemalloc

    held = _repeat_map([("a", "v" * 40), (1, [datetime(2024, 1, 15).isoformat()]), (b"b", {"__date__": True})])

    def every_path() -> None:
        for data in (*_ALL_CASES.values(), _auto_pack({"k": ["v" * 40, (1, 2), {3}, UUID(int=5)] * 3}, True), held):
            _decode_or_refuse(data)

    for _ in range(50):  # warm caches and lazily imported msgpack classes
        every_path()
    gc.collect()
    tracemalloc.start()
    sizes = []
    for _ in range(LEAK_ROUNDS + 1):
        for _ in range(LEAK_CALLS_PER_ROUND):
            every_path()
        gc.collect()
        sizes.append(tracemalloc.get_traced_memory()[0])
    tracemalloc.stop()
    return sizes


# As in test_standard_decode.py: a path that keeps an object it allocated keeps at least one per call, so it adds at
# least LEAK_CALLS_PER_ROUND * 28 bytes to every round; the gate bounds the quietest of five rounds.
LEAK_ROUNDS = 5
LEAK_CALLS_PER_ROUND = 200
LEAK_ROUND_BUDGET = 4 * 1024


def test_no_path_leaks_an_object() -> None:
    """An object a decode allocates and never frees climbs traced memory every round (per-call allocations only)."""
    sizes = measure_in_subprocess(_measure_leaks)
    rounds = [after - before for before, after in zip(sizes, sizes[1:], strict=False)]
    assert min(rounds) < LEAK_ROUND_BUDGET, sizes


# Empty and one-byte bytes are shared cached singletons (immortal from 3.12), so their counts are not the decode's alone.
_MULTI_BYTE = {name: data for name, data in _ALL_CASES.items() if len(data) > 1}


@pytest.mark.parametrize("data", _MULTI_BYTE.values(), ids=_MULTI_BYTE.keys())
def test_no_decode_keeps_a_reference_to_its_input(data: bytes) -> None:
    """Every path, from every buffer type, gives back each reference it took to the input, its buffer and the hooks."""
    owned = bytes(bytearray(data))  # a fresh object: a code constant may be immortal too
    view, buffer = memoryview(owned), bytearray(data)
    assert view.obj is owned
    watched = (owned, view, buffer, _auto_object_hook, interop._revive_sentinels, interop._build_map)
    before = [sys.getrefcount(o) for o in watched]
    for source in (owned, view, buffer):
        for _ in range(50):
            _decode_or_refuse(source)
    del source  # still bound to the last input
    assert [sys.getrefcount(o) for o in watched] == before
    buffer.append(0)  # a buffer export never released refuses the resize with BufferError


def test_threads_decode_one_document_concurrently() -> None:
    rows = [{"id": i, "at": datetime(2024, 1, 15), "pair": (i, str(i)), "tags": {i}} for i in range(50)]
    auto_doc = _auto_pack({"rows": rows}, True)
    sentinel = {"__datetime__": True, "value": "2024-01-01T00:00:00+00:00"}
    interop_doc = _repeat_map([("rows", [{"id": i, "at": sentinel} for i in range(50)]), (7, sentinel)])
    expected = (auto_reference(auto_doc), interop_reference(interop_doc))
    failures: list[object] = []
    barrier = threading.Barrier(8)

    def work() -> None:
        barrier.wait()
        try:
            failures.extend(1 for _ in range(100) if (auto_rust(auto_doc), decode_interop_value(interop_doc)) != expected)
        except Exception as e:  # noqa: BLE001 - a raise would end only this thread; the test thread asserts it
            failures.append(e)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not failures
