"""Unit tests for SerializationWrapper binary-frame envelope.

The wrapper frames serializer output for cache storage. It MUST:
- avoid base64 (which inflated binary payloads 1.33x and forced ~4 full copies),
- round-trip arbitrary binary payloads (including non-UTF-8 bytes),
- remain backward-compatible on read with the legacy base64+JSON envelope,
so already-stored cache entries stay readable across the upgrade.
"""

from __future__ import annotations

import base64
import json

import pytest

from cachekit.serializers.wrapper import SerializationWrapper

PAYLOAD = b"\x00\x01\xff\xfe\x00ARROW1\x00\x00binary-not-text\x80\x81"
META = {"format": "arrow", "compressed": True, "original_type": "arrow"}


class TestBinaryFrame:
    def test_roundtrip_returns_payload_metadata_serializer(self):
        wrapped = SerializationWrapper.wrap(PAYLOAD, META, "arrow")
        data, meta, name = SerializationWrapper.unwrap(wrapped)
        assert data == PAYLOAD
        assert meta == META
        assert name == "arrow"

    def test_output_is_bytes(self):
        assert isinstance(SerializationWrapper.wrap(PAYLOAD, META, "arrow"), bytes)

    def test_payload_is_not_base64_encoded(self):
        """The raw payload bytes must appear verbatim in the frame (no base64)."""
        wrapped = SerializationWrapper.wrap(PAYLOAD, META, "default")
        assert PAYLOAD in wrapped
        # base64 of the payload must NOT be present (proves we dropped base64)
        assert base64.b64encode(PAYLOAD) not in wrapped

    def test_no_size_inflation(self):
        """Frame overhead is a small fixed header, not base64's 1.33x."""
        big = b"\x07" * 1_000_000
        wrapped = SerializationWrapper.wrap(big, META, "arrow")
        # < 1KB of framing overhead; nowhere near base64's +333KB
        assert len(wrapped) - len(big) < 1024

    def test_non_utf8_payload_roundtrips(self):
        """Binary payloads that are not valid UTF-8 must survive unwrap (no decode)."""
        evil = bytes(range(256)) * 10
        data, _, _ = SerializationWrapper.unwrap(SerializationWrapper.wrap(evil, {}, "default"))
        assert data == evil

    def test_empty_payload_roundtrips(self):
        data, meta, name = SerializationWrapper.unwrap(SerializationWrapper.wrap(b"", {"format": "msgpack"}, "default"))
        assert data == b""
        assert name == "default"

    def test_metadata_with_encryption_fields_roundtrips(self):
        enc_meta = {"format": "msgpack", "encrypted": True, "tenant_id": "acme", "key_fingerprint": "abc123"}
        _, meta, _ = SerializationWrapper.unwrap(SerializationWrapper.wrap(b"cipher", enc_meta, "default"))
        assert meta == enc_meta


class TestLegacyBackwardCompat:
    """Old base64+JSON entries (written before this change) must still deserialize."""

    @staticmethod
    def _legacy_wrap(data: bytes, metadata: dict, serializer_name: str, version: str = "2.0") -> bytes:
        wrapper = {
            "data": base64.b64encode(data).decode("ascii"),
            "metadata": metadata,
            "serializer": serializer_name,
            "version": version,
        }
        return json.dumps(wrapper, ensure_ascii=False).encode("utf-8")

    def test_unwrap_reads_legacy_bytes_envelope(self):
        legacy = self._legacy_wrap(PAYLOAD, META, "arrow")
        data, meta, name = SerializationWrapper.unwrap(legacy)
        assert data == PAYLOAD
        assert meta == META
        assert name == "arrow"

    def test_unwrap_reads_legacy_str_envelope(self):
        """Some backends hand back str; legacy JSON must still decode from str."""
        legacy = self._legacy_wrap(PAYLOAD, META, "arrow").decode("utf-8")
        data, _, name = SerializationWrapper.unwrap(legacy)
        assert data == PAYLOAD
        assert name == "arrow"

    def test_new_and_legacy_are_distinguishable(self):
        """New frame starts with magic; legacy JSON starts with '{'. Sniffing is unambiguous."""
        new = SerializationWrapper.wrap(PAYLOAD, META, "arrow")
        legacy = self._legacy_wrap(PAYLOAD, META, "arrow")
        assert new[:1] != b"{"
        assert legacy[:1] == b"{"


class TestUnwrapRejectsGarbage:
    def test_unrecognized_envelope_raises(self):
        with pytest.raises((ValueError, Exception)):
            SerializationWrapper.unwrap(b"\x99\x98 not a frame and not json")
