"""Byte-verification of interop mode against the protocol test vectors.

Fixture: tests/unit/protocol/fixtures/interop-mode.json, vendored from
cachekit-io/protocol test-vectors/interop-mode.json 1.3.0
(https://github.com/cachekit-io/protocol/pull/164)
(sha256 e1ca6c2361509f347d17f3352e0d7ab4d4b61488737bdf0056bb5769d9794e72).
Regenerate ONLY by re-copying from the protocol repo — never by hand.

Every group is exercised through the SDK's own implementation:
- 44 key vectors: canonical argument bytes, args hash, and full key
- 6 value vectors: canonical plain-MessagePack value bytes (and decode round-trip)
- 34 error vectors: inputs that MUST be rejected
- 6 reader accept vectors: well-formed, non-canonical documents the value reader MUST decode
- 1 reader reject vector: a document the value reader MUST reject
- 1 AAD vector: the REAL EncryptionWrapper AAD builder over an interop key
- 1 encryption vector: HKDF-SHA256 key derivation + AES-256-GCM decrypt through
  the REAL Rust encryption stack (cross-SDK decryption capability, not just
  construction)
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import msgpack
import pytest

from cachekit.interop import (
    InteropDecodeError,
    InteropError,
    args_hash,
    canonical_args_bytes,
    decode_interop_value,
    encode_interop_value,
    generate_interop_key,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "interop-mode.json"
FIXTURE_SHA256 = "e1ca6c2361509f347d17f3352e0d7ab4d4b61488737bdf0056bb5769d9794e72"  # pragma: allowlist secret

VECTORS = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

# The counts below are part of the conformance claim: a fixture update that
# adds or removes vectors must be a conscious change, not a silent drift.
EXPECTED_COUNTS = {
    "key_vectors": 44,
    "value_vectors": 6,
    "error_vectors": 34,
    "reader_accept_vectors": 6,
    "reader_reject_vectors": 1,
    "aad_vectors": 1,
    "encryption_vectors": 1,
}


class _TaggedSet:
    """Ordered stand-in for a set from tagged JSON (elements may be unhashable)."""

    def __init__(self, elements: list[Any]) -> None:
        self.elements = elements


def from_tagged(v: Any) -> Any:
    """Decode the vector file's tagged-JSON convention into Python values."""
    if isinstance(v, list):
        return [from_tagged(e) for e in v]
    if isinstance(v, dict):
        if len(v) == 1:
            ((k, val),) = v.items()
            if k == "$set":
                return _TaggedSet([from_tagged(e) for e in val])
            if k == "$bytes":
                return bytes.fromhex(val)
            if k == "$datetime":
                return datetime.fromisoformat(val)
            if k == "$uuid":
                return UUID(val)
            if k == "$float":
                return float(val)
            if k == "$int":
                return int(val)
            if k.startswith("$"):
                raise ValueError(f"unknown tag {k!r}")
        return {k: from_tagged(val) for k, val in v.items()}
    return v


def resolve_sets(v: Any) -> Any:
    """Convert _TaggedSet stand-ins to real sets/frozensets where hashable.

    Vector sets contain only hashable elements, so frozenset is always safe
    here; the stand-in exists because JSON cannot express sets directly.
    """
    if isinstance(v, _TaggedSet):
        return frozenset(resolve_sets(e) for e in v.elements)
    if isinstance(v, list):
        return [resolve_sets(e) for e in v]
    if isinstance(v, dict):
        return {k: resolve_sets(val) for k, val in v.items()}
    return v


def vector_args(raw: list[Any]) -> list[Any]:
    return [resolve_sets(from_tagged(a)) for a in raw]


def test_fixture_integrity():
    """The vendored fixture is byte-identical to the pinned protocol revision."""
    digest = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    assert digest == FIXTURE_SHA256, (
        f"fixtures/interop-mode.json sha256 {digest} != pinned {FIXTURE_SHA256}. "
        "If the protocol vectors were intentionally updated, refresh the pin AND the counts."
    )


@pytest.mark.parametrize("group,count", sorted(EXPECTED_COUNTS.items()))
def test_vector_counts(group: str, count: int):
    assert len(VECTORS[group]) == count


@pytest.mark.parametrize("vector", VECTORS["key_vectors"], ids=lambda v: v["name"])
def test_key_vectors(vector: dict[str, Any]):
    """Canonical argument bytes, args hash, and full interop key — byte-exact."""
    args = vector_args(vector["args"])

    assert canonical_args_bytes(args).hex() == vector["canonical_args_hex"], "canonical argument encoding mismatch"
    assert args_hash(args) == vector["args_hash"], "args hash mismatch"
    assert generate_interop_key(vector["namespace"], vector["operation"], args) == vector["expected_key"]


@pytest.mark.parametrize("vector", VECTORS["value_vectors"], ids=lambda v: v["name"])
def test_value_vectors(vector: dict[str, Any]):
    """Canonical plain-MessagePack value bytes — byte-exact — and strict decode."""
    value = from_tagged(vector["value"])
    encoded = encode_interop_value(value)
    assert encoded.hex() == vector["canonical_msgpack_hex"], "canonical value encoding mismatch"

    decoded = decode_interop_value(bytes.fromhex(vector["canonical_msgpack_hex"]))
    if vector["name"] == "datetime_sentinel_value":
        # The sentinel map revives to a native datetime on the Python read path.
        assert decoded == datetime.fromisoformat(vector["value"]["value"])
    else:
        assert decoded == value


@pytest.mark.parametrize("vector", VECTORS["error_vectors"], ids=lambda v: v["name"])
def test_error_vectors(vector: dict[str, Any]):
    """Every error vector MUST be rejected (message text is not normative)."""
    with pytest.raises((InteropError, ValueError, OverflowError)):
        if "namespace" in vector:
            generate_interop_key(vector["namespace"], vector["operation"], vector_args(vector["args"]))
        else:
            canonical_args_bytes(vector_args(vector["args"]))


# Decoded values for the reader accept vectors tagged JSON cannot carry (the fixture omits their "value").
READER_VALUES_NOT_IN_FIXTURE = {
    "reader_non_string_map_key": {1: 42},
    "reader_ext_type": msgpack.ExtType(1, b"\x2a"),
}


@pytest.mark.parametrize("vector", VECTORS["reader_accept_vectors"], ids=lambda v: v["name"])
def test_reader_accept_vectors(vector: dict[str, Any]):
    """The value reader decodes every well-formed, non-canonical document to its value: the
    fixture's tagged-JSON one, or ours where tagged JSON cannot carry it."""
    expected = from_tagged(vector["value"]) if "value" in vector else READER_VALUES_NOT_IN_FIXTURE[vector["name"]]
    assert decode_interop_value(bytes.fromhex(vector["input_hex"])) == expected


@pytest.mark.parametrize("vector", VECTORS["reader_reject_vectors"], ids=lambda v: v["name"])
def test_reader_reject_vectors(vector: dict[str, Any]):
    """The value reader rejects every reader reject vector by its trailing-bytes check (message text
    is not normative); the cause pins that it is not the CK-frame diagnostic."""
    with pytest.raises(InteropDecodeError) as excinfo:
        decode_interop_value(bytes.fromhex(vector["input_hex"]))
    assert isinstance(excinfo.value.__cause__, msgpack.exceptions.ExtraData)


def _colliding_timestamps(n: int) -> list[msgpack.Timestamp]:
    """n distinct Timestamps with one hash, by inverting CPython's 64-bit two-item tuple hash.

    Timestamp hashes (seconds, nanoseconds) with no seed, so a forged entry can do the same: without a
    bound, 4,000 such keys made one decode take ~0.6 s (quadratic in the key count).
    """
    mask, p1, p2, p5 = (1 << 64) - 1, 11400714785074694791, 14029467366897019727, 2870177450012600261

    def rotr(x: int, r: int) -> int:
        return ((x >> r) | (x << (64 - r))) & mask

    out, inv1, inv2, target = [], pow(p1, -1, 1 << 64), pow(p2, -1, 1 << 64), 12345
    nanoseconds = 0
    while len(out) < n:
        nanoseconds += 1
        acc1 = (rotr(target * inv1 & mask, 31) - hash(nanoseconds) * p2) & mask
        seconds = ((rotr(acc1 * inv1 & mask, 31) - p5) * inv2) & mask
        if seconds < (1 << 61) - 1:  # hash(seconds) == seconds below the modulus
            out.append(msgpack.Timestamp(seconds, nanoseconds))
    return out


def _map_document(pairs: list[tuple[Any, Any]]) -> bytes:
    """A map16 of the given pairs, in order, duplicates kept (packb would merge them)."""
    return b"\xde" + len(pairs).to_bytes(2, "big") + b"".join(msgpack.packb(k) + msgpack.packb(v) for k, v in pairs)


class TestReaderHashFlood:
    """strict_map_key=False lets unseeded-hash keys in; the reader must bound their collisions."""

    def test_timestamp_keys_sharing_one_hash_are_bounded(self):
        keys = _colliding_timestamps(4000)
        assert len({hash(k) for k in keys}) == 1 and len(set(keys)) == 4000
        assert decode_interop_value(_map_document([(k, 0) for k in keys[:32]])) == dict.fromkeys(keys[:32], 0)
        with pytest.raises(InteropDecodeError, match="distinct keys with one hash"):
            decode_interop_value(_map_document([(k, 0) for k in keys[:33]]))
        # The 4,000-key flood is refused at the 33rd collider instead of after ~8M comparisons.
        with pytest.raises(InteropDecodeError, match="distinct keys with one hash"):
            decode_interop_value(_map_document([(k, 0) for k in keys]))

    def test_float_keys_sharing_one_hash_are_bounded(self):
        # 2.0**a hashes to 2**(a mod 61): every a in one residue class collides, 34 of them in float64's range.
        keys = [2.0**a for a in range(-1074, 1024) if a % 61 == 5]
        assert len(keys) == 34 and len({hash(k) for k in keys}) == 1
        with pytest.raises(InteropDecodeError, match="distinct keys with one hash"):
            decode_interop_value(_map_document([(k, 0) for k in keys]))

    def test_repeated_key_is_not_counted_as_a_collider(self):
        # A repeated key overwrites (last wins); it adds no probe-chain length, so it must not trip the bound.
        assert decode_interop_value(_map_document([(1, i) for i in range(100)])) == {1: 99}

    def test_non_str_key_types_still_decode(self):
        # The bound narrows no key type: ext and revived temporal keys still decode alongside str keys.
        sentinel = {"__datetime__": True, "value": "2024-01-01T00:00:00+00:00"}
        doc = _map_document([("a", 1), (msgpack.ExtType(1, b"*"), 2), (b"k", 3), (None, 4), (1.5, 5)])
        assert decode_interop_value(doc) == {"a": 1, msgpack.ExtType(1, b"*"): 2, b"k": 3, None: 4, 1.5: 5}
        revived = decode_interop_value(b"\x81" + msgpack.packb(sentinel) + b"\x01")
        assert revived == {datetime.fromisoformat(sentinel["value"]): 1}


def test_lone_surrogate_rejected():
    """Strings must be well-formed Unicode scalar sequences (spec self-test:
    portable JSON cannot express a lone surrogate, so there is no error vector)."""
    with pytest.raises(InteropError):
        canonical_args_bytes(["\ud800"])


def test_aad_vector():
    """The real EncryptionWrapper AAD builder produces the pinned interop AAD.

    Interop AAD is v0x03 with EXACTLY four components (tenant_id, cache_key,
    "msgpack", "False") — no original_type. Built via EncryptionWrapper._create_aad
    (the production code path), not a test-local reimplementation.
    """
    pytest.importorskip("cachekit._rust_serializer")
    from cachekit.serializers.base import SerializationFormat, SerializationMetadata
    from cachekit.serializers.encryption_wrapper import EncryptionWrapper
    from cachekit.serializers.interop_serializer import InteropSerializer

    vector = VECTORS["aad_vectors"][0]
    enc_vector = VECTORS["encryption_vectors"][0]
    wrapper = EncryptionWrapper(
        serializer=InteropSerializer(),
        master_key=bytes.fromhex(enc_vector["master_key_hex"]),
        tenant_id=vector["tenant_id"],
    )
    metadata = SerializationMetadata(
        serialization_format=SerializationFormat.MSGPACK,
        compressed=False,
        original_type=None,
    )
    aad = wrapper._create_aad(metadata, vector["cache_key"])
    assert aad.hex() == vector["aad_hex"]
    # Exactly four components: version byte + 4 length-prefixed fields
    parsed = wrapper._parse_aad(aad)
    assert parsed["original_type"] is None
    assert parsed["format"] == "msgpack"
    assert parsed["compressed"] == "False"


def test_encryption_vector_decrypts_through_real_stack():
    """HKDF-SHA256 chain + AES-256-GCM decrypt of the pinned interop ciphertext.

    Uses the production Rust primitives (derive_tenant_keys, ZeroKnowledgeEncryptor)
    — a reader that verifies this tag has demonstrated cross-SDK decryption of an
    interop entry written by the protocol reference implementation.
    """
    rust = pytest.importorskip("cachekit._rust_serializer")

    vector = VECTORS["encryption_vectors"][0]
    master_key = bytes.fromhex(vector["master_key_hex"])
    tenant_keys = rust.derive_tenant_keys(master_key, vector["tenant_id"])

    # Ground-truth continuity: the derived key fingerprint is the one already
    # published in test-vectors/encryption.json.
    assert tenant_keys.encryption_fingerprint().hex() == vector["derived_key_fingerprint_hex"]

    encryptor = rust.ZeroKnowledgeEncryptor()
    ciphertext = bytes.fromhex(vector["ciphertext_hex"])
    aad = bytes.fromhex(vector["aad_hex"])
    plaintext = encryptor.decrypt_with_keys(ciphertext, aad, tenant_keys)
    assert plaintext.hex() == vector["plaintext_hex"]

    # The plaintext is a plain-MessagePack interop value — decode it.
    assert decode_interop_value(bytes(plaintext)) == {"name": "alice", "age": 30}

    # Tamper check: flipping one ciphertext bit must fail authentication.
    tampered = bytearray(ciphertext)
    tampered[-1] ^= 0x01
    with pytest.raises(Exception):  # noqa: B017 - any auth failure is a pass
        encryptor.decrypt_with_keys(bytes(tampered), aad, tenant_keys)


def test_spec_equalities():
    """Intentional equalities the spec calls out (cheap cross-checks)."""
    by_name = {v["name"]: v for v in VECTORS["key_vectors"]}
    # 2.0 and 2 hash identically (number canonicalization)
    assert by_name["float_integral_collapse"]["args_hash"] == by_name["single_int_two"]["args_hash"]
    # Same instant, different UTC offset -> same key
    assert by_name["datetime_fractional"]["expected_key"] == by_name["datetime_non_utc_offset"]["expected_key"]
