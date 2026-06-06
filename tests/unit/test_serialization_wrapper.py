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

from cachekit.serializers.base import SerializationError
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


class TestEncryptionThroughFrame:
    """The binary frame is on the hot path for @cache.secure too: encrypted payloads and
    their encryption metadata must survive the frame, AAD binding must still hold, and old
    base64+JSON encrypted entries must still decrypt. (Regression for the wrapper rewrite.)"""

    KEY = "user:42:credentials"

    @pytest.fixture
    def enc_handler(self):
        import os

        from cachekit.config.singleton import reset_settings

        reset_settings()
        os.environ["CACHEKIT_MASTER_KEY"] = "a" * 64
        from cachekit.cache_handler import CacheSerializationHandler

        handler = CacheSerializationHandler(
            serializer_name="default",
            encryption=True,
            single_tenant_mode=True,
            deployment_uuid="00000000-0000-0000-0000-000000000001",
        )
        yield handler
        reset_settings()
        os.environ.pop("CACHEKIT_MASTER_KEY", None)

    def test_encrypted_payload_round_trips_through_frame(self, enc_handler):
        secret = {"ssn": "123-45-6789", "balance": 99999}
        blob = enc_handler.serialize_data(secret, cache_key=self.KEY)
        assert blob[:2] == b"CK"  # new binary frame
        assert b"123-45-6789" not in blob  # plaintext never present
        assert enc_handler.deserialize_data(blob, cache_key=self.KEY) == secret

    def test_encryption_metadata_survives_frame_header(self, enc_handler):
        blob = enc_handler.serialize_data({"k": "v"}, cache_key=self.KEY)
        _, meta, _ = SerializationWrapper.unwrap(blob)
        assert meta["encrypted"] is True
        assert meta["tenant_id"]
        assert meta["encryption_algorithm"] == "AES-256-GCM"

    def test_wrong_cache_key_is_rejected(self, enc_handler):
        """AAD binding: ciphertext is bound to the cache key; a mismatched key must not decrypt."""
        blob = enc_handler.serialize_data({"k": "v"}, cache_key=self.KEY)
        # EncryptionError subclasses SerializationError; AAD mismatch must raise, never silently succeed.
        with pytest.raises(SerializationError):
            enc_handler.deserialize_data(blob, cache_key="WRONG:key")

    def test_legacy_base64_json_encrypted_entry_still_decrypts(self, enc_handler):
        """A pre-upgrade encrypted entry (base64+JSON envelope) must remain readable."""
        new_blob = enc_handler.serialize_data({"old": "secret"}, cache_key=self.KEY)
        inner, meta, name = SerializationWrapper.unwrap(new_blob)
        legacy = json.dumps(
            {"data": base64.b64encode(inner).decode("ascii"), "metadata": meta, "serializer": name, "version": "2.0"}
        ).encode("utf-8")
        assert enc_handler.deserialize_data(legacy, cache_key=self.KEY) == {"old": "secret"}
