"""Byte-verification of interop mode against the protocol test vectors.

Fixture: tests/unit/protocol/fixtures/interop-mode.json, vendored from
cachekit-io/protocol @ ef3e6d4dd29c200908a06f3a02dab605193fa32a
(sha256 a1f24b61e4957e9500a01ce7ed9fbb3ec601847514b481bf54813d9e470226df).
Regenerate ONLY by re-copying from the protocol repo — never by hand.

Every group is exercised through the SDK's own implementation:
- 33 key vectors: canonical argument bytes, args hash, and full key
- 4 value vectors: canonical plain-MessagePack value bytes (and decode round-trip)
- 9 error vectors: inputs that MUST be rejected
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

import pytest

from cachekit.interop import (
    InteropError,
    args_hash,
    canonical_args_bytes,
    decode_interop_value,
    encode_interop_value,
    generate_interop_key,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "interop-mode.json"
FIXTURE_SHA256 = "a1f24b61e4957e9500a01ce7ed9fbb3ec601847514b481bf54813d9e470226df"

VECTORS = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

# The counts below are part of the conformance claim: a fixture update that
# adds or removes vectors must be a conscious change, not a silent drift.
EXPECTED_COUNTS = {"key_vectors": 33, "value_vectors": 4, "error_vectors": 9, "aad_vectors": 1, "encryption_vectors": 1}


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
