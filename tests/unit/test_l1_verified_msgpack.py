"""A non-encrypted backed cache's L1 hit swaps its entry for the verified MessagePack, where that is no longer.

The first L1 hit on an entry decodes the envelope as before, and its retrieve() yields the MessagePack;
L1 then holds that in place of the envelope (``CacheSerializationHandler.deserialize_l1``,
``L1Cache.shrink_if_held``), and later hits decode it without retrieve(). Each rule that keeps this from costing
L1 hits or security is pinned here:

- L1 admits and stores envelopes exactly as before; a hit only ever shrinks an entry in place. So L1 never
  holds fewer entries than it would hold envelopes (the replay property below).
- MessagePack enters L1 only from a retrieve() that verified it; a corrupt envelope is never swapped.
- Encrypted handlers keep their ciphertext envelope in L1, and every hit on it decrypts (protocol PRE-10,
  PRE-11: the secure preset holds only ciphertext in L1).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from typing import Any

import msgpack
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cachekit import cache
from cachekit.cache_handler import CacheSerializationHandler
from cachekit.config.nested import L1CacheConfig
from cachekit.l1_cache import L1Cache, get_l1_cache
from cachekit.serializers.base import SerializationError
from cachekit.serializers.encryption_wrapper import EncryptionWrapper
from cachekit.serializers.standard_serializer import VerifiedMsgpack

pytestmark = pytest.mark.unit

MASTER_KEY = "a" * 64


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
    backend, namespace = _Backend(), _namespace()

    @cache(backend=backend, ttl=60, namespace=namespace)
    def compute(x: int) -> str:
        return "ab" * 600

    for _ in range(3):
        assert compute(1) == "ab" * 600
    envelope, entry = _stored(backend, namespace)
    assert len(msgpack.packb("ab" * 600)) > len(envelope)
    assert type(entry) is bytes and entry == envelope
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


@pytest.mark.parametrize("damage", [_corrupt, lambda e: e[:-30]], ids=["flipped-byte", "truncated"])
def test_deserialize_l1_refuses_an_envelope_retrieve_refuses(damage: Callable[[bytes], bytes]) -> None:
    handler = CacheSerializationHandler()
    envelope = damage(handler.serialize_data({"x": 1}, cache_key="k"))
    with pytest.raises(SerializationError):
        handler.deserialize_l1(envelope, cache_key="k")


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
    else:
        assert type(entry) is VerifiedMsgpack and len(entry) <= len(envelope)
        assert handler.deserialize_l1(entry, cache_key="k") == (expected, None)


@pytest.mark.parametrize(
    "handler",
    [
        CacheSerializationHandler(serializer_name="auto"),
        CacheSerializationHandler(enable_integrity_checking=False),
        CacheSerializationHandler(encryption=True, master_key=MASTER_KEY, single_tenant_mode=True),
    ],
    ids=["auto", "integrity-off", "encrypted"],
)
def test_other_handlers_keep_their_envelope_and_refuse_verified_msgpack(handler: CacheSerializationHandler) -> None:
    """The L1 hit sites send these handlers' entries to deserialize_data, which refuses a swapped entry another
    handler left under the same key as a corrupt frame."""
    envelope = handler.serialize_data({"x": 1}, cache_key="k")
    assert not handler.keeps_msgpack_in_l1
    assert handler.deserialize_l1(envelope, cache_key="k") == ({"x": 1}, None)
    planted = VerifiedMsgpack(msgpack.packb({"x": 2}))
    for decode in (handler.deserialize_l1, handler.deserialize_data):
        with pytest.raises(SerializationError, match="Corrupt cache envelope"):
            decode(planted, cache_key="k")  # type: ignore[arg-type]


def _frames() -> dict[str, bytes]:
    """Entries a plaintext default-serializer handler must decode exactly as deserialize_data does."""
    k = "k"
    own = CacheSerializationHandler(encryption=False).serialize_data({"x": 1}, cache_key=k)
    return {
        "own-envelope": own,
        "other-serializer": CacheSerializationHandler(serializer_name="auto").serialize_data({"x": 1}, cache_key=k),
        "encrypted-drift": CacheSerializationHandler(
            encryption=True, master_key=MASTER_KEY, single_tenant_mode=True
        ).serialize_data({"x": 1}, cache_key=k),
        "integrity-off": CacheSerializationHandler(enable_integrity_checking=False).serialize_data({"x": 1}, cache_key=k),
        "corrupt-header": b"\x00" + own[1:],
        "corrupt-payload": _corrupt(own),
        "not-a-frame": msgpack.packb({"x": 1}),
    }


@pytest.mark.parametrize("name", list(_frames()))
def test_deserialize_l1_decodes_or_refuses_every_entry_as_deserialize_data_does(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """deserialize_l1's fast path repeats deserialize_data's frame checks, so the two must agree on every entry."""
    data = _frames()[name]
    monkeypatch.setenv("CACHEKIT_MASTER_KEY", MASTER_KEY)  # lets the encrypted-drift entry decrypt, as it would
    handler = CacheSerializationHandler(encryption=False)

    def outcome(decode: Callable[[], Any]) -> Any:
        try:
            return decode()
        except Exception as e:  # noqa: BLE001 - the outcome under comparison
            return type(e)

    expected = outcome(lambda: handler.deserialize_data(data, cache_key="k"))
    assert outcome(lambda: handler.deserialize_l1(data, cache_key="k")[0]) == expected


# ── L1Cache.shrink_if_held ───────────────────────────────────────────────────────────────────────────


def test_replace_shrinks_the_entry_in_place() -> None:
    l1 = L1Cache(max_memory_mb=1, namespace=_namespace())
    old = b"e" * 100
    l1.put("a", old, redis_ttl=60)
    l1.put("b", b"b" * 10, redis_ttl=60)
    expires_at = l1._state.cache["a"].expires_at

    new = VerifiedMsgpack(b"m" * 40)
    assert l1.shrink_if_held("a", old, new)

    assert l1.peek("a") is new
    assert l1._state.memory_bytes == 50
    assert l1._state.cache["a"].expires_at == expires_at
    assert list(l1._state.cache) == ["a", "b"]  # LRU place kept


@pytest.mark.parametrize(
    "change", ["removed", "stored-again", "longer"], ids=["removed-since-read", "stored-again-since-read", "longer"]
)
def test_replace_leaves_an_entry_it_did_not_read_or_would_grow(change: str) -> None:
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


def test_an_encrypted_reader_refuses_verified_msgpack_in_its_l1() -> None:
    """A plaintext wrapper sharing the namespace and key cannot make an encrypted one serve plaintext."""
    backend, namespace = _Backend(), _namespace()

    @_SECURE["secure"](namespace, backend)
    def compute(x: int) -> dict[str, int]:
        return {"x": x}

    compute(1)
    ((key, _),) = backend.store.items()
    l1 = get_l1_cache(namespace)
    assert l1.shrink_if_held(key, l1.peek(key), VerifiedMsgpack(msgpack.packb({"x": "planted"})))  # type: ignore[arg-type]
    assert type(l1.peek(key)) is VerifiedMsgpack
    assert compute(1) == {"x": 1}  # read from L2 instead
    assert type(l1.peek(key)) is bytes


# ── L1 hits: never fewer than with envelopes ──────────────────────────────────────────────────
#
# A replay over the real L1Cache. A read that misses puts the entry a put rule picks from the entry's
# envelope and MessagePack sizes; with promote, a read that hits an envelope swaps it for its MessagePack
# when that is no longer, as the L1 hit sites do (deserialize_l1, then L1Cache.shrink_if_held, which alone
# refuses a longer value here). The shipped design
# is the envelope rule with promote. The two rejected put rules are the counterexamples an adversarial
# review found against storing MessagePack at write time; they show the pinned traces bite.

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
