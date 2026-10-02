"""AutoSerializer packs with strict_types=True instead of rebuilding the value tree (LAB-7069).

The old write path copied every list and dict in ``_wrap_tuples`` just to find tuples, then packed
with ``strict_types=False``. Now msgpack hands every tuple and builtin subclass to ``_auto_default``,
which must reproduce the old bytes exactly. ``_legacy_packb`` below is the old path, kept verbatim
as the reference. It uses today's ``_auto_default``: without strict_types msgpack packs every
builtin subclass natively, so the new branches at the top of the default are unreachable there.

Two inputs change bytes on purpose. A tuple inside a set or frozenset, and a tuple used as a dict
key, packed as a bare array before; neither could be read back. The set case now round-trips.
"""

from __future__ import annotations

import enum
import sys
from collections import Counter, OrderedDict, defaultdict, namedtuple
from datetime import date, datetime, time, timezone
from typing import Any
from uuid import UUID

import msgpack
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cachekit.cache_handler import CacheSerializationHandler
from cachekit.serializers.auto_serializer import AutoSerializer, _auto_default
from cachekit.serializers.base import SerializationError

try:
    import numpy as np
except ImportError:  # a dev-group dependency; the free-threaded CI job installs only the test group
    np = None


def _legacy_wrap_tuples(obj: Any) -> Any:
    if isinstance(obj, tuple):
        return {"__tuple__": True, "value": [_legacy_wrap_tuples(x) for x in obj]}
    if isinstance(obj, list):
        return [_legacy_wrap_tuples(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _legacy_wrap_tuples(v) for k, v in obj.items()}
    return obj


def _legacy_packb(obj: Any) -> bytes:
    return msgpack.packb(_legacy_wrap_tuples(obj), use_bin_type=True, strict_types=False, default=_auto_default)


def _packb(obj: Any) -> bytes:
    return msgpack.packb(obj, **AutoSerializer()._msgpack_pack_opts)


Point = namedtuple("Point", "x y")


class Color(enum.IntEnum):
    RED = 1
    BIG = 2**40


class Perm(enum.IntFlag):
    R = 4
    W = 2


class Mood(str, enum.Enum):
    HAPPY = "happy"


class Tagged:
    """Mixin giving each builtin subclass below a ``__dict__``: the custom-class check must never see them."""

    note = "x"


class MyDict(Tagged, dict):
    pass


class MyList(Tagged, list):
    pass


class MyTuple(Tagged, tuple):
    pass


class MyStr(Tagged, str):
    def __str__(self) -> str:  # msgpack packs the code points, never str(); so must the default
        return "overridden"


class MyInt(Tagged, int):
    def __int__(self) -> int:
        return -1


class MyFloat(Tagged, float):
    pass


class MyBytes(Tagged, bytes):
    def __bytes__(self) -> bytes:
        return b"overridden"


class MyBytearray(Tagged, bytearray):
    pass


def _reordered() -> OrderedDict:
    od = OrderedDict(a=1, b=(2, 3), c=[4])
    od.move_to_end("a")  # .items() order now differs from the underlying dict's insertion order
    return od


CORPUS: dict[str, Any] = {
    "plain-records": [{"id": i, "name": f"u{i}", "score": i / 3, "tags": ["a", "b"], "ok": i % 2 == 0} for i in range(20)],
    "nested-tuples": (1, (2, (3, ())), [(), (4,)], {"k": (5, 6)}),
    "empty-tuple": (),
    "namedtuple": [Point(1, 2), {"p": Point((3,), [4])}],
    "ordered-dict-moved": _reordered(),
    "defaultdict": defaultdict(list, {"a": [(1, 2)], "b": []}),
    "counter": Counter("hello"),
    "int-enum": [Color.RED, Color.BIG, {"c": Color.RED}],
    "int-flag": Perm.R | Perm.W,
    "str-enum": [Mood.HAPPY, {"m": Mood.HAPPY}],
    "bytes-like": [b"\x00\xff", bytearray(b"ab"), memoryview(b"mv")],
    "subclasses-with-dict": [
        MyDict(a=(1,)),
        MyList([1, (2,)]),
        MyTuple((1, [2])),
        MyStr("text"),
        MyInt(7),
        MyFloat(2.5),
        MyBytes(b"raw"),
        MyBytearray(b"arr"),
    ],
    "subclass-keys": {MyStr("k"): 1, Mood.HAPPY: 2, Color.RED: 3},
    "temporal-uuid": [datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc), date(2026, 1, 2), time(3, 4), UUID(int=7)],
    "sets-of-scalars": [{1, 2, 3}, frozenset({"a", "b"})],
    "scalars": [None, True, False, 0, -1, 2**63, -(2**63), 1.0, -0.0, float("inf"), "", "ü", b""],
    "deep-acyclic": [[[[[[[[(1,)]]]]]]]],
}
if np is not None:
    CORPUS["np-float64"] = [np.float64(1.5), np.float64(-0.0)]
    CORPUS["ndarray"] = {"arr": np.arange(6, dtype=np.int32).reshape(2, 3)}
if sys.version_info >= (3, 11):

    class Level(enum.StrEnum):
        HIGH = "high"

    CORPUS["strenum"] = [Level.HIGH, {Level.HIGH: Level.HIGH}]


@pytest.mark.parametrize("value", CORPUS.values(), ids=list(CORPUS))
def test_bytes_match_the_legacy_path(value: Any) -> None:
    assert _packb(value) == _legacy_packb(value)


_leaves = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**63), max_value=2**64 - 1),
    st.floats(allow_nan=False),
    st.text(max_size=8),
    st.binary(max_size=8),
    st.sampled_from(
        [Color.RED, Perm.W, Mood.HAPPY, MyStr("s"), MyInt(3), MyFloat(1.0)] + ([np.float64(0.25)] if np is not None else [])
    ),
    st.builds(bytearray, st.binary(max_size=4)),
    st.frozensets(st.integers(min_value=-(2**63), max_value=2**64 - 1), max_size=3),
)
_values = st.recursive(
    _leaves,
    lambda inner: st.one_of(
        st.lists(inner, max_size=4),
        st.lists(inner, max_size=4).map(tuple),
        st.lists(inner, max_size=2).map(lambda xs: Point(*(xs + [None, None])[:2])),
        st.dictionaries(st.text(max_size=4), inner, max_size=4),
        st.dictionaries(st.text(max_size=4), inner, max_size=4).map(OrderedDict),
        st.dictionaries(st.text(max_size=4), inner, max_size=4).map(MyDict),
        st.lists(inner, max_size=3).map(MyList),
        st.lists(inner, max_size=3).map(MyTuple),
    ),
    max_leaves=20,
)


@settings(max_examples=500, deadline=None)
@given(_values)
def test_fuzzed_values_pack_byte_identical(value: Any) -> None:
    assert _packb(value) == _legacy_packb(value)


_UNSUPPORTED: dict[str, Any] = {
    "object": object(),
    "custom-class": Tagged(),
    "int-past-u64": 2**64,
    "int-below-i64": -(2**63) - 1,
}
if np is not None:
    _UNSUPPORTED["np-int64"] = np.int64(1)


@pytest.mark.parametrize("value", _UNSUPPORTED.values(), ids=list(_UNSUPPORTED))
def test_unsupported_values_fail_as_before(value: Any) -> None:
    """Same exception type and message as the legacy path, so serialize_data's handling is unchanged."""
    with pytest.raises(TypeError) as legacy:
        _legacy_packb(value)
    with pytest.raises(TypeError) as new:
        _packb(value)
    assert str(new.value) == str(legacy.value)


@pytest.mark.parametrize("integrity", [True, False], ids=["integrity", "no-integrity"])
def test_tuples_inside_sets_now_round_trip(integrity: bool) -> None:
    """The one deliberate byte change: the legacy bytes raised on every read ("unhashable list")."""
    value = {"s": frozenset({(1, 2), (3, (4,))}), "t": {("a",)}}
    s = AutoSerializer(enable_integrity_checking=integrity)
    data, meta = s.serialize(value)
    assert s.deserialize(data, meta) == value
    assert _packb(value) != _legacy_packb(value)


def test_cyclic_value_is_a_serialization_error() -> None:
    """The legacy pre-pass hit RecursionError, wrapped by serialize_data; msgpack's own limit raises
    ValueError, which serialize_data deliberately re-raises raw, so _serialize_msgpack converts it."""
    cyclic: list[Any] = []
    cyclic.append(cyclic)
    with pytest.raises(SerializationError):
        AutoSerializer().serialize(cyclic)
    with pytest.raises(SerializationError):
        CacheSerializationHandler(serializer_name="auto").serialize_data(cyclic, cache_key="k")


def test_round_trip_preserves_types() -> None:
    value = {"t": (1, (2,)), "nt": Point(1, 2), "e": Color.RED, "od": _reordered(), "f": MyFloat(0.5)}
    s = AutoSerializer()
    data, meta = s.serialize(value)
    out = s.deserialize(data, meta)
    assert out == {"t": (1, (2,)), "nt": (1, 2), "e": 1, "od": {"b": (2, 3), "c": [4], "a": 1}, "f": 0.5}
    assert type(out["t"]) is tuple and list(out["od"]) == ["b", "c", "a"]
