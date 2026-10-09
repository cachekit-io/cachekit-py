"""CK v3 storage frame against the protocol python-frame vectors.

Fixture: tests/unit/protocol/fixtures/python-frame.json, vendored from
cachekit-io/protocol @ b4ae567a402051752dabf040bd16e870657d3dec (the file carries no
version field; sha256 b677d5f14de4a3cd1fa5307d4b46eae0545a20e51e9163f96495ee7ad15600d0).
Regenerate ONLY by re-copying from the protocol repo — never by hand.

spec/wire-format.md, "CK v3 frame". Through the real read paths, this module proves that:

1. **Frames read back**: each ``frame_vectors`` frame unwraps to the header and payload it
   pins, and the frame writer reproduces it byte for byte from them.
2. **Writes are reproduced**: each write vector's value, written by a cache under the write's
   configuration, is stored as the pinned frame (an Arrow write: its header and envelope
   layout, since Arrow IPC bytes are not canonical), and a reader under that configuration
   reads the frame back to the value.
3. **A frame recording another serializer name is a miss**: planted at a cache's key, a frame
   whose recorded name is not the reader's canonical name, or that records none, is recomputed,
   not read. That covers the auto and ``StandardSerializer()`` writes and the alias recorded as
   given under a ``default`` reader, and the default write under an ``auto`` reader.
4. **Malformed frames are rejected at the check they break**: each error vector's
   ``rejected_by`` names the frame check (magic, the 7-byte prefix, version, header length,
   serializer name), and the cache read path refuses it with that check's own error. A frame without the CK
   magic is not a frame to cachekit-py, so it falls to the legacy base64+JSON reader, which
   refuses it as non-UTF-8. A CK frame fed to the interop reader gets its CK diagnostic.
5. **Encrypted caches fail closed**: a reader configured for encryption, resolving its own
   tenant, refuses every ``encrypted_read_vectors`` frame with ``encryption_fail_closed``
   off and on, each at the check that names it: a plaintext claim or a foreign serializer,
   a key fingerprint the reader lacks (refused before decrypting when the policy is on,
   by AES-GCM when it is off), a header tenant that is not the reader's, a decrypted
   plaintext that is not the ByteStorage envelope the reader is configured for (whatever
   the header's compressed flag says), or a serializer name that is not the reader's or
   is missing. The tenant check compares the header's tenant before any key derivation,
   so this module does not prove the tenant is bound into the key or the AAD. Three
   controls keep the refusals honest: the reader reads its own entry, a reader resolving
   the other tenant decrypts ``ciphertext_other_tenant``, and each serializer-name frame,
   re-recorded as ``default``, reads back to the value.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cachekit import cache
from cachekit.cache_handler import CacheSerializationHandler
from cachekit.decorators.tenant_context import CallableExtractor
from cachekit.interop import InteropDecodeError, decode_interop_value
from cachekit.serializers.base import SerializationError, SuspiciousCacheEntryError
from cachekit.serializers.encryption_wrapper import DecryptionAuthenticationError, EncryptionError, TenantMismatchError
from cachekit.serializers.standard_serializer import StandardSerializer
from cachekit.serializers.wrapper import SerializationWrapper
from tests.unit.protocol.test_cache_key_vectors import _KeyRecordingBackend

pytestmark = pytest.mark.unit

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "python-frame.json"
FIXTURE_SHA256 = "b677d5f14de4a3cd1fa5307d4b46eae0545a20e51e9163f96495ee7ad15600d0"  # pragma: allowlist secret

_FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
FRAME_VECTORS: list[dict[str, Any]] = _FIXTURE["frame_vectors"]
ERROR_VECTORS: list[dict[str, Any]] = [v for v in _FIXTURE["error_vectors"] if "rejected_by" in v]
ENCRYPTED_READER: dict[str, str] = _FIXTURE["encrypted_reader"]
ENCRYPTED_READ_VECTORS: list[dict[str, Any]] = _FIXTURE["encrypted_read_vectors"]
CACHE_KEY = ENCRYPTED_READER["cache_key"]
FRAMES = {v["name"]: v for v in FRAME_VECTORS}
RECOMPUTED = "RECOMPUTED"


def _arrow_serializer(**kwargs: Any) -> Any:
    from cachekit.serializers.arrow_serializer import ArrowSerializer  # needs the [data] extra

    return ArrowSerializer(**kwargs)


# Each write vector's configuration: (serializer, integrity_checking). A factory, so every test gets its own instance.
WRITE_CONFIGS: dict[str, tuple[Callable[[], Any], bool]] = {
    "default_saas_write_msgpack_bytestorage_bin": (lambda: "default", True),
    "arrow_dataframe_write": (lambda: "arrow", True),
    "auto_serializer_write": (lambda: "auto", True),
    "standard_serializer_instance_write": (StandardSerializer, True),
    "std_alias_write": (lambda: "std", True),
    "pythonic_alias_write": (lambda: "pythonic", True),
    "integrity_checking_off_write": (lambda: "default", False),
    "arrow_compression_off_write": (lambda: _arrow_serializer(compression=None), True),
}
# The alias writes wrote a map holding a tuple; value_json is how the frame stores it. StandardSerializer
# reads it back as value_json (an array), AutoSerializer restores the tuple its marker records.
WRITTEN_VALUES = {"std_alias_write": {"pair": (1, "two")}, "pythonic_alias_write": {"pair": (1, "two")}}
READ_VALUES = {"pythonic_alias_write": {"pair": (1, "two")}}
# The table both Arrow frames hold (they carry no value_json; this is what their IPC files decode to).
ARROW_COLUMNS = {"id": [1, 2], "score": [1.5, 2.5]}
# (reader serializer, frame) pairs that MUST miss: the frame records a name that is not the reader's canonical one.
NAME_MISSES = [
    ("default", "auto_serializer_write"),
    ("default", "standard_serializer_instance_write"),
    ("default", "std_recorded_as_given_frame"),
    ("auto", "default_saas_write_msgpack_bytestorage_bin"),
]

_PLAINTEXT_CLAIM = "^Encryption is enabled but the cache entry's header claims plaintext"

# The refusal (error class, message pattern) each encrypted read vector gets, keyed by encryption_fail_closed.
# The policy changes the path only where the key fingerprint names a key the reader lacks: off warns and fails
# AES-GCM under the current key, on refuses before decrypting.
ENCRYPTED_READ_ERRORS: dict[str, dict[bool, tuple[type[SerializationError], str]]] = {
    "forged_plaintext_encrypted_false": dict.fromkeys((False, True), (SuspiciousCacheEntryError, _PLAINTEXT_CLAIM)),
    # Refused for its serializer name or its plaintext claim, whichever the reader checks first.
    "forged_plaintext_orjson": dict.fromkeys((False, True), (SerializationError, f"^Serializer mismatch|{_PLAINTEXT_CLAIM}")),
    "ciphertext_key_not_in_keyring": {
        False: (DecryptionAuthenticationError, "^Decryption failed"),
        True: (DecryptionAuthenticationError, "^Key fingerprint mismatch"),
    },
    "ciphertext_other_tenant": dict.fromkeys((False, True), (TenantMismatchError, "^Tenant mismatch")),
    # Authenticated, then refused: plain MessagePack is not the envelope the reader is configured for.
    "ciphertext_plain_msgpack_not_envelope": dict.fromkeys(
        (False, True), (EncryptionError, "^Deserialization failed after successful decryption")
    ),
    "ciphertext_plain_msgpack_claims_uncompressed": dict.fromkeys(
        (False, True), (EncryptionError, "^Deserialization failed after successful decryption")
    ),
    "ciphertext_serializer_name_auto": dict.fromkeys((False, True), (SerializationError, "^Serializer mismatch")),
    "ciphertext_serializer_name_missing": dict.fromkeys(
        (False, True), (SerializationError, "^Corrupt cache envelope: Cache envelope records no serializer name")
    ),
}

# The frame parser's own error for each check (src/cachekit/serializers/wrapper.py, _split_frame).
# "magic" has none: a non-frame goes to the legacy reader, whose UTF-8 decode refuses it.
FRAME_CHECK_ERRORS = {
    "prefix_length": "Truncated cache envelope frame",
    "version": "Unsupported cache envelope frame version",
    "header_length": "Invalid cache envelope header length",
    "serializer_name": "Cache envelope records no serializer name",
}


def _encrypted_reader(*, fail_closed: bool, tenant_id: str = ENCRYPTED_READER["tenant_id"]) -> CacheSerializationHandler:
    """The fixture's encrypted reader: multi-tenant, so it resolves its tenant itself, never from the header."""
    return CacheSerializationHandler(
        "default",
        encryption=True,
        master_key=ENCRYPTED_READER["master_key_hex"],
        encryption_fail_closed=fail_closed,
        tenant_extractor=CallableExtractor(lambda *_a, **_k: tenant_id),
    )


def _write(name: str, value: Any) -> bytes:
    """The frame a cache under the write vector's configuration stores for ``value``."""
    serializer, integrity_checking = WRITE_CONFIGS[name]
    backend = _KeyRecordingBackend()

    @cache(backend=backend, ttl=60, l1_enabled=False, serializer=serializer(), integrity_checking=integrity_checking)
    def compute() -> Any:
        return value

    compute()
    (frame,) = backend.store.values()
    return frame


def _planted_read(frame_hex: str, serializer: Any, integrity_checking: bool = True) -> Any:
    """What a cache returns when the frame sits at its key: the frame's value, or RECOMPUTED on a miss."""
    backend = _KeyRecordingBackend()

    @cache(backend=backend, ttl=60, l1_enabled=False, serializer=serializer, integrity_checking=integrity_checking)
    def compute() -> Any:
        return RECOMPUTED

    compute()  # learns the key; an Arrow cache cannot store the str, so the key comes from the backend's get
    backend.store[backend.keys[0]] = bytes.fromhex(frame_hex)
    return compute()


def _read_value(name: str) -> Any:
    """The value a reader under the write vector's configuration returns for its frame."""
    if name in READ_VALUES:
        return READ_VALUES[name]
    if "value_json" in FRAMES[name]:
        return FRAMES[name]["value_json"]
    pytest.importorskip("pyarrow")  # the Arrow writes need the [data] extra, absent from the free-threaded lane
    return pytest.importorskip("pandas").DataFrame(ARROW_COLUMNS)


def _assert_same_value(actual: Any, expected: Any) -> None:
    if type(expected).__name__ == "DataFrame":
        pytest.importorskip("pandas").testing.assert_frame_equal(actual, expected)
    else:
        assert (actual, type(actual)) == (expected, type(expected))


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
        "auto_serializer_write",
        "standard_serializer_instance_write",
        "std_alias_write",
        "pythonic_alias_write",
        "integrity_checking_off_write",
        "arrow_compression_off_write",
        "std_recorded_as_given_frame",
    ]
    # Writes the current writer produces: all but the parse-level frame, the legacy-encoding original
    # (its _bin twin is the current write) and the constructed frame.
    assert set(WRITE_CONFIGS) == set(FRAMES) - {
        "raw_payload_frame",
        "default_saas_write_msgpack_bytestorage",
        "std_recorded_as_given_frame",
    }
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
        "serializer_name_missing": "serializer_name",
        "serializer_name_empty": "serializer_name",
    }
    assert [v["name"] for v in ENCRYPTED_READ_VECTORS] == list(ENCRYPTED_READ_ERRORS)
    assert ENCRYPTED_READER["tenant_source"] == "reader"


class TestFrameVectors:
    """Each pinned frame reads back to the header and payload it pins."""

    @pytest.mark.parametrize(
        "vector", [v for v in FRAME_VECTORS + ENCRYPTED_READ_VECTORS if "s" in v["expected_header"]], ids=lambda v: v["name"]
    )
    def test_unwrap_reads_pinned_header_and_payload(self, vector: dict[str, Any]) -> None:
        frame = bytes.fromhex(vector["frame_hex"])
        header = vector["expected_header"]

        payload, metadata, serializer_name = SerializationWrapper.unwrap(frame)

        assert (serializer_name, metadata) == (header["s"], header["m"])
        if "arrow_detection" not in vector:  # Arrow IPC bytes are not canonical; test_arrow_frame_payload_structure
            assert bytes(payload).hex() == vector["expected_payload_hex"]
        # The header's "v" is not returned by unwrap, so the writer proves it: same header, same bytes.
        assert SerializationWrapper.wrap(bytes(payload), header["m"], header["s"], header["v"]) == frame

    @pytest.mark.parametrize("vector", [v for v in FRAME_VECTORS if "arrow_detection" in v], ids=lambda v: v["name"])
    def test_arrow_frame_payload_structure(self, vector: dict[str, Any]) -> None:
        """Arrow IPC bytes are not canonical across pyarrow versions; the spec pins only the structure."""
        detection = vector["arrow_detection"]
        payload = bytes(SerializationWrapper.unwrap(bytes.fromhex(vector["frame_hex"]))[0])
        assert payload[: detection["checksum_len"]].hex() == detection["checksum_hex"]
        _assert_arrow_layout(payload, detection)


def _assert_arrow_layout(payload: bytes, detection: dict[str, Any]) -> None:
    magic_at = detection["ipc_magic_offset"]
    assert magic_at == detection["checksum_len"]
    assert payload[magic_at : magic_at + len(detection["ipc_magic"])] == detection["ipc_magic"].encode("ascii")


@pytest.mark.parametrize("name", list(WRITE_CONFIGS))
class TestWriteVectors:
    """Each write is reproduced by writing its value under its configuration, and reads back under it."""

    def test_write_stores_the_pinned_frame(self, name: str) -> None:
        vector = FRAMES[name]
        frame = _write(name, WRITTEN_VALUES[name] if name in WRITTEN_VALUES else _read_value(name))

        if "arrow_detection" not in vector:
            assert frame.hex() == vector["frame_hex"]
            return
        # Arrow: the header byte for byte, and the envelope layout; the IPC bytes depend on the pyarrow version.
        payload, metadata, serializer_name = SerializationWrapper.unwrap(frame)
        header = vector["expected_header"]
        assert (serializer_name, metadata) == (header["s"], header["m"])
        _assert_arrow_layout(bytes(payload), vector["arrow_detection"])

    def test_reader_under_the_write_configuration_reads_it_back(self, name: str) -> None:
        expected = _read_value(name)
        serializer, integrity_checking = WRITE_CONFIGS[name]
        _assert_same_value(_planted_read(FRAMES[name]["frame_hex"], serializer(), integrity_checking), expected)


class TestSerializerNameMisses:
    """The recorded serializer name must equal the reader's canonical name, or the entry is a miss."""

    @pytest.mark.parametrize(("reader", "name"), NAME_MISSES, ids=[f"{r}_reader-{n}" for r, n in NAME_MISSES])
    def test_planted_frame_is_a_miss(self, reader: str, name: str) -> None:
        assert _planted_read(FRAMES[name]["frame_hex"], reader) == RECOMPUTED

    @pytest.mark.parametrize(
        "vector", [v for v in ERROR_VECTORS if v["rejected_by"] == "serializer_name"], ids=lambda v: v["name"]
    )
    def test_nameless_frame_is_a_miss(self, vector: dict[str, Any]) -> None:
        assert _planted_read(vector["frame_hex"], "default") == RECOMPUTED

    def test_instance_reader_reads_the_instance_write(self) -> None:
        """Control: the StandardSerializer() frame misses under "default" for its name, not its bytes."""
        vector = FRAMES["standard_serializer_instance_write"]
        assert _planted_read(vector["frame_hex"], StandardSerializer()) == vector["value_json"]


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

    def test_other_tenants_reader_decrypts_other_tenant_frame(self, fail_closed: bool) -> None:
        """Control: ciphertext_other_tenant authenticates for its own tenant, so the refusal below is the tenant's."""
        (vector,) = [v for v in ENCRYPTED_READ_VECTORS if v["name"] == "ciphertext_other_tenant"]
        (written,) = [v for v in FRAME_VECTORS if v["name"] == "default_saas_write_msgpack_bytestorage_bin"]
        reader = _encrypted_reader(fail_closed=fail_closed, tenant_id=vector["expected_header"]["m"]["tenant_id"])
        assert reader.deserialize_data(bytes.fromhex(vector["frame_hex"]), cache_key=CACHE_KEY) == written["value_json"]

    @pytest.mark.parametrize(
        "vector",
        [v for v in ENCRYPTED_READ_VECTORS if v["name"].startswith("ciphertext_serializer_name_")],
        ids=lambda v: v["name"],
    )
    def test_serializer_name_frame_recorded_as_default_reads_back(self, fail_closed: bool, vector: dict[str, Any]) -> None:
        """Control: the name-check frames decrypt to the value, so their refusal below is the name's."""
        header = vector["expected_header"]
        frame = SerializationWrapper.wrap(bytes.fromhex(vector["expected_payload_hex"]), header["m"], "default", header["v"])
        value = FRAMES["default_saas_write_msgpack_bytestorage_bin"]["value_json"]
        assert _encrypted_reader(fail_closed=fail_closed).deserialize_data(frame, cache_key=CACHE_KEY) == value

    @pytest.mark.parametrize("vector", ENCRYPTED_READ_VECTORS, ids=lambda v: v["name"])
    def test_read_fails_closed(self, fail_closed: bool, vector: dict[str, Any]) -> None:
        assert vector["outcome"] == "fail_closed"
        reader = _encrypted_reader(fail_closed=fail_closed)
        error_type, message = ENCRYPTED_READ_ERRORS[vector["name"]][fail_closed]
        with pytest.raises(error_type, match=message):
            reader.deserialize_data(bytes.fromhex(vector["frame_hex"]), cache_key=CACHE_KEY)
