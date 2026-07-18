"""Security hardening for the decrypt fail path (cachekit-py#170 / LAB-108).

Covers:
- Exception taxonomy: AES-GCM auth failure and tenant mismatch raise
  DecryptionAuthenticationError (tamper-class); envelope inconsistencies raise
  SuspiciousCacheEntryError; post-decrypt failures stay plain EncryptionError.
- Key-fingerprint mismatch: warn-and-attempt by default, hard raise with fail_closed.
- handle_decrypt_failure: classification, metric, fail policy, never-break-the-read-path.
- CacheSerializationHandler fail-closed resolution (explicit > env > default False).
- CacheOperationHandler.get_cached_value fail policy (evict+miss vs raise+retain).
- Config-drift reads: warn once per key + counter, decryption still succeeds.
- Decorator-level fail-closed: ciphertext substitution raises instead of recomputing,
  and a poisoned L1 entry is invalidated before the raise.
- Regressions that must NOT change: nonce uniqueness, HKDF per-tenant isolation and
  determinism, and the CWE-757 downgrade guard staying miss+evict under fail-closed
  (lazy migration, LAB-241).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from cachekit.cache_handler import (
    CacheOperationHandler,
    CacheSerializationHandler,
    handle_decrypt_failure,
)
from cachekit.key_generator import CacheKeyGenerator
from cachekit.serializers.base import SerializationError, SuspiciousCacheEntryError
from cachekit.serializers.encryption_wrapper import (
    DecryptionAuthenticationError,
    EncryptionError,
    EncryptionWrapper,
)

_HEX_KEY = "a" * 64
_KEY_BYTES = b"\xaa" * 32


class TestExceptionTaxonomy:
    """DecryptionAuthenticationError marks tamper-class failures only."""

    def test_is_subclass_of_encryption_and_serialization_error(self):
        """Backward compatibility: existing except clauses keep catching it."""
        assert issubclass(DecryptionAuthenticationError, EncryptionError)
        assert issubclass(DecryptionAuthenticationError, SerializationError)

    def test_wrong_cache_key_raises_auth_error(self):
        """AAD mismatch (ciphertext substitution) is a tamper-class failure."""
        wrapper = EncryptionWrapper(master_key=_KEY_BYTES, tenant_id="t1")
        enc, meta = wrapper.serialize({"v": 1}, cache_key="key:a")
        with pytest.raises(DecryptionAuthenticationError, match="Decryption failed"):
            wrapper.deserialize(enc, meta, cache_key="key:b")

    def test_tampered_ciphertext_raises_auth_error(self):
        """A flipped ciphertext byte fails the AES-GCM tag as tamper-class."""
        wrapper = EncryptionWrapper(master_key=_KEY_BYTES, tenant_id="t1")
        enc, meta = wrapper.serialize({"v": 1}, cache_key="key:a")
        tampered = bytearray(enc)
        tampered[len(tampered) // 2] ^= 0xFF
        with pytest.raises(DecryptionAuthenticationError, match="Decryption failed"):
            wrapper.deserialize(bytes(tampered), meta, cache_key="key:a")

    def test_post_decrypt_deserialize_failure_is_not_auth_error(self):
        """A failure AFTER successful authentication is corruption-class, not tamper."""

        class _BrokenDeserializer:
            def serialize(self, obj: Any):
                from cachekit.serializers.standard_serializer import StandardSerializer

                return StandardSerializer().serialize(obj)

            def deserialize(self, data: bytes | memoryview, metadata: Any = None) -> Any:
                raise ValueError("boom on authenticated plaintext")

        wrapper = EncryptionWrapper(master_key=_KEY_BYTES, tenant_id="t1")
        enc, meta = wrapper.serialize({"v": 1}, cache_key="key:a")
        wrapper.serializer = _BrokenDeserializer()
        with pytest.raises(EncryptionError, match="after successful decryption") as exc_info:
            wrapper.deserialize(enc, meta, cache_key="key:a")
        assert not isinstance(exc_info.value, DecryptionAuthenticationError)


class TestFingerprintMismatch:
    """Key-fingerprint mismatch policy: warn-and-attempt vs fail closed."""

    def _entry_with_doctored_fingerprint(self, wrapper: EncryptionWrapper):
        enc, meta = wrapper.serialize({"v": 1}, cache_key="key:a")
        meta.key_fingerprint = "deadbeef" * 8  # SerializationMetadata is a plain mutable class
        return enc, meta

    def test_default_warns_and_still_decrypts_when_key_actually_matches(self, caplog):
        """Fail-open: a stale/doctored fingerprint alone must not break reads."""
        wrapper = EncryptionWrapper(master_key=_KEY_BYTES, tenant_id="t1")
        enc, meta = self._entry_with_doctored_fingerprint(wrapper)
        with caplog.at_level(logging.WARNING, logger="cachekit.serializers.encryption_wrapper"):
            assert wrapper.deserialize(enc, meta, cache_key="key:a") == {"v": 1}
        assert any("Key fingerprint mismatch" in r.message for r in caplog.records)

    def test_default_wrong_key_warns_then_fails_authentication(self, caplog):
        """Fail-open with a genuinely different key ends in the GCM auth error."""
        writer = EncryptionWrapper(master_key=_KEY_BYTES, tenant_id="t1")
        reader = EncryptionWrapper(master_key=b"\xbb" * 32, tenant_id="t1")
        enc, meta = writer.serialize({"v": 1}, cache_key="key:a")
        with caplog.at_level(logging.WARNING, logger="cachekit.serializers.encryption_wrapper"):
            with pytest.raises(DecryptionAuthenticationError, match="Decryption failed"):
                reader.deserialize(enc, meta, cache_key="key:a")
        assert any("Key fingerprint mismatch" in r.message for r in caplog.records)

    def test_fail_closed_raises_before_attempting_decryption(self):
        """fail_closed=True refuses the decrypt attempt on fingerprint mismatch."""
        wrapper = EncryptionWrapper(master_key=_KEY_BYTES, tenant_id="t1", fail_closed=True)
        enc, meta = self._entry_with_doctored_fingerprint(wrapper)
        with pytest.raises(DecryptionAuthenticationError, match="fingerprint mismatch"):
            wrapper.deserialize(enc, meta, cache_key="key:a")


class TestTenantMismatchRegression:
    """Tenant mismatch always raises at the wrapper, and is tamper-class
    (auth_tamper telemetry + honored by fail-closed) per panel review — a
    foreign-tenant entry at this cache key is a cross-tenant substitution
    signature, not corruption."""

    @pytest.mark.parametrize("fail_closed", [False, True])
    def test_tenant_mismatch_always_raises_as_tamper_class(self, fail_closed):
        writer = EncryptionWrapper(master_key=_KEY_BYTES, tenant_id="tenant-1")
        reader = EncryptionWrapper(master_key=_KEY_BYTES, tenant_id="tenant-2", fail_closed=fail_closed)
        enc, meta = writer.serialize({"v": 1}, cache_key="key:a")
        with pytest.raises(DecryptionAuthenticationError, match="Tenant mismatch"):
            reader.deserialize(enc, meta, cache_key="key:a")


class TestNonceHkdfRegression:
    """Nonce uniqueness and HKDF tenant isolation must survive the hardening."""

    def test_nonce_uniqueness_same_plaintext_differs(self):
        wrapper = EncryptionWrapper(master_key=_KEY_BYTES, tenant_id="t1")
        enc1, _ = wrapper.serialize({"v": 1}, cache_key="key:a")
        enc2, _ = wrapper.serialize({"v": 1}, cache_key="key:a")
        assert enc1 != enc2  # fresh nonce per encryption

    def test_hkdf_derives_distinct_keys_per_tenant(self):
        """Forging metadata to pass the tenant check still fails AES-GCM: keys differ."""
        w1 = EncryptionWrapper(master_key=_KEY_BYTES, tenant_id="tenant-1")
        w2 = EncryptionWrapper(master_key=_KEY_BYTES, tenant_id="tenant-2")
        assert w1.encryption_key_fingerprint != w2.encryption_key_fingerprint

        enc, meta = w1.serialize({"v": 1}, cache_key="key:a")
        meta.tenant_id = "tenant-2"
        meta.key_fingerprint = w2.encryption_key_fingerprint
        with pytest.raises(DecryptionAuthenticationError):
            w2.deserialize(enc, meta, cache_key="key:a")

    def test_hkdf_is_deterministic_across_instances(self):
        """Same master key + tenant on a fresh instance decrypts (restart survival)."""
        writer = EncryptionWrapper(master_key=_KEY_BYTES, tenant_id="t1")
        reader = EncryptionWrapper(master_key=_KEY_BYTES, tenant_id="t1")
        assert writer.encryption_key_fingerprint == reader.encryption_key_fingerprint
        enc, meta = writer.serialize({"v": 42}, cache_key="key:a")
        assert reader.deserialize(enc, meta, cache_key="key:a") == {"v": 42}


class TestHandleDecryptFailure:
    """Single policy point: classification, metric, fail policy, resilience."""

    def test_classifies_auth_tamper(self):
        err = DecryptionAuthenticationError("x")
        assert handle_decrypt_failure(err, tier="l2", cache_key="k", fail_closed=False) == "auth_tamper"

    def test_classifies_suspicious_envelope(self):
        err = SuspiciousCacheEntryError("x")
        assert handle_decrypt_failure(err, tier="l2", cache_key="k", fail_closed=False) == "suspicious_envelope"

    def test_classifies_corruption(self):
        assert handle_decrypt_failure(SerializationError("x"), tier="l2", cache_key="k", fail_closed=False) == "corruption"
        assert handle_decrypt_failure(EncryptionError("x"), tier="l1", cache_key="k", fail_closed=False) == "corruption"

    def test_fail_closed_raises_only_for_tamper_class(self):
        with pytest.raises(DecryptionAuthenticationError):
            handle_decrypt_failure(DecryptionAuthenticationError("x"), tier="l2", cache_key="k", fail_closed=True)
        # suspicious_envelope and corruption stay fail-open even under fail_closed
        assert (
            handle_decrypt_failure(SuspiciousCacheEntryError("x"), tier="l2", cache_key="k", fail_closed=True)
            == "suspicious_envelope"
        )
        assert handle_decrypt_failure(SerializationError("x"), tier="l2", cache_key="k", fail_closed=True) == "corruption"

    def test_records_counter_with_reason_and_tier_labels(self, monkeypatch):
        recorded: list[tuple[str, dict[str, Any]]] = []

        class _Collector:
            def record_counter(self, name, labels=None, value=1.0):
                recorded.append((name, labels or {}))

        import cachekit.reliability.async_metrics as am

        monkeypatch.setattr(am, "get_async_metrics_collector", lambda **kw: _Collector())
        handle_decrypt_failure(DecryptionAuthenticationError("x"), tier="l1", cache_key="k", fail_closed=False)
        assert recorded == [("cachekit_decrypt_failures_total", {"reason": "auth_tamper", "tier": "l1"})]

    def test_metric_recorded_even_when_failing_closed(self, monkeypatch):
        recorded: list[tuple[str, dict[str, Any]]] = []

        class _Collector:
            def record_counter(self, name, labels=None, value=1.0):
                recorded.append((name, labels or {}))

        import cachekit.reliability.async_metrics as am

        monkeypatch.setattr(am, "get_async_metrics_collector", lambda **kw: _Collector())
        with pytest.raises(DecryptionAuthenticationError):
            handle_decrypt_failure(DecryptionAuthenticationError("x"), tier="l2", cache_key="k", fail_closed=True)
        assert recorded == [("cachekit_decrypt_failures_total", {"reason": "auth_tamper", "tier": "l2"})]

    def test_metrics_failure_never_breaks_the_read_path(self, monkeypatch):
        class _ExplodingCollector:
            def record_counter(self, *a, **kw):
                raise RuntimeError("prometheus down")

        import cachekit.reliability.async_metrics as am

        monkeypatch.setattr(am, "get_async_metrics_collector", lambda **kw: _ExplodingCollector())
        assert handle_decrypt_failure(SerializationError("x"), tier="l2", cache_key="k", fail_closed=False) == "corruption"


class TestFailClosedResolution:
    """Explicit param > CACHEKIT_ENCRYPTION_FAIL_CLOSED env > default False."""

    def test_default_is_fail_open(self, monkeypatch):
        monkeypatch.delenv("CACHEKIT_ENCRYPTION_FAIL_CLOSED", raising=False)
        from cachekit.config.singleton import reset_settings

        reset_settings()
        try:
            assert CacheSerializationHandler().encryption_fail_closed is False
        finally:
            reset_settings()

    def test_env_var_enables_fleet_wide(self, monkeypatch):
        monkeypatch.setenv("CACHEKIT_ENCRYPTION_FAIL_CLOSED", "true")
        from cachekit.config.singleton import reset_settings

        reset_settings()
        try:
            assert CacheSerializationHandler().encryption_fail_closed is True
        finally:
            reset_settings()

    def test_explicit_false_overrides_env_true(self, monkeypatch):
        monkeypatch.setenv("CACHEKIT_ENCRYPTION_FAIL_CLOSED", "true")
        from cachekit.config.singleton import reset_settings

        reset_settings()
        try:
            assert CacheSerializationHandler(encryption_fail_closed=False).encryption_fail_closed is False
        finally:
            reset_settings()

    def test_explicit_true_without_env(self, monkeypatch):
        monkeypatch.delenv("CACHEKIT_ENCRYPTION_FAIL_CLOSED", raising=False)
        from cachekit.config.singleton import reset_settings

        reset_settings()
        try:
            assert CacheSerializationHandler(encryption_fail_closed=True).encryption_fail_closed is True
        finally:
            reset_settings()


class _DictCacheStrategy:
    """Minimal CacheHandlerStrategy stub: dict store + delete tracking."""

    def __init__(self, store: dict[str, bytes]):
        self.store = store
        self.deleted: list[str] = []

    def get(self, key: str, refresh_ttl=None):
        return self.store.get(key)

    def delete(self, key: str):
        self.deleted.append(key)
        self.store.pop(key, None)
        return True


def _make_operation_handler(*, fail_closed: bool) -> tuple[CacheOperationHandler, _DictCacheStrategy, CacheSerializationHandler]:
    serialization = CacheSerializationHandler(
        encryption=True,
        single_tenant_mode=True,
        master_key=_HEX_KEY,
        encryption_fail_closed=fail_closed,
    )
    strategy = _DictCacheStrategy({})
    handler = CacheOperationHandler(serialization, CacheKeyGenerator(), cache_handler=strategy)  # type: ignore[arg-type]
    return handler, strategy, serialization


class TestGetCachedValueFailPolicy:
    """Read-path policy: evict+miss (fail open) vs raise+retain (fail closed)."""

    def test_fail_open_substituted_ciphertext_is_miss_and_evicted(self):
        handler, strategy, serialization = _make_operation_handler(fail_closed=False)
        # Ciphertext substitution: entry encrypted for key:a served under key:b
        strategy.store["key:b"] = serialization.serialize_data({"v": 1}, cache_key="key:a")
        assert handler.get_cached_value("key:b") is None  # treated as miss
        assert strategy.deleted == ["key:b"]  # poisoned entry evicted

    def test_fail_closed_substituted_ciphertext_raises_and_retains_entry(self):
        handler, strategy, serialization = _make_operation_handler(fail_closed=True)
        strategy.store["key:b"] = serialization.serialize_data({"v": 1}, cache_key="key:a")
        with pytest.raises(DecryptionAuthenticationError):
            handler.get_cached_value("key:b")
        assert strategy.deleted == []  # evidence retained
        assert "key:b" in strategy.store

    def test_fail_closed_downgrade_guard_still_fails_open(self):
        """Only tamper-class failures escalate. A plaintext entry under an
        encryption-enabled handler trips the CWE-757 downgrade guard —
        SuspiciousCacheEntryError (suspicious_envelope, NOT auth_tamper) — and
        must stay a miss+evict even with fail_closed=True, or lazy
        plaintext→encrypted migration (LAB-241) would break."""
        handler, strategy, _ = _make_operation_handler(fail_closed=True)
        plaintext_writer = CacheSerializationHandler()  # no encryption
        strategy.store["key:c"] = plaintext_writer.serialize_data({"v": 1}, cache_key="key:c")
        assert handler.get_cached_value("key:c") is None
        assert strategy.deleted == ["key:c"]

    def test_fail_open_valid_entry_roundtrips(self):
        """Regression: the happy path is untouched by the policy plumbing."""
        handler, strategy, serialization = _make_operation_handler(fail_closed=True)
        strategy.store["key:a"] = serialization.serialize_data({"v": 7}, cache_key="key:a")
        assert handler.get_cached_value("key:a") == (True, {"v": 7})


class TestConfigDriftRead:
    """Encryption-disabled handler reading an encrypted entry warns loudly."""

    def test_drift_read_warns_once_per_key_and_decrypts(self, monkeypatch, caplog):
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _HEX_KEY)
        from cachekit.config.singleton import reset_settings

        reset_settings()
        try:
            writer = CacheSerializationHandler(encryption=True, single_tenant_mode=True, master_key=_HEX_KEY)
            entry = writer.serialize_data({"v": 1}, cache_key="key:a")

            reader = CacheSerializationHandler(encryption=False)
            assert reader.encryption is False
            with caplog.at_level(logging.WARNING):
                assert reader.deserialize_data(entry, cache_key="key:a") == {"v": 1}
                assert reader.deserialize_data(entry, cache_key="key:a") == {"v": 1}  # second read
            drift_warnings = [r for r in caplog.records if "Config-drift read" in r.message]
            assert len(drift_warnings) == 1  # warn once per key, not per read (log-flood guard)
        finally:
            reset_settings()


class _DictBackend:
    """Minimal sync backend for decorator-level tests."""

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


class TestDecoratorFailClosed:
    """End-to-end: ciphertext substitution under @cache raises with fail_closed."""

    def _poison_by_substitution(self, backend: _DictBackend) -> None:
        """Swap the stored bytes between the two cached entries (same tenant)."""
        keys = sorted(backend.store)
        assert len(keys) == 2, f"expected 2 entries, got {keys}"
        backend.store[keys[0]], backend.store[keys[1]] = (backend.store[keys[1]], backend.store[keys[0]])

    def test_fail_closed_raises_on_substituted_entry(self):
        from cachekit import cache

        backend = _DictBackend()

        @cache(
            backend=backend,
            ttl=300,
            l1_enabled=False,
            encryption=True,
            single_tenant_mode=True,
            master_key=_HEX_KEY,
            fail_closed=True,
        )
        def get_value(x: int) -> dict:
            return {"result": x}

        assert get_value(1) == {"result": 1}
        assert get_value(2) == {"result": 2}
        self._poison_by_substitution(backend)
        with pytest.raises(DecryptionAuthenticationError):
            get_value(1)

    def test_fail_open_recomputes_on_substituted_entry(self):
        from cachekit import cache

        backend = _DictBackend()
        calls: list[int] = []

        @cache(
            backend=backend,
            ttl=300,
            l1_enabled=False,
            encryption=True,
            single_tenant_mode=True,
            master_key=_HEX_KEY,
            fail_closed=False,
        )
        def get_value(x: int) -> dict:
            calls.append(x)
            return {"result": x}

        assert get_value(1) == {"result": 1}
        assert get_value(2) == {"result": 2}
        self._poison_by_substitution(backend)
        assert get_value(1) == {"result": 1}  # fail open: recompute, correct value
        assert calls.count(1) == 2  # recomputed exactly once after poisoning

    def test_fail_closed_invalidates_poisoned_l1_before_raising(self):
        """A fail-closed raise from the L1 path must evict the poisoned L1 entry
        first — otherwise stale process-local L1 keeps raising after the operator
        remediates the durable L2 copy (panel finding). L2 stays the evidence."""
        from cachekit import cache
        from cachekit.l1_cache import get_l1_cache

        backend = _DictBackend()

        @cache(
            backend=backend,
            ttl=300,
            encryption=True,
            single_tenant_mode=True,
            master_key=_HEX_KEY,
            fail_closed=True,
            namespace="lab108-l1",
        )
        def get_value(x: int) -> dict:
            return {"result": x}

        assert get_value(1) == {"result": 1}
        assert get_value(2) == {"result": 2}
        k1, k2 = sorted(backend.store)

        # Plant ciphertext-substituted bytes directly in process-local L1
        l1 = get_l1_cache("lab108-l1")
        l1.put(k1, backend.store[k2], redis_ttl=300)

        with pytest.raises(DecryptionAuthenticationError):
            # Whichever of the two args maps to k1 raises from the L1 hit path
            get_value(1)
            get_value(2)

        found, _ = l1.get(k1)
        assert not found  # poisoned L1 entry invalidated before the raise
