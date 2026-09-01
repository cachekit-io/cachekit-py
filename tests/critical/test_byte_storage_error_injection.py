"""
CRITICAL PATH TEST: ByteStorage Error Injection and Security Validation

This test MUST pass for ByteStorage security and error handling to work.
Tests error paths, corruption detection, and security boundary validation.
"""

import pytest

from ..utils.redis_test_helpers import RedisIsolationMixin

# Mark all tests in this module as critical
pytestmark = pytest.mark.critical


class TestByteStorageErrorInjection(RedisIsolationMixin):
    """Critical tests for ByteStorage error handling and security."""

    def test_corrupted_envelope_detection_malformed_messagepack(self):
        """CRITICAL: ByteStorage must reject corrupted envelopes (malformed MessagePack)."""
        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")

        # Write completely invalid data (not MessagePack)
        invalid_data = b"\x00\x01\x02\xff\xfe\xfd invalid_data_not_msgpack"

        # Retrieval should fail with deserialization error
        with pytest.raises(ValueError) as exc_info:
            storage.retrieve(invalid_data)

        error_msg = str(exc_info.value).lower()
        assert "deserialization" in error_msg or "failed" in error_msg

    def test_corrupted_checksum_detection(self):
        """CRITICAL: ByteStorage must detect data corruption via xxHash3-64 checksum."""
        import msgpack

        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")
        test_data = b"This is test data that will be corrupted"

        # Store data
        envelope_bytes = storage.store(test_data, None)

        # Deserialize envelope (Rust serde serializes structs as arrays by default)
        envelope = msgpack.unpackb(envelope_bytes)

        # Extract fields by index: [compressed_data, checksum, original_size, format]
        corrupted_data = bytearray(envelope[0])  # Index 0: compressed_data
        corrupted_data[0] ^= 0xFF  # Flip first byte
        corrupted_data[1] ^= 0xAA  # Flip second byte

        # Rebuild envelope with corrupted data but original checksum (as array)
        corrupted_envelope = [
            bytes(corrupted_data),  # compressed_data
            envelope[1],  # checksum (now wrong for corrupted data)
            envelope[2],  # original_size
            envelope[3],  # format
        ]

        corrupted_envelope_bytes = msgpack.packb(corrupted_envelope)

        # Retrieval should detect checksum mismatch
        with pytest.raises(ValueError) as exc_info:
            storage.retrieve(corrupted_envelope_bytes)

        error_msg = str(exc_info.value).lower()
        # Corruption may be detected at decompression stage OR checksum validation stage
        assert (
            "checksum" in error_msg
            or "validation" in error_msg
            or "corruption" in error_msg
            or "decompression" in error_msg
            or "failed" in error_msg
        )

    def test_malformed_lz4_compressed_data_handling(self):
        """CRITICAL: ByteStorage must handle malformed LZ4 compressed data gracefully."""
        import blake3
        import msgpack

        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")

        # Create a fake envelope with invalid LZ4 data
        fake_original_data = b"x" * 100
        fake_compressed_data = b"\x00\x01\x02\xff\xfe\xfd not_valid_lz4_data"

        # Generate checksum for the FAKE original data (not compressed)
        fake_checksum = blake3.blake3(fake_original_data).digest()

        malformed_envelope = {
            b"compressed_data": fake_compressed_data,
            b"checksum": fake_checksum,
            b"original_size": len(fake_original_data),
            b"format": b"msgpack",
        }

        malformed_envelope_bytes = msgpack.packb(malformed_envelope)

        # Retrieval should fail with LZ4 decompression error
        with pytest.raises(ValueError) as exc_info:
            storage.retrieve(malformed_envelope_bytes)

        error_msg = str(exc_info.value).lower()
        assert "lz4" in error_msg or "decompression" in error_msg or "failed" in error_msg

    def test_oversized_input_data_security_violation(self):
        """CRITICAL: ByteStorage must reject oversized input data (512MB limit)."""
        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")

        # Try to store data larger than 512MB limit
        # Use a size just over the limit to trigger security check
        oversized_data = b"\x00" * (512 * 1024 * 1024 + 1)  # 512MB + 1 byte

        with pytest.raises(ValueError) as exc_info:
            storage.store(oversized_data, None)

        error_msg = str(exc_info.value).lower()
        assert "exceeds maximum size" in error_msg or "too large" in error_msg

    def test_oversized_envelope_security_violation(self):
        """CRITICAL: ByteStorage must reject oversized envelopes on retrieval."""
        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")

        # Create an oversized envelope (512MB + 1 byte)
        oversized_envelope = b"\x00" * (512 * 1024 * 1024 + 1)

        with pytest.raises(ValueError) as exc_info:
            storage.retrieve(oversized_envelope)

        error_msg = str(exc_info.value).lower()
        assert "exceeds maximum size" in error_msg or "envelope too large" in error_msg

    def test_compression_ratio_bomb_protection(self):
        """CRITICAL: ByteStorage must detect decompression bomb attacks (>100x expansion)."""
        import msgpack

        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")

        # Create a fake envelope claiming suspicious compression ratio
        # Small compressed data, huge claimed original size (>100x expansion)
        fake_compressed = b"\x00" * 1000  # 1KB compressed
        fake_original_size = 200 * 1024 * 1024  # Claims 200MB original (200x expansion)

        # Generate a fake checksum (won't matter, ratio check happens first)
        fake_checksum = b"\x00" * 32

        bomb_envelope = {
            b"compressed_data": fake_compressed,
            b"checksum": fake_checksum,
            b"original_size": fake_original_size,
            b"format": b"msgpack",
        }

        bomb_envelope_bytes = msgpack.packb(bomb_envelope)

        # Retrieval should detect suspicious compression ratio
        with pytest.raises(ValueError) as exc_info:
            storage.retrieve(bomb_envelope_bytes)

        error_msg = str(exc_info.value).lower()
        assert "compression ratio" in error_msg or "decompression bomb" in error_msg

    def test_size_validation_mismatch_detection(self):
        """CRITICAL: ByteStorage must detect size mismatches after decompression."""
        import msgpack

        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")

        # Store valid data first to get a proper envelope
        original_data = b"x" * 100
        envelope_bytes = storage.store(original_data, None)

        # Deserialize envelope (as array)
        envelope = msgpack.unpackb(envelope_bytes)

        # Corrupt the original_size field (index 2)
        corrupted_envelope = [
            envelope[0],  # compressed_data (valid)
            envelope[1],  # checksum (valid for original_data)
            50,  # WRONG original_size (actual is 100)
            envelope[3],  # format
        ]

        corrupted_envelope_bytes = msgpack.packb(corrupted_envelope)

        # Retrieval should detect size mismatch
        with pytest.raises(ValueError) as exc_info:
            storage.retrieve(corrupted_envelope_bytes)

        error_msg = str(exc_info.value).lower()
        # May fail at LZ4 decompression (wrong size hint) or size validation after
        assert (
            "size" in error_msg
            or "validation" in error_msg
            or "decompression" in error_msg
            or "corruption" in error_msg
            or "failed" in error_msg
        )

    def test_estimate_compression_oversized_data(self):
        """CRITICAL: estimate_compression must reject oversized data."""
        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")

        # Try to estimate compression for oversized data
        oversized_data = b"\x00" * (512 * 1024 * 1024 + 1)

        with pytest.raises(ValueError) as exc_info:
            storage.estimate_compression(oversized_data)

        error_msg = str(exc_info.value).lower()
        assert "exceeds maximum size" in error_msg or "too large" in error_msg

    def test_validate_oversized_envelope_returns_false(self):
        """CRITICAL: validate() must return False for oversized envelopes."""
        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")

        # Create an oversized envelope
        oversized_envelope = b"\x00" * (512 * 1024 * 1024 + 1)

        # validate() should return False (not raise exception)
        result = storage.validate(oversized_envelope)
        assert result is False

    def test_validate_corrupted_envelope_returns_false(self):
        """CRITICAL: validate() must return False for corrupted envelopes."""
        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")

        # Completely invalid data (not MessagePack)
        invalid_envelope = b"\xff\xfe\xfd invalid data not msgpack"

        # validate() should return False (not raise exception)
        result = storage.validate(invalid_envelope)
        assert result is False

    def test_validate_envelope_with_bad_checksum_returns_false(self):
        """CRITICAL: validate() must return False for envelopes with bad checksums."""
        import msgpack

        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")

        # Store valid data first
        test_data = b"test data"
        envelope_bytes = storage.store(test_data, None)

        # Deserialize and corrupt checksum (envelope is an array)
        envelope = msgpack.unpackb(envelope_bytes)
        corrupted_envelope = list(envelope)  # Make mutable copy
        corrupted_envelope[1] = b"\x00" * 32  # Wrong checksum (index 1)

        corrupted_envelope_bytes = msgpack.packb(corrupted_envelope)

        # validate() should return False
        result = storage.validate(corrupted_envelope_bytes)
        assert result is False

    def test_security_limits_getters_return_correct_values(self):
        """CRITICAL: Security limit getters must return correct configured limits."""
        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")

        # Check security limits
        max_uncompressed = storage.max_uncompressed_size
        max_compressed = storage.max_compressed_size
        max_ratio = storage.max_compression_ratio

        # Verify limits match expected values
        assert max_uncompressed == 512 * 1024 * 1024  # 512MB
        assert max_compressed == 512 * 1024 * 1024  # 512MB
        assert max_ratio == 1000.0  # 1000x max expansion (conservative for edge cases)

    @pytest.mark.slow
    def test_edge_case_exactly_at_size_limit(self):
        """CRITICAL: ByteStorage must accept data exactly at the 512MB limit."""
        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")

        # Create data exactly at 512MB limit (use random data to avoid bomb detection)
        import random

        random.seed(42)
        # Use less compressible data (random bytes) at exactly the limit
        max_size_data = bytes([random.randint(0, 255) for _ in range(100 * 1024 * 1024)])  # 100MB random (won't compress much)

        # Should succeed
        envelope_bytes = storage.store(max_size_data, None)
        assert envelope_bytes is not None

        # Should be able to retrieve it
        retrieved_data, format_str = storage.retrieve(envelope_bytes)
        assert len(retrieved_data) == len(max_size_data)

    def test_zero_size_edge_case(self):
        """CRITICAL: ByteStorage must handle empty data correctly."""
        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")

        # Empty data
        empty_data = b""

        # Should succeed with empty data
        envelope_bytes = storage.store(empty_data, None)
        assert envelope_bytes is not None

        # Should be able to retrieve it
        retrieved_data, format_str = storage.retrieve(envelope_bytes)
        assert retrieved_data == b""
        assert format_str == "msgpack"

    def test_validate_method_detects_various_corruptions(self):
        """CRITICAL: validate() method must detect various types of corruption."""
        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")

        # Test various invalid inputs
        test_cases = [
            (b"\xff\xfe\xfd invalid", "completely invalid data"),
            (b"", "empty data"),
            (b"\x00" * (512 * 1024 * 1024 + 1), "oversized envelope"),
            (b"\x91\x00", "valid msgpack but wrong structure"),
        ]

        for invalid_data, description in test_cases:
            result = storage.validate(invalid_data)
            assert result is False, f"validate() should return False for: {description}"

    @pytest.mark.slow
    def test_final_envelope_size_security_check(self):
        """CRITICAL: ByteStorage must check final envelope size after serialization.

        Since cachekit-core 0.4.0 the envelope encodes compressed_data as msgpack
        bin (~1.004x payload for incompressible input, protocol 1.1), so the input
        must sit just under the 512MiB input cap for the LZ4 incompressible-block
        overhead to push the final envelope over the limit. (Pre-0.4.0, array-of-ints
        inflation tripped this at 500MB.)
        """
        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")

        # Incompressible input just under the 512MiB input cap: passes the input
        # check, but the final envelope (input * ~1.0039 + header) exceeds the cap.
        import random

        random.seed(42)
        # Chunked: randbytes() overflows past 256MiB (getrandbits takes a C int).
        near_limit_data = b"".join(random.randbytes(128 * 1024 * 1024) for _ in range(4))[:-16]

        with pytest.raises(ValueError) as exc_info:
            storage.store(near_limit_data, None)

        error_msg = str(exc_info.value).lower()
        assert "exceeds maximum size" in error_msg or "too large" in error_msg


class TestByteStorageBufferProtocol:
    """retrieve() accepts the buffer protocol (LAB-770) — no bytes() coercion needed.

    The zero-copy read path hands retrieve() a memoryview (SerializationWrapper.unwrap
    slices one past the frame header); the PyO3 boundary must take it directly, plus
    fall back to a copy for writable or non-contiguous exporters.
    """

    def test_retrieve_accepts_readonly_memoryview(self):
        """Zero-copy path: memoryview over bytes round-trips identically to bytes."""
        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")
        payload = b"buffer-protocol-roundtrip" * 1000
        envelope = storage.store(payload, None)

        data, fmt = storage.retrieve(memoryview(envelope))
        assert data == payload
        assert fmt == "msgpack"

    def test_retrieve_accepts_offset_memoryview(self):
        """The exact shape unwrap produces: a view sliced past a frame prefix."""
        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")
        payload = b"offset-view" * 500
        envelope = storage.store(payload, None)

        framed = b"JUNKHDR" + envelope
        data, _ = storage.retrieve(memoryview(framed)[7:])
        assert data == payload

    def test_retrieve_accepts_writable_buffer(self):
        """Copy-fallback path: bytearray (writable exporter) still round-trips."""
        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")
        payload = b"writable-exporter" * 500
        envelope = storage.store(payload, None)

        data, _ = storage.retrieve(bytearray(envelope))
        assert data == payload
        data, _ = storage.retrieve(memoryview(bytearray(envelope)))
        assert data == payload

    def test_retrieve_readonly_view_over_mutable_exporter_round_trips(self):
        """A readonly VIEW whose backing storage is still mutable must not be borrowed
        across the GIL release (data race). It takes the copy path — readonly() alone
        is not the zero-copy gate; the exporter must be immutable bytes."""
        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")
        payload = b"readonly-view-mutable-backing" * 500
        envelope = storage.store(payload, None)

        ro_view = memoryview(bytearray(envelope)).toreadonly()
        assert ro_view.readonly
        data, _ = storage.retrieve(ro_view)
        assert data == payload

    def test_retrieve_spoofed_obj_attribute_round_trips(self):
        """A PEP 688 exporter with a decoy bytes `.obj` attribute must not trick the
        zero-copy gate: the pointer-range proof sees its memory is NOT inside the
        decoy bytes and takes the copy path. Round-trip stays correct."""
        import sys

        if sys.version_info < (3, 12):
            pytest.skip("__buffer__ protocol requires Python 3.12+")
        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")
        payload = b"spoofed-exporter" * 500
        envelope = storage.store(payload, None)

        class Spoof:
            obj = b"decoy-bytes-not-the-buffer"

            def __init__(self, backing: bytearray) -> None:
                self.backing = backing

            def __buffer__(self, flags: int) -> memoryview:
                return memoryview(self.backing).toreadonly()

        data, _ = storage.retrieve(Spoof(bytearray(envelope)))
        assert data == payload

    def test_retrieve_accepts_non_contiguous_view(self):
        """Copy-fallback path: a strided view is copied, not misread."""
        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")
        payload = b"strided-view" * 500
        envelope = storage.store(payload, None)

        interleaved = bytes(b for byte in envelope for b in (byte, 0xFF))
        data, _ = storage.retrieve(memoryview(interleaved)[::2])
        assert data == payload

    def test_retrieve_rejects_corrupt_memoryview(self):
        """Error semantics are unchanged for buffer-protocol inputs."""
        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")
        with pytest.raises(ValueError):
            storage.retrieve(memoryview(b"not an envelope"))

    def test_retrieve_memoryview_is_zero_copy(self):
        """No PYTHON-side full-envelope copy on the memoryview path (LAB-770).

        tracemalloc sees only Python-heap allocations: retrieve's output bytes (~1x
        payload for incompressible input). A bytes(data)-style coercion creeping back
        in front of retrieve adds another ~1x and fails here. Known blind spot: a
        Rust-side copy (to_vec) is invisible to tracemalloc (measured: borrow and
        copy paths both read 1.00x), so the borrow itself is pinned by review of
        retrieve() in rust/src/python_bindings.rs, not by this suite.
        """
        import gc
        import os
        import tracemalloc

        from cachekit._rust_serializer import ByteStorage

        storage = ByteStorage("msgpack")
        payload = os.urandom(8 * 1024 * 1024)  # incompressible: envelope ~= payload
        envelope = storage.store(payload, None)
        view = memoryview(envelope)

        gc.collect()
        tracemalloc.start()
        data, _ = storage.retrieve(view)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()

        assert data == payload
        assert peak / len(payload) < 1.5, (
            f"retrieve(memoryview) peak {peak / len(payload):.2f}x payload — the zero-copy borrow "
            f"regressed to a full envelope copy (expected ~1x: just the output bytes)"
        )

    def test_standard_serializer_deserialize_memoryview(self):
        """End-to-end: deserialize() takes unwrap's memoryview without re-coercing."""
        from cachekit.serializers.standard_serializer import StandardSerializer

        serializer = StandardSerializer()
        obj = {"key": [1, 2, 3], "blob": b"x" * 4096}
        data, _ = serializer.serialize(obj)

        assert serializer.deserialize(memoryview(data)) == obj

    def test_standard_serializer_deserialize_non_u8_buffer_raises_serialization_error(self):
        """A non-u8 exporter (rejected as BufferError at the PyO3 boundary) keeps the
        documented SerializationError contract — pre-LAB-770 the bytes() coercion
        surfaced these as ValueError -> SerializationError."""
        np = pytest.importorskip("numpy")
        from cachekit.serializers.base import SerializationError
        from cachekit.serializers.standard_serializer import StandardSerializer

        with pytest.raises(SerializationError):
            StandardSerializer().deserialize(np.zeros(4))
