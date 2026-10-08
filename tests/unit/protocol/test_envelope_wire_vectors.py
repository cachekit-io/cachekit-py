"""SDK-level byte-verification of the ByteStorage envelope against the protocol wire-format vectors.

Fixture: tests/unit/protocol/fixtures/wire-format.json, vendored from
cachekit-io/protocol @ 4b8fddb2120b9e3355d7fc9d593130dba8a345ac
(fixture 1.4.0, sha256 2f6818903a552a7c09414c1e5c02caf21fa92dcd56ccff5634c8257038e7575c).
Regenerate ONLY by re-copying from the protocol repo — never by hand.

The fixture is append-only (protocol 1.1, decisions/envelope-bin-encoding.md):
nine legacy vectors pin the pre-0.4.0 array-of-ints encoding of
``compressed_data`` and are retained forever as legacy-read proof; their nine
``*_bin`` twins pin the MessagePack ``bin`` encoding that cachekit-core 0.4.0
writers emit, at the bin8 and bin16 header widths. This module proves — not asserts — through the real Python
paths that:

1. **Legacy-read**: pre-0.4.0 envelopes decode through ``ByteStorage.retrieve``
   (the exact FFI call every serializer's deserialize path makes) and through
   the full decorator retrieve path.
2. **Bin-emit**: fresh writes carry ``bin``-encoded envelopes (marker
   ``0xc4``/``0xc5``/``0xc6`` on element[0]), observed inside the CK v3 frame
   through the real decorator store path and at every width tier through the
   real serializer path — including bin32, which the protocol pins
   deliberately leave uncovered (spec/wire-format.md).
3. **Re-encode identity**: the 0.4.0 writer reproduces every ``*_bin`` pin
   byte-identically from the vector inputs.
4. **Round-trip identity** through the full stack (store → retrieve) for
   compressible and incompressible payloads.
5. **Constructed vectors**: every ``constructed_vectors`` envelope decodes to its
   constructed input: the bin16 maximum, the bin32 minimum in both encodings, and
   ``envelope_ratio_product_wraps_32_bits``, whose ``1000 * compressed_size``
   overflows 32 bits. cachekit-py ships 64-bit wheels only, where a
   pointer-width product passes that vector anyway, so it is a regression guard
   and does not discharge the spec's MUST for 32-bit targets.
6. **Named rejections**: every ``reject_vectors`` envelope is rejected by
   ``ByteStorage.retrieve`` with the error class and message its row names. The
   spec also asks that the reads of ``reject_original_size_over_cap`` and
   ``reject_ratio_bomb`` stay below ``original_size`` in allocation; that bound
   is not asserted here, because an SDK over cachekit-core may rely on it only
   once core's allocation probe runs in CI on the core version this package pins.
   ``reject_envelope_slots_overclaim`` is an expected failure: the pinned
   cachekit-core rejects it in the typed decode, not with the pre-scan error the
   spec requires.
7. **Payload decode bounds**: every ``payload_reject_vectors`` envelope passes the
   envelope read, and ``StandardSerializer`` then refuses its payload with the
   structural guard's own error, before the decode allocates.
8. **Temporal sentinels**: every ``temporal_sentinel_vectors`` payload revives as
   its temporal type through ``StandardSerializer``.

A failure here is a wire-format break to triage, never a fixture to silently
regenerate: envelopes are shared cross-SDK (py/ts read each other's bytes),
so a changed encoding orphans or corrupts every existing cache entry.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path

import msgpack
import pytest

from cachekit import cache
from cachekit._rust_serializer import ByteStorage, EnvelopeIntegrityError
from cachekit.backends.file import FileBackend, FileBackendConfig
from cachekit.key_generator import CacheKeyGenerator
from cachekit.serializers.base import SerializationError
from cachekit.serializers.standard_serializer import StandardSerializer
from cachekit.serializers.wrapper import SerializationWrapper

pytestmark = pytest.mark.unit

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "wire-format.json"
FIXTURE_SHA256 = "2f6818903a552a7c09414c1e5c02caf21fa92dcd56ccff5634c8257038e7575c"  # pragma: allowlist secret

_FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
VECTORS = _FIXTURE["vectors"]
LEGACY_VECTORS = [v for v in VECTORS if "envelope_encoding" not in v]
BIN_VECTORS = [v for v in VECTORS if v.get("envelope_encoding") == "bin"]

# Envelope is a positional fixarray(4): [compressed_data, checksum, original_size, format].
FIXARRAY_4 = 0x94


def _named_vector(group: str, name: str) -> dict:
    """The fixture vector ``name`` in ``group``; fails by name if either is missing."""
    for vector in _FIXTURE.get(group, []):
        if vector["name"] == name:
            return vector
    pytest.fail(f"fixtures/wire-format.json has no {group}[{name!r}]")


def _construct(segments: list[dict]) -> bytes:
    """Expand a fixture ``{hex, count}`` segment list into the bytes it describes."""
    return b"".join(bytes.fromhex(seg["hex"]) * seg["count"] for seg in segments)


def _incompressible(n: int) -> bytes:
    """Deterministic high-entropy bytes (SHA-256 counter stream) — LZ4 cannot shrink these."""
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:n])


def _expected_bin_marker(compressed_len: int) -> int:
    """The one MessagePack bin marker a conforming writer emits for this length: bin8 / bin16 / bin32."""
    if compressed_len <= 0xFF:
        return 0xC4
    if compressed_len <= 0xFFFF:
        return 0xC5
    return 0xC6


def _to_legacy_encoding(envelope: bytes) -> bytes:
    """Transcode a bin-encoded envelope to the pre-0.4.0 array-of-ints encoding.

    Round-trips the envelope through msgpack with ``compressed_data`` as a list
    of ints — exactly the shape rmp_serde emitted for a plain ``Vec<u8>``
    before core 0.4.0. Element order and all other fields are untouched.
    """
    compressed, checksum, original_size, fmt = msgpack.unpackb(envelope, use_list=True, raw=False)
    assert isinstance(compressed, bytes), "expected a bin-encoded envelope to transcode"
    legacy = msgpack.packb([list(compressed), checksum, original_size, fmt], use_bin_type=True)
    assert isinstance(legacy, bytes)
    return legacy


class TestWireFormatFixture:
    """The vendored fixture is byte-identical to the pinned protocol revision."""

    def test_fixture_integrity(self):
        digest = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
        assert digest == FIXTURE_SHA256, (
            f"fixtures/wire-format.json sha256 {digest} != pinned {FIXTURE_SHA256}. "
            "If the protocol vectors were intentionally updated, refresh the pin AND the counts."
        )

    def test_vector_counts(self):
        # Append-only contract: 9 legacy vectors retained forever + 9 *_bin twins.
        assert len(LEGACY_VECTORS) == 9
        assert len(BIN_VECTORS) == 9
        assert {v["derived_from"] for v in BIN_VECTORS} == {v["name"] for v in LEGACY_VECTORS}


class TestFfiDualRead:
    """Both envelope encodings decode via the real FFI retrieve path, byte-identically."""

    @pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["name"])
    def test_envelope_decodes(self, vector):
        """Dual-read proof: legacy (array-of-ints) AND bin envelopes decode, byte-identically.

        The plain-named vectors pin the pre-0.4.0 legacy encoding, which stays a
        permanently accepted read format; their ``*_bin`` twins pin the 0.4.0 writer.
        """
        storage = ByteStorage("msgpack")
        payload, fmt = storage.retrieve(bytes.fromhex(vector["envelope_hex"]))
        assert bytes(payload) == bytes.fromhex(vector["input_hex"])
        assert fmt == vector["format"]

    @pytest.mark.parametrize("vector", BIN_VECTORS, ids=lambda v: v["name"])
    def test_bin_vector_marker_matches_compressed_length(self, vector):
        """Each *_bin pin carries exactly the width its compressed_data length demands — never a tolerated set."""
        envelope = bytes.fromhex(vector["envelope_hex"])
        compressed = msgpack.unpackb(envelope, raw=False)[0]
        assert isinstance(compressed, bytes)
        assert envelope[0] == FIXARRAY_4
        assert envelope[1] == _expected_bin_marker(len(compressed))

    def test_fixture_pins_bin16_width_boundary(self):
        """The fixture's reason for being at 1.1.1: a pinned twin whose header is wider than bin8."""
        markers = {bytes.fromhex(v["envelope_hex"])[1] for v in BIN_VECTORS}
        assert markers == {0xC4, 0xC5}

    @pytest.mark.parametrize("vector", BIN_VECTORS, ids=lambda v: v["name"])
    def test_store_reencodes_bin_vectors_byte_identically(self, vector):
        """The 0.4.0 writer reproduces every *_bin protocol pin exactly — proven, not asserted."""
        storage = ByteStorage("msgpack")
        envelope = bytes(storage.store(bytes.fromhex(vector["input_hex"]), "msgpack"))
        assert envelope.hex() == vector["envelope_hex"]


CONSTRUCTED_VECTORS = _FIXTURE["constructed_vectors"]


class TestConstructedVectors:
    """Vectors too large to store as one hex string, rebuilt from their segment lists."""

    def test_constructed_vector_names_are_pinned(self):
        """An emptied or renamed group fails here instead of passing on zero parametrized cases."""
        assert {v["name"] for v in CONSTRUCTED_VECTORS} == {
            "envelope_ratio_product_wraps_32_bits",  # 1000 * compressed_size overflows 32 bits
            "envelope_bin16_max",
            "envelope_bin32_min",
            "envelope_legacy_array32_min",
        }

    @pytest.mark.parametrize("vector", CONSTRUCTED_VECTORS, ids=lambda v: v["name"])
    def test_constructed_envelope_decodes(self, vector):
        envelope = _construct(vector["envelope_construction"])
        expected = _construct(vector["input_construction"])
        assert len(envelope) == vector["envelope_size"]
        assert len(expected) == vector["original_size"]

        payload, fmt = ByteStorage("msgpack").retrieve(envelope)
        assert bytes(payload) == expected
        assert fmt == "msgpack"


# Each reject vector's named error, as cachekit-py surfaces it (rust/src/python_bindings.rs maps
# DeserializationFailed to a plain ValueError, every other core error to EnvelopeIntegrityError).
REJECT_EXPECTATIONS: dict[str, tuple[type[Exception], str]] = {
    "reject_original_size_over_cap": (EnvelopeIntegrityError, "input exceeds maximum size"),
    # A Retrieve Flow step-2 range-checked decode into u32, which the row allows.
    "reject_original_size_wraps_u32": (ValueError, "expected u32"),
    # Core shares one error variant between its zero-length and ratio checks, so the message is the
    # ratio one; with original_size 0 the ratio check cannot fire (1000 * 0 = 0), so only the
    # zero-length check can have rejected it.
    "reject_zero_length_compressed_data": (EnvelopeIntegrityError, "decompression ratio exceeds safety limit"),
    "reject_ratio_bomb": (EnvelopeIntegrityError, "decompression ratio exceeds safety limit"),
    "reject_decompressed_length_mismatch": (EnvelopeIntegrityError, "size validation failed"),
    "reject_checksum_mismatch": (EnvelopeIntegrityError, "integrity check failed"),
    "reject_original_size_sign_bit": (ValueError, "expected u32"),  # range-checked decode, as wraps_u32
    "reject_ratio_float32_rounds": (EnvelopeIntegrityError, "decompression ratio exceeds safety limit"),
    # Retrieve Flow step 2's typed decode: rmp_serde's own error for each break.
    "reject_envelope_arity_5": (ValueError, "deserialization failed: array had incorrect length, expected 4"),
    "reject_envelope_arity_3": (ValueError, "deserialization failed: invalid length 3, expected struct StorageEnvelope"),
    "reject_checksum_nine_elements": (ValueError, "deserialization failed: array had incorrect length, expected 8"),
    "reject_checksum_seven_elements": (ValueError, "deserialization failed: invalid length 7, expected an array of length 8"),
    "reject_legacy_element_above_255": (ValueError, "deserialization failed: invalid value: integer `360`, expected u8"),
    # Retrieve Flow step 2's decode-bounds pre-scan, before anything is materialised. The spec asks for
    # the pre-scan's own error because the typed decode rejects this vector too.
    "reject_envelope_slots_overclaim": (
        ValueError,
        "deserialization failed: decode pre-scan: declares more elements than the input can back",
    ),
}

# Vectors the pinned cachekit-core fails, each recorded as a strict expected failure so it fails loudly
# the day it starts passing. Every other reject vector must pass outright.
REJECT_XFAIL: dict[str, str] = {
    "reject_envelope_slots_overclaim": (
        "WIRE-9: the pinned cachekit-core has no envelope pre-scan, so its typed decode rejects this "
        "vector (invalid type at element 1) instead of the pre-scan"
    ),
}


class TestRejectVectors:
    """Every reject vector is refused by the real FFI retrieve path with the error its row names."""

    def test_reject_vector_names_are_pinned(self):
        """An emptied or renamed group fails here instead of passing on zero parametrized cases."""
        assert {v["name"] for v in _FIXTURE.get("reject_vectors", [])} == set(REJECT_EXPECTATIONS)

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            pytest.param(
                name,
                expected,
                id=name,
                marks=[pytest.mark.xfail(strict=True, raises=AssertionError, reason=REJECT_XFAIL[name])]
                if name in REJECT_XFAIL
                else [],
            )
            for name, expected in REJECT_EXPECTATIONS.items()
        ],
    )
    def test_retrieve_rejects_with_named_error(self, name, expected):
        error_type, message = expected
        envelope = bytes.fromhex(_named_vector("reject_vectors", name)["envelope_hex"])
        with pytest.raises(error_type, match=message) as excinfo:
            ByteStorage("msgpack").retrieve(envelope)
        if error_type is ValueError:
            assert not isinstance(excinfo.value, EnvelopeIntegrityError)


# The structural guard's own error for each reject reason (rust/src/msgpack_bounds.rs).
PAYLOAD_REJECT_ERRORS = {"overclaim": "declares more elements than the input can back"}


class TestPayloadRejectVectors:
    """Envelopes every Retrieve Flow check accepts, whose payload the bounded decode must refuse."""

    def test_payload_reject_vector_names_are_pinned(self):
        """An emptied or renamed group fails here instead of passing on zero parametrized cases."""
        assert {v["name"] for v in _FIXTURE.get("payload_reject_vectors", [])} == {
            "payload_array32_max_claim_alone",
            "payload_nested_array16_each_header_fits_sum_overclaims",
        }

    @pytest.mark.parametrize("vector", _FIXTURE["payload_reject_vectors"], ids=lambda v: v["name"])
    def test_payload_decode_rejects_with_guard_error(self, vector):
        envelope = bytes.fromhex(vector["envelope_hex"])
        payload, fmt = ByteStorage("msgpack").retrieve(envelope)  # the envelope itself is sound
        assert bytes(payload) == bytes.fromhex(vector["input_hex"])
        assert fmt == vector["format"]

        (reason,) = vector["reject_reasons"]
        with pytest.raises(SerializationError, match=PAYLOAD_REJECT_ERRORS[reason]):
            StandardSerializer().deserialize(envelope)


_TEMPORAL_TYPES = {"datetime": datetime.datetime, "date": datetime.date, "time": datetime.time}


class TestTemporalSentinelVectors:
    """Auto-mode temporal sentinel maps revive as their temporal type, never as the raw map."""

    def test_temporal_sentinel_vector_names_are_pinned(self):
        """An emptied or renamed group fails here instead of passing on zero parametrized cases."""
        assert {v["name"]: v["revives_to"]["type"] for v in _FIXTURE.get("temporal_sentinel_vectors", [])} == {
            "temporal_sentinel_datetime": "datetime",
            "temporal_sentinel_date": "date",
            "temporal_sentinel_time": "time",
        }

    @pytest.mark.parametrize("vector", _FIXTURE["temporal_sentinel_vectors"], ids=lambda v: v["name"])
    def test_payload_revives_as_temporal_type(self, vector):
        # The fixture pins the payload, not an envelope, so it goes to the decode as-is.
        value = StandardSerializer(enable_integrity_checking=False).deserialize(bytes.fromhex(vector["payload_hex"]))
        expected_type = _TEMPORAL_TYPES[vector["revives_to"]["type"]]
        assert type(value) is expected_type
        assert value.isoformat() == vector["revives_to"]["iso"]  # the same instant AND the same offset


class TestBinEmitWidths:
    """Fresh writes emit bin envelopes at every header width through the real serializer path.

    The protocol *_bin vectors pin bin8 and bin16 only (bin32 is a recorded
    won't-do in spec/wire-format.md), so all three width tiers are exercised
    here against the real writer.
    """

    @pytest.mark.parametrize(
        ("payload_size", "expected_marker"),
        [
            (16, 0xC4),  # bin8: compressed_data <= 255 B
            (1024, 0xC5),  # bin16: 256 B <= compressed_data <= 65535 B
            (128 * 1024, 0xC6),  # bin32: compressed_data > 65535 B
        ],
        ids=["bin8", "bin16", "bin32"],
    )
    def test_serialize_emits_expected_bin_width(self, payload_size, expected_marker):
        envelope, _ = StandardSerializer().serialize(_incompressible(payload_size))
        assert envelope[0] == FIXARRAY_4
        assert envelope[1] == expected_marker
        compressed, checksum, original_size, fmt = msgpack.unpackb(envelope, use_list=True, raw=False)
        assert isinstance(compressed, bytes)  # bin decodes to bytes; legacy array-of-ints would be a list
        assert isinstance(checksum, list) and len(checksum) == 8  # checksum stays array-of-ints (spec exclusion)
        assert fmt == "msgpack"


class TestFullStackEnvelope:
    """Envelope encoding proven through the real decorator store/retrieve stack (L2 = FileBackend)."""

    @pytest.fixture
    def file_backend(self, tmp_path):
        return FileBackend(FileBackendConfig(cache_dir=tmp_path, max_size_mb=256))

    @staticmethod
    def _stored_frame(backend, func, args, namespace):
        cache_key = CacheKeyGenerator().generate_key(func, args, {}, namespace)
        raw = backend.get(cache_key)
        assert raw is not None, f"no stored frame for key {cache_key}"
        return cache_key, bytes(raw)

    def test_fresh_write_emits_bin_envelope_through_store_path(self, file_backend):
        """A fresh decorator write stores a CK v3 frame whose payload is a bin-encoded envelope."""

        @cache(backend=file_backend, ttl=300, namespace="test_envelope_bin_emit", l1_enabled=False)
        def compute(x: int) -> dict:
            return {"user_id": x, "name": "Alice", "active": True}

        compute(1)
        _, frame = self._stored_frame(file_backend, compute, (1,), "test_envelope_bin_emit")

        assert frame[:2] == b"CK"
        payload, _metadata, _name = SerializationWrapper.unwrap(frame)
        envelope = bytes(payload)
        assert envelope[0] == FIXARRAY_4
        compressed, checksum, _original_size, fmt = msgpack.unpackb(envelope, use_list=True, raw=False)
        assert isinstance(compressed, bytes)
        assert envelope[1] == _expected_bin_marker(len(compressed))
        assert isinstance(checksum, list) and len(checksum) == 8
        assert fmt == "msgpack"

    def test_legacy_envelope_decodes_through_retrieve_path(self, file_backend):
        """A pre-0.4.0 (array-of-ints) envelope planted in L2 is served by the real retrieve path."""
        calls = 0

        @cache(backend=file_backend, ttl=300, namespace="test_envelope_legacy_read", l1_enabled=False)
        def compute(x: int) -> dict:
            nonlocal calls
            calls += 1
            return {"user_id": x, "name": "Alice", "active": True}

        expected = compute(7)
        assert calls == 1

        # Rewrite the stored frame with its envelope transcoded to the legacy encoding.
        cache_key, frame = self._stored_frame(file_backend, compute, (7,), "test_envelope_legacy_read")
        payload, metadata, serializer_name = SerializationWrapper.unwrap(frame)
        legacy_envelope = _to_legacy_encoding(bytes(payload))
        assert legacy_envelope != bytes(payload)  # the transcode actually changed the encoding
        file_backend.set(cache_key, SerializationWrapper.wrap(legacy_envelope, metadata, serializer_name), ttl=300)

        # The next read must be served from the legacy envelope, not recomputed.
        assert compute(7) == expected
        assert calls == 1, "legacy envelope was not decoded by the retrieve path (function re-ran)"

    @pytest.mark.parametrize(
        "payload",
        [b"A" * 100_000, _incompressible(100_000)],
        ids=["compressible", "incompressible"],
    )
    def test_round_trip_identity_full_stack(self, file_backend, payload):
        """store → retrieve returns byte-identical payloads for both compressibility extremes."""
        calls = 0

        @cache(backend=file_backend, ttl=300, namespace="test_envelope_round_trip", l1_enabled=False)
        def compute(tag: str) -> bytes:
            nonlocal calls
            calls += 1
            return payload

        assert compute("k") == payload
        assert calls == 1
        assert compute("k") == payload  # served from L2 through the full retrieve path
        assert calls == 1
