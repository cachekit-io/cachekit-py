"""CK v3 storage frame against the protocol python-frame vectors.

Fixture: tests/unit/protocol/fixtures/python-frame.json, vendored from
cachekit-io/protocol @ 4b8fddb2120b9e3355d7fc9d593130dba8a345ac (the file carries no
version field; sha256 1210a2cdf00ef420e59d4d1c75f4979385ad1cb023181b39f46d7f994761770a).
Regenerate ONLY by re-copying from the protocol repo — never by hand.

spec/wire-format.md, "CK v3 frame". Through the real read paths, this module proves that:

1. **Frames read back**: each ``frame_vectors`` frame unwraps to the header and payload it
   pins, the writer reproduces it byte for byte, and a default write reads back to its value.
2. **Malformed frames are rejected at the check they break**: each error vector's
   ``rejected_by`` names the frame check (magic, the 7-byte prefix, version, header length),
   and the cache read path refuses it with that check's own error. A frame without the CK
   magic is not a frame to cachekit-py, so it falls to the legacy base64+JSON reader, which
   refuses it as non-UTF-8. A CK frame fed to the interop reader gets its CK diagnostic.
3. **Encrypted caches fail closed**: a reader configured for encryption, resolving its own
   tenant, refuses every ``encrypted_read_vectors`` frame under both tamper policies,
   whatever the frame's header claims. A positive control reads the reader's own entry, so
   a refusal is never a broken reader.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from cachekit.cache_handler import CacheSerializationHandler
from cachekit.decorators.tenant_context import CallableExtractor
from cachekit.interop import InteropDecodeError, decode_interop_value
from cachekit.serializers.base import SerializationError
from cachekit.serializers.wrapper import SerializationWrapper

pytestmark = pytest.mark.unit

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "python-frame.json"
FIXTURE_SHA256 = "1210a2cdf00ef420e59d4d1c75f4979385ad1cb023181b39f46d7f994761770a"  # pragma: allowlist secret

_FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
FRAME_VECTORS: list[dict[str, Any]] = _FIXTURE["frame_vectors"]
ERROR_VECTORS: list[dict[str, Any]] = [v for v in _FIXTURE["error_vectors"] if "rejected_by" in v]
ENCRYPTED_READER: dict[str, str] = _FIXTURE["encrypted_reader"]
ENCRYPTED_READ_VECTORS: list[dict[str, Any]] = _FIXTURE["encrypted_read_vectors"]
CACHE_KEY = ENCRYPTED_READER["cache_key"]

# The frame parser's own error for each check (src/cachekit/serializers/wrapper.py, _split_frame).
# "magic" has none: a non-frame goes to the legacy reader, whose UTF-8 decode refuses it.
FRAME_CHECK_ERRORS = {
    "prefix_length": "Truncated cache envelope frame",
    "version": "Unsupported cache envelope frame version",
    "header_length": "Invalid cache envelope header length",
}


def _encrypted_reader(*, fail_closed: bool) -> CacheSerializationHandler:
    """The fixture's encrypted reader: multi-tenant, so it resolves its tenant itself, never from the header."""
    return CacheSerializationHandler(
        "default",
        encryption=True,
        master_key=ENCRYPTED_READER["master_key_hex"],
        encryption_fail_closed=fail_closed,
        tenant_extractor=CallableExtractor(lambda *_a, **_k: ENCRYPTED_READER["tenant_id"]),
    )


def test_fixture_integrity() -> None:
    """The vendored fixture is byte-identical to the pinned protocol revision."""
    digest = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    assert digest == FIXTURE_SHA256, (
        f"fixtures/python-frame.json sha256 {digest} != pinned {FIXTURE_SHA256}. "
        "If the protocol vectors were intentionally updated, refresh the pin AND the names."
    )


def test_vector_names() -> None:
    """A fixture update that adds, drops or re-classifies a vector must be a conscious change."""
    assert [v["name"] for v in FRAME_VECTORS] == [
        "raw_payload_frame",
        "default_saas_write_msgpack_bytestorage",
        "arrow_dataframe_write",
        "default_saas_write_msgpack_bytestorage_bin",
    ]
    assert {v["name"]: v.get("rejected_by") for v in _FIXTURE["error_vectors"]} == {
        "truncated_frame": "prefix_length",
        "truncated_frame_one_short": "prefix_length",
        "unsupported_frame_version": "version",
        "unsupported_frame_version_2": "version",
        "unsupported_frame_version_4": "version",
        "header_overrun": "header_length",
        "header_overrun_by_one": "header_length",
        "bare_envelope_fed_to_frame_reader": "magic",
        "plain_msgpack_fed_to_frame_reader": "magic",
        "ck_frame_fed_to_interop_reader": None,
    }
    assert [v["name"] for v in ENCRYPTED_READ_VECTORS] == [
        "forged_plaintext_encrypted_false",
        "forged_plaintext_orjson",
        "ciphertext_key_not_in_keyring",
        "ciphertext_other_tenant",
    ]
    assert ENCRYPTED_READER["tenant_source"] == "reader"


class TestFrameVectors:
    """Each pinned frame reads back to the header and payload it pins."""

    @pytest.mark.parametrize("vector", FRAME_VECTORS + ENCRYPTED_READ_VECTORS, ids=lambda v: v["name"])
    def test_unwrap_reads_pinned_header_and_payload(self, vector: dict[str, Any]) -> None:
        frame = bytes.fromhex(vector["frame_hex"])
        header = vector["expected_header"]

        payload, metadata, serializer_name = SerializationWrapper.unwrap(frame)

        assert (serializer_name, metadata) == (header["s"], header["m"])
        if "expected_payload_hex" in vector:
            assert bytes(payload).hex() == vector["expected_payload_hex"]
        # The header's "v" is not returned by unwrap, so the writer proves it: same header, same bytes.
        assert SerializationWrapper.wrap(bytes(payload), header["m"], header["s"], header["v"]) == frame

    def test_arrow_frame_payload_structure(self) -> None:
        """Arrow IPC bytes are not canonical across pyarrow versions; the spec pins only the structure."""
        (vector,) = [v for v in FRAME_VECTORS if "arrow_detection" in v]
        detection = vector["arrow_detection"]
        payload = bytes(SerializationWrapper.unwrap(bytes.fromhex(vector["frame_hex"]))[0])
        assert payload[: detection["checksum_len"]].hex() == detection["checksum_hex"]
        magic_at = detection["ipc_magic_offset"]
        assert payload[magic_at : magic_at + len(detection["ipc_magic"])] == detection["ipc_magic"].encode("ascii")

    @pytest.mark.parametrize("vector", [v for v in FRAME_VECTORS if "value_json" in v], ids=lambda v: v["name"])
    def test_default_write_reads_back_to_value(self, vector: dict[str, Any]) -> None:
        handler = CacheSerializationHandler(serializer_name="default")
        assert handler.deserialize_data(bytes.fromhex(vector["frame_hex"]), cache_key=CACHE_KEY) == vector["value_json"]


class TestErrorVectors:
    """Each malformed frame is refused by the cache read path at the check its ``rejected_by`` names."""

    @pytest.mark.parametrize("vector", ERROR_VECTORS, ids=lambda v: v["name"])
    def test_read_path_rejects_at_named_check(self, vector: dict[str, Any]) -> None:
        handler = CacheSerializationHandler(serializer_name="default")
        with pytest.raises(SerializationError, match="^Corrupt cache envelope: ") as excinfo:
            handler.deserialize_data(bytes.fromhex(vector["frame_hex"]), cache_key=CACHE_KEY)

        cause = excinfo.value.__cause__
        if vector["rejected_by"] == "magic":
            assert type(cause) is UnicodeDecodeError  # the legacy reader's refusal, not a frame check's
        else:
            assert type(cause) is ValueError
            assert str(cause).startswith(FRAME_CHECK_ERRORS[vector["rejected_by"]])

    def test_ck_frame_rejected_by_interop_reader(self) -> None:
        (vector,) = [v for v in _FIXTURE["error_vectors"] if v["name"] == "ck_frame_fed_to_interop_reader"]
        with pytest.raises(InteropDecodeError, match=r"Python-SDK-internal auto-mode entry \(CK v3 frame\)"):
            decode_interop_value(bytes.fromhex(vector["frame_hex"]))


@pytest.mark.parametrize("fail_closed", [False, True], ids=["fail_closed_off", "fail_closed_on"])
class TestEncryptedReadVectors:
    """A cache configured for encryption never returns a value it cannot authenticate as its own ciphertext."""

    def test_reader_reads_its_own_entry(self, fail_closed: bool) -> None:
        """Positive control: the refusals below are not a reader that refuses everything."""
        reader = _encrypted_reader(fail_closed=fail_closed)
        value = {"user_id": 42, "name": "cachekit", "active": True}
        assert reader.deserialize_data(reader.serialize_data(value, cache_key=CACHE_KEY), cache_key=CACHE_KEY) == value

    @pytest.mark.parametrize("vector", ENCRYPTED_READ_VECTORS, ids=lambda v: v["name"])
    def test_read_fails_closed(self, fail_closed: bool, vector: dict[str, Any]) -> None:
        assert vector["outcome"] == "fail_closed"
        reader = _encrypted_reader(fail_closed=fail_closed)
        # Every read-path refusal derives from SerializationError; the forged orjson frame may be refused
        # for its serializer name or its plaintext claim, whichever the reader checks first.
        with pytest.raises(SerializationError):
            reader.deserialize_data(bytes.fromhex(vector["frame_hex"]), cache_key=CACHE_KEY)
