"""Encrypted reads against the protocol's ``aad_reject_vectors`` and ``decrypted_container`` (spec/encryption.md).

Fixture: tests/unit/protocol/fixtures/encryption.json, pinned by test_encryption_master_key_vectors.py.

Each row is presented as cachekit-py stores an entry: a CK v3 frame whose header carries the row's AAD
inputs (format, compressed, original_type) over the row's ciphertext, read by a single-tenant cache, which
takes the tenant from the header, under the fixture's master key. Each row's tenant, ``cross-sdk-test``, is
no UUID, so a multi-tenant reader's extractor would refuse it before the read. Every header carries the
reader's own key fingerprint, so the tamper policy is never consulted before these refusals; the policy's
own paths are proven in test_python_frame_vectors.py.

- ``aad_reject_vectors``: the read fails AES-GCM authentication (``DecryptionAuthenticationError``, which
  the read sites turn into a miss, or an error under fail-closed). Two controls keep that honest: the
  header builds exactly the row's ``aad_hex``, and the same read path, given the sealed row's own inputs,
  gets past AES-GCM. The fixture notes say why only the authentication failure itself tells a reader that
  retries with another AAD apart.
- ``decrypted_container``: the rows for cachekit-py's checksummed Arrow and orjson readers decrypt (the
  header builds the row's ``aad_hex``) and return no value, refused after decryption because the plaintext
  is not the container the reader is configured for. The other rows bind plain-MessagePack, envelope and
  interop readers cachekit-py does not configure under these AADs.
"""

from __future__ import annotations

from typing import Any

import pytest

from cachekit.cache_handler import CacheSerializationHandler
from cachekit.serializers.base import SerializationMetadata
from cachekit.serializers.encryption_wrapper import DecryptionAuthenticationError, EncryptionError
from cachekit.serializers.wrapper import SerializationWrapper
from tests.unit.protocol.test_encryption_master_key_vectors import FIXTURE

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("isolated_keys")]

AAD_REJECT_VECTORS: list[dict[str, Any]] = FIXTURE["aad_reject_vectors"]
SEALED = {vector["name"]: vector for vector in FIXTURE["vectors"]}
# A sealed row read under its own inputs by a "default" reader: its plaintext is that reader's own container
# (python-frame.json's default write payload), so it returns the value; every other row's plaintext is not the
# integrity-checked StandardSerializer container that reader expects, so it is refused after decryption.
SEALED_READ_VALUES = {"standard_serializer_default": {"user_id": 42, "name": "cachekit", "active": True}}
# The cachekit-py serializer each decrypted_container reader names, and the optional package it needs (the
# free-threaded lane installs neither); the other readers are not cachekit-py's.
CONTAINER_SERIALIZERS = {"arrow_checksummed": ("arrow", "pyarrow"), "orjson_checksummed": ("orjson", "orjson")}
CONTAINER_READERS = {
    "container_envelope_to_plain_reader": "plain_msgpack",
    "container_trailing_byte_to_interop_reader": "interop",
    "container_plain_to_envelope_reader": "bytestorage_envelope",
    "container_bare_arrow_to_arrow_reader": "arrow_checksummed",
    "container_plain_json_to_orjson_reader": "orjson_checksummed",
    "container_incomplete_tail_to_interop_reader": "interop",
}
CONTAINER_VECTORS = [v for v in FIXTURE["decrypted_container"]["vectors"] if v["reader"] in CONTAINER_SERIALIZERS]


def _reader(serializer: str) -> CacheSerializationHandler:
    return CacheSerializationHandler(serializer, encryption=True, single_tenant_mode=True, master_key=FIXTURE["master_key_hex"])


def _header(vector: dict[str, Any]) -> dict[str, Any]:
    """The frame metadata of an encrypted write with the row's AAD inputs, for the fixture's tenant and key."""
    metadata: dict[str, Any] = {"format": vector["format"], "encoding": "utf-8", "compressed": vector["compressed"]}
    if "original_type" in vector:
        metadata["original_type"] = vector["original_type"]
    return metadata | {
        "encrypted": True,
        "tenant_id": FIXTURE["tenant_id"],
        "encryption_algorithm": "AES-256-GCM",
        "key_fingerprint": FIXTURE["derived_key_fingerprint_hex"],
    }


def _aad(reader: CacheSerializationHandler, vector: dict[str, Any]) -> bytes:
    """The AAD the reader builds from a header carrying the row's inputs."""
    wrapper = reader._get_cached_encryption_wrapper(FIXTURE["tenant_id"])
    assert wrapper.encryption_key_fingerprint == FIXTURE["derived_key_fingerprint_hex"]
    return wrapper._create_aad(SerializationMetadata.from_dict(_header(vector)), vector["cache_key"])


def _read(reader: CacheSerializationHandler, vector: dict[str, Any]) -> Any:
    frame = SerializationWrapper.wrap(bytes.fromhex(vector["ciphertext_hex"]), _header(vector), reader._serializer_string_name)
    return reader.deserialize_data(frame, cache_key=vector["cache_key"])


def test_vector_names() -> None:
    """A fixture update that adds or drops a row must be a conscious change."""
    assert len(AAD_REJECT_VECTORS) == 10
    assert {v["name"]: v["reader"] for v in FIXTURE["decrypted_container"]["vectors"]} == CONTAINER_READERS


@pytest.mark.parametrize("vector", AAD_REJECT_VECTORS, ids=lambda v: v["name"])
class TestAadRejectVectors:
    """A ciphertext presented under one other AAD input fails authentication; cachekit-py never retries."""

    def test_header_builds_the_presented_aad(self, vector: dict[str, Any]) -> None:
        assert _aad(_reader("default"), vector).hex() == vector["aad_hex"]

    def test_read_path_authenticates_the_sealed_row(self, vector: dict[str, Any]) -> None:
        """Control: through the same read path, the sealed row's own inputs get past AES-GCM."""
        sealed = SEALED[vector["sealed_as"]]
        assert sealed["ciphertext_hex"] == vector["ciphertext_hex"]
        if sealed["name"] in SEALED_READ_VALUES:
            assert _read(_reader("default"), sealed) == SEALED_READ_VALUES[sealed["name"]]
            return
        with pytest.raises(EncryptionError, match="^Deserialization failed after successful decryption"):
            _read(_reader("default"), sealed)

    def test_read_fails_authentication(self, vector: dict[str, Any]) -> None:
        with pytest.raises(DecryptionAuthenticationError, match="^Decryption failed"):
            _read(_reader("default"), vector)


@pytest.mark.parametrize("vector", CONTAINER_VECTORS, ids=lambda v: v["name"])
def test_decrypted_container_returns_no_value(vector: dict[str, Any]) -> None:
    """The plaintext authenticates under the reader's own AAD, and its container is refused after decryption."""
    assert vector["outcome"] == "error"
    serializer, requires = CONTAINER_SERIALIZERS[vector["reader"]]
    pytest.importorskip(requires)
    reader = _reader(serializer)
    assert _aad(reader, vector).hex() == vector["aad_hex"]
    with pytest.raises(EncryptionError, match="^Deserialization failed after successful decryption"):
        _read(reader, vector)
