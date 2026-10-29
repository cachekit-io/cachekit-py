"""A non-encrypted backed cache keeps verified MessagePack in L1 where it is no longer than the envelope.

An L1 hit on such an entry decodes without ``ByteStorage.retrieve()``. Three rules keep that from costing
L1 hits or security, and each is pinned here:

- Admission is judged on the envelope, as before; the shorter form only replaces an envelope L1 admits.
  So L1 never holds fewer entries than it would hold envelopes (the replay property below).
- MessagePack enters L1 only from a ``retrieve()`` that verified it; a corrupt envelope stays an envelope.
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
from cachekit.cache_handler import CacheSerializationHandler, VerifiedMsgpack
from cachekit.config.nested import L1CacheConfig
from cachekit.l1_cache import L1Cache, get_l1_cache
from cachekit.serializers.base import SerializationError
from cachekit.serializers.encryption_wrapper import EncryptionWrapper

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


# ── what _put_l1 stores ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value", ["hello", 42, {"id": 7, "tags": ["a"]}, os.urandom(5000)], ids=["str", "int", "dict", "random"]
)
def test_a_value_no_longer_than_its_envelope_is_kept_as_verified_msgpack(value: Any) -> None:
    backend, namespace, runs = _Backend(), _namespace(), []

    @cache(backend=backend, ttl=60, namespace=namespace)
    def compute(x: int) -> Any:
        runs.append(x)
        return value

    assert compute(1) == value
    envelope, entry = _stored(backend, namespace)
    assert type(entry) is VerifiedMsgpack
    assert entry == msgpack.packb(value, use_bin_type=True)
    assert len(entry) <= len(envelope)

    before = compute.cache_info().l1_hits  # type: ignore[attr-defined]  # stats are per function name
    assert compute(1) == value  # the L1 hit decodes it
    assert (runs, backend.gets) == ([1], 1)
    assert compute.cache_info().l1_hits == before + 1  # type: ignore[attr-defined]


def test_a_value_longer_than_its_envelope_keeps_the_envelope() -> None:
    backend, namespace = _Backend(), _namespace()

    @cache(backend=backend, ttl=60, namespace=namespace)
    def compute(x: int) -> str:
        return "ab" * 600

    assert compute(1) == "ab" * 600
    envelope, entry = _stored(backend, namespace)
    assert len(msgpack.packb("ab" * 600)) > len(envelope)
    assert type(entry) is bytes and entry == envelope
    assert compute(1) == "ab" * 600
    assert backend.gets == 1


def test_an_envelope_over_the_admission_cap_is_refused_even_when_its_msgpack_would_fit() -> None:
    """Admission is judged on the envelope. Judged on the MessagePack, this entry would get in and evict others."""
    backend, namespace = _Backend(), _namespace()
    value = os.urandom(131_067)  # MessagePack 131,072 B, the cap at 1 MiB; the envelope is longer

    @cache(backend=backend, ttl=60, namespace=namespace, l1=L1CacheConfig(max_size_mb=1))
    def compute(x: int) -> bytes:
        return value

    assert compute(1) == value
    envelope, entry = _stored(backend, namespace)
    l1 = get_l1_cache(namespace)
    assert not l1.admits(len(envelope))
    assert l1.admits(len(CacheSerializationHandler().l1_entry(envelope)))  # the shorter form alone would get in
    assert entry is None


def test_an_l2_hit_backfills_verified_msgpack() -> None:
    backend, namespace = _Backend(), _namespace()

    @cache(backend=backend, ttl=60, namespace=namespace)
    def compute(x: int) -> dict[str, int]:
        return {"x": x}

    assert compute(1) == {"x": 1}
    get_l1_cache(namespace).clear()
    assert compute(1) == {"x": 1}  # L2 hit, backfilled
    _, entry = _stored(backend, namespace)
    assert type(entry) is VerifiedMsgpack
    assert compute(1) == {"x": 1}
    assert backend.gets == 2


def test_a_backfill_from_a_corrupt_envelope_writes_no_msgpack(monkeypatch: pytest.MonkeyPatch) -> None:
    """MessagePack enters L1 only from a retrieve() that verified it. The L2 decode is stubbed to succeed, as
    if it had not checked the bytes: the backfill still puts the envelope, which the next L1 read refuses."""
    backend, namespace = _Backend(), _namespace()

    @cache(backend=backend, ttl=60, namespace=namespace)
    def compute(x: int) -> dict[str, int]:
        return {"x": x}

    compute(1)
    ((key, envelope),) = backend.store.items()
    backend.store[key] = corrupt = _corrupt(envelope)
    get_l1_cache(namespace).clear()
    real = CacheSerializationHandler.deserialize_data
    monkeypatch.setattr(
        CacheSerializationHandler,
        "deserialize_data",
        lambda self, data, *a, **kw: {"x": 1} if bytes(data) == corrupt else real(self, data, *a, **kw),
    )

    assert compute(1) == {"x": 1}
    entry = get_l1_cache(namespace).peek(key)
    assert type(entry) is bytes and entry == corrupt


@pytest.mark.parametrize("damage", [_corrupt, lambda e: e[:-30]], ids=["flipped-byte", "truncated"])
def test_l1_entry_keeps_an_envelope_retrieve_refuses(damage: Callable[[bytes], bytes]) -> None:
    handler = CacheSerializationHandler()
    envelope = damage(handler.serialize_data({"x": 1}, cache_key="k"))
    with pytest.raises(SerializationError):
        handler.deserialize_data(envelope, cache_key="k")
    assert handler.l1_entry(envelope) is envelope


_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers(-(2**63), 2**64 - 1) | st.floats(allow_nan=False) | st.text() | st.binary(),
    lambda inner: st.lists(inner, max_size=8) | st.dictionaries(st.text(max_size=8), inner, max_size=8),
    max_leaves=40,
)


@settings(max_examples=300, deadline=None)
@given(value=_VALUES | st.binary(min_size=1000, max_size=20_000) | st.text(min_size=1000, max_size=20_000))
def test_l1_entry_is_never_longer_than_the_envelope_and_decodes_to_the_same_value(value: Any) -> None:
    handler = CacheSerializationHandler()
    envelope = handler.serialize_data(value, cache_key="k")
    entry = handler.l1_entry(envelope)
    assert len(entry) <= len(envelope)
    assert type(entry) is (VerifiedMsgpack if len(msgpack.packb(value, use_bin_type=True)) <= len(envelope) else bytes)
    assert handler.deserialize_data(entry, cache_key="k") == handler.deserialize_data(envelope, cache_key="k")


@pytest.mark.parametrize(
    "handler",
    [
        CacheSerializationHandler(serializer_name="auto"),
        CacheSerializationHandler(enable_integrity_checking=False),
        CacheSerializationHandler(encryption=True, master_key=MASTER_KEY, single_tenant_mode=True),
    ],
    ids=["auto", "integrity-off", "encrypted"],
)
def test_other_handlers_keep_the_envelope_and_refuse_verified_msgpack(handler: CacheSerializationHandler) -> None:
    envelope = handler.serialize_data({"x": 1}, cache_key="k")
    assert handler.l1_entry(envelope) is envelope
    with pytest.raises(SerializationError):  # SuspiciousCacheEntryError under encryption: a plaintext entry
        handler.deserialize_data(VerifiedMsgpack(msgpack.packb({"x": 2})), cache_key="k")


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
    envelope, entry = _stored(backend, namespace)
    assert type(entry) is bytes and entry == envelope
    assert compute(1) == {"x": 1}  # L1 hit
    assert (len(decrypts), backend.gets) == (1, 1)

    get_l1_cache(namespace).clear()
    assert compute(1) == {"x": 1}  # L2 hit: the backfill is the ciphertext too
    envelope, entry = _stored(backend, namespace)
    assert type(entry) is bytes and entry == envelope
    assert compute(1) == {"x": 1}
    assert (len(decrypts), backend.gets) == (3, 2)


def test_an_encrypted_reader_refuses_verified_msgpack_in_its_l1() -> None:
    """A plaintext wrapper sharing the namespace and key cannot make an encrypted one serve plaintext."""
    backend, namespace = _Backend(), _namespace()

    @_SECURE["secure"](namespace, backend)
    def compute(x: int) -> dict[str, int]:
        return {"x": x}

    compute(1)
    ((key, _),) = backend.store.items()
    get_l1_cache(namespace).put(key, VerifiedMsgpack(msgpack.packb({"x": "planted"})))
    assert compute(1) == {"x": 1}  # read from L2 instead
    assert type(get_l1_cache(namespace).peek(key)) is bytes


# ── L1 hits: never fewer than with envelopes ──────────────────────────────────────────────────
#
# A replay over the real L1Cache. An access reads the key and, on a miss, puts the entry that a rule
# picks from the entry's envelope and MessagePack sizes. _kept_rule is _put_l1's: the MessagePack only
# when L1 admits the envelope (L1Cache.admits) and the MessagePack is no longer (l1_entry); the tests
# above pin that _put_l1 applies it. The two rejected rules are the counterexamples an adversarial
# review found; they show the pinned traces bite.

Rule = Callable[[L1Cache, int, int], int]


def _envelope_rule(l1: L1Cache, envelope: int, raw: int) -> int:
    return envelope


def _kept_rule(l1: L1Cache, envelope: int, raw: int) -> int:
    return raw if l1.admits(envelope) and raw <= envelope else envelope


def _growth_allowance_rule(l1: L1Cache, envelope: int, raw: int) -> int:
    return raw if l1.admits(envelope) and raw <= envelope * 1.1 else envelope


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


def _replay(trace: list[tuple[str, str, int, int]], budget_mb: int, rule: Rule) -> list[int]:
    """The indices of the trace's reads that hit L1.

    Ops: ("read", key, ...) reads and, on a miss, puts; ("write", key, ...) puts whatever L1 holds (a
    revalidation, another writer); ("set", key, envelope, raw) is a new value at L2: L1 drops the key, and
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
        if op == "read" and l1.get(key)[0]:
            hits.append(i)
            continue
        l1.put(key, _Sized(rule(l1, *sizes.setdefault(key, (envelope, raw)))))
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
@settings(max_examples=100, deadline=None)  # enough: each rejected rule below fails it in every run tried
@given(data=st.data())
def test_no_access_trace_gets_fewer_l1_hits_than_with_envelopes(budget_mb: int, data: st.DataObject) -> None:
    trace = data.draw(_traces(budget_mb))
    with_envelopes = _replay(trace, budget_mb, _envelope_rule)
    kept = _replay(trace, budget_mb, _kept_rule)
    assert set(with_envelopes) <= set(kept)  # every read that hit before still hits


def test_entries_growing_past_their_envelope_keep_it_so_a_near_full_lru_keeps_its_hits() -> None:
    """104 keys cycling through 100 MiB, each 1,000,000 B as an envelope and 1,040,000 B as MessagePack. The
    envelopes fit; with a 1.1x growth allowance the MessagePack would not, and LRU would miss every read."""
    keys = [f"k{i}" for i in range(104)]
    trace = [("read", k, 1_000_000, 1_040_000) for _ in range(3) for k in keys]
    hot = [i for i in range(len(trace)) if i >= len(keys)]

    assert _replay(trace, 100, _envelope_rule) == hot  # 208/208
    assert _replay(trace, 100, _kept_rule) == hot
    assert _replay(trace, 100, _growth_allowance_rule) == []  # 0/208


def test_an_entry_shrunk_under_the_admission_cap_stays_out_so_the_hot_set_keeps_its_hits() -> None:
    """Under 1 MiB (cap 131,072 B), an entry whose envelope is 131,080 B is refused. Admitted on its 131,000 B
    MessagePack, it would evict the least recent hot entry, and each hot read would then evict the next."""
    hot_keys = [f"h{i}" for i in range(10)]
    warm = [("read", k, 100_000, 100_000) for k in hot_keys]
    rounds = [op for _ in range(2) for op in [("read", "big", 131_080, 131_000), *warm]]
    trace = warm + rounds

    assert len(_replay(trace, 1, _envelope_rule)) == 20
    assert len(_replay(trace, 1, _kept_rule)) == 20
    assert _replay(trace, 1, _admit_on_raw_rule) == []  # 20 hits fell to 0
