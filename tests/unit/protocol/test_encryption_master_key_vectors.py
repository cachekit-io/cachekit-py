"""Master key input against the protocol test vectors (spec/intent-presets.md § Master Key Input).

Fixture: tests/unit/protocol/fixtures/encryption.json, vendored from
cachekit-io/protocol @ 4b34c015878ccdaf48005251a97b406a5f2060db (vectors 1.3.0,
sha256 pinned below). Regenerate ONLY by re-copying from the protocol repo — never by hand.

This file drives the ``master_key_input`` block. Rows go through every place a key enters, not
a shared decoder. Hex keys enter through ``@cache.secure``'s ``master_key=``, CACHEKIT_MASTER_KEY
(read by the decorator, and by ``EncryptionWrapper`` when it is given no key) and
CACHEKIT_PREVIOUS_MASTER_KEYS. Raw keys enter through ``EncryptionWrapper``'s ``master_key=`` and
``previous_master_keys=`` (a bytes ``master_key=`` on ``@cache.secure`` is a TypeError, so it is
not a raw-bytes entry point).

The accept row's entry is planted in the backend and must be read as a hit, alone and next to
``default_tenant_interop`` with that row's key as a previous key: the functions return a sentinel,
so a recompute (the key or the tenant derived wrongly) fails the value assertion. Its raw bytes
must derive the row's pinned fingerprint. Every reject row must be refused at each entry point.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
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
FIXTURE_SHA256 = "f701951147a47c42a968850fe6cb73a544313728e46f45fec3d811c20ed4b377"  # pragma: allowlist secret
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
MASTER_KEY_INPUT = FIXTURE["master_key_input"]
EXPECTED_COUNTS = {"accept_vectors": 1, "reject_vectors": 11, "raw_reject_vectors": 5}

(ACCEPT,) = MASTER_KEY_INPUT["accept_vectors"]
# interop-mode.json value_vectors[mixed_array], the accept row's plaintext, as the interop reader returns it.
ACCEPT_VALUE = [1, "two", 3.5, None, True]
# default_tenant.vectors[default_tenant_interop]: same tenant, another master key, cache key and value.
(DEFAULT_TENANT,) = FIXTURE["default_tenant"]["vectors"]
DEFAULT_TENANT_MASTER_KEY_HEX = FIXTURE["master_key_hex"]

# One refusal each: a non-hex string, or one that decodes short. Anything else (a missing key) fails the match.
DECORATOR_REFUSAL = r"CACHEKIT_MASTER_KEY must be (hex-encoded|at least 32 bytes)"
WRAPPER_REFUSAL = r"Invalid master key format|Master key must be at least 32 bytes"


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


def test_fixture_integrity() -> None:
    """The vendored fixture is byte-identical to the pinned protocol revision and holds every row."""
    digest = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    assert digest == FIXTURE_SHA256, (
        f"fixtures/encryption.json sha256 {digest} != pinned {FIXTURE_SHA256}. "
        "If the protocol vectors were intentionally updated, refresh the pin."
    )
    assert FIXTURE["version"] == "1.3.0"
    assert {group: len(MASTER_KEY_INPUT[group]) for group in EXPECTED_COUNTS} == EXPECTED_COUNTS
    assert MASTER_KEY_INPUT["tenant_id"] == "default"


class TestAcceptRow:
    """With no tenant, the hex entry points read the sealed entry; the raw bytes derive the pinned key."""

    def test_master_key_argument(self) -> None:
        backend = DictBackend()
        _plant(backend, ACCEPT)
        get_all = _secure_get_all(backend, master_key=ACCEPT["master_key_hex"])

        assert get_all() == ACCEPT_VALUE

    def test_cachekit_master_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", ACCEPT["master_key_hex"])
        reset_settings()
        backend = DictBackend()
        _plant(backend, ACCEPT)
        get_all = _secure_get_all(backend)

        assert get_all() == ACCEPT_VALUE

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

    def test_raw_bytes_entry_point_derives_the_pinned_key(self) -> None:
        """The accept row's 32 bytes, several above 7f, reach the raw-bytes entry point intact."""
        wrapper = EncryptionWrapper(master_key=bytes.fromhex(ACCEPT["master_key_hex"]), previous_master_keys=[])

        assert wrapper.tenant_id == MASTER_KEY_INPUT["tenant_id"]
        assert wrapper.encryption_key_fingerprint == ACCEPT["derived_key_fingerprint_hex"]


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
