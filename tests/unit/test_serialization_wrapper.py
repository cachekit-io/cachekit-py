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


class TestZeroCopyRead:
    """#162: unwrap must return the v3 payload as a zero-copy VIEW of the input frame, not a
    fresh ``bytes`` copy. The full-payload copy on every read (incl. every L1 hit) was the
    read-RSS regression; for a 300MB Arrow frame it doubled peak. A memoryview slice past the
    frame header flows zero-copy into ``pa.py_buffer`` (and, later, the mmap read path)."""

    def test_v3_payload_is_a_memoryview(self):
        data, _, _ = SerializationWrapper.unwrap(SerializationWrapper.wrap(PAYLOAD, META, "arrow"))
        assert isinstance(data, memoryview)

    def test_v3_payload_aliases_input_no_copy(self):
        """Mutating the source frame shows through the returned payload => no copy was made.
        (A bytes copy would not reflect the mutation.)"""
        frame = bytearray(SerializationWrapper.wrap(b"PAYLOAD!!!", META, "arrow"))
        data, _, _ = SerializationWrapper.unwrap(frame)
        assert bytes(data) == b"PAYLOAD!!!"  # correct slice past the header
        hdr_len = int.from_bytes(bytes(frame[3:7]), "big")
        payload_off = 7 + hdr_len  # MAGIC(2)+ver(1)+hdrlen(4)+header
        frame[payload_off] ^= 0xFF  # mutate the SOURCE buffer in place
        assert data[0] == frame[payload_off]  # view reflects it; a copy would not


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

    def test_truncated_frame_raises_valueerror(self):
        # Starts with the CK magic but is shorter than the 7-byte prefix.
        with pytest.raises(ValueError, match="Truncated cache envelope frame"):
            SerializationWrapper.unwrap(b"CK\x03")

    def test_unsupported_frame_version_raises_valueerror(self):
        # Valid prefix length but an unknown frame version byte.
        frame = b"CK" + bytes((99,)) + (2).to_bytes(4, "big") + b"{}"
        with pytest.raises(ValueError, match="Unsupported cache envelope frame version"):
            SerializationWrapper.unwrap(frame)

    def test_header_length_exceeds_frame_raises_valueerror(self):
        # Declares a header far larger than the actual frame body.
        frame = b"CK" + bytes((3,)) + (9999).to_bytes(4, "big") + b"{}"
        with pytest.raises(ValueError, match="Invalid cache envelope header length"):
            SerializationWrapper.unwrap(frame)

    def test_frame_without_serializer_name_raises_valueerror(self):
        # LAB-4432: the protocol treats a nameless value as a mismatch; never default a name.
        header = b'{"m":{},"v":"2.0"}'
        frame = b"CK" + bytes((3,)) + len(header).to_bytes(4, "big") + header + b"payload"
        with pytest.raises(ValueError, match="records no serializer name"):
            SerializationWrapper.unwrap(frame)

    @pytest.mark.parametrize("name", [None, "", 0])
    def test_frame_with_non_name_serializer_raises_valueerror(self, name):
        header = json.dumps({"m": {}, "s": name, "v": "2.0"}).encode("utf-8")
        frame = b"CK" + bytes((3,)) + len(header).to_bytes(4, "big") + header + b"payload"
        with pytest.raises(ValueError, match="records no serializer name"):
            SerializationWrapper.unwrap(frame)

    def test_legacy_envelope_without_serializer_name_raises_valueerror(self):
        legacy = json.dumps({"data": base64.b64encode(PAYLOAD).decode("ascii"), "metadata": META, "version": "2.0"})
        with pytest.raises(ValueError, match="records no serializer name"):
            SerializationWrapper.unwrap(legacy)


class TestEncryptionThroughFrame:
    """The binary frame is on the hot path for @cache.secure too: encrypted payloads and
    their encryption metadata must survive the frame, AAD binding must still hold, and old
    base64+JSON encrypted entries must still decrypt. (Regression for the wrapper rewrite.)"""

    KEY = "user:42:credentials"

    @pytest.fixture
    def enc_handler(self, monkeypatch):
        from cachekit.config.singleton import reset_settings

        reset_settings()
        # monkeypatch.setenv restores any pre-existing CACHEKIT_MASTER_KEY on teardown
        # (and unsets it if it was absent), avoiding cross-test process-env leakage.
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", "a" * 64)
        from cachekit.cache_handler import CacheSerializationHandler

        handler = CacheSerializationHandler(
            serializer_name="default",
            encryption=True,
            single_tenant_mode=True,
            deployment_uuid="00000000-0000-0000-0000-000000000001",
        )
        yield handler
        reset_settings()

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


def _frame(header: bytes, payload: bytes = b"p") -> bytes:
    return b"CK\x03" + len(header).to_bytes(4, "big") + header + payload


class TestHeaderMemo:
    """unwrap_metadata parses each distinct header once (LAB-7069). The key is untrusted bytes from
    the backend, so only validated parses are kept, the memo is bounded, and every check the read
    path runs on the parsed metadata still runs on every read."""

    @pytest.fixture(autouse=True)
    def _fresh_memo(self):
        from cachekit.serializers import wrapper

        wrapper._parse_header_memo.cache_clear()
        wrapper._encode_prefix_memo.cache_clear()

    def test_repeat_reads_share_one_read_only_parse(self):
        frame = SerializationWrapper.wrap(PAYLOAD, {"format": "msgpack", "compressed": True}, "default")
        p1, m1, n1 = SerializationWrapper.unwrap_metadata(frame)
        p2, m2, n2 = SerializationWrapper.unwrap_metadata(frame)
        assert m1 is m2 and (n1, n2) == ("default", "default") and bytes(p1) == bytes(p2) == PAYLOAD
        assert m1.compressed is True and m1.format.value == "msgpack"
        with pytest.raises(AttributeError, match="read-only"):
            m1.compressed = False
        with pytest.raises(AttributeError, match="read-only"):
            del m1.encrypted

    def test_unwrap_returns_a_fresh_dict_the_memo_never_sees(self):
        frame = SerializationWrapper.wrap(PAYLOAD, {"format": "msgpack", "encrypted": True, "tenant_id": "t"}, "default")
        SerializationWrapper.unwrap_metadata(frame)
        _, meta, _ = SerializationWrapper.unwrap(frame)
        meta["encrypted"] = False
        assert SerializationWrapper.unwrap_metadata(frame)[1].encrypted is True

    @pytest.mark.parametrize(
        "header",
        [
            b'{"m": {"format": "msgpack"}, "v": "2.0"}',  # no serializer name
            b'{"s": "", "m": {"format": "msgpack"}}',
            b'{"s": "default", "m": {"format": "pickle"}}',  # not a SerializationFormat
            b'{"s": "default", "m": {}}',  # no format
            b'["s", "default"]',
            b"{not json",
        ],
        ids=["no-name", "empty-name", "bad-format", "no-format", "not-an-object", "not-json"],
    )
    def test_a_header_that_fails_validation_is_never_kept(self, header):
        from cachekit.serializers import wrapper

        for _ in range(2):
            with pytest.raises((AttributeError, KeyError, TypeError, ValueError)):
                SerializationWrapper.unwrap_metadata(_frame(header))
        assert wrapper._parse_header_memo.cache_info().currsize == 0

    def test_a_header_past_the_cap_is_parsed_but_not_kept(self):
        from cachekit.serializers import wrapper

        meta = {"format": "msgpack", "original_type": "x" * 600}
        frame = SerializationWrapper.wrap(PAYLOAD, meta, "default")
        _, parsed, _ = SerializationWrapper.unwrap_metadata(frame)
        assert parsed.original_type == "x" * 600
        assert wrapper._parse_header_memo.cache_info().currsize == 0

    def test_the_memo_stays_bounded_when_headers_vary_per_tenant(self):
        from cachekit.serializers import wrapper

        for i in range(1000):
            meta = {"format": "msgpack", "encrypted": True, "tenant_id": f"tenant-{i}"}
            assert (
                SerializationWrapper.unwrap_metadata(SerializationWrapper.wrap(b"", meta, "default"))[1].tenant_id
                == f"tenant-{i}"
            )
        assert wrapper._parse_header_memo.cache_info().currsize == wrapper._MEMO_ENTRIES
        assert wrapper._encode_prefix_memo.cache_info().currsize == wrapper._MEMO_ENTRIES

    def test_a_memoized_plaintext_header_still_trips_the_downgrade_guard(self, monkeypatch):
        """CWE-757: the guard runs on the parsed metadata, so a cached parse cannot skip it."""
        from cachekit.cache_handler import CacheSerializationHandler
        from cachekit.config.singleton import reset_settings
        from cachekit.serializers.base import SuspiciousCacheEntryError

        plain = CacheSerializationHandler(serializer_name="default", encryption=False)
        blob = plain.serialize_data({"k": "v"}, cache_key="k")
        assert plain.deserialize_data(blob, cache_key="k") == {"k": "v"}  # fills the memo
        reset_settings()
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", "a" * 64)
        enc = CacheSerializationHandler(serializer_name="default", encryption=True, single_tenant_mode=True)
        for _ in range(2):
            with pytest.raises(SuspiciousCacheEntryError):
                enc.deserialize_data(blob, cache_key="k")
        reset_settings()

    @pytest.mark.parametrize(
        "meta",
        [
            {"format": "msgpack", "compressed": True},
            {"format": "msgpack", "compressed": 1},  # == True: must not share True's bytes
            {"format": "msgpack", "n": 0.0},
            {"format": "msgpack", "n": -0.0},  # == 0.0, renders differently: not memoized
            {"format": "msgpack", "nested": {"a": [1]}},  # unhashable: not memoized
            {"format": "msgpack", "tenant_id": "ü"},
            {"format": "arrow", "compressed": None},
        ],
    )
    def test_the_write_prefix_is_byte_identical_to_json(self, meta):
        expected_header = json.dumps({"s": "default", "m": meta, "v": "2.0"}, ensure_ascii=False).encode("utf-8")
        expected = b"CK\x03" + len(expected_header).to_bytes(4, "big") + expected_header
        SerializationWrapper.wrap_prefix({"format": "msgpack", "compressed": True}, "default")  # a True entry first
        SerializationWrapper.wrap_prefix({"format": "msgpack", "n": 0.0}, "default")
        assert SerializationWrapper.wrap_prefix(meta, "default") == expected
        assert SerializationWrapper.wrap_prefix(meta, "default") == expected
