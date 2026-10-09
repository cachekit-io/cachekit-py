"""Encrypted reads against the protocol's ``aad_reject_vectors`` and ``decrypted_container`` (spec/encryption.md).

Fixture: tests/unit/protocol/fixtures/encryption.json, pinned by test_encryption_master_key_vectors.py.

Each row is presented as cachekit-py stores an entry: a CK v3 frame whose header carries the row's AAD
inputs (format, compressed, original_type) over the row's ciphertext, read by a single-tenant cache, which
takes the tenant from the header, under the fixture's master key. Each row's tenant, ``cross-sdk-test``, is
no UUID, so a multi-tenant reader's extractor would refuse it before the read.

- ``aad_reject_vectors``: the read fails AES-GCM authentication (``DecryptionAuthenticationError``, which
  the read sites turn into a miss, or an error under fail-closed), under both tamper policies. Two controls
  keep that honest: the header builds exactly the row's ``aad_hex``, and the ciphertext authenticates under
  the AAD it was sealed with. The fixture notes say why only the authentication failure itself tells a
  reader that retries with another AAD apart.
- ``decrypted_container``: the rows for cachekit-py's checksummed Arrow and orjson readers decrypt (the
  header builds the row's ``aad_hex``) and return no value, refused after decryption because the plaintext
  is not the container the reader is configured for. The other rows bind plain-MessagePack, envelope and
  interop readers cachekit-py does not configure under these AADs.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from cachekit.cache_handler import CacheSerializationHandler
from cachekit.config.singleton import reset_settings
from cachekit.serializers.base import SerializationMetadata
from cachekit.serializers.encryption_wrapper import DecryptionAuthenticationError, EncryptionError
from cachekit.serializers.wrapper import SerializationWrapper
from tests.unit.protocol.test_encryption_master_key_vectors import FIXTURE

pytestmark = pytest.mark.unit

AAD_REJECT_VECTORS: list[dict[str, Any]] = FIXTURE["aad_reject_vectors"]
SEALED = {vector["name"]: vector for vector in FIXTURE["vectors"]}
# The cachekit-py serializer each decrypted_container reader names, and the optional package it needs (the
# free-threaded lane installs neither); the other readers are not cachekit-py's.
CONTAINER_SERIALIZERS = {"arrow_checksummed": ("arrow", "pyarrow"), "orjson_checksummed": ("orjson", "orjson")}
CONTAINER_VECTORS = [v for v in FIXTURE["decrypted_container"]["vectors"] if v["reader"] in CONTAINER_SERIALIZERS]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("CACHEKIT_DEPLOYMENT_UUID", "CACHEKIT_MASTER_KEY", "CACHEKIT_PREVIOUS_MASTER_KEYS"):
        monkeypatch.delenv(name, raising=False)
    reset_settings()
    yield
    reset_settings()


def _reader(serializer: str, *, fail_closed: bool) -> CacheSerializationHandler:
    return CacheSerializationHandler(
        serializer,
        encryption=True,
        single_tenant_mode=True,
        master_key=FIXTURE["master_key_hex"],
        encryption_fail_closed=fail_closed,
    )


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
    assert {v["sealed_as"] for v in AAD_REJECT_VECTORS} <= set(SEALED)
    assert [v["name"] for v in CONTAINER_VECTORS] == [
        "container_bare_arrow_to_arrow_reader",
        "container_plain_json_to_orjson_reader",
    ]


@pytest.mark.parametrize("vector", AAD_REJECT_VECTORS, ids=lambda v: v["name"])
class TestAadRejectVectors:
    """A ciphertext presented under one other AAD input fails authentication; cachekit-py never retries."""

    def test_header_builds_the_presented_aad(self, vector: dict[str, Any]) -> None:
        assert _aad(_reader("default", fail_closed=False), vector).hex() == vector["aad_hex"]

    def test_ciphertext_authenticates_as_sealed(self, vector: dict[str, Any]) -> None:
        """Control: the failure below is the presented AAD's, not a ciphertext no AAD opens."""
        sealed = SEALED[vector["sealed_as"]]
        assert sealed["ciphertext_hex"] == vector["ciphertext_hex"]
        wrapper = _reader("default", fail_closed=False)._get_cached_encryption_wrapper(FIXTURE["tenant_id"])
        plaintext = wrapper.encryptor.decrypt_with_keys(
            bytes.fromhex(vector["ciphertext_hex"]), bytes.fromhex(sealed["aad_hex"]), wrapper.tenant_keys
        )
        assert bytes(plaintext).hex() == sealed["plaintext_hex"]

    @pytest.mark.parametrize("fail_closed", [False, True], ids=["fail_closed_off", "fail_closed_on"])
    def test_read_fails_authentication(self, vector: dict[str, Any], fail_closed: bool) -> None:
        with pytest.raises(DecryptionAuthenticationError, match="^Decryption failed"):
            _read(_reader("default", fail_closed=fail_closed), vector)


@pytest.mark.parametrize("fail_closed", [False, True], ids=["fail_closed_off", "fail_closed_on"])
@pytest.mark.parametrize("vector", CONTAINER_VECTORS, ids=lambda v: v["name"])
def test_decrypted_container_returns_no_value(vector: dict[str, Any], fail_closed: bool) -> None:
    """The plaintext authenticates under the reader's own AAD, and its container is refused after decryption."""
    assert vector["outcome"] == "error"
    serializer, requires = CONTAINER_SERIALIZERS[vector["reader"]]
    pytest.importorskip(requires)
    reader = _reader(serializer, fail_closed=fail_closed)
    assert _aad(reader, vector).hex() == vector["aad_hex"]
    with pytest.raises(EncryptionError, match="^Deserialization failed after successful decryption"):
        _read(reader, vector)
