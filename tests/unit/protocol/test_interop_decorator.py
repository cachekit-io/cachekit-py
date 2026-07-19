"""End-to-end interop mode through the @cache decorator.

Verifies the SDK-level contract on top of the byte vectors
(test_interop_vectors.py): keys on the wire, plain-MessagePack stored bytes
(never the CK frame), loud model rejections, fail-closed guards, encryption
round-trip, and auto-mode remaining byte-identical for non-opted-in callers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from cachekit import cache
from cachekit.config.validation import ConfigurationError
from cachekit.interop import InteropError
from cachekit.serializers.wrapper import SerializationWrapper

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "interop-mode.json").read_text(encoding="utf-8"))
KEY_VECTORS = {v["name"]: v for v in VECTORS["key_vectors"]}
VALUE_VECTORS = {v["name"]: v for v in VECTORS["value_vectors"]}


class DictBackend:
    """Transparent in-memory backend: stores keys and bytes exactly as given."""

    def __init__(self, key_prefix: str = "") -> None:
        self.store: dict[str, bytes] = {}
        self._key_prefix = key_prefix

    @property
    def key_prefix(self) -> str:
        return self._key_prefix

    def _k(self, key: str) -> str:
        return f"{self._key_prefix}{key}"

    def get(self, key: str) -> Optional[bytes]:
        return self.store.get(self._k(key))

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        self.store[self._k(key)] = bytes(value)

    def delete(self, key: str) -> bool:
        return self.store.pop(self._k(key), None) is not None

    def exists(self, key: str) -> bool:
        return self._k(key) in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {"latency_ms": 0.0, "backend_type": "dict"}


@pytest.fixture
def backend() -> DictBackend:
    return DictBackend()


def _decorate(backend: DictBackend, **kwargs: Any):
    """@cache with L1 disabled so every read/write hits the DictBackend."""

    def apply(fn):
        return cache(backend=backend, l1_enabled=False, **kwargs)(fn)

    return apply


class TestInteropKeysAndValues:
    def test_key_and_stored_bytes_match_vectors(self, backend: DictBackend):
        """The wire key and stored bytes are the byte-pinned interop forms."""
        vector = KEY_VECTORS["single_int"]

        @_decorate(backend, interop="get_user", namespace="users")
        def get_user(user_id: int):
            return {"name": "alice", "age": 30}

        result = get_user(42)
        assert result == {"name": "alice", "age": 30}

        expected_key = vector["expected_key"]
        assert list(backend.store) == [expected_key], "wire key must be the pinned interop key"
        stored = backend.store[expected_key]
        # Plain MessagePack, byte-identical to the issue_example_object value
        # vector — and definitively NOT a CK v3 frame.
        assert stored.hex() == VALUE_VECTORS["issue_example_object"]["canonical_msgpack_hex"]
        assert not stored.startswith(b"CK")

        # Cache hit round-trips through the interop read path
        assert get_user(42) == {"name": "alice", "age": 30}
        assert get_user.cache_info().hits == 1

    def test_binding_and_defaults_are_call_style_invariant(self, backend: DictBackend):
        """f(42), f(user_id=42) and f(42, include_profile=False) share one key."""

        @_decorate(backend, interop="get_user", namespace="users")
        def get_user(user_id: int, include_profile: bool = False):
            return user_id

        get_user(42)
        get_user(user_id=42)
        get_user(42, include_profile=False)
        assert len(backend.store) == 1
        assert get_user.cache_info().hits == 2

    def test_cross_writer_read(self, backend: DictBackend):
        """A value planted by a foreign SDK (plain msgpack) is read as a hit."""
        import msgpack

        vector = KEY_VECTORS["single_int"]
        backend.store[vector["expected_key"]] = msgpack.packb({"name": "from-rust"}, use_bin_type=True)

        calls = []

        @_decorate(backend, interop="get_user", namespace="users")
        def get_user(user_id: int):
            calls.append(user_id)
            return {"name": "local"}

        assert get_user(42) == {"name": "from-rust"}
        assert calls == [], "foreign-written entry must be a hit, not a recompute"

    def test_auto_mode_unchanged(self, backend: DictBackend):
        """Non-opted-in functions keep auto-mode keys and the CK v3 frame."""

        @_decorate(backend, namespace="users")
        def get_user(user_id: int):
            return {"name": "alice"}

        get_user(42)
        (key,) = backend.store
        assert key.startswith("ns:users:func:")
        assert backend.store[key].startswith(b"CK"), "auto mode must keep the CK v3 frame"
        # And the frame still unwraps with the standard envelope reader
        payload, metadata, serializer_name = SerializationWrapper.unwrap(backend.store[key])
        assert serializer_name == "default"

    def test_invalidate_cache_deletes_interop_key(self, backend: DictBackend):
        @_decorate(backend, interop="get_user", namespace="users")
        def get_user(user_id: int):
            return user_id

        get_user(42)
        assert backend.store
        get_user.invalidate_cache(42)
        assert not backend.store

    @pytest.mark.asyncio
    async def test_async_wrapper_interop(self, backend: DictBackend):
        vector = KEY_VECTORS["single_int"]

        @_decorate(backend, interop="get_user", namespace="users")
        async def get_user(user_id: int):
            return {"name": "alice", "age": 30}

        assert await get_user(42) == {"name": "alice", "age": 30}
        assert list(backend.store) == [vector["expected_key"]]
        assert backend.store[vector["expected_key"]].hex() == VALUE_VECTORS["issue_example_object"]["canonical_msgpack_hex"]
        assert await get_user(42) == {"name": "alice", "age": 30}
        assert get_user.cache_info().hits == 1


class TestInteropRejections:
    def test_missing_namespace_rejected_at_decoration(self, backend: DictBackend):
        with pytest.raises(ConfigurationError, match="namespace"):

            @_decorate(backend, interop="get_user")
            def f(x: int):
                return x

    @pytest.mark.parametrize("bad", ["Users", "get:user", "users\n", "", "a" * 65])
    def test_bad_segments_rejected_at_decoration(self, backend: DictBackend, bad: str):
        with pytest.raises((ConfigurationError, InteropError)):

            @_decorate(backend, interop=bad, namespace="users")
            def f(x: int):
                return x

    def test_custom_key_function_rejected(self, backend: DictBackend):
        with pytest.raises(ConfigurationError, match="key"):

            @_decorate(backend, interop="op", namespace="ns", key=lambda x: str(x))
            def f(x: int):
                return x

    def test_non_default_serializer_rejected(self, backend: DictBackend):
        with pytest.raises(ConfigurationError, match="serializer"):

            @_decorate(backend, interop="op", namespace="ns", serializer="orjson")
            def f(x: int):
                return x

    def test_out_of_model_argument_raises_at_call(self, backend: DictBackend):
        """Interop keygen fails LOUD — never degrades to uncached execution."""

        class Custom:
            pass

        calls = []

        @_decorate(backend, interop="op", namespace="ns")
        def f(x):
            calls.append(x)
            return 1

        with pytest.raises(InteropError):
            f(Custom())
        assert calls == [], "function must NOT run when the arguments are out of model"

    def test_out_of_model_value_raises_at_store(self, backend: DictBackend):
        """A value outside the interop model fails loud at store time."""

        @_decorate(backend, interop="op", namespace="ns")
        def f(x: int):
            return {1, 2, 3}  # sets do not round-trip cross-SDK

        with pytest.raises(InteropError):
            f(1)
        assert not backend.store

    def test_key_prefixing_backend_fails_closed_at_decoration(self):
        """CWE-636 guard: a wire-level key prefix breaks cross-SDK key identity.
        With an explicit backend the guard fires at decoration time."""
        prefixed = DictBackend(key_prefix="app:")

        with pytest.raises(ConfigurationError, match="prefix"):

            @_decorate(prefixed, interop="op", namespace="ns")
            def f(x: int):
                return x

        assert not prefixed.store

    def test_key_prefix_appearing_later_fails_closed_per_call(self):
        """The guard re-checks per call: a prefix appearing after decoration
        (contract-violating dynamic backend) still fails closed."""
        mutable = DictBackend(key_prefix="")

        @_decorate(mutable, interop="op", namespace="ns")
        def f(x: int):
            return x

        assert f(1) == 1  # clean backend works
        mutable._key_prefix = "tenant-a:"  # contract violation after the fact
        with pytest.raises(ConfigurationError, match="prefix"):
            f(2)
        assert not any(k.startswith("tenant-a:") for k in mutable.store)

    def test_tenant_scoped_redis_wrapper_fails_closed(self):
        """Panel CRIT regression (CWE-636): the default provider's Redis path is a
        tenant-scoping wrapper (t:{tenant}:{key} on the wire) — it must expose
        key_prefix so the interop guard rejects it instead of silently diverging
        from the bare keys other SDKs use."""
        from unittest.mock import Mock

        from cachekit.backends.redis.provider import PerRequestRedisBackend
        from cachekit.interop import ensure_interop_backend_compatible

        scoped = PerRequestRedisBackend(Mock(), tenant_id="default")
        assert scoped.key_prefix == "t:default:"
        with pytest.raises(ConfigurationError, match="prefix"):
            ensure_interop_backend_compatible(scoped)

    def test_l1_only_mode_rejected(self):
        """interop with backend=None (L1-only) has nothing to interoperate with,
        and raw-object storage would skip the cross-SDK value contract."""
        with pytest.raises(ConfigurationError, match="shared backend"):

            @cache(backend=None, interop="op", namespace="ns")
            def f(x: int):
                return x

    def test_z_suffix_sentinel_revives(self):
        """Panel MAJ regression: JS/Rust writers emit ISO-8601 with a Z designator;
        Python 3.10's fromisoformat cannot parse it — the decoder must normalize,
        or foreign datetime-bearing entries get evicted on read."""
        import msgpack

        from cachekit.interop import decode_interop_value

        foreign = msgpack.packb({"__datetime__": True, "value": "2024-01-01T12:30:45.123Z"}, use_bin_type=True)
        revived = decode_interop_value(foreign)
        from datetime import datetime, timezone

        assert revived == datetime(2024, 1, 1, 12, 30, 45, 123000, tzinfo=timezone.utc)

    def test_ck_frame_at_interop_key_is_diagnosed_and_recomputed(self, backend: DictBackend):
        """A planted CK frame is rejected with the protocol#11 diagnostic path,
        treated as a miss, and overwritten with a valid interop value."""
        vector = KEY_VECTORS["single_int"]
        backend.store[vector["expected_key"]] = b"CK\x03\x00\x00\x00\x02{}garbage"

        @_decorate(backend, interop="get_user", namespace="users")
        def get_user(user_id: int):
            return {"name": "alice", "age": 30}

        assert get_user(42) == {"name": "alice", "age": 30}
        # Self-healed: the poisoned entry was replaced by the canonical bytes
        assert backend.store[vector["expected_key"]].hex() == VALUE_VECTORS["issue_example_object"]["canonical_msgpack_hex"]


class TestInteropEncryption:
    MASTER_KEY_HEX = "61" * 32  # matches the encryption vector's master key

    def _secure_decorator(self, backend: DictBackend, **kwargs: Any):
        return _decorate(
            backend,
            interop="get_user",
            namespace="users",
            encryption=True,
            master_key=self.MASTER_KEY_HEX,
            single_tenant_mode=True,
            deployment_uuid="00000000-0000-0000-0000-000000000001",
            **kwargs,
        )

    def test_encrypted_roundtrip_no_frame_no_metadata(self, backend: DictBackend):
        pytest.importorskip("cachekit._rust_serializer")

        @self._secure_decorator(backend)
        def get_user(user_id: int):
            return {"name": "alice", "age": 30}

        assert get_user(42) == {"name": "alice", "age": 30}
        (key,) = backend.store
        assert key == KEY_VECTORS["single_int"]["expected_key"]
        stored = backend.store[key]
        # nonce(12) || ciphertext || tag(16) with NO rotation header and NO frame:
        # ciphertext length == plaintext length for AES-GCM
        plaintext_len = len(bytes.fromhex(VALUE_VECTORS["issue_example_object"]["canonical_msgpack_hex"]))
        assert len(stored) == 12 + plaintext_len + 16
        assert not stored.startswith(b"CK")
        # Cache hit decrypts through the config-driven read path
        assert get_user(42) == {"name": "alice", "age": 30}
        assert get_user.cache_info().hits == 1

    def test_plaintext_entry_under_encryption_is_never_returned(self, backend: DictBackend):
        """Fail closed (LAB-241 posture): with encryption on, a planted plaintext
        msgpack value must not be served — it fails auth, gets evicted, and the
        recomputed value is stored encrypted."""
        import msgpack

        pytest.importorskip("cachekit._rust_serializer")
        key = KEY_VECTORS["single_int"]["expected_key"]
        forged = msgpack.packb({"name": "attacker"}, use_bin_type=True)
        backend.store[key] = forged

        @self._secure_decorator(backend)
        def get_user(user_id: int):
            return {"name": "alice", "age": 30}

        assert get_user(42) == {"name": "alice", "age": 30}, "forged plaintext must never be returned"
        assert backend.store[key] != forged, "forged entry must be overwritten with ciphertext"

    def test_encrypted_out_of_model_value_raises_at_store(self, backend: DictBackend):
        """Panel CRIT regression: EncryptionWrapper must not launder InteropError
        into SerializationError — the encrypted store path fails loud too."""
        pytest.importorskip("cachekit._rust_serializer")

        @self._secure_decorator(backend)
        def get_user(user_id: int):
            return {1, 2, 3}  # sets do not round-trip cross-SDK

        with pytest.raises(InteropError):
            get_user(42)
        assert not backend.store

    def test_machine_local_deployment_uuid_rejected(self, backend: DictBackend, monkeypatch):
        """Panel MAJ regression: interop encryption must never fall back to the
        per-machine auto-generated deployment UUID (other hosts/SDKs could never
        decrypt). Explicit deployment_uuid or CACHEKIT_DEPLOYMENT_UUID required."""
        pytest.importorskip("cachekit._rust_serializer")
        from cachekit.config.singleton import reset_settings

        monkeypatch.delenv("CACHEKIT_DEPLOYMENT_UUID", raising=False)
        reset_settings()
        try:
            with pytest.raises(ConfigurationError, match="deployment UUID"):

                @_decorate(
                    backend,
                    interop="op",
                    namespace="ns",
                    encryption=True,
                    master_key=self.MASTER_KEY_HEX,
                    single_tenant_mode=True,
                )
                def f(x: int):
                    return x
        finally:
            reset_settings()

    def test_non_canonical_deployment_uuid_rejected(self, backend: DictBackend):
        """Panel MAJ regression: Python normalizes UUIDs before key derivation;
        other SDKs use the raw string — interop requires the canonical form."""
        pytest.importorskip("cachekit._rust_serializer")
        with pytest.raises(ConfigurationError, match="canonical"):

            @_decorate(
                backend,
                interop="op",
                namespace="ns",
                encryption=True,
                master_key=self.MASTER_KEY_HEX,
                single_tenant_mode=True,
                deployment_uuid="00000000-0000-0000-0000-00000000000A",  # uppercase hex digit
            )
            def f(x: int):
                return x

    def test_tenant_extractor_rejected(self, backend: DictBackend):
        from cachekit.decorators.tenant_context import ArgumentNameExtractor

        with pytest.raises(ConfigurationError, match="tenant_extractor"):

            @_decorate(
                backend,
                interop="op",
                namespace="ns",
                encryption=True,
                master_key=self.MASTER_KEY_HEX,
                tenant_extractor=ArgumentNameExtractor("tenant_id"),
            )
            def f(x: int, tenant_id: str = "t"):
                return x
