"""Unit tests for the standalone checksum FFI (cachekit-core#13, Phase 2).

The Rust core exposes checksum/verify_checksum decoupled from LZ4 compression
(cachekit-core 0.3.0). These bindings mirror them so every serializer can get
the canonical xxHash3-64 wire value without paying for compression.

Byte-verification: the KAT constants below are pinned in cachekit-core's own
test suite (src/checksum.rs) and must match the pure-Python xxhash package —
three independent implementations agreeing on the exact wire bytes.

Note: NON-cryptographic. Detects corruption, not tampering. Tamper-resistance
comes from AES-256-GCM (@cache.secure), never from this checksum.
"""

from __future__ import annotations

import pytest
import xxhash

from cachekit import _rust_serializer as rs

# Protocol test vectors — pinned in cachekit-core src/checksum.rs (KAT tests).
# Big-endian = xxhash canonical byte order, identical to the value embedded in
# every StorageEnvelope.
KAT_CACHEKIT = bytes([209, 35, 204, 155, 190, 157, 164, 177])  # checksum(b"cachekit-kat")
KAT_EMPTY = bytes([0x2D, 0x06, 0x80, 0x05, 0x38, 0xD3, 0x94, 0xC2])  # checksum(b"")


class TestChecksumFFI:
    def test_checksum_is_8_bytes_and_deterministic(self):
        c = rs.checksum(b"payload")
        assert isinstance(c, bytes)
        assert len(c) == 8
        assert rs.checksum(b"payload") == c

    def test_checksum_matches_protocol_test_vectors(self):
        """Byte-verified against the KAT vectors pinned in cachekit-core."""
        assert rs.checksum(b"cachekit-kat") == KAT_CACHEKIT
        assert rs.checksum(b"") == KAT_EMPTY

    def test_checksum_matches_python_xxhash_package(self):
        """FFI and the pure-Python xxhash package must agree byte-for-byte.

        The Arrow/orjson serializers compute envelopes via xxhash.xxh3_64_digest;
        this proves the FFI is a drop-in producer of the same wire bytes across
        xxHash3's size-dependent code paths — short, the 17-240 B mid path, and
        the >64 KB accumulator-merge path (all previously unchecked above ~10 KB,
        where an xxhash-rust vs libxxhash divergence would ship silently).
        """
        payloads = (
            b"",
            b"cachekit-kat",
            b"payload",
            bytes(range(200)),  # 200 B — xxHash3 mid-size path
            bytes(range(256)) * 41,  # ~10 KB
            bytes(i % 251 for i in range(65_536 + 7)),  # > 64 KB — accumulator merge
            bytes(i % 251 for i in range(1_048_576)),  # 1 MB
        )
        for data in payloads:
            assert rs.checksum(data) == xxhash.xxh3_64_digest(data)


class TestVerifyChecksumFFI:
    def test_verify_round_trips(self):
        assert rs.verify_checksum(b"payload", rs.checksum(b"payload")) is True
        assert rs.verify_checksum(b"tampered", rs.checksum(b"payload")) is False

    def test_verify_rejects_single_bit_flip(self):
        corrupted = bytearray(rs.checksum(b"payload"))
        corrupted[0] ^= 0x01
        assert rs.verify_checksum(b"payload", bytes(corrupted)) is False

    @pytest.mark.parametrize("bad_len", [0, 7, 9, 32])
    def test_verify_rejects_wrong_length_expected(self, bad_len):
        """expected must be exactly 8 bytes; anything else raises, never lies."""
        with pytest.raises(ValueError, match="8 bytes"):
            rs.verify_checksum(b"payload", b"\x00" * bad_len)


class TestBufferProtocol:
    """The FFI must accept any buffer-protocol object, not only `bytes`.

    The Arrow serializer hashes a `memoryview` on both write
    (arrow_serializer.py:245) and verify (`body = mv[8:]`, :283). A bytes-only
    signature would raise TypeError the moment a serializer migrates onto this
    FFI — while a bytes-only test suite stayed green. These tests lock the
    buffer-protocol contract that makes that migration safe.
    """

    PAYLOAD = b"cachekit-kat payload of some length \x00\xff\x7f"

    @pytest.mark.parametrize("wrap", [bytes, bytearray, memoryview], ids=["bytes", "bytearray", "memoryview"])
    def test_checksum_accepts_buffer_types(self, wrap):
        assert rs.checksum(wrap(self.PAYLOAD)) == xxhash.xxh3_64_digest(self.PAYLOAD)

    @pytest.mark.parametrize("wrap", [bytes, bytearray, memoryview], ids=["bytes", "bytearray", "memoryview"])
    def test_verify_accepts_buffer_types(self, wrap):
        digest = rs.checksum(self.PAYLOAD)
        assert rs.verify_checksum(wrap(self.PAYLOAD), digest) is True
        assert rs.verify_checksum(wrap(self.PAYLOAD), wrap(digest)) is True

    def test_verify_accepts_memoryview_slice_like_arrow(self):
        """Mirror the Arrow verify path exactly: envelope = [8-byte checksum][body],
        then verify the body (a memoryview slice) against the sliced checksum."""
        body = self.PAYLOAD
        envelope = memoryview(rs.checksum(body) + body)
        assert rs.verify_checksum(envelope[8:], envelope[:8]) is True

        tampered = bytearray(envelope)
        tampered[-1] ^= 0x01  # corrupt the body, leave the checksum prefix intact
        mv = memoryview(tampered)
        assert rs.verify_checksum(mv[8:], mv[:8]) is False
