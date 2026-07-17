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

        The Arrow/orjson serializers currently compute envelopes via
        xxhash.xxh3_64_digest; this proves the FFI is a drop-in producer of
        the same wire bytes.
        """
        for data in (b"", b"cachekit-kat", b"payload", bytes(range(256)) * 41):
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
