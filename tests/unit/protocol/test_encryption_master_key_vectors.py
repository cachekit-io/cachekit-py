"""Master key input against the protocol test vectors (spec/intent-presets.md § Master Key Input).

Fixture: tests/unit/protocol/fixtures/encryption.json, vendored from
cachekit-io/protocol @ b4ae567a402051752dabf040bd16e870657d3dec (vectors 1.5.0,
sha256 pinned below). Regenerate ONLY by re-copying from the protocol repo — never by hand.

This file drives the ``master_key_input`` and ``keyring.configuration`` blocks;
test_encryption_read_vectors.py drives ``aad_reject_vectors`` and ``decrypted_container``.
Rows go through every place a key enters, not
a shared decoder. Hex keys enter through ``@cache.secure``'s ``master_key=``, CACHEKIT_MASTER_KEY
(read by the decorator, and by ``EncryptionWrapper`` when it is given no key) and
CACHEKIT_PREVIOUS_MASTER_KEYS. Raw keys enter through ``EncryptionWrapper``'s ``master_key=`` and
``previous_master_keys=`` (a bytes ``master_key=`` on ``@cache.secure`` is a TypeError, so it is
not a raw-bytes entry point).

Each accept row's entry is planted in the backend and must be read as a hit; the first row's also
next to ``default_tenant_interop`` with that row's key as a previous key. The functions return a
sentinel, so a recompute (the key or the tenant derived wrongly) fails the value assertion. Each
accept row's raw bytes must derive its pinned fingerprint. Every reject row must be refused at each
entry point.

Each ``keyring.configuration`` row loads its decrypt-only keys from CACHEKIT_PREVIOUS_MASTER_KEYS,
with its current key from ``master_key=`` and from CACHEKIT_MASTER_KEY. An accept row's keyring
reads the entry its current key sealed; a reject row is refused before that entry is returned.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cachekit import cache
from cachekit._rust_serializer import KeyringConfigurationError
from cachekit.config.singleton import reset_settings
from cachekit.config.validation import ConfigurationError
from cachekit.l1_cache import get_l1_cache_manager
from cachekit.serializers.encryption_wrapper import EncryptionError, EncryptionWrapper
from tests.unit.protocol.test_interop_decorator import DictBackend

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "encryption.json"
FIXTURE_SHA256 = "1a8a3735408675bf9660ce1372f77723a9ffda8084010fa52eb756a330961589"  # pragma: allowlist secret
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
MASTER_KEY_INPUT = FIXTURE["master_key_input"]
EXPECTED_COUNTS = {"accept_vectors": 2, "reject_vectors": 11, "raw_reject_vectors": 5}
KEYRING_CONFIGURATION = FIXTURE["keyring"]["configuration"]["vectors"]

# Each accept row's entry as an interop read: (operation, args, value the interop reader returns).
# The values are interop-mode.json value_vectors[mixed_array] and [float_value_stays_float64]; the args
# are key_vectors[empty_args] and [uuid_lowercased] (an uppercase UUID the key lowercases).
ACCEPT_READS: dict[str, tuple[str, tuple[Any, ...], Any]] = {
    "master_key_every_hex_digit": ("get_all", (), [1, "two", 3.5, None, True]),
    "master_key_first_byte_80": ("get_by_uuid", (uuid.UUID("550E8400-E29B-41D4-A716-446655440000"),), 2.0),
}
ACCEPT_ROWS = {vector["name"]: vector for vector in MASTER_KEY_INPUT["accept_vectors"]}
# The row the other rows' notes name as "the accept row": every reject row and the keyring rows derive from its key.
ACCEPT = ACCEPT_ROWS["master_key_every_hex_digit"]
ACCEPT_VALUE = ACCEPT_READS[ACCEPT["name"]][2]
# default_tenant.vectors[default_tenant_interop]: same tenant, another master key, cache key and value.
(DEFAULT_TENANT,) = FIXTURE["default_tenant"]["vectors"]
DEFAULT_TENANT_MASTER_KEY_HEX = FIXTURE["master_key_hex"]

# One refusal each: a non-hex string, or one that decodes short. Anything else (a missing key) fails the match.
DECORATOR_REFUSAL = r"CACHEKIT_MASTER_KEY must be (hex-encoded|at least 32 bytes)"
WRAPPER_REFUSAL = r"Invalid master key format|Master key must be at least 32 bytes"
# The refusal each keyring reject row gets: the settings validator (ValidationError, at decoration) or, for a
# current key given as master_key=, the native keyring (KeyringConfigurationError, when the keyring is built).
KEYRING_REFUSALS = {
    "keyring_four_decrypt_only_keys": r"accepts at most 3 decrypt-only keys, got 4",
    "keyring_current_key_decrypt_only": r"must not appear in (previous_master_keys|the decrypt-only list)",
    "keyring_current_key_decrypt_only_uppercase": r"must not appear in (previous_master_keys|the decrypt-only list)",
}


def _ids(group: str) -> list[str]:
    return [vector["name"] for vector in MASTER_KEY_INPUT[group]]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No tenant and no key from the environment unless a test sets one; L1 is process-global per key."""
    for name in ("CACHEKIT_DEPLOYMENT_UUID", "CACHEKIT_MASTER_KEY", "CACHEKIT_PREVIOUS_MASTER_KEYS"):
        monkeypatch.delenv(name, raising=False)
    reset_settings()
    get_l1_cache_manager().clear_all()
    yield
    get_l1_cache_manager().clear_all()
    reset_settings()


def _plant(backend: DictBackend, vector: dict[str, Any]) -> None:
    backend.store[vector["cache_key"]] = bytes.fromhex(vector["ciphertext_hex"])


def _secure_get_all(backend: DictBackend, **kwargs: Any):
    @cache.secure(interop="get_all", namespace="users", backend=backend, **kwargs)
    def get_all():
        return ["RECOMPUTED"]  # must never run: a miss here is a key-derivation bug

    return get_all


def _secure_read(backend: DictBackend, vector: dict[str, Any], **kwargs: Any) -> Callable[[], Any]:
    """The accept row's interop read, through ``@cache.secure`` with no tenant."""
    operation, args, _ = ACCEPT_READS[vector["name"]]
    if operation == "get_all":
        return _secure_get_all(backend, **kwargs)

    @cache.secure(interop=operation, namespace="users", backend=backend, **kwargs)
    def get_by_uuid(user_uuid: uuid.UUID):
        return "RECOMPUTED"  # must never run: a miss here is a key-derivation bug

    return lambda: get_by_uuid(*args)


def _assert_read(read: Callable[[], Any], vector: dict[str, Any]) -> None:
    value = ACCEPT_READS[vector["name"]][2]
    result = read()
    assert (result, type(result)) == (value, type(value))


def test_fixture_integrity() -> None:
    """The vendored fixture is byte-identical to the pinned protocol revision and holds every row."""
    digest = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    assert digest == FIXTURE_SHA256, (
        f"fixtures/encryption.json sha256 {digest} != pinned {FIXTURE_SHA256}. "
        "If the protocol vectors were intentionally updated, refresh the pin."
    )
    assert FIXTURE["version"] == "1.5.0"
    assert {group: len(MASTER_KEY_INPUT[group]) for group in EXPECTED_COUNTS} == EXPECTED_COUNTS
    assert MASTER_KEY_INPUT["tenant_id"] == "default"
    assert list(ACCEPT_ROWS) == list(ACCEPT_READS)
    assert {vector["name"]: vector["verdict"] for vector in KEYRING_CONFIGURATION} == {
        "keyring_three_decrypt_only_keys": "accept",
        **dict.fromkeys(KEYRING_REFUSALS, "reject"),
    }


@pytest.mark.parametrize("vector", MASTER_KEY_INPUT["accept_vectors"], ids=_ids("accept_vectors"))
class TestAcceptRows:
    """With no tenant, the hex entry points read each sealed entry; the raw bytes derive the pinned key."""

    def test_master_key_argument(self, vector: dict[str, Any]) -> None:
        backend = DictBackend()
        _plant(backend, vector)

        _assert_read(_secure_read(backend, vector, master_key=vector["master_key_hex"]), vector)

    def test_cachekit_master_key(self, vector: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", vector["master_key_hex"])
        reset_settings()
        backend = DictBackend()
        _plant(backend, vector)

        _assert_read(_secure_read(backend, vector), vector)

    def test_raw_bytes_entry_point_derives_the_pinned_key(self, vector: dict[str, Any]) -> None:
        """The row's 32 bytes, several above 7f, reach the raw-bytes entry point intact."""
        wrapper = EncryptionWrapper(master_key=bytes.fromhex(vector["master_key_hex"]), previous_master_keys=[])

        assert wrapper.tenant_id == MASTER_KEY_INPUT["tenant_id"]
        assert wrapper.encryption_key_fingerprint == vector["derived_key_fingerprint_hex"]


class TestRotation:
    """The first accept row's key read next to default_tenant_interop's, one current and one previous."""

    def test_current_and_previous_key_read_both_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fixture note's rotation case: one key current, the other previous, both entries hit."""
        monkeypatch.setenv("CACHEKIT_PREVIOUS_MASTER_KEYS", DEFAULT_TENANT_MASTER_KEY_HEX)
        reset_settings()
        backend = DictBackend()
        _plant(backend, ACCEPT)
        _plant(backend, DEFAULT_TENANT)
        get_all = _secure_get_all(backend, master_key=ACCEPT["master_key_hex"])

        @cache.secure(interop="get_user", namespace="users", backend=backend, master_key=ACCEPT["master_key_hex"])
        def get_user(user_id: int):
            return {"name": "RECOMPUTED", "age": -1}

        assert get_all() == ACCEPT_VALUE
        assert get_user(42) == {"name": "alice", "age": 30}


@pytest.mark.parametrize("vector", MASTER_KEY_INPUT["reject_vectors"], ids=_ids("reject_vectors"))
class TestHexRejectRows:
    """Every hex entry point refuses each reject row before any read or write."""

    def test_master_key_argument(self, vector: dict[str, Any]) -> None:
        with pytest.raises(ConfigurationError, match=DECORATOR_REFUSAL):
            _secure_get_all(DictBackend(), master_key=vector["master_key_hex"])

    def test_cachekit_master_key(self, vector: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", vector["master_key_hex"])
        reset_settings()
        with pytest.raises(ConfigurationError, match=DECORATOR_REFUSAL):
            _secure_get_all(DictBackend())

    def test_cachekit_master_key_read_by_encryption_wrapper(
        self, vector: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Given no key, the wrapper decodes CACHEKIT_MASTER_KEY itself, past the decorator's check."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", vector["master_key_hex"])
        reset_settings()
        with pytest.raises(EncryptionError, match=WRAPPER_REFUSAL):
            EncryptionWrapper(previous_master_keys=[])

    def test_cachekit_previous_master_keys(self, vector: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHEKIT_PREVIOUS_MASTER_KEYS", vector["master_key_hex"])
        reset_settings()
        with pytest.raises(ValidationError, match=r"previous_master_keys\[0\]"):
            _secure_get_all(DictBackend(), master_key=ACCEPT["master_key_hex"])


@pytest.mark.parametrize("vector", MASTER_KEY_INPUT["raw_reject_vectors"], ids=_ids("raw_reject_vectors"))
class TestRawRejectRows:
    """The raw-bytes entry point takes exactly 32 bytes, for the current key and a previous one alike."""

    def test_master_key(self, vector: dict[str, Any]) -> None:
        with pytest.raises(EncryptionError, match="exactly 32 bytes"):
            EncryptionWrapper(master_key=bytes.fromhex(vector["raw_key_hex"]), previous_master_keys=[])

    def test_previous_master_key(self, vector: dict[str, Any]) -> None:
        with pytest.raises(KeyringConfigurationError, match="exactly 32 bytes"):
            EncryptionWrapper(
                master_key=bytes.fromhex(ACCEPT["master_key_hex"]),
                previous_master_keys=[bytes.fromhex(vector["raw_key_hex"])],
            )


@pytest.mark.parametrize("current_key_from", ["master_key_argument", "cachekit_master_key"])
@pytest.mark.parametrize("vector", KEYRING_CONFIGURATION, ids=lambda vector: vector["name"])
class TestKeyringConfiguration:
    """Each keyring configuration row is accepted or refused when the keyring is loaded, as its verdict says."""

    def test_verdict(self, vector: dict[str, Any], current_key_from: str, monkeypatch: pytest.MonkeyPatch) -> None:
        # Every row's current key is the accept row's, so its sealed entry is what the keyring reads.
        assert vector["current_master_key_hex"] == ACCEPT["master_key_hex"]
        monkeypatch.setenv("CACHEKIT_PREVIOUS_MASTER_KEYS", ",".join(vector["decrypt_only_master_keys_hex"]))
        kwargs: dict[str, Any] = {}
        if current_key_from == "master_key_argument":
            kwargs["master_key"] = vector["current_master_key_hex"]
        else:
            monkeypatch.setenv("CACHEKIT_MASTER_KEY", vector["current_master_key_hex"])
        backend = DictBackend()
        _plant(backend, ACCEPT)

        if vector["verdict"] == "accept":
            reset_settings()
            assert _secure_get_all(backend, **kwargs)() == ACCEPT_VALUE
            return
        with pytest.raises((ValidationError, KeyringConfigurationError), match=KEYRING_REFUSALS[vector["name"]]):
            reset_settings()
            _secure_get_all(backend, **kwargs)()
