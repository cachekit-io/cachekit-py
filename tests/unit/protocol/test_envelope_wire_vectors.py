"""SDK-level byte-verification of the ByteStorage envelope against the protocol wire-format vectors.

Fixture: tests/unit/protocol/fixtures/wire-format.json, vendored from
cachekit-io/protocol @ 5be35d5240817617275e3983de486a5826a587af
(fixture 1.1.1, sha256 b902db88fb9b2c4a2d0def7266f8199a858fcb921262c1eaf2c2c03412b5b56a).
Regenerate ONLY by re-copying from the protocol repo — never by hand.

The fixture is append-only (protocol 1.1, decisions/envelope-bin-encoding.md):
seven legacy vectors pin the pre-0.4.0 array-of-ints encoding of
``compressed_data`` and are retained forever as legacy-read proof; their seven
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

A failure here is a wire-format break to triage, never a fixture to silently
regenerate: envelopes are shared cross-SDK (py/ts read each other's bytes),
so a changed encoding orphans or corrupts every existing cache entry.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import msgpack
import pytest

from cachekit import cache
from cachekit._rust_serializer import ByteStorage
from cachekit.backends.file import FileBackend, FileBackendConfig
from cachekit.key_generator import CacheKeyGenerator
from cachekit.serializers.standard_serializer import StandardSerializer
from cachekit.serializers.wrapper import SerializationWrapper

pytestmark = pytest.mark.unit

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "wire-format.json"
FIXTURE_SHA256 = "b902db88fb9b2c4a2d0def7266f8199a858fcb921262c1eaf2c2c03412b5b56a"

_FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
VECTORS = _FIXTURE["vectors"]
LEGACY_VECTORS = [v for v in VECTORS if "envelope_encoding" not in v]
BIN_VECTORS = [v for v in VECTORS if v.get("envelope_encoding") == "bin"]

# Envelope is a positional fixarray(4): [compressed_data, checksum, original_size, format].
FIXARRAY_4 = 0x94


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
        # Append-only contract: 7 legacy vectors retained forever + 7 *_bin twins.
        assert len(LEGACY_VECTORS) == 7
        assert len(BIN_VECTORS) == 7
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
