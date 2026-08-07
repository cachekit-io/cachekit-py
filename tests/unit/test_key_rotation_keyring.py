"""Key rotation via the master-key keyring (LAB-684, LAB-516 stage 2).

Spec: protocol spec/encryption.md → "Key Rotation (Keyring)" and
decisions/key-rotation.md. cachekit-py stores a per-entry key_fingerprint in CK
frame metadata, so it is the SDK the spec requires to do fingerprint-based
keyring selection — never trial decryption across the keyring.

Covers:
- Config surface: CachekitConfig.previous_master_keys — comma-separated hex env
  parsing, cap of 3 (rejected, never truncated), per-key validation identical to
  master_key, forward-only subset check (master_key must not re-appear in the
  decrypt-only list), and log redaction.
- Fingerprint-based selection: the frame's key_fingerprint is matched against
  each keyring entry's HKDF-derived per-tenant encryption-key fingerprint (never
  the master-key fingerprint); the matched entry is the ONLY key used.
- Binding match: authentication failure of the matched entry is terminal — no
  further keyring entries are attempted.
- No match: pre-keyring mismatch semantics unchanged (fail-closed raises before
  attempting; fail-open attempts the current key only).
- End-to-end rotation round-trip through CacheSerializationHandler with the env
  configuration: write under k1, rotate to k2 with k1 decrypt-only, read without
  re-encryption; drop k1, read follows the fail policy.
- The dead KeyRotationState PyO3 binding (LAB-275) is gone; master-key and
  derived-key material never crosses the FFI boundary into Python.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from cachekit.config.settings import MAX_PREVIOUS_MASTER_KEYS, CachekitConfig
from cachekit.serializers.encryption_wrapper import (
    DecryptionAuthenticationError,
    EncryptionWrapper,
)

K1 = b"\x11" * 32  # retiring master key
K2 = b"\x22" * 32  # current master key after rotation
K3 = b"\x33" * 32  # unrelated decrypt-only key
TENANT = "tenant-rotation"


class TestPreviousMasterKeysConfig:
    """CachekitConfig.previous_master_keys load-time validation."""

    def test_env_comma_separated_hex_parses_to_secretstr_list(self, monkeypatch):
        monkeypatch.setenv("CACHEKIT_PREVIOUS_MASTER_KEYS", f"{K1.hex()}, {K3.hex()}")
        config = CachekitConfig()
        assert [k.get_secret_value() for k in config.previous_master_keys] == [K1.hex(), K3.hex()]
        assert all(isinstance(k, SecretStr) for k in config.previous_master_keys)

    def test_env_blank_segments_ignored(self, monkeypatch):
        monkeypatch.setenv("CACHEKIT_PREVIOUS_MASTER_KEYS", f" {K1.hex()} ,, ")
        assert len(CachekitConfig().previous_master_keys) == 1

    def test_default_is_empty_list(self, monkeypatch):
        monkeypatch.delenv("CACHEKIT_PREVIOUS_MASTER_KEYS", raising=False)
        assert CachekitConfig().previous_master_keys == []

    def test_more_than_three_keys_raises_never_truncates(self):
        four = [SecretStr(f"{i:02x}" * 32) for i in range(1, 5)]
        with pytest.raises(ValidationError, match=f"at most {MAX_PREVIOUS_MASTER_KEYS}"):
            CachekitConfig(previous_master_keys=four)

    def test_more_than_three_keys_via_env_raises(self, monkeypatch):
        monkeypatch.setenv("CACHEKIT_PREVIOUS_MASTER_KEYS", ",".join(f"{i:02x}" * 32 for i in range(1, 5)))
        with pytest.raises(ValidationError, match="at most"):
            CachekitConfig()

    def test_exactly_three_keys_accepted(self):
        three = [SecretStr(f"{i:02x}" * 32) for i in range(1, 4)]
        assert len(CachekitConfig(previous_master_keys=three).previous_master_keys) == 3

    def test_master_key_in_previous_keys_rejected(self):
        """Detectable subset of the forward-only invariant (decisions/key-rotation.md)."""
        with pytest.raises(ValidationError, match="must not appear in previous_master_keys"):
            CachekitConfig(master_key=SecretStr(K1.hex()), previous_master_keys=[SecretStr(K1.hex())])

    def test_master_key_in_previous_keys_rejected_despite_hex_case(self):
        """Comparison is over decoded bytes — hex case cannot smuggle the key past."""
        with pytest.raises(ValidationError, match="must not appear in previous_master_keys"):
            CachekitConfig(
                master_key=SecretStr("aa" * 32),
                previous_master_keys=[SecretStr("AA" * 32)],
            )

    @pytest.mark.parametrize(
        "bad_key,reason",
        [
            ("zz" * 32, "not valid hex"),
            ("aa" * 31, "at least 32 bytes"),
        ],
    )
    def test_per_key_validation_identical_to_master_key(self, bad_key, reason):
        """Same requirements as master_key: hex-encoded, ≥32 bytes decoded."""
        with pytest.raises(ValidationError, match=reason):
            CachekitConfig(previous_master_keys=[SecretStr(bad_key)])

    def test_previous_keys_redacted_in_repr_str_and_safe_repr(self):
        config = CachekitConfig(previous_master_keys=[SecretStr(K1.hex())])
        for rendered in (repr(config), str(config), str(config.get_safe_repr())):
            assert K1.hex() not in rendered
            assert "REDACTED" in rendered

    def test_validation_errors_never_echo_key_material(self, monkeypatch):
        """CWE-532: a rejected keyring config must not leak key hex into the
        ValidationError (str/errors/json all reach startup logs and error
        trackers). Covers all three env-sourced reject paths — raw env strings
        are exactly the representation pydantic would otherwise echo."""
        cases = [
            {"CACHEKIT_PREVIOUS_MASTER_KEYS": ",".join(f"{i:02x}" * 32 for i in range(1, 5))},  # cap
            {"CACHEKIT_MASTER_KEY": "aa" * 32, "CACHEKIT_PREVIOUS_MASTER_KEYS": "aa" * 32},  # subset
            {"CACHEKIT_PREVIOUS_MASTER_KEYS": "aa" * 31},  # short key
        ]
        for env in cases:
            for key, value in env.items():
                monkeypatch.setenv(key, value)
            with pytest.raises(ValidationError) as exc_info:
                CachekitConfig()
            for rendered in (str(exc_info.value), str(exc_info.value.errors()), exc_info.value.json()):
                assert "aa" * 31 not in rendered
                assert "01" * 32 not in rendered
            # Chain severed: the original error (raw inputs recoverable via
            # .errors()) must not hang off __context__/__cause__ for error
            # trackers that walk exception chains.
            assert exc_info.value.__context__ is None
            assert exc_info.value.__cause__ is None
            for key in env:
                monkeypatch.delenv(key)


class _KeyringSpy:
    """Delegates to the real Rust Keyring, recording every decrypt call.

    The real keyring and encryptor are captured at construction: PyO3 extracts
    concrete pyclass types at the FFI boundary, so a Python spy object must
    never itself be passed into Rust. (Fingerprints are cached on the wrapper
    at construction, before instrumentation, so no fingerprint delegate is
    needed.)
    """

    def __init__(self, real_keyring: Any, real_encryptor: Any):
        self._real = real_keyring
        self._encryptor = real_encryptor
        self.decrypt_at_indices: list[int] = []
        self.sequential_decrypt_calls = 0

    def decrypt_at(self, index: int, encryptor: Any, ciphertext: bytes, tenant_id: str, aad: bytes) -> bytes:
        self.decrypt_at_indices.append(index)
        return self._real.decrypt_at(index, self._encryptor, ciphertext, tenant_id, aad)

    def decrypt(self, encryptor: Any, ciphertext: bytes, tenant_id: str, aad: bytes) -> bytes:
        self.sequential_decrypt_calls += 1
        return self._real.decrypt(self._encryptor, ciphertext, tenant_id, aad)


class _EncryptorSpy:
    """Delegates to the real Rust encryptor, counting decrypt_with_keys calls."""

    def __init__(self, real_encryptor: Any):
        self._real = real_encryptor
        self.decrypt_with_keys_calls = 0

    def decrypt_with_keys(self, ciphertext: bytes, aad: bytes, tenant_keys: Any) -> bytes:
        self.decrypt_with_keys_calls += 1
        return self._real.decrypt_with_keys(ciphertext, aad, tenant_keys)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _instrument(wrapper: EncryptionWrapper) -> tuple[_KeyringSpy, _EncryptorSpy]:
    """Swap the wrapper's keyring and encryptor for call-recording spies."""
    keyring_spy = _KeyringSpy(wrapper._keyring, wrapper.encryptor)
    encryptor_spy = _EncryptorSpy(wrapper.encryptor)
    wrapper._keyring = keyring_spy  # type: ignore[assignment]
    wrapper.encryptor = encryptor_spy  # type: ignore[assignment]
    return keyring_spy, encryptor_spy


class TestFingerprintSelection:
    """Entry selection by exact derived-key fingerprint match — no trial decryption."""

    def test_rotated_entry_decrypts_via_matched_entry_only(self):
        """AC: the matched entry is the only key used; no trial-decrypt across the keyring."""
        writer = EncryptionWrapper(master_key=K1, tenant_id=TENANT, previous_master_keys=[])
        enc, meta = writer.serialize({"v": 42}, cache_key="key:a")

        # K1 sits at keyring index 2 (current=K2 is 0, decrypt-only K3=1, K1=2):
        # a sequential trial-decrypt would try indices 0 and 1 first and be visible.
        reader = EncryptionWrapper(master_key=K2, tenant_id=TENANT, previous_master_keys=[K3, K1])
        keyring_spy, encryptor_spy = _instrument(reader)

        assert reader.deserialize(enc, meta, cache_key="key:a") == {"v": 42}
        assert keyring_spy.decrypt_at_indices == [2]  # exactly one attempt, the matched entry
        assert encryptor_spy.decrypt_with_keys_calls == 0  # current key never attempted

    def test_current_key_entry_never_touches_decrypt_only_entries(self):
        """Fresh writes (fingerprint == current) stay on the cached-tenant-keys hot path."""
        wrapper = EncryptionWrapper(master_key=K2, tenant_id=TENANT, previous_master_keys=[K1])
        enc, meta = wrapper.serialize({"v": 1}, cache_key="key:a")
        keyring_spy, encryptor_spy = _instrument(wrapper)

        assert wrapper.deserialize(enc, meta, cache_key="key:a") == {"v": 1}
        assert keyring_spy.decrypt_at_indices == []
        assert encryptor_spy.decrypt_with_keys_calls == 1

    def test_selection_uses_derived_key_fingerprints_not_master_key(self):
        """Spec L131-135: fingerprint is over the HKDF-derived per-tenant encryption
        key. The master-key fingerprint must NOT match any keyring entry."""
        from cachekit._rust_serializer import key_fingerprint

        wrapper = EncryptionWrapper(master_key=K2, tenant_id=TENANT, previous_master_keys=[K1])
        derived_fps = wrapper._keyring_fingerprints

        assert derived_fps[0] == wrapper.tenant_keys.encryption_fingerprint().hex()
        assert key_fingerprint(K2).hex() not in derived_fps
        assert key_fingerprint(K1).hex() not in derived_fps

    def test_fingerprints_are_per_tenant(self):
        """Different tenants derive different fingerprints for the same keyring."""
        w1 = EncryptionWrapper(master_key=K2, tenant_id="tenant-a", previous_master_keys=[K1])
        w2 = EncryptionWrapper(master_key=K2, tenant_id="tenant-b", previous_master_keys=[K1])
        assert set(w1._keyring_fingerprints).isdisjoint(w2._keyring_fingerprints)


class TestBindingMatch:
    """A fingerprint match is binding: auth failure of the matched entry is terminal."""

    @pytest.mark.parametrize("fail_closed", [False, True])
    def test_matched_entry_auth_failure_is_terminal(self, fail_closed):
        """AC: tampered ciphertext whose fingerprint matches a decrypt-only entry
        fails there — the remaining keyring entries are NOT tried."""
        writer = EncryptionWrapper(master_key=K1, tenant_id=TENANT, previous_master_keys=[])
        enc, meta = writer.serialize({"v": 1}, cache_key="key:a")
        tampered = bytearray(enc)
        tampered[len(tampered) // 2] ^= 0xFF

        reader = EncryptionWrapper(
            master_key=K2,
            tenant_id=TENANT,
            previous_master_keys=[K1, K3],
            fail_closed=fail_closed,
        )
        keyring_spy, encryptor_spy = _instrument(reader)

        with pytest.raises(DecryptionAuthenticationError, match="Decryption failed"):
            reader.deserialize(bytes(tampered), meta, cache_key="key:a")

        assert keyring_spy.decrypt_at_indices == [1]  # matched entry only — terminal
        assert encryptor_spy.decrypt_with_keys_calls == 0  # no retreat to the current key

    def test_wrong_cache_key_on_rotated_entry_is_terminal_auth_failure(self):
        """AAD binding survives rotation: substitution across cache keys still fails."""
        writer = EncryptionWrapper(master_key=K1, tenant_id=TENANT, previous_master_keys=[])
        enc, meta = writer.serialize({"v": 1}, cache_key="key:a")

        reader = EncryptionWrapper(master_key=K2, tenant_id=TENANT, previous_master_keys=[K1])
        keyring_spy, _ = _instrument(reader)

        with pytest.raises(DecryptionAuthenticationError, match="Decryption failed"):
            reader.deserialize(enc, meta, cache_key="key:b")
        assert keyring_spy.decrypt_at_indices == [1]


class TestNoMatchSemanticsUnchanged:
    """No fingerprint match → pre-keyring behaviour, byte-for-byte."""

    def test_fail_open_attempts_current_key_only(self, caplog):
        """Fail-open no-match warns and attempts the current key — decrypt-only
        entries are never tried."""
        import logging

        writer = EncryptionWrapper(master_key=b"\x44" * 32, tenant_id=TENANT, previous_master_keys=[])
        enc, meta = writer.serialize({"v": 1}, cache_key="key:a")

        reader = EncryptionWrapper(master_key=K2, tenant_id=TENANT, previous_master_keys=[K1])
        keyring_spy, encryptor_spy = _instrument(reader)

        with caplog.at_level(logging.WARNING, logger="cachekit.serializers.encryption_wrapper"):
            with pytest.raises(DecryptionAuthenticationError, match="Decryption failed"):
                reader.deserialize(enc, meta, cache_key="key:a")

        assert any("Key fingerprint mismatch" in r.message for r in caplog.records)
        assert encryptor_spy.decrypt_with_keys_calls == 1  # current key only
        assert keyring_spy.decrypt_at_indices == []  # keyring never trialled

    def test_fail_closed_raises_before_any_attempt(self):
        writer = EncryptionWrapper(master_key=b"\x44" * 32, tenant_id=TENANT, previous_master_keys=[])
        enc, meta = writer.serialize({"v": 1}, cache_key="key:a")

        reader = EncryptionWrapper(master_key=K2, tenant_id=TENANT, previous_master_keys=[K1], fail_closed=True)
        keyring_spy, encryptor_spy = _instrument(reader)

        with pytest.raises(DecryptionAuthenticationError, match="fingerprint mismatch"):
            reader.deserialize(enc, meta, cache_key="key:a")
        assert encryptor_spy.decrypt_with_keys_calls == 0
        assert keyring_spy.decrypt_at_indices == []


class TestWrapperKeyringConfig:
    """Wrapper-level keyring construction re-validates behind the FFI boundary.

    Violations raise ValueError — config-class, NEVER EncryptionError: an
    EncryptionError (a SerializationError) would be classified as corruption by
    handle_decrypt_failure and fail OPEN even under fail_closed=True, turning a
    misconfigured keyring into silent 100% misses plus entry-by-entry eviction
    (the LAB-241/LAB-683 config-vs-crypto error class).
    """

    def test_cap_exceeded_raises_value_error(self):
        with pytest.raises(ValueError, match="Keyring configuration invalid"):
            EncryptionWrapper(
                master_key=K2,
                tenant_id=TENANT,
                previous_master_keys=[b"\x0a" * 32, b"\x0b" * 32, b"\x0c" * 32, b"\x0d" * 32],
            )

    def test_current_key_in_decrypt_only_list_raises_value_error(self):
        with pytest.raises(ValueError, match="Keyring configuration invalid"):
            EncryptionWrapper(master_key=K2, tenant_id=TENANT, previous_master_keys=[K1, K2])

    def test_short_previous_key_raises_with_master_key_parity_message(self):
        with pytest.raises(ValueError, match="identical to master_key"):
            EncryptionWrapper(master_key=K2, tenant_id=TENANT, previous_master_keys=[b"\x0a" * 31])

    def test_keyring_config_errors_are_not_serialization_errors(self):
        """The read path fails LOUD on keyring misconfig — never miss+evict."""
        from cachekit.serializers.base import SerializationError

        for previous in ([K1, K2], [b"\x0a" * 31]):
            with pytest.raises(ValueError) as exc_info:
                EncryptionWrapper(master_key=K2, tenant_id=TENANT, previous_master_keys=previous)
            assert not isinstance(exc_info.value, SerializationError)


class TestDecryptErrorTaxonomy:
    """A misconfigured keyring must never be reported as tamper.

    `handle_decrypt_failure` records `DecryptionAuthenticationError` as
    `auth_tamper`, which the docs tell operators to alert on as a security
    event. Collapsing config/structural failures into it pages someone for an
    attack that never happened, and under fail_closed raises to the caller.
    """

    def test_binding_separates_auth_failure_from_config_failure(self):
        from cachekit._rust_serializer import (
            Keyring,
            KeyringConfigurationError,
            ZeroKnowledgeEncryptor,
        )

        keyring = Keyring(K1, [])
        encryptor = ZeroKnowledgeEncryptor()

        # Structural: shorter than nonce(12) + tag(16). Fails identically under
        # every key, so it is terminal and is not a wrong-key signal.
        with pytest.raises(KeyringConfigurationError):
            keyring.decrypt(encryptor, b"short", TENANT, b"aad")

        # Caller bug: single-entry keyring has no index 5.
        with pytest.raises(KeyringConfigurationError):
            keyring.decrypt_at(5, encryptor, b"\x00" * 64, TENANT, b"aad")

        # Well-formed length, garbage content: a real AES-GCM tag failure. This
        # one MUST stay the plain ValueError the wrapper converts to tamper.
        with pytest.raises(ValueError) as exc_info:
            keyring.decrypt_at(0, encryptor, b"\x00" * 64, TENANT, b"aad")
        assert not isinstance(exc_info.value, KeyringConfigurationError)

    def test_wrapper_does_not_relabel_config_error_as_tamper(self):
        from cachekit._rust_serializer import KeyringConfigurationError

        writer = EncryptionWrapper(master_key=K1, tenant_id=TENANT, previous_master_keys=[])
        enc, meta = writer.serialize({"v": 42}, cache_key="key:a")

        # K1 is a decrypt-only entry here, so the read takes the fingerprint
        # path (decrypt_at) — the path CodeRabbit did not flag but which shares
        # the binding, and therefore the defect.
        reader = EncryptionWrapper(master_key=K2, tenant_id=TENANT, previous_master_keys=[K1])

        class _ConfigFailKeyring:
            def decrypt_at(self, *args: Any, **kwargs: Any) -> bytes:
                raise KeyringConfigurationError("Keyring decrypt failed: simulated config fault")

            def decrypt(self, *args: Any, **kwargs: Any) -> bytes:
                raise KeyringConfigurationError("Keyring decrypt failed: simulated config fault")

        reader._keyring = _ConfigFailKeyring()  # type: ignore[assignment]

        with pytest.raises(KeyringConfigurationError) as exc_info:
            reader.deserialize(enc, meta, cache_key="key:a")
        assert not isinstance(exc_info.value, DecryptionAuthenticationError)

    def test_fingerprint_derivation_failure_is_not_a_serialization_error(self, monkeypatch):
        """Regression: the derivation call used to sit inside the try block whose
        handler raises EncryptionError — a SerializationError, which the read
        policy treats as corruption and fails open (silent miss + evict)."""
        import cachekit.serializers.encryption_wrapper as ew
        from cachekit.serializers.base import SerializationError

        class _BadKeyring:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def encryption_fingerprints(self, tenant_id: str) -> list[bytes]:
                raise ValueError("Keyring fingerprint derivation failed: simulated")

        monkeypatch.setattr(ew, "Keyring", _BadKeyring)

        with pytest.raises(ValueError) as exc_info:
            ew.EncryptionWrapper(master_key=K1, tenant_id=TENANT, previous_master_keys=[])
        assert not isinstance(exc_info.value, SerializationError)


class TestEndToEndRotation:
    """AC round-trip through CacheSerializationHandler with env configuration."""

    @pytest.fixture(autouse=True)
    def _pin_deployment_uuid(self, monkeypatch):
        """Pin the tenant identity these tests derive their keys from.

        Unset, `CacheSerializationHandler._get_deterministic_deployment_uuid`
        falls through to `~/.cachekit/deployment_uuid` and CREATES that file.
        Two problems: the suite writes to the developer's and the CI runner's
        home directory, and the derived key then depends on filesystem state
        outside the test. `TestInteropRotation._handler` already pins it.
        """
        monkeypatch.setenv("CACHEKIT_DEPLOYMENT_UUID", "00000000-0000-0000-0000-00000000abcd")

    def _reset(self):
        from cachekit.config.singleton import reset_settings

        reset_settings()

    def test_rotation_round_trip_then_drop(self, monkeypatch):
        """Write under k1 → rotate (k2 current, k1 decrypt-only) → read without
        re-encryption → drop k1 → read follows the configured fail policy."""
        from cachekit.cache_handler import CacheSerializationHandler

        # Phase 1: fleet on k1
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", K1.hex())
        monkeypatch.delenv("CACHEKIT_PREVIOUS_MASTER_KEYS", raising=False)
        self._reset()
        try:
            writer = CacheSerializationHandler(encryption=True, single_tenant_mode=True)
            entry = writer.serialize_data({"v": 42}, cache_key="key:a")

            # Phase 2: k2 promoted, k1 decrypt-only — entry readable, NOT re-encrypted
            monkeypatch.setenv("CACHEKIT_MASTER_KEY", K2.hex())
            monkeypatch.setenv("CACHEKIT_PREVIOUS_MASTER_KEYS", K1.hex())
            self._reset()
            rotated = CacheSerializationHandler(encryption=True, single_tenant_mode=True)
            assert rotated.deserialize_data(entry, cache_key="key:a") == {"v": 42}

            # Phase 3a: k1 dropped, fail-open — auth failure surfaces (handler
            # read paths classify it as a miss via handle_decrypt_failure)
            monkeypatch.delenv("CACHEKIT_PREVIOUS_MASTER_KEYS")
            self._reset()
            cutover_open = CacheSerializationHandler(encryption=True, single_tenant_mode=True, encryption_fail_closed=False)
            with pytest.raises(DecryptionAuthenticationError, match="Decryption failed"):
                cutover_open.deserialize_data(entry, cache_key="key:a")

            # Phase 3b: k1 dropped, fail-closed — refuses before attempting
            cutover_closed = CacheSerializationHandler(encryption=True, single_tenant_mode=True, encryption_fail_closed=True)
            with pytest.raises(DecryptionAuthenticationError, match="fingerprint mismatch"):
                cutover_closed.deserialize_data(entry, cache_key="key:a")
        finally:
            self._reset()

    def test_decorator_read_survives_rotation(self, monkeypatch):
        """Decorator-level proof: a cached value written under k1 is served from
        cache (not recomputed) after rotation to k2 with k1 decrypt-only."""
        from cachekit import cache

        class _DictBackend:
            def __init__(self):
                self.store: dict[str, bytes] = {}

            def get(self, key: str):
                return self.store.get(key)

            def set(self, key: str, value: bytes, ttl=None):
                self.store[key] = value

            def delete(self, key: str) -> bool:
                return self.store.pop(key, None) is not None

            def exists(self, key: str) -> bool:
                return key in self.store

            def health_check(self):
                return True, {"backend_type": "dict_test"}

        backend = _DictBackend()
        calls: list[int] = []

        def make_cached():
            @cache(
                backend=backend,
                ttl=300,
                l1_enabled=False,
                encryption=True,
                single_tenant_mode=True,
            )
            def get_value(x: int) -> dict:
                calls.append(x)
                return {"result": x}

            return get_value

        monkeypatch.setenv("CACHEKIT_MASTER_KEY", K1.hex())
        monkeypatch.delenv("CACHEKIT_PREVIOUS_MASTER_KEYS", raising=False)
        self._reset()
        try:
            assert make_cached()(1) == {"result": 1}
            assert calls == [1]
            assert len(backend.store) == 1
            stored_before = dict(backend.store)

            monkeypatch.setenv("CACHEKIT_MASTER_KEY", K2.hex())
            monkeypatch.setenv("CACHEKIT_PREVIOUS_MASTER_KEYS", K1.hex())
            self._reset()
            assert make_cached()(1) == {"result": 1}
            assert calls == [1]  # cache hit — NOT recomputed
            assert backend.store == stored_before  # NOT re-encrypted on read
        finally:
            self._reset()


class TestInteropRotation:
    """Interop entries carry no key fingerprint → sequential keyring attempts
    (spec 'Decrypt — without per-entry key identity'), same operator surface."""

    def _handler(self, monkeypatch, master_hex: str, previous_hex: str | None):
        from cachekit.cache_handler import CacheSerializationHandler
        from cachekit.config.singleton import reset_settings

        monkeypatch.setenv("CACHEKIT_MASTER_KEY", master_hex)
        monkeypatch.setenv("CACHEKIT_DEPLOYMENT_UUID", "00000000-0000-0000-0000-00000000abcd")
        if previous_hex is None:
            monkeypatch.delenv("CACHEKIT_PREVIOUS_MASTER_KEYS", raising=False)
        else:
            monkeypatch.setenv("CACHEKIT_PREVIOUS_MASTER_KEYS", previous_hex)
        reset_settings()
        return CacheSerializationHandler(encryption=True, single_tenant_mode=True, interop_mode=True)

    def test_interop_rotation_round_trip_then_drop(self, monkeypatch):
        from cachekit.config.singleton import reset_settings

        try:
            writer = self._handler(monkeypatch, K1.hex(), None)
            entry = writer.serialize_data({"v": 9}, cache_key="ns:app:func:f:args:x:v1")

            rotated = self._handler(monkeypatch, K2.hex(), K1.hex())
            assert rotated.deserialize_data(entry, cache_key="ns:app:func:f:args:x:v1") == {"v": 9}

            cutover = self._handler(monkeypatch, K2.hex(), None)
            with pytest.raises(DecryptionAuthenticationError, match="Decryption failed"):
                cutover.deserialize_data(entry, cache_key="ns:app:func:f:args:x:v1")
        finally:
            reset_settings()

    def test_interop_rotation_uses_sequential_decrypt_not_fingerprints(self, monkeypatch):
        """The interop read path routes through Keyring.decrypt (sequential),
        never decrypt_at (fingerprint selection needs a stored fingerprint)."""
        from cachekit.config.singleton import reset_settings

        try:
            writer = self._handler(monkeypatch, K1.hex(), None)
            entry = writer.serialize_data({"v": 9}, cache_key="ns:app:func:f:args:x:v1")

            rotated = self._handler(monkeypatch, K2.hex(), K1.hex())
            wrapper = rotated._get_cached_encryption_wrapper("00000000-0000-0000-0000-00000000abcd")
            keyring_spy, encryptor_spy = _instrument(wrapper)

            assert rotated.deserialize_data(entry, cache_key="ns:app:func:f:args:x:v1") == {"v": 9}
            assert keyring_spy.sequential_decrypt_calls == 1
            assert keyring_spy.decrypt_at_indices == []
            assert encryptor_spy.decrypt_with_keys_calls == 0
        finally:
            reset_settings()

    def test_interop_single_key_stays_on_cached_hot_path(self, monkeypatch):
        """No rotation configured → interop reads keep the cached tenant keys
        (no per-read HKDF through the keyring)."""
        from cachekit.config.singleton import reset_settings

        try:
            handler = self._handler(monkeypatch, K1.hex(), None)
            entry = handler.serialize_data({"v": 9}, cache_key="ns:app:func:f:args:x:v1")
            wrapper = handler._get_cached_encryption_wrapper("00000000-0000-0000-0000-00000000abcd")
            keyring_spy, encryptor_spy = _instrument(wrapper)

            assert handler.deserialize_data(entry, cache_key="ns:app:func:f:args:x:v1") == {"v": 9}
            assert keyring_spy.sequential_decrypt_calls == 0
            assert encryptor_spy.decrypt_with_keys_calls == 1
        finally:
            reset_settings()


class TestDeadBindingRemoved:
    """LAB-275: the KeyRotationState PyO3 binding had zero Python callers."""

    def test_key_rotation_state_no_longer_importable(self):
        with pytest.raises(ImportError):
            from cachekit._rust_serializer import KeyRotationState  # noqa: F401

    def test_keyring_exposes_no_key_material(self):
        """FFI hygiene: the Keyring binding's public surface is construction,
        fingerprints (safe), and the two decrypt entry points (return
        plaintext) — nothing that hands key bytes back to Python."""
        from cachekit._rust_serializer import Keyring

        public = {name for name in dir(Keyring) if not name.startswith("_")}
        assert public == {"encryption_fingerprints", "decrypt_at", "decrypt"}
