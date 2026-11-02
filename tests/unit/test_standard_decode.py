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
- no path leaks an object it allocates, nor a reference to its input, the input's buffer or the hook,
  and the decode is safe to run from many threads.

The inputs are values packed as the serializer packs them, the same with ``bytes`` packed as str
(invalid UTF-8), and both after random byte edits, so the bounds, truncation, reserved-marker and
trailing-byte paths are reached as well as the happy path.
"""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import sysconfig
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import msgpack
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cachekit._rust_serializer import unpackb_standard
from cachekit.serializers.base import MSGPACK_MAX_NESTING, SerializationError, unpackb_bounded
from cachekit.serializers.standard_serializer import _standard_default, _standard_object_hook
from tests.utils.tracemalloc_isolation import measure_in_subprocess

pytestmark = pytest.mark.unit

GUARD_ERROR_PREFIX = "Unpack failed: MessagePack document "


def reference(data: bytes) -> Any:
    """The decode every StandardSerializer read ran before this one."""
    return unpackb_bounded(data, raw=False, use_list=True, object_hook=_standard_object_hook)


def rust(data: bytes | bytearray | memoryview) -> Any:
    return unpackb_standard(data, MSGPACK_MAX_NESTING, _standard_object_hook)


def _decode_or_refuse(data: bytes | bytearray | memoryview) -> None:
    """Decode ``data``, swallowing only the refusals CASES pins; any other error fails the caller."""
    try:
        rust(data)
    except (ValueError, SerializationError, TypeError):  # noqa: S110 - the refusal is the expected outcome
        pass


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


_OUT_OF_MEMORY = """
import resource, sys
from cachekit._rust_serializer import unpackb_standard
n = 1 << 27
data = {shape}
with open("/proc/self/statm") as f:
    mapped = int(f.read().split()[0]) * resource.getpagesize()
resource.setrlimit(resource.RLIMIT_AS, (mapped + (64 << 20), resource.getrlimit(resource.RLIMIT_AS)[1]))
try:
    unpackb_standard(data, 1024, None)
except MemoryError:
    sys.exit(0)
sys.exit(3)
"""
# Each single allocation the input sizes (n is 128 MiB), as the `data` expression the script decodes. Many small
# objects (dicts, short lists, floats, ints past the small-int cache) still abort: see msgpack_decode.rs's module doc.
_OUT_OF_MEMORY_SHAPES = {
    "str": 'b"\\xdb" + n.to_bytes(4, "big") + b"a" * n',
    "bin": 'b"\\xc6" + n.to_bytes(4, "big") + bytes(n)',
    "ext": 'b"\\xc9" + n.to_bytes(4, "big") + b"\\x05" + bytes(n)',
    "trailing bytes": 'b"\\xc0" + bytes(n)',
    "array of n / 8 items": 'b"\\xdd" + (n >> 3).to_bytes(4, "big") + b"\\xc0" * (n >> 3)',
}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_AS is enforced on Linux")
@pytest.mark.skipif(
    bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
    reason="mimalloc's reserved arenas hold the allocation, so RLIMIT_AS never refuses it; the GIL legs run the same Rust",
)
@pytest.mark.parametrize("shape", _OUT_OF_MEMORY_SHAPES.values(), ids=_OUT_OF_MEMORY_SHAPES.keys())
def test_an_allocation_the_input_sizes_raises_memory_error(shape: str) -> None:
    """With panic = "abort", a PyO3 constructor that meets a NULL would kill the process, not raise."""
    script = _OUT_OF_MEMORY.format(shape=shape)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}  # import this checkout's cachekit, as pytest does
    run = subprocess.run([sys.executable, "-c", script], capture_output=True, check=False, env=env)  # noqa: S603 - this module's own script
    assert run.returncode == 0, run.stderr.decode()[-2000:]


def _measure_leaks() -> list[int]:
    import tracemalloc

    def every_path() -> None:
        for data in (*CASES.values(), _pack({"k": ["v" * 40, datetime(2024, 1, 15), b"b"] * 3}, True)):
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


# This gate catches per-call allocations only: a path that keeps an object it allocated keeps at least one per call,
# and the smallest object (an int) is 28 bytes, so it adds at least 500 * 28 = 13.7 KiB to EVERY round. A leaked
# reference to an object that already exists (the input, its buffer, the hook) allocates nothing, so tracemalloc
# cannot see it: test_no_decode_keeps_a_reference_to_its_input counts those. The interpreter's own growth (caches,
# free lists, a dict resizing) comes in a few one-off steps, and one such step can exceed any fixed budget on the
# total. So the gate bounds the quietest of five rounds, which grows by under 1 KiB when nothing leaks.
LEAK_ROUNDS = 5
LEAK_CALLS_PER_ROUND = 500
LEAK_ROUND_BUDGET = 8 * 1024


def test_no_path_leaks_an_object() -> None:
    """An object a decode allocates and never frees climbs traced memory every round (per-call allocations only)."""
    sizes = measure_in_subprocess(_measure_leaks)
    rounds = [after - before for before, after in zip(sizes, sizes[1:], strict=False)]
    assert min(rounds) < LEAK_ROUND_BUDGET, sizes


# Empty and one-byte bytes are shared cached singletons (immortal from 3.12), so their counts are not the decode's alone.
_MULTI_BYTE = {name: data for name, data in CASES.items() if len(data) > 1}


@pytest.mark.parametrize("data", _MULTI_BYTE.values(), ids=_MULTI_BYTE.keys())
def test_no_decode_keeps_a_reference_to_its_input(data: bytes) -> None:
    """Every path, from every buffer type, gives back each reference it took to the input, its buffer and the hook."""
    owned = bytes(bytearray(data))  # a fresh object: a code constant may be immortal too
    view, buffer = memoryview(owned), bytearray(data)
    assert view.obj is owned
    watched = (owned, view, buffer, _standard_object_hook)
    before = [sys.getrefcount(o) for o in watched]
    for source in (owned, view, buffer):
        for _ in range(100):
            _decode_or_refuse(source)
    del source  # still bound to the last input
    assert [sys.getrefcount(o) for o in watched] == before
    buffer.append(0)  # a buffer export never released refuses the resize with BufferError


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
