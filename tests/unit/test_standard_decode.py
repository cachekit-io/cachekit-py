"""StandardSerializer's Rust decode (``unpackb_standard``) against the decode it replaced.

Every StandardSerializer read used to run ``unpackb_bounded(data, raw=False, use_list=True,
object_hook=_standard_object_hook)``: cachekit-core's structure walk, then msgpack-python. The Rust
extension now does both in one pass. These tests hold it to that reference, input for input:

- every input the reference accepts decodes to an equal value of the same types (``repr`` tells
  1 from 1.0 from True, a datetime from the marker map it came from, bytes from str);
- every input it refuses raises the same exception class, and a bound it refuses with is refused
  with the same ``Unpack failed: MessagePack document ...`` message, so the bounds tests and the
  read paths' error handling see what they saw before;
- the hook runs only on maps holding a marker key (the rule the speed rests on), and a heap
  amplification payload costs no more than it did;
- no path leaks a reference, and the decode is safe to run from many threads.

The inputs are values packed as the serializer packs them, the same with ``bytes`` packed as str
(invalid UTF-8), and both after random byte edits, so the bounds, truncation, reserved-marker and
trailing-byte paths are reached as well as the happy path.
"""

from __future__ import annotations

import gc
import math
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any

import msgpack
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cachekit._rust_serializer import unpackb_standard
from cachekit.serializers.base import MSGPACK_MAX_NESTING, SerializationError, unpackb_bounded
from cachekit.serializers.standard_serializer import StandardSerializer, _standard_default, _standard_object_hook
from tests.utils.tracemalloc_isolation import measure_in_subprocess

pytestmark = pytest.mark.unit

GUARD_ERROR_PREFIX = "Unpack failed: MessagePack document "


def reference(data: bytes) -> Any:
    """The decode every StandardSerializer read ran before this one."""
    return unpackb_bounded(data, raw=False, use_list=True, object_hook=_standard_object_hook)


def rust(data: bytes) -> Any:
    return unpackb_standard(data, MSGPACK_MAX_NESTING, _standard_object_hook)


def outcome(decode: Callable[[bytes], Any], data: bytes) -> tuple[Any, ...]:
    """What ``decode`` returned, as a repr, or the class it raised. A bound's message is kept: it names the bound."""
    try:
        return ("value", repr(decode(data)))
    except msgpack.ExtraData as e:
        return ("extra", repr(e.unpacked), e.extra)
    except Exception as e:  # noqa: BLE001 - the outcome under comparison
        message = str(e)
        return ("raised", type(e), message if message.startswith(GUARD_ERROR_PREFIX) else None)


def assert_same(data: bytes) -> None:
    assert outcome(rust, data) == outcome(reference, data), data.hex()


# ── generated values ──────────────────────────────────────────────────────────────────────────

_MARKER_KEYS = ("__datetime__", "__date__", "__time__")
_ZONES = st.sampled_from([None, UTC, timezone(timedelta(hours=5, minutes=30)), timezone(-timedelta(hours=8))])
_TEMPORAL = st.datetimes(timezones=_ZONES) | st.dates() | st.times(timezones=_ZONES)
_ISO = _TEMPORAL.map(lambda t: t.isoformat())
_FLAGS = st.sampled_from([True, False, 1, 0, 0.0, float("nan"), "yes", "", b"", [], [0], {}, None])
# Maps a caller wrote that look like markers: a sentinel, truthy or not, with a value that parses, does not
# parse, is not a str, or is missing; and maps of another size that carry a sentinel.
_MIMICS = (
    st.builds(
        lambda k, f, v: {k: f, "value": v},
        st.sampled_from(_MARKER_KEYS),
        _FLAGS,
        _ISO | st.text(max_size=12) | st.none() | st.integers(-(2**63), 2**64 - 1) | st.binary(max_size=4),
    )
    | st.builds(lambda k, f, o: {k: f, o: 1}, st.sampled_from(_MARKER_KEYS), _FLAGS, st.text(max_size=6))
    | st.builds(lambda k, v: {k: True, "value": v, "extra": 1}, st.sampled_from(_MARKER_KEYS), _ISO)
    | st.builds(lambda k: {k: True}, st.sampled_from(_MARKER_KEYS))
    | st.builds(lambda k, v: {k.encode(): True, "value": v}, st.sampled_from(_MARKER_KEYS), _ISO)  # bin key
)
_EXT = st.builds(msgpack.ExtType, st.integers(0, 127), st.binary(max_size=20)) | st.builds(
    msgpack.Timestamp, st.integers(-(2**40), 2**40), st.integers(0, 999_999_999)
)
_KEYS = st.text(max_size=8) | st.binary(max_size=4) | st.sampled_from((*_MARKER_KEYS, "value")) | st.integers(-3, 3)
_LEAVES = (
    st.none()
    | st.booleans()
    | st.integers(-(2**63), 2**64 - 1)
    | st.floats()  # NaN, infinities and -0.0 included
    | st.text()
    | st.binary()
    | _TEMPORAL
    | _MIMICS
    | _EXT
    | st.sampled_from(_MARKER_KEYS)
)
VALUES = st.recursive(
    _LEAVES,
    lambda inner: st.lists(inner, max_size=6) | st.dictionaries(_KEYS, inner, max_size=6) | st.tuples(inner, inner),
    max_leaves=40,
)


def _pack(value: Any, use_bin_type: bool, single_float: bool = False) -> bytes:
    """``single_float`` reaches the float32 marker, for values whose floats all fit one."""
    try:
        return msgpack.packb(value, use_bin_type=use_bin_type, use_single_float=single_float, default=_standard_default)
    except OverflowError:
        return msgpack.packb(value, use_bin_type=use_bin_type, default=_standard_default)


@settings(max_examples=1500, deadline=None)
@given(value=VALUES, use_bin_type=st.booleans(), single_float=st.booleans())
def test_every_packed_value_decodes_as_before(value: Any, use_bin_type: bool, single_float: bool) -> None:
    """use_bin_type=False packs bytes as str, so invalid UTF-8 reaches the str path; int keys reach strict_map_key."""
    assert_same(_pack(value, use_bin_type, single_float))


_EDITS = st.lists(
    st.tuples(st.sampled_from(["set", "insert", "delete"]), st.integers(0, 2**16), st.integers(0, 255)), max_size=4
)


@settings(max_examples=1500, deadline=None)
@given(value=VALUES, use_bin_type=st.booleans(), edits=_EDITS, cut=st.integers(0, 3), tail=st.binary(max_size=3))
def test_every_edited_document_decodes_or_refuses_as_before(
    value: Any, use_bin_type: bool, edits: list[tuple[str, int, int]], cut: int, tail: bytes
) -> None:
    """Byte edits, a truncation and trailing bytes reach the bounds, the reserved marker, ExtraData and broken markers."""
    data = bytearray(_pack(value, use_bin_type))
    for op, at, byte in edits:
        at %= len(data) + 1
        if op == "insert":
            data.insert(at, byte)
        elif at < len(data):
            if op == "set":
                data[at] = byte
            else:
                del data[at]
    assert_same(bytes(data[: max(len(data) - cut, 0)]) + tail)


@settings(max_examples=3000, deadline=None)
@given(data=st.binary(max_size=48))
def test_arbitrary_bytes_decode_or_refuse_as_before(data: bytes) -> None:
    assert_same(data)


# ── explicit cases ────────────────────────────────────────────────────────────────────────────

CASES = {
    "empty input": b"",
    "nil": b"\xc0",
    "reserved 0xc1": b"\x91\xc1",
    "trailing nil": b"\xc0\xc0",
    "trailing garbage after a map": msgpack.packb({"a": 1}) + b"\xc1\xff",
    "int key": msgpack.packb({1: "a"}),
    "float key": msgpack.packb({1.5: "a"}),
    "nil key": msgpack.packb({None: 1}),
    "list key": b"\x81\x91\x01\x02",
    "map key holding a datetime marker": b"\x81" + msgpack.packb({"__date__": True, "value": "2024-01-15"}) + b"\x01",
    "bytes key": msgpack.packb({b"k": 1}, use_bin_type=True),
    "duplicate keys, last wins": b"\x82\xa1a\x01\xa1a\x02",
    "duplicate keys collapse to a marker": b"\x83\xa8__date__\xc3\xa8__date__\xc3\xa5value\xaa2024-01-15",
    "invalid UTF-8 str": b"\xa2\xff\xfe",
    "invalid UTF-8 key": b"\x81\xa2\xff\xfe\x01",
    "invalid UTF-8 after a bound violation": b"\x92\xa2\xff\xfe\xdd\xff\xff\xff\xff",
    "lone surrogate": b"\xa3\xed\xa0\x80",
    "str8 marker key": b"\x82\xd9\x08__date__\xc3\xa5value\xaa2024-01-15",
    "marker missing value": msgpack.packb({"__datetime__": True, "x": 1}),
    "marker with nil value": msgpack.packb({"__time__": True, "value": None}),
    "marker with int value": msgpack.packb({"__date__": True, "value": 7}),
    "marker with unparseable value": msgpack.packb({"__datetime__": True, "value": "yesterday"}),
    "marker with falsy flag": msgpack.packb({"__datetime__": 0, "value": "2024-01-15T00:00:00"}),
    "marker with nan flag": msgpack.packb({"__date__": float("nan"), "value": "2024-01-15"}),
    "two sentinels": msgpack.packb({"__datetime__": False, "__date__": True}),
    "marker error before a trailing byte": msgpack.packb({"__date__": True, "value": "x"}) + b"\xc0",
    "nested marker": msgpack.packb([{"a": {"__time__": True, "value": "12:30:00+05:30"}}]),
    "uint64 max": msgpack.packb(2**64 - 1),
    "int64 min": msgpack.packb(-(2**63)),
    "float32": msgpack.packb(1.1, use_single_float=True),
    "nan": msgpack.packb(float("nan")),
    "negative zero": msgpack.packb(-0.0),
    "ext": msgpack.packb(msgpack.ExtType(5, b"abc")),
    "fixext 16": b"\xd8\x05" + b"\x00" * 16,
    "negative ext code": b"\xd4\xfe\x00",
    "timestamp 32": msgpack.packb(msgpack.Timestamp(1, 0)),
    "timestamp 64": msgpack.packb(msgpack.Timestamp(2**33, 5)),
    "timestamp 96": msgpack.packb(msgpack.Timestamp(-(2**40), 999_999_999)),
    "timestamp bad length": b"\xc7\x05\xff" + b"\x00" * 5,
    "timestamp bad nanoseconds": b"\xd7\xff" + (0x3FFFFFFF << 34 | 1).to_bytes(8, "big"),
    "array32 over-claim": b"\xdd\xff\xff\xff\xff",
    "map32 over-claim": b"\xdf\xff\xff\xff\xff",
    "str32 over-claim": b"\xdb\xff\xff\xff\xff",
    "ext32 over-claim": b"\xc9\xff\xff\xff\xff\x05",
    "truncated length prefix": b"\xda\x00",
    "ends mid-array": b"\x93\x01\x02",
    "nested-header bomb": b"\xdc\x07\xd0" * 5000,
}


@pytest.mark.parametrize("data", CASES.values(), ids=CASES.keys())
def test_explicit_case_decodes_or_refuses_as_before(data: bytes) -> None:
    assert_same(data)


def test_the_hook_errors_are_the_serializers_own() -> None:
    # Not only the same class as before: the class the read paths already turn into a miss.
    with pytest.raises(SerializationError, match="missing 'value' field"):
        rust(CASES["marker missing value"])


@pytest.mark.parametrize("header", [b"\x91", b"\x81\xa0"], ids=["array", "map"])
def test_the_nesting_ceiling_is_the_walks(header: bytes) -> None:
    """At the ceiling it decodes; one level past it, or an empty container one level past it, the walk's error."""
    at = header * MSGPACK_MAX_NESTING + b"\xc0"
    value, depth = rust(at), 0
    while isinstance(value, (list, dict)):  # iteratively: nested == recurses past the recursion limit
        depth, value = depth + 1, next(iter(value.values()) if isinstance(value, dict) else iter(value))
    assert (depth, value) == (MSGPACK_MAX_NESTING, None)
    empty = b"\x90" if header == b"\x91" else b"\x80"
    for past in (header * (MSGPACK_MAX_NESTING + 1) + b"\xc0", header * MSGPACK_MAX_NESTING + empty):
        with pytest.raises(ValueError, match=f"^{GUARD_ERROR_PREFIX}nests deeper than {MSGPACK_MAX_NESTING} levels$"):
            rust(past)


def test_buffer_inputs_decode_like_bytes() -> None:
    doc = msgpack.packb({"k": [1, 2, b"x"]}, use_bin_type=True)
    for data in (bytearray(doc), memoryview(doc), memoryview(bytearray(doc)), memoryview(doc).cast("c")):
        assert rust(data) == {"k": [1, 2, b"x"]}


def test_a_value_round_trips_through_the_serializer() -> None:
    value = {"at": datetime(2024, 1, 15, 12, 30, tzinfo=UTC), "on": date(2024, 1, 15), "t": time(9), "n": [math.pi, -1]}
    serializer = StandardSerializer()
    assert serializer.deserialize(serializer.serialize(value)[0]) == value


# ── the marker rule ───────────────────────────────────────────────────────────────────────────


def test_the_hook_runs_only_on_maps_holding_a_marker_key() -> None:
    """The reference ran the hook on every map; it returns any map without a marker str key unchanged, because
    each marker test reads its key with dict.get. So skipping those maps changes no value."""
    seen: list[dict[Any, Any]] = []

    def hook(obj: dict[Any, Any]) -> Any:
        seen.append(dict(obj))
        return _standard_object_hook(obj)

    doc = _pack(
        {
            "plain": {"a": 1},
            "dated": datetime(2024, 1, 15),
            "look-alike": {"__date__": False, "value": 1, "x": 2},
            "bin key": {b"__date__": True, "value": "2024-01-15"},
            "as a value": {"k": "__date__"},
        },
        use_bin_type=True,
    )
    unpackb_standard(doc, MSGPACK_MAX_NESTING, hook)
    assert seen == [{"__datetime__": True, "value": "2024-01-15T00:00:00"}, {"__date__": False, "value": 1, "x": 2}]


# ── heap, references, threads ─────────────────────────────────────────────────────────────────

# Legal payloads that materialise many objects per byte: N empty maps (the shape the decode bounds leave
# open), N empty arrays, and N repeats of a short str.
_N = 200_000
AMPLIFIERS = {
    "empty maps": b"\xdd" + _N.to_bytes(4, "big") + b"\x80" * _N,
    "empty arrays": b"\xdd" + _N.to_bytes(4, "big") + b"\x90" * _N,
    "short strs": b"\xdd" + _N.to_bytes(4, "big") + b"\xa2ab" * _N,
}


def _measure_amplifier_peaks() -> dict[str, list[int]]:
    import tracemalloc

    peaks = {}
    for name, data in AMPLIFIERS.items():
        pair = []
        for decode in (reference, rust):
            gc.collect()
            tracemalloc.start()
            value = decode(data)
            pair.append(tracemalloc.get_traced_memory()[1])
            tracemalloc.stop()
            del value
        peaks[name] = pair
    return peaks


def test_an_amplifying_payload_costs_no_more_heap_than_before() -> None:
    for name, (before, now) in measure_in_subprocess(_measure_amplifier_peaks).items():
        assert now <= before * 1.01, f"{name}: {now} bytes at peak, {before} before"


def _measure_leaks() -> list[int]:
    import tracemalloc

    def every_path() -> None:
        for data in (*CASES.values(), _pack({"k": ["v" * 40, datetime(2024, 1, 15), b"b"] * 3}, True)):
            try:
                rust(data)
            except Exception:  # noqa: BLE001, S110 - every path is the point, not its result
                pass

    for _ in range(50):  # warm caches and lazily imported msgpack classes
        every_path()
    gc.collect()
    tracemalloc.start()
    sizes = []
    for _ in range(4):
        for _ in range(300):
            every_path()
        gc.collect()
        sizes.append(tracemalloc.get_traced_memory()[0])
    tracemalloc.stop()
    return sizes


def test_no_path_leaks_a_reference() -> None:
    """A leaked reference keeps its object alive, so traced memory would climb with every round."""
    sizes = measure_in_subprocess(_measure_leaks)
    assert sizes[-1] - sizes[0] < 16 * 1024, sizes


def test_threads_decode_one_document_concurrently() -> None:
    doc = _pack({"users": [{"id": i, "name": f"user {i}", "at": datetime(2024, 1, 15)} for i in range(50)]}, True)
    expected, failures, barrier = reference(doc), [], threading.Barrier(8)

    def work() -> None:
        barrier.wait()
        failures.extend(1 for _ in range(200) if rust(doc) != expected)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not failures
