"""A non-encrypted backed cache's L1 hit swaps its envelope for the verified MessagePack (``VerifiedMsgpack``) when
that is no longer; otherwise L1 keeps the envelope.

The first L1 hit on an entry decodes the envelope as before, and its retrieve() yields the MessagePack; when that is
no longer than the envelope, L1 then holds it in place of the envelope (``CacheSerializationHandler.deserialize_l1``,
``L1Cache.shrink_if_held``), and later hits skip retrieve(). An envelope L1 keeps is decoded in full on every hit.
No hit skips the structure bounds: every StandardSerializer decode is one Rust pass (``unpackb_standard``) that
enforces them as it decodes, kept entry or not. Each rule that keeps this from costing L1 hits or security is
pinned here:

- L1 admits and stores envelopes exactly as before; a hit only ever shrinks an entry in place. So L1 never
  holds fewer entries than it would hold envelopes (the replay property below).
- MessagePack enters L1 only from a retrieve() that verified it; a corrupt envelope is never swapped.
- Every decode, of bytes from L1 or L2 and of a kept entry alike, goes through the bounded decode, so a nesting
  bomb is refused wherever it comes from, and nothing refused is ever kept.
- Every decode equals the decode every read ran before the Rust one: the structure walk, then msgpack with
  the object hook (the equality fuzz below), datetimes and look-alike marker maps at any depth included.
- Encrypted handlers keep their ciphertext envelope in L1, and every hit on it decrypts and decodes it in full
  (protocol PRE-10, PRE-11: the secure preset holds only ciphertext in L1, and nothing derived from the
  plaintext sits beside it).
"""

from __future__ import annotations

import asyncio
import os
import random
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, timedelta, timezone
from typing import Any

import msgpack
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cachekit import cache
from cachekit._rust_serializer import ByteStorage
from cachekit.cache_handler import CacheSerializationHandler
from cachekit.config import reset_settings
from cachekit.config.nested import L1CacheConfig
from cachekit.l1_cache import L1Cache, get_l1_cache
from cachekit.serializers import base
from cachekit.serializers.base import SerializationError, SuspiciousCacheEntryError, unpackb_bounded
from cachekit.serializers.encryption_wrapper import EncryptionWrapper
from cachekit.serializers.standard_serializer import VerifiedMsgpack, _standard_object_hook
from cachekit.serializers.wrapper import SerializationWrapper
from tests.fixtures.zero_config import clear_cachekit_env

pytestmark = pytest.mark.unit

MASTER_KEY = "a" * 64
GUARD_ERROR_PREFIX = "Unpack failed: MessagePack document "  # only the structure bounds raise this


@pytest.fixture(autouse=True)
def _no_cachekit_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Handlers here read key settings when built, so no CACHEKIT_* variable in the shell may reach them."""
    clear_cachekit_env(monkeypatch)
    yield


class _Backend:
    """In-memory byte store that counts its reads."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.gets = 0

    def get(self, key: str) -> bytes | None:
        self.gets += 1
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        self.store[key] = bytes(value)

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {}


def _namespace() -> str:
    return f"l1vm-{uuid.uuid4().hex}"  # backed-mode L1 is shared by namespace across decorations


def _stored(backend: _Backend, namespace: str) -> tuple[bytes, bytes | None]:
    """The one L2 envelope and the L1 entry under its key."""
    ((key, envelope),) = backend.store.items()
    return envelope, get_l1_cache(namespace).peek(key)


def _corrupt(envelope: bytes) -> bytes:
    """``envelope`` with one byte of its ByteStorage payload flipped: the frame still parses, retrieve() fails."""
    corrupt = bytearray(envelope)
    corrupt[-20] ^= 0xFF
    return bytes(corrupt)


# ── what L1 holds ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value", ["hello", 42, {"id": 7, "tags": ["a"]}, os.urandom(5000)], ids=["str", "int", "dict", "random"]
)
def test_the_first_hit_swaps_an_envelope_for_its_msgpack_when_no_longer(value: Any) -> None:
    backend, namespace, runs = _Backend(), _namespace(), []

    @cache(backend=backend, ttl=60, namespace=namespace)
    def compute(x: int) -> Any:
        runs.append(x)
        return value

    assert compute(1) == value  # miss: L1 gets the envelope L2 got, as before
    envelope, entry = _stored(backend, namespace)
    assert type(entry) is bytes and entry == envelope

    before = compute.cache_info().l1_hits  # type: ignore[attr-defined]  # stats are per function name
    assert compute(1) == value  # first hit: decodes the envelope, keeps its MessagePack
    _, entry = _stored(backend, namespace)
    assert type(entry) is VerifiedMsgpack
    assert entry.data == msgpack.packb(value, use_bin_type=True)
    assert len(entry) <= len(envelope)

    assert compute(1) == value  # decodes the MessagePack
    assert (runs, backend.gets) == ([1], 1)
    assert compute.cache_info().l1_hits == before + 2  # type: ignore[attr-defined]


def test_a_value_longer_as_msgpack_keeps_its_envelope() -> None:
    """Swapping in the longer MessagePack would grow the entry, so L1 keeps the envelope bytes it admitted."""
    backend, namespace = _Backend(), _namespace()

    @cache(backend=backend, ttl=60, namespace=namespace)
    def compute(x: int) -> str:
        return "ab" * 600

    assert compute(1) == "ab" * 600
    envelope, entry = _stored(backend, namespace)
    held = get_l1_cache(namespace)._state.memory_bytes
    for _ in range(2):
        assert compute(1) == "ab" * 600
    _, entry = _stored(backend, namespace)
    assert len(msgpack.packb("ab" * 600)) > len(envelope)
    assert type(entry) is bytes and entry == envelope
    assert get_l1_cache(namespace)._state.memory_bytes == held
    assert backend.gets == 1


def test_an_envelope_over_the_admission_cap_stays_out_though_its_msgpack_would_fit() -> None:
    """Admission is judged on the envelope, as before. Judged on the MessagePack, this entry would get in and
    evict others."""
    backend, namespace = _Backend(), _namespace()
    value = os.urandom(131_067)  # MessagePack 131,072 B, the cap at 1 MiB; the envelope is longer

    @cache(backend=backend, ttl=60, namespace=namespace, l1=L1CacheConfig(max_size_mb=1))
    def compute(x: int) -> bytes:
        return value

    for _ in range(3):
        assert compute(1) == value
    envelope, entry = _stored(backend, namespace)
    assert len(envelope) > 131_072 >= len(msgpack.packb(value))
    assert entry is None
    assert backend.gets == 3  # every call reads L2


def test_an_l2_hit_backfills_the_envelope_and_the_next_hit_swaps_it() -> None:
    backend, namespace = _Backend(), _namespace()

    @cache(backend=backend, ttl=60, namespace=namespace)
    def compute(x: int) -> dict[str, int]:
        return {"x": x}

    assert compute(1) == {"x": 1}
    get_l1_cache(namespace).clear()
    assert compute(1) == {"x": 1}  # L2 hit
    envelope, entry = _stored(backend, namespace)
    assert type(entry) is bytes and entry == envelope
    assert compute(1) == {"x": 1}
    _, entry = _stored(backend, namespace)
    assert type(entry) is VerifiedMsgpack
    assert backend.gets == 2


def test_a_corrupt_envelope_backfilled_into_l1_is_never_swapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """MessagePack enters L1 only from a retrieve() that verified it. The L2 decode is stubbed to accept the
    corrupt envelope, so the backfill puts it in L1; the next hit's retrieve() refuses it, and L1 drops it."""
    backend, namespace = _Backend(), _namespace()

    @cache(backend=backend, ttl=60, namespace=namespace)
    def compute(x: int) -> dict[str, int]:
        return {"x": x}

    compute(1)
    ((key, envelope),) = backend.store.items()
    backend.store[key] = corrupt = _corrupt(envelope)
    l1 = get_l1_cache(namespace)
    l1.clear()
    real = CacheSerializationHandler.deserialize_data
    monkeypatch.setattr(
        CacheSerializationHandler,
        "deserialize_data",
        lambda self, data, *a, **kw: {"x": 1} if bytes(data) == corrupt else real(self, data, *a, **kw),
    )

    assert compute(1) == {"x": 1}  # L2 hit on the corrupt envelope: backfilled as it is
    assert l1.peek(key) == corrupt
    assert compute(1) == {"x": 1}  # the L1 hit refuses it; L2 serves and backfills it again
    entry = l1.peek(key)
    assert type(entry) is bytes and entry == corrupt


_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers(-(2**63), 2**64 - 1) | st.floats(allow_nan=False) | st.text() | st.binary(),
    lambda inner: st.lists(inner, max_size=8) | st.dictionaries(st.text(max_size=8), inner, max_size=8),
    max_leaves=40,
)


@settings(max_examples=300, deadline=None)
@given(value=_VALUES | st.binary(min_size=1000, max_size=20_000) | st.text(min_size=1000, max_size=20_000))
def test_deserialize_l1_keeps_msgpack_only_when_no_longer_and_it_decodes_the_same(value: Any) -> None:
    handler = CacheSerializationHandler()
    envelope = handler.serialize_data(value, cache_key="k")
    decoded, entry = handler.deserialize_l1(envelope, cache_key="k")
    expected = handler.deserialize_data(envelope, cache_key="k")
    assert decoded == expected
    if len(msgpack.packb(value, use_bin_type=True)) > len(envelope):
        assert entry is None
        assert handler.deserialize_l1(envelope, cache_key="k") == (expected, None)  # every hit on it: the same again
    else:
        assert type(entry) is VerifiedMsgpack and len(entry) <= len(envelope)
        assert handler.deserialize_l1(entry, cache_key="k") == (expected, None)


def test_msgpack_exactly_as_long_as_the_envelope_is_kept_and_one_byte_longer_is_not() -> None:
    """Random bytes followed by k zero bytes: each zero lengthens the MessagePack by one byte, and LZ4 folds the run,
    so the MessagePack overtakes the envelope, passing through both lengths."""
    handler, head = CacheSerializationHandler(), random.Random(0).randbytes(300)
    for extra, kept in ((0, VerifiedMsgpack), (1, type(None))):
        for k in range(2000):
            value = head + bytes(k)
            envelope = handler.serialize_data(value, cache_key="k")
            if len(msgpack.packb(value, use_bin_type=True)) == len(envelope) + extra:
                break
        else:
            pytest.fail(f"no k makes the MessagePack {extra} byte(s) longer than the envelope")
        assert type(handler.deserialize_l1(envelope, cache_key="k")[1]) is kept


_CORRUPT = (SerializationError, "Corrupt cache envelope")
_OTHER_HANDLERS: dict[str, tuple[Callable[[], CacheSerializationHandler], tuple[type[Exception], str]]] = {
    "auto": (lambda: CacheSerializationHandler(serializer_name="auto"), _CORRUPT),
    "integrity-off": (lambda: CacheSerializationHandler(enable_integrity_checking=False), _CORRUPT),
    "encrypted": (
        lambda: CacheSerializationHandler(encryption=True, master_key=MASTER_KEY, single_tenant_mode=True),
        (SuspiciousCacheEntryError, "another handler's plaintext entry"),
    ),
}


def _planted() -> VerifiedMsgpack:
    """The kept entry a plaintext default-serializer handler leaves in L1."""
    return VerifiedMsgpack(msgpack.packb({"x": 2}))


@pytest.mark.parametrize(("make", "refusal"), list(_OTHER_HANDLERS.values()), ids=list(_OTHER_HANDLERS))
def test_other_handlers_keep_their_envelope_and_refuse_a_kept_entry(
    make: Callable[[], CacheSerializationHandler], refusal: tuple[type[Exception], str]
) -> None:
    """The L1 hit sites send these handlers' entries to deserialize_data, which refuses a swapped entry another
    handler left under the same key: under encryption as the plaintext envelope it replaced would be."""
    handler = make()
    envelope = handler.serialize_data({"x": 1}, cache_key="k")
    assert not handler.keeps_msgpack_in_l1
    assert handler.deserialize_l1(envelope, cache_key="k") == ({"x": 1}, None)
    error, message = refusal
    for decode in (handler.deserialize_l1, handler.deserialize_data):
        with pytest.raises(error, match=message):
            decode(_planted(), cache_key="k")  # type: ignore[arg-type]


def _frames() -> dict[str, bytes]:
    """Entries a plaintext default-serializer handler must decode exactly as deserialize_data does. Only the
    first is its own plaintext envelope, the one entry L1 may swap."""
    k = "k"
    own = CacheSerializationHandler(encryption=False).serialize_data({"x": 1}, cache_key=k)
    payload, metadata, name = SerializationWrapper.unwrap(own)
    return {
        "own-envelope": own,
        "header-not-compressed": SerializationWrapper.wrap(bytes(payload), metadata | {"compressed": False}, name),
        "other-serializer": CacheSerializationHandler(serializer_name="auto").serialize_data({"x": 1}, cache_key=k),
        "encrypted-drift": CacheSerializationHandler(
            encryption=True, master_key=MASTER_KEY, single_tenant_mode=True
        ).serialize_data({"x": 1}, cache_key=k),
        "integrity-off": CacheSerializationHandler(enable_integrity_checking=False).serialize_data({"x": 1}, cache_key=k),
        "corrupt-header": b"\x00" + own[1:],
        "corrupt-payload": _corrupt(own),
        "truncated": own[:-30],
        "not-a-frame": msgpack.packb({"x": 1}),
    }


_FRAME_NAMES = (
    "own-envelope",
    "header-not-compressed",
    "other-serializer",
    "encrypted-drift",
    "integrity-off",
    "corrupt-header",
    "corrupt-payload",
    "truncated",
    "not-a-frame",
)


def test_frame_names_list_every_frame() -> None:
    """_FRAME_NAMES is spelled out because _frames() builds handlers, which must wait for the env fixture."""
    assert set(_frames()) == set(_FRAME_NAMES)


@pytest.mark.parametrize("name", _FRAME_NAMES)
def test_deserialize_l1_decodes_or_refuses_every_entry_as_deserialize_data_does(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """deserialize_l1's fast path repeats deserialize_data's frame checks, so the two must agree on every entry,
    and only the handler's own plaintext envelope may come back with MessagePack to keep."""
    data = _frames()[name]
    monkeypatch.setenv("CACHEKIT_MASTER_KEY", MASTER_KEY)  # lets the encrypted-drift entry decrypt, as it would
    reset_settings()
    handler = CacheSerializationHandler(encryption=False)

    def outcome(decode: Callable[[], Any]) -> Any:
        try:
            return decode()
        except Exception as e:  # noqa: BLE001 - the outcome under comparison
            return type(e)

    expected = outcome(lambda: handler.deserialize_data(data, cache_key="k"))
    got = outcome(lambda: handler.deserialize_l1(data, cache_key="k"))
    assert (got[0] if isinstance(got, tuple) else got) == expected
    assert (isinstance(got, tuple) and got[1] is not None) == (name == "own-envelope")


# ── what a hit decodes and retrieves ─────────────────────────────────────────────────────────


@pytest.fixture
def retrieves(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """One entry per ByteStorage.retrieve() call, by any instance."""
    seen: list[int] = []
    real = ByteStorage.retrieve

    def recording(self: ByteStorage, data: Any) -> Any:
        seen.append(1)
        return real(self, data)

    monkeypatch.setattr(ByteStorage, "retrieve", recording)
    return seen


_PLAIN = {"id": 7, "tags": ["a"]}  # its MessagePack is no longer than its envelope: kept as VerifiedMsgpack
_COMPRESSIBLE = {"users": [{"name": f"user {i}", "theme": "dark"} for i in range(50)]}  # L1 keeps its envelope


def _call(fn: Callable[[int], Any], mode: str) -> Any:
    return asyncio.run(fn(1)) if mode == "async" else fn(1)


def _decorated(backend: _Backend, namespace: str, value: Any, mode: str) -> Callable[[int], Any]:
    if mode == "async":

        @cache(backend=backend, ttl=60, namespace=namespace)
        async def acompute(x: int) -> Any:
            return value

        return acompute

    @cache(backend=backend, ttl=60, namespace=namespace)
    def compute(x: int) -> Any:
        return value

    return compute


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize(
    ("value", "kept"),
    [(_PLAIN, VerifiedMsgpack), (_COMPRESSIBLE, bytes)],
    ids=["msgpack", "envelope"],
)
def test_every_hit_runs_the_bounded_decode_and_only_kept_msgpack_skips_retrieve(
    value: Any, kept: type, mode: str, decodes: list[str], retrieves: list[int]
) -> None:
    """Kept MessagePack saves a hit retrieve(), never the structure bounds: each hit's decode is one bounded pass,
    the first hit's and the kept entry's alike. An envelope L1 keeps is retrieved and decoded on every hit."""
    backend, namespace = _Backend(), _namespace()
    fn = _decorated(backend, namespace, value, mode)
    assert _call(fn, mode) == value  # miss: L1 holds the envelope
    assert _call(fn, mode) == value  # first hit: retrieves and decodes the envelope's MessagePack, keeps it if no longer
    assert (decodes, len(retrieves)) == (["ok"], 1)
    _, entry = _stored(backend, namespace)
    assert type(entry) is kept

    decodes.clear()
    retrieves.clear()
    for _ in range(2):
        assert _call(fn, mode) == value
    assert (decodes, len(retrieves), backend.gets) == (["ok", "ok"], 0 if kept is VerifiedMsgpack else 2, 1)


def test_a_nesting_bomb_put_in_l1_as_bytes_is_refused_by_the_bounded_decode(decodes: list[str]) -> None:
    """The type is the mark: an entry still held as bytes takes the full decode, whoever put it there. A nesting bomb
    put in L1 directly is refused by the bounds, and the call reads L2 instead."""
    backend, namespace = _Backend(), _namespace()

    @cache(backend=backend, ttl=60, namespace=namespace)
    def compute(x: int) -> dict[str, int]:
        return {"x": x}

    compute(1)
    ((key, envelope),) = backend.store.items()
    l1 = get_l1_cache(namespace)
    l1.put(key, _bomb_frame(envelope), redis_ttl=60)
    decodes.clear()
    assert compute(1) == {"x": 1}  # the L1 hit is refused by the bounds, then L2 is read and decoded
    assert decodes == ["refused", "ok"]
    assert backend.gets == 2


def _bomb_frame(own: bytes) -> bytes:
    """``own``'s frame around a valid ByteStorage envelope whose MessagePack nests one level past the ceiling."""
    _, metadata, name = SerializationWrapper.unwrap(own)
    bomb = b"\x91" * (base.MSGPACK_MAX_NESTING + 1) + b"\xc0"
    return SerializationWrapper.wrap(ByteStorage("msgpack").store(bomb, "msgpack"), metadata, name)


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_an_l2_read_of_a_nesting_bomb_is_refused_by_the_bounded_decode(mode: str, decodes: list[str]) -> None:
    backend, namespace, runs = _Backend(), _namespace(), []

    def compute(x: int) -> dict[str, int]:
        runs.append(x)
        return {"x": x}

    async def acompute(x: int) -> dict[str, int]:
        return compute(x)

    fn = cache(backend=backend, ttl=60, namespace=namespace)(acompute if mode == "async" else compute)
    _call(fn, mode)
    ((key, envelope),) = backend.store.items()
    backend.store[key] = _bomb_frame(envelope)
    get_l1_cache(namespace).clear()
    decodes.clear()

    assert _call(fn, mode) == {"x": 1}  # the L2 entry is refused by the bounds and recomputed
    assert decodes == ["refused"]
    assert runs == [1, 1]
    assert backend.store[key] == envelope


def test_a_kept_entry_is_never_built_from_an_entry_that_fails_its_bounds(decodes: list[str]) -> None:
    handler = CacheSerializationHandler()
    bomb = _bomb_frame(handler.serialize_data({"x": 1}, cache_key="k"))
    for decode in (handler.deserialize_l1, handler.deserialize_data):
        with pytest.raises(SerializationError, match=GUARD_ERROR_PREFIX):  # raises, so returns nothing to keep
            decode(bomb, cache_key="k")
    assert decodes == ["refused", "refused"]


# ── every decode equals the walked, hooked decode ─────────────────────────────────────────────

_MARKER_KEYS = ("__datetime__", "__date__", "__time__")
_ZONES = st.sampled_from([None, UTC, timezone(timedelta(hours=5, minutes=30)), timezone(-timedelta(hours=8))])
_TEMPORAL = st.datetimes(timezones=_ZONES) | st.dates() | st.times(timezones=_ZONES)
_ISO = _TEMPORAL.map(lambda t: t.isoformat())
_FLAGS = st.sampled_from([True, False, 1, 0, "yes", "", None])
# Maps a caller wrote that look like markers: two keys with a sentinel, truthy or not, and a
# value that parses, does not parse, or is missing; and maps of another size that carry a sentinel.
_MIMICS = (
    st.builds(lambda k, f, v: {k: f, "value": v}, st.sampled_from(_MARKER_KEYS), _FLAGS, _ISO | st.text(max_size=12) | st.none())
    | st.builds(
        lambda k, f, o: {k: f, o: 1},
        st.sampled_from(_MARKER_KEYS),
        _FLAGS,
        st.text(max_size=6).filter(lambda o: o not in _MARKER_KEYS),
    )
    | st.builds(lambda k, v: {k: True, "value": v, "extra": 1}, st.sampled_from(_MARKER_KEYS), _ISO)
    | st.builds(lambda k: {k: True}, st.sampled_from(_MARKER_KEYS))
)
_KEYS = st.text(max_size=8) | st.sampled_from((*_MARKER_KEYS, "value"))
_DATED_VALUES = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-(2**63), 2**64 - 1)
    | st.floats(allow_nan=False)
    | st.text()
    | st.binary()
    | _TEMPORAL
    | _MIMICS
    | st.sampled_from(_MARKER_KEYS),
    lambda inner: st.lists(inner, max_size=6) | st.dictionaries(_KEYS, inner, max_size=6),
    max_leaves=30,
)


def _outcome(decode: Callable[[], Any]) -> Any:
    """repr of what ``decode`` returns, which tells a datetime from the map it came from, or the type it raised."""
    try:
        return repr(decode())
    except Exception as e:  # noqa: BLE001 - the outcome under comparison
        return type(e)


def _verified(envelope: bytes) -> VerifiedMsgpack:
    """``envelope``'s MessagePack as retrieve() verifies it, held as L1 holds a kept entry, whatever its length."""
    return VerifiedMsgpack(ByteStorage("msgpack").retrieve(SerializationWrapper.unwrap(envelope)[0])[0])


def _walked_hooked(envelope: bytes) -> Any:
    """The outcome of the decode every StandardSerializer read ran before the Rust one: retrieve(), the structure
    walk, then msgpack with the object hook on every map. Any error is SerializationError, as the read paths raise it."""
    raw = _verified(envelope).data
    outcome = _outcome(lambda: unpackb_bounded(raw, raw=False, use_list=True, object_hook=_standard_object_hook))
    return outcome if isinstance(outcome, str) else SerializationError


@settings(max_examples=500, deadline=None)
@given(value=_DATED_VALUES)
def test_every_decode_of_a_kept_entry_equals_the_walked_hooked_decode(value: Any) -> None:
    """The L2 read, the first L1 hit and the kept MessagePack decode to exactly the walked, hooked decode's outcome,
    datetimes and look-alike maps at any depth included, though the Rust decode calls the hook only on a map with
    a marker key."""
    handler = CacheSerializationHandler()
    envelope = handler.serialize_data(value, cache_key="k")
    expected = _walked_hooked(envelope)
    assert _outcome(lambda: handler.deserialize_data(envelope, cache_key="k")) == expected
    try:
        first, kept = handler.deserialize_l1(envelope, cache_key="k")
    except SerializationError:
        assert expected is SerializationError  # refused as the reference refuses it, and nothing is kept
        return
    assert repr(first) == expected
    verified = _verified(envelope)
    assert kept is None or kept.data == verified.data  # what L1 would hold is what retrieve() verified
    assert _outcome(lambda: handler.deserialize_l1(verified, cache_key="k")[0]) == expected


@pytest.mark.parametrize(
    "value",
    [
        [{"a": i} for i in range(5)] + [{"deep": [{"__datetime__": True, "value": "2024-01-15T12:30:00"}]}],
        {"__date__": 1, "value": "2024-01-15"},
        {"__time__": "yes", "value": "12:30:00+05:30"},
        {"x": {"__datetime__": False, "value": "2024-01-15"}},
        {"note": "__time__ appears here only as text"},
    ],
    ids=["datetime-look-alike-deep", "date-look-alike", "time-look-alike", "falsy-sentinel", "text-only"],
)
def test_look_alike_maps_decode_as_the_walked_hooked_decode(value: Any) -> None:
    """Pinned cases of the fuzz above: a caller's map that looks like a marker, at any depth, comes back from every
    decode as the walked, hooked decode returns it, and text that merely mentions a marker key is text."""
    handler = CacheSerializationHandler()
    envelope = handler.serialize_data(value, cache_key="k")
    expected = _walked_hooked(envelope)
    first, _ = handler.deserialize_l1(envelope, cache_key="k")
    assert repr(handler.deserialize_data(envelope, cache_key="k")) == expected
    assert repr(first) == repr(handler.deserialize_l1(_verified(envelope), cache_key="k")[0]) == expected


@pytest.mark.parametrize("sentinel", _MARKER_KEYS)
def test_a_broken_look_alike_raises_on_every_decode_and_is_never_kept(sentinel: str) -> None:
    handler = CacheSerializationHandler()
    envelope = handler.serialize_data([{sentinel: True, "other": 1}], cache_key="k")
    for decode in (handler.deserialize_data, handler.deserialize_l1):
        with pytest.raises(SerializationError, match="missing 'value' field"):
            decode(envelope, cache_key="k")


# ── L1Cache.shrink_if_held ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("size", [40, 100], ids=["shorter", "as-long"])
def test_shrink_if_held_swaps_the_entry_in_place(size: int) -> None:
    l1 = L1Cache(max_memory_mb=1, namespace=_namespace())
    old = b"e" * 100
    l1.put("a", old, redis_ttl=60)
    l1.put("b", b"b" * 10, redis_ttl=60)
    expires_at = l1._state.cache["a"].expires_at

    new = VerifiedMsgpack(b"m" * size)
    assert l1.shrink_if_held("a", old, new)

    assert l1.peek("a") is new
    assert l1._state.memory_bytes == size + 10
    assert l1._state.cache["a"].expires_at == expires_at
    assert list(l1._state.cache) == ["a", "b"]  # LRU place kept


@pytest.mark.parametrize(
    "change", ["removed", "stored-again", "longer"], ids=["removed-since-read", "stored-again-since-read", "longer"]
)
def test_shrink_if_held_leaves_an_entry_it_did_not_read_or_would_grow(change: str) -> None:
    l1 = L1Cache(max_memory_mb=1, namespace=_namespace())
    old = b"e" * 100
    l1.put("a", old, redis_ttl=60)
    new = VerifiedMsgpack(b"m" * 40)
    if change == "removed":
        l1.invalidate("a")
    elif change == "stored-again":
        l1.put("a", bytes(bytearray(old)), redis_ttl=60)  # equal bytes, another object: a newer write
    else:
        new = VerifiedMsgpack(b"m" * 101)
    held = l1.peek("a")

    assert not l1.shrink_if_held("a", old, new)

    assert l1.peek("a") is held
    assert l1._state.memory_bytes == (0 if held is None else 100)


# ── encrypted handlers: ciphertext in L1, every hit decrypts ──────────────────────────────────


_SECURE = {
    "secure": lambda ns, b: cache.secure(master_key=MASTER_KEY, backend=b, ttl=60, namespace=ns),
    "secure-default-serializer": lambda ns, b: cache.secure(
        master_key=MASTER_KEY, backend=b, ttl=60, namespace=ns, serializer="default"
    ),
    "encryption-true": lambda ns, b: cache(
        backend=b, ttl=60, namespace=ns, encryption=True, single_tenant_mode=True, master_key=MASTER_KEY
    ),
}


@pytest.mark.parametrize("decorate", list(_SECURE.values()), ids=list(_SECURE))
def test_encrypted_l1_holds_the_l2_ciphertext_and_every_hit_decrypts(
    decorate: Callable[[str, _Backend], Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    decrypts: list[int] = []
    real = EncryptionWrapper.deserialize
    monkeypatch.setattr(EncryptionWrapper, "deserialize", lambda self, *a, **kw: decrypts.append(1) or real(self, *a, **kw))
    backend, namespace = _Backend(), _namespace()

    @decorate(namespace, backend)
    def compute(x: int) -> dict[str, int]:
        return {"x": x}

    assert compute(1) == {"x": 1}  # miss: L1 gets the very envelope L2 got
    for hits in (1, 2):
        assert compute(1) == {"x": 1}  # L1 hit
        envelope, entry = _stored(backend, namespace)
        assert type(entry) is bytes and entry == envelope
        assert (len(decrypts), backend.gets) == (hits, 1)

    get_l1_cache(namespace).clear()
    assert compute(1) == {"x": 1}  # L2 hit: the backfill is the ciphertext too
    assert compute(1) == {"x": 1}
    envelope, entry = _stored(backend, namespace)
    assert type(entry) is bytes and entry == envelope
    assert (len(decrypts), backend.gets) == (4, 2)


@pytest.mark.parametrize("decorate", list(_SECURE.values()), ids=list(_SECURE))
def test_every_encrypted_l1_hit_decodes_its_plaintext_through_the_bounded_decode(
    decorate: Callable[[str, _Backend], Any], decodes: list[str]
) -> None:
    """Decided for encrypted handlers: nothing is kept. Every hit decrypts the ciphertext L1 holds and decodes the
    plaintext in full, and no entry or flag derived from the plaintext sits beside the ciphertext in L1, which
    PRE-10 and PRE-11 keep to ciphertext alone."""
    backend, namespace = _Backend(), _namespace()

    @decorate(namespace, backend)
    def compute(x: int) -> dict[str, int]:
        return {"x": x}

    compute(1)
    decodes.clear()
    for _ in range(3):
        assert compute(1) == {"x": 1}
    assert decodes == ["ok"] * 3
    envelope, entry = _stored(backend, namespace)
    assert type(entry) is bytes and entry == envelope


def test_an_encrypted_reader_refuses_a_kept_entry_in_its_l1() -> None:
    """A plaintext wrapper sharing the namespace and key cannot make an encrypted one serve plaintext."""
    backend, namespace = _Backend(), _namespace()

    @_SECURE["secure"](namespace, backend)
    def compute(x: int) -> dict[str, int]:
        return {"x": x}

    compute(1)
    ((key, _),) = backend.store.items()
    l1 = get_l1_cache(namespace)
    planted = _planted()
    held = l1.peek(key)
    assert held is not None and len(planted) <= len(held)
    assert l1.shrink_if_held(key, held, planted)
    assert type(l1.peek(key)) is VerifiedMsgpack
    assert compute(1) == {"x": 1}  # read from L2 instead
    assert type(l1.peek(key)) is bytes


# ── L1 hits: never fewer than with envelopes ──────────────────────────────────────────────────
#
# A replay over the real L1Cache. A read that misses puts the entry a put rule picks from the entry's
# envelope and MessagePack sizes; with promote, a read that hits an envelope swaps it for its MessagePack
# when that is no longer, as the L1 hit sites do (deserialize_l1, then L1Cache.shrink_if_held, which alone
# refuses a longer value here).
#
# The shipped design is the envelope rule with promote. The two rejected put rules are counterexamples
# against storing MessagePack at write time; they show the pinned traces bite.

Rule = Callable[[L1Cache, int, int], int]


def _envelope_rule(l1: L1Cache, envelope: int, raw: int) -> int:
    return envelope


def _growth_allowance_rule(l1: L1Cache, envelope: int, raw: int) -> int:
    return raw if raw <= envelope * 1.1 and envelope <= l1.max_memory_bytes / 8 else envelope


def _admit_on_raw_rule(l1: L1Cache, envelope: int, raw: int) -> int:
    return raw if raw <= envelope else envelope


class _Sized(bytes):
    """A replayed L1 value: L1Cache accounts len(), and the replay never reads the bytes, so none are allocated."""

    def __new__(cls, size: int) -> _Sized:
        value = super().__new__(cls)
        value.size = size  # type: ignore[attr-defined]
        return value

    def __len__(self) -> int:
        return self.size  # type: ignore[attr-defined]


class _SizedMsgpack(_Sized):
    """A replayed entry a hit swapped in."""


def _replay(trace: list[tuple[str, str, int, int]], budget_mb: int, rule: Rule, *, promote: bool = False) -> list[int]:
    """The indices of the trace's reads that hit L1.

    Ops: ("read", key, ...) reads and, on a miss, puts; ("write", key, ...) puts whether or not L1 holds the
    key (a revalidation, another writer); ("set", key, envelope, raw) is a new value at L2: L1 drops the key, and
    later puts use the new sizes. A key's first op fixes its sizes until a "set".
    """
    l1 = L1Cache(max_memory_mb=budget_mb, namespace=_namespace())
    sizes: dict[str, tuple[int, int]] = {}
    hits = []
    for i, (op, key, envelope, raw) in enumerate(trace):
        if op == "set":
            l1.invalidate(key)
            sizes[key] = (envelope, raw)
            continue
        envelope, raw = sizes.setdefault(key, (envelope, raw))
        if op == "read":
            found, held = l1.get(key)
            if found:
                hits.append(i)
                if promote and not isinstance(held, _SizedMsgpack):
                    l1.shrink_if_held(key, held, _SizedMsgpack(raw))  # type: ignore[arg-type]
                    assert l1._state.cache[key].size_bytes <= envelope  # a swap never grows an entry
                continue
        l1.put(key, _Sized(rule(l1, envelope, raw)))
    return hits


def _msgpack_sizes(e: int, cap: int) -> st.SearchStrategy[tuple[int, int]]:
    """An envelope of ``e`` bytes and its MessagePack: the same, a little shorter or longer, anywhere up to
    double, or either side of the admission cap."""
    return st.tuples(
        st.just(e),
        st.one_of(st.just(e), st.integers(max(1, e - 512), e + 512), st.integers(1, 2 * e), st.integers(cap - 256, cap + 256)),
    )


@st.composite
def _traces(draw: st.DrawFn, budget_mb: int) -> list[tuple[str, str, int, int]]:
    """50 to 200 ops over n keys that together about fill L1, two keys of any size, and two that straddle the
    admission cap: one whose envelope is refused but whose MessagePack would fit, and the reverse. The trace
    opens with a "set" fixing each key's sizes; later "set"s draw new ones."""
    budget = budget_mb * 1024 * 1024
    cap = budget // 8
    n = draw(st.integers(2, 12))
    fill = min(cap, budget // n)
    any_size = st.one_of(st.integers(cap - 256, cap + 256), st.integers(cap // 2, cap), st.integers(1, cap // 32)).flatmap(
        lambda e: _msgpack_sizes(e, cap)
    )
    sizes = {f"fill{i}": st.integers(max(1, fill - 512), fill).flatmap(lambda e: _msgpack_sizes(e, cap)) for i in range(n)}
    sizes |= {"any0": any_size, "any1": any_size}
    sizes |= {
        "refused": st.tuples(st.integers(cap + 1, cap + 512), st.integers(cap - 512, cap)),
        "admitted": st.tuples(st.integers(cap - 512, cap), st.integers(cap + 1, cap + 512)),
    }
    trace = [("set", key, *draw(strategy)) for key, strategy in sizes.items()]
    ops = st.sampled_from(["read", "read", "read", "write", "set"])
    for op, key in draw(st.lists(st.tuples(ops, st.sampled_from(sorted(sizes))), min_size=50, max_size=200)):
        trace.append((op, key, *draw(sizes[key])) if op == "set" else (op, key, 0, 0))
    return trace


@pytest.mark.parametrize("budget_mb", [1, 100], ids=["1MiB-minimum", "100MiB-default"])
@settings(max_examples=100, deadline=None)
@given(data=st.data())
def test_no_access_trace_gets_fewer_l1_hits_than_with_envelopes(budget_mb: int, data: st.DataObject) -> None:
    trace = data.draw(_traces(budget_mb))
    with_envelopes = _replay(trace, budget_mb, _envelope_rule)
    promoted = _replay(trace, budget_mb, _envelope_rule, promote=True)
    assert set(with_envelopes) <= set(promoted)  # every read that hit before still hits


def test_entries_growing_past_their_envelope_keep_it_so_a_near_full_lru_keeps_its_hits() -> None:
    """104 keys cycling through 100 MiB, each 1,000,000 B as an envelope and 1,040,000 B as MessagePack. The
    envelopes fit; with a 1.1x growth allowance the MessagePack would not, and LRU would miss every read."""
    keys = [f"k{i}" for i in range(104)]
    trace = [("read", k, 1_000_000, 1_040_000) for _ in range(3) for k in keys]
    hot = [i for i in range(len(trace)) if i >= len(keys)]

    assert _replay(trace, 100, _envelope_rule) == hot  # 208/208
    assert _replay(trace, 100, _envelope_rule, promote=True) == hot
    assert _replay(trace, 100, _growth_allowance_rule) == []  # 0/208


def test_an_entry_shrunk_under_the_admission_cap_stays_out_so_the_hot_set_keeps_its_hits() -> None:
    """Under 1 MiB (cap 131,072 B), an entry whose envelope is 131,080 B is refused. Admitted on its 131,000 B
    MessagePack, it would evict the least recent hot entry, and each hot read would then evict the next."""
    hot_keys = [f"h{i}" for i in range(10)]
    warm = [("read", k, 100_000, 100_000) for k in hot_keys]
    rounds = [op for _ in range(2) for op in [("read", "big", 131_080, 131_000), *warm]]
    trace = warm + rounds

    assert len(_replay(trace, 1, _envelope_rule)) == 20
    assert len(_replay(trace, 1, _envelope_rule, promote=True)) == 20
    assert _replay(trace, 1, _admit_on_raw_rule) == []  # 20 hits fell to 0
