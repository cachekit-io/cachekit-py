"""Cache keys must carry the serializer code (LAB-4351).

`CacheKeyGenerator.generate_key` has always accepted `serializer_type`, but no
caller on the main read/write path passed it, so every serializer fell back to
`"s"` and two decorators over the same function differing only in serializer
produced the *same* key. The deserialize-time serializer-name guard caught the
collision on read and evicted, so each decorator evicted the other's entry on
every call: a permanent 0% hit rate for both, with no error surfaced.

These tests observe the key the decorator hands the backend, not a key handed
to `generate_key` by the test itself — the pass-through is the thing under test,
so asserting on a hand-fed argument would pin nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

import pytest

from cachekit import cache
from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.cache_handler import CacheOperationHandler, CacheSerializationHandler
from cachekit.key_generator import CacheKeyGenerator
from cachekit.serializers.standard_serializer import StandardSerializer
from tests.unit.test_invalidate_no_args import ScopedFlakyBackend
from tests.unit.test_invalidate_no_args import _tenant as _flaky_tenant
from tests.unit.test_key_registry import ScopedBackend, TrackingBackend
from tests.unit.test_key_registry import _tenant as _registry_tenant


class _RecordingBackend:
    """Plain byte store that keeps every key it is asked about."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        self.store[key] = value

    def delete(self, key: str) -> bool:
        self.deleted.append(key)
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {}


def _suffix(key: str) -> str:
    """The `{ic_flag}{serializer_code}` metadata suffix, last segment of the key."""
    return key.rsplit(":", 1)[-1]


@pytest.mark.unit
class TestSerializerCodeReachesTheKey:
    def test_two_serializers_over_one_function_do_not_collide(self):
        """The reported bug: same function, same args, same namespace, different serializer."""
        backend_std = _RecordingBackend()
        backend_auto = _RecordingBackend()

        @cache(backend=backend_std, ttl=60, namespace="lab4351", serializer="std")
        def compute(x: int) -> dict:
            return {"result": x * 2}

        @cache(backend=backend_auto, ttl=60, namespace="lab4351", serializer="auto")
        def compute_auto(x: int) -> dict:
            return {"result": x * 2}

        # Same qualname is what makes this a collision test, so force it.
        compute_auto.__wrapped__.__qualname__ = compute.__wrapped__.__qualname__

        compute(5)
        compute_auto(5)

        (key_std,) = backend_std.store
        (key_auto,) = backend_auto.store
        assert _suffix(key_std) == "1s"
        assert _suffix(key_auto) == "1a"
        assert key_std != key_auto, "std and auto decorators still share a cache key"

    @pytest.mark.parametrize(("serializer", "expected"), [("auto", "1a"), ("pythonic", "1a")])
    def test_decorator_emits_the_serializer_code(self, serializer: str, expected: str):
        """The decorator, not the test, supplies the serializer. Fails without the fix."""
        backend = _RecordingBackend()

        @cache(backend=backend, ttl=60, namespace=f"lab4351-{serializer}", serializer=serializer)
        def fn(x: int) -> int:
            return x

        fn(1)
        (key,) = backend.store
        assert _suffix(key) == expected

    def test_invalidate_deletes_the_key_the_write_path_wrote(self):
        """invalidate_cache must delete the exact key the write path wrote, serializer suffix included."""
        backend = _RecordingBackend()
        calls = 0

        @cache(backend=backend, ttl=60, namespace="lab4351-inv", serializer="auto")
        def fn(x: int) -> int:
            nonlocal calls
            calls += 1
            return x

        fn(1)
        assert calls == 1
        (written_key,) = backend.store

        fn.invalidate_cache(1)
        assert written_key in backend.deleted, (
            f"invalidate_cache deleted {backend.deleted!r}, but the write path wrote {written_key!r}"
        )
        assert backend.store == {}

    def test_integrity_flag_still_independent_of_serializer_code(self):
        """The ic half of the suffix was correct before this fix and must stay so."""
        backend = _RecordingBackend()

        @cache(backend=backend, ttl=60, namespace="lab4351-ic", serializer="auto", integrity_checking=False)
        def fn(x: int) -> int:
            return x

        fn(1)
        (key,) = backend.store
        assert _suffix(key) == "0a"


@pytest.mark.unit
class TestSerializerCodeTable:
    def test_aliases_resolve_to_the_canonical_code(self):
        """Alias spellings must key identically to the name they alias."""

        def fn(x: int) -> int:
            return x

        gen = CacheKeyGenerator()

        def key(serializer: str) -> str:
            return gen.generate_key(fn, (1,), {}, None, True, serializer_type=serializer)

        assert key("std") == key("default")
        assert key("pythonic") == key("auto")
        assert key("default") != key("auto")

    @pytest.mark.parametrize("bad", [None, ["auto"]], ids=lambda v: type(v).__name__)
    def test_non_string_identity_is_rejected(self, bad: object):
        """Before the dict lookup — the unhashable case proves the guard runs first."""
        with pytest.raises(TypeError, match="serializer_type"):
            CacheKeyGenerator.serializer_code(bad)  # type: ignore[arg-type]

    def test_serializer_class_is_rejected_not_bucketed(self):
        """A class (missing "()") identifies as its metaclass, 'type' — one shared code for all."""
        from cachekit.serializers.standard_serializer import StandardSerializer

        with pytest.raises(TypeError, match="not the class StandardSerializer"):
            CacheSerializationHandler(StandardSerializer)  # type: ignore[arg-type]

    def test_empty_identity_is_rejected(self):
        """Not `or "default"`: a fallback code is a shared bucket, the bug this module exists to pin."""
        with pytest.raises(ValueError, match="serializer_type"):
            CacheKeyGenerator.serializer_code("")

    def test_code_tables_are_read_only(self):
        """A mutation here would silently re-key every entry process-wide."""
        with pytest.raises(TypeError):
            CacheKeyGenerator.SERIALIZER_CODES["evil"] = "s"  # type: ignore[index]
        with pytest.raises(TypeError):
            CacheKeyGenerator.SERIALIZER_NAME_ALIASES["evil"] = "default"  # type: ignore[index]

    def test_every_registered_serializer_has_a_code(self):
        """A name users can pass must never fall into the custom bucket by omission."""
        from cachekit.serializers import SERIALIZER_REGISTRY

        codes = CacheKeyGenerator.SERIALIZER_CODES
        aliases = CacheKeyGenerator.SERIALIZER_NAME_ALIASES
        # "encrypted" is registered but unreachable as a serializer name: without a master key
        # CacheSerializationHandler raises EncryptionError, and with one, an unstated intent raises
        # and encryption=True's CROSS_SDK_SERIALIZER_NAMES check rejects it. It has no keyspace to protect.
        unreachable = {"encrypted"}
        for name in SERIALIZER_REGISTRY:
            if name in unreachable:
                continue
            assert aliases.get(name, name) in codes, f"{name!r} is registered but has no serializer code"

    def test_custom_serializer_does_not_share_the_standard_keyspace(self):
        """A custom SerializerProtocol instance must not land on the default keyspace."""

        class MyCustomSerializer:
            cross_sdk_compatible = False

            def serialize(self, obj: Any) -> tuple[bytes, dict[str, Any]]:
                return b"", {}

            def deserialize(self, data: bytes, metadata: Any = None) -> Any:
                return None

        def fn(x: int) -> int:
            return x

        gen = CacheKeyGenerator()
        handler = CacheSerializationHandler(serializer_name=MyCustomSerializer())
        op = CacheOperationHandler(handler, gen)

        custom_key = op.get_cache_key(fn, (1,), {}, None)
        std_key = gen.generate_key(fn, (1,), {}, None, True, serializer_type="std")

        assert _suffix(custom_key).startswith("1" + CacheKeyGenerator.UNKNOWN_SERIALIZER_CODE)
        assert custom_key != std_key

    def test_custom_class_cannot_impersonate_a_builtin_serializer(self):
        """A class named after a built-in must not inherit that built-in's key code.

        The frame tag it writes IS the class name, so a class literally called `auto`
        also passes the deserialize-time serializer-mismatch guard — that guard compares
        the same string. Separating the keys is what keeps the two from ever meeting.
        """

        class auto:  # noqa: N801 - deliberately mimics the built-in's canonical name
            cross_sdk_compatible = False

            def serialize(self, obj: Any) -> tuple[bytes, dict[str, Any]]:
                return b"", {}

            def deserialize(self, data: bytes, metadata: Any = None) -> Any:
                return "impersonated"

        def fn(x: int) -> int:
            return x

        gen = CacheKeyGenerator()
        imposter = CacheSerializationHandler(serializer_name=auto())
        genuine = CacheSerializationHandler(serializer_name="auto")

        # Same frame tag — the mismatch guard cannot tell these apart.
        assert imposter._serializer_string_name == genuine._serializer_string_name == "auto"

        imposter_key = CacheOperationHandler(imposter, gen).get_cache_key(fn, (1,), {}, None)
        genuine_key = CacheOperationHandler(genuine, gen).get_cache_key(fn, (1,), {}, None)
        assert imposter_key != genuine_key, "a custom class named 'auto' claimed AutoSerializer's keyspace"
        assert _suffix(imposter_key).startswith("1x")
        assert _suffix(genuine_key) == "1a"

    def test_distinct_serializer_instances_get_distinct_codes(self):
        """The custom bucket must not be shared — a shared one IS the LAB-4351 bug.

        An instance is the only way to configure a built-in (``ArrowSerializer(...)``), so
        two instance-configured decorators over one function is a normal arrangement, not an
        exotic one. Collapsing them onto one code gives two decorators one key with different
        frame tags: each read fails the mismatch guard, evicts, and recomputes forever.
        """
        codes = {
            CacheKeyGenerator.serializer_code(CacheKeyGenerator.CUSTOM_SERIALIZER_PREFIX + name)
            for name in ("StandardSerializer", "AutoSerializer", "OrjsonSerializer", "ArrowSerializer", "MySerializer")
        }
        assert len(codes) == 5, f"serializer codes collapsed onto a shared bucket: {codes}"
        assert all(c.startswith(CacheKeyGenerator.UNKNOWN_SERIALIZER_CODE) for c in codes)
        # Stable across processes: derived from the identity, never from id()/hash().
        assert CacheKeyGenerator.serializer_code("<custom>:Foo") == CacheKeyGenerator.serializer_code("<custom>:Foo")

    def test_two_builtin_instances_do_not_evict_each_other(self):
        """End-to-end on one shared backend: the arrangement that reproduced the bug."""
        from cachekit.serializers.auto_serializer import AutoSerializer
        from cachekit.serializers.standard_serializer import StandardSerializer

        backend = _RecordingBackend()
        calls = 0

        @cache(backend=backend, ttl=60, namespace="lab4351-inst", serializer=StandardSerializer())
        def fa(x: int) -> dict:
            nonlocal calls
            calls += 1
            return {"v": x}

        @cache(backend=backend, ttl=60, namespace="lab4351-inst", serializer=AutoSerializer())
        def fb(x: int) -> dict:
            nonlocal calls
            calls += 1
            return {"v": x}

        # Same qualname is what makes this a collision test, so force it.
        fb.__wrapped__.__qualname__ = fa.__wrapped__.__qualname__

        for _ in range(3):
            fa(1)
            fb(1)

        assert len(backend.store) == 2, f"two serializer instances shared a key: {list(backend.store)}"
        assert calls == 2, f"expected 2 misses then hits, got {calls} calls — the two decorators are evicting each other"


def _pre_020_key(current_key: str) -> str:
    """The key a pre-0.20.0 release wrote for the same call: same key, serializer code ``s``.

    Derived from the key the write path produced, not from ``generate_key``, so the test
    pins the legacy key's shape independently of the code under test.
    """
    head, suffix = current_key.rsplit(":", 1)
    return f"{head}:{suffix[0]}s"


NON_DEFAULT_SERIALIZERS = [pytest.param("auto", id="auto"), pytest.param(StandardSerializer(), id="instance")]


class _FailOnKeysBackend(_RecordingBackend):
    """Recording backend whose delete raises for chosen keys, after recording the attempt."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_on: set[str] = set()

    def delete(self, key: str) -> bool:
        if key in self.fail_on:
            self.deleted.append(key)
            raise BackendError("delete refused", error_type=BackendErrorType.TRANSIENT)
        return super().delete(key)


@pytest.mark.unit
class TestInvalidationReachesPre020Keys:
    """`invalidate_cache(args)` also deletes the pre-0.20.0 `:{ic}s` entry (LAB-5288).

    Before LAB-4351 every generated key ended in `s`. After upgrading, a non-default
    serializer writes and invalidates a new key, so an erasure that deletes only the new
    key returns normally while the pre-upgrade copy survives to its TTL.
    """

    @pytest.mark.parametrize("serializer", NON_DEFAULT_SERIALIZERS)
    def test_sync_invalidate_deletes_current_and_legacy_key(self, serializer: Any):
        backend = _RecordingBackend()
        calls = 0

        @cache(backend=backend, ttl=None, namespace="lab5288-sync", serializer=serializer)
        def fn(x: int) -> dict:
            nonlocal calls
            calls += 1
            return {"v": x}

        fn(1)
        (current_key,) = backend.store
        legacy_key = _pre_020_key(current_key)
        assert legacy_key != current_key
        backend.store[legacy_key] = b"pre-0.20.0 plaintext copy"

        fn.invalidate_cache(1)

        assert legacy_key not in backend.store, "pre-0.20.0 entry survived invalidation"
        assert current_key not in backend.store
        fn(1)
        assert calls == 2

    @pytest.mark.parametrize("serializer", NON_DEFAULT_SERIALIZERS)
    async def test_async_invalidate_deletes_current_and_legacy_key(self, serializer: Any):
        backend = _RecordingBackend()
        calls = 0

        @cache(backend=backend, ttl=None, namespace="lab5288-async", serializer=serializer)
        async def fn(x: int) -> dict:
            nonlocal calls
            calls += 1
            return {"v": x}

        await fn(1)
        (current_key,) = backend.store
        legacy_key = _pre_020_key(current_key)
        assert legacy_key != current_key
        backend.store[legacy_key] = b"pre-0.20.0 plaintext copy"

        await fn.ainvalidate_cache(1)

        assert legacy_key not in backend.store, "pre-0.20.0 entry survived invalidation"
        assert current_key not in backend.store
        await fn(1)
        assert calls == 2

    def test_default_serializer_issues_exactly_one_delete(self):
        """Code `s` already is the legacy key: no second round-trip."""
        backend = _RecordingBackend()

        @cache(backend=backend, ttl=60, namespace="lab5288-default", serializer="default")
        def fn(x: int) -> int:
            return x

        fn(1)
        (current_key,) = backend.store
        assert _suffix(current_key) == "1s"
        backend.deleted.clear()

        fn.invalidate_cache(1)

        assert backend.deleted == [current_key]

    async def test_default_serializer_issues_exactly_one_delete_async(self):
        backend = _RecordingBackend()

        @cache(backend=backend, ttl=60, namespace="lab5288-default-async", serializer="default")
        async def fn(x: int) -> int:
            return x

        await fn(1)
        (current_key,) = backend.store
        backend.deleted.clear()

        await fn.ainvalidate_cache(1)

        assert backend.deleted == [current_key]

    @pytest.mark.parametrize("failing", ["legacy", "current"])
    def test_one_failed_delete_does_not_skip_the_other(self, failing: str):
        """Each delete is independent: a failure on one key still attempts the other."""
        backend = _FailOnKeysBackend()

        @cache(backend=backend, ttl=None, namespace="lab5288-fail", serializer="auto")
        def fn(x: int) -> int:
            return x

        fn(1)
        (current_key,) = backend.store
        legacy_key = _pre_020_key(current_key)
        backend.store[legacy_key] = b"old"
        backend.fail_on = {legacy_key if failing == "legacy" else current_key}
        backend.deleted.clear()

        fn.invalidate_cache(1)

        assert backend.deleted == [current_key, legacy_key]
        assert list(backend.store) == [legacy_key if failing == "legacy" else current_key]

    @pytest.mark.parametrize("failing", ["legacy", "current"])
    async def test_one_failed_delete_does_not_skip_the_other_async(self, failing: str):
        backend = _FailOnKeysBackend()

        @cache(backend=backend, ttl=None, namespace="lab5288-fail-async", serializer="auto")
        async def fn(x: int) -> int:
            return x

        await fn(1)
        (current_key,) = backend.store
        legacy_key = _pre_020_key(current_key)
        backend.store[legacy_key] = b"old"
        backend.fail_on = {legacy_key if failing == "legacy" else current_key}
        backend.deleted.clear()

        await fn.ainvalidate_cache(1)

        assert backend.deleted == [current_key, legacy_key]
        assert list(backend.store) == [legacy_key if failing == "legacy" else current_key]

    @pytest.mark.parametrize("mode", ["key=", "fast_mode", "interop"])
    def test_non_generated_keys_have_no_legacy_twin(self, mode: str):
        """Only a generated key carries a serializer code; other key modes issue one delete."""
        from cachekit.decorators.wrapper import create_cache_wrapper

        backend = _RecordingBackend()

        def fn(x: int) -> int:
            return x

        # key= is read only from DecoratorConfig (the @cache path); fast_mode is internal-only.
        if mode == "key=":
            wrapped = cache(backend=backend, l1_enabled=False, namespace="lab5288-mode", serializer="auto", key=str)(fn)
        elif mode == "interop":
            # Interop requires a cross-SDK serializer, so the default one: its generated key would
            # still differ from the interop key, so only the mode flag stops a second delete.
            wrapped = cache(backend=backend, l1_enabled=False, namespace="lab5288-mode", interop="lab5288_op")(fn)
        else:
            wrapped = create_cache_wrapper(
                fn, backend=backend, l1_enabled=False, namespace="lab5288-mode", serializer="auto", fast_mode=True
            )
        wrapped(1)
        (written_key,) = backend.store
        backend.deleted.clear()

        wrapped.invalidate_cache(1)

        assert backend.deleted == [written_key]

    def test_failed_legacy_delete_is_retried_by_no_args_invalidation(self):
        """A twin whose delete failed stays tracked, so the next whole-function invalidation retries it."""
        backend = _FailOnKeysBackend()

        @cache(backend=backend, ttl=None, namespace="lab5288-retry", serializer="auto", l1_enabled=False)
        def fn(x: int) -> int:
            return x

        fn(1)
        (current_key,) = backend.store
        legacy_key = _pre_020_key(current_key)
        backend.store[legacy_key] = b"old"
        backend.fail_on = {legacy_key}

        fn.invalidate_cache(1)
        assert list(backend.store) == [legacy_key]

        backend.fail_on = set()
        fn.invalidate_cache()
        assert backend.store == {}, "no-args invalidation did not retry the failed legacy delete"


# A raw key over 250 characters is stored as a prefix plus a hash of the whole key, so its
# pre-0.20.0 twin cannot be read off the current key's suffix the way _pre_020_key does.
# This literal is the key cachekit 0.19.0 wrote for `fn(1)` under the fixed module, qualname
# and namespace below, taken from the 0.19.0 wheel:
#   CacheKeyGenerator().generate_key(fn, (1,), {}, _LONG_NAMESPACE, True)
_LONG_NAMESPACE = "lab6361_" + "n" * 292
_V019_LONG_KEY = "ns:lab6361_nnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnn:8c42af11facf9cd7e05b02ead5c9bdfc"

_F = TypeVar("_F", bound=Callable[..., Any])


def _pin_identity(fn: _F) -> _F:
    """Fix the identity the key hashes, so the literal does not depend on how pytest imports this file."""
    fn.__module__ = "lab6361"
    fn.__qualname__ = "fn"
    return fn


@pytest.mark.unit
class TestHashedLegacyKeyMatchesV019:
    """The legacy twin of a hashed key is byte-identical to the key 0.19.0 wrote (LAB-6361).

    Both tests pin one identity, so they share a key: L1 is off, or the second would hit the
    first one's process-wide L1 entry and never reach the backend.
    """

    def test_sync_invalidate_deletes_the_v019_hashed_key(self):
        backend = _RecordingBackend()

        @cache(backend=backend, ttl=None, namespace=_LONG_NAMESPACE, serializer="auto", l1_enabled=False)
        @_pin_identity
        def fn(x: int) -> dict:
            return {"v": x}

        fn(1)
        (current_key,) = backend.store
        assert current_key != _V019_LONG_KEY
        backend.store[_V019_LONG_KEY] = b"0.19.0 copy"

        fn.invalidate_cache(1)

        assert backend.store == {}, "the key 0.19.0 wrote survived invalidation"

    async def test_async_invalidate_deletes_the_v019_hashed_key(self):
        backend = _RecordingBackend()

        @cache(backend=backend, ttl=None, namespace=_LONG_NAMESPACE, serializer="auto", l1_enabled=False)
        @_pin_identity
        async def fn(x: int) -> dict:
            return {"v": x}

        await fn(1)
        (current_key,) = backend.store
        assert current_key != _V019_LONG_KEY
        backend.store[_V019_LONG_KEY] = b"0.19.0 copy"

        await fn.ainvalidate_cache(1)

        assert backend.store == {}, "the key 0.19.0 wrote survived invalidation"


@pytest.mark.unit
class TestNoArgsInvalidationReachesPre020Keys:
    """No-args `invalidate_cache()`, `ainvalidate_cache()` and `cache_clear()` also delete the
    pre-0.20.0 twin of every generated key this process recorded.

    The write path records each key's twin next to the key, so both whole-function paths
    (the local sweep and the key-registry drain) delete it, count its failure and retry it
    like any recorded key. Keys this process never recorded stay out of reach.
    """

    @staticmethod
    def _write_and_seed_twin(fn: Any, backend: Any) -> tuple[str, str]:
        fn(1)
        (current_key,) = backend.store
        assert _suffix(current_key) == "1a", "key too long: it was hashed, so _pre_020_key cannot derive its twin"
        legacy_key = _pre_020_key(current_key)
        backend.store[legacy_key] = b"pre-0.20.0 copy"
        return current_key, legacy_key

    @pytest.mark.parametrize("backend_cls", [_RecordingBackend, TrackingBackend], ids=["local_sweep", "registry_drain"])
    @pytest.mark.parametrize("clear", ["invalidate_cache", "cache_clear"])
    def test_sync_no_args_deletes_the_twin(self, backend_cls: type, clear: str):
        backend = backend_cls()

        @cache(backend=backend, ttl=None, namespace=f"twin-s{backend_cls is TrackingBackend:d}{clear[0]}", serializer="auto")
        def fn(x: int) -> dict:
            return {"v": x}

        for _ in range(2):  # the second round re-records after the first trimmed everything
            self._write_and_seed_twin(fn, backend)
            getattr(fn, clear)()
            assert backend.store == {}, "the pre-0.20.0 twin survived a no-args invalidation"

    @pytest.mark.parametrize("backend_cls", [_RecordingBackend, TrackingBackend], ids=["local_sweep", "registry_drain"])
    async def test_async_no_args_deletes_the_twin(self, backend_cls: type):
        backend = backend_cls()

        @cache(backend=backend, ttl=None, namespace=f"twin-a{backend_cls is TrackingBackend:d}", serializer="auto")
        async def fn(x: int) -> dict:
            return {"v": x}

        for _ in range(2):
            await fn(1)
            (current_key,) = backend.store
            assert _suffix(current_key) == "1a"
            backend.store[_pre_020_key(current_key)] = b"pre-0.20.0 copy"

            await fn.ainvalidate_cache()

            assert backend.store == {}, "the pre-0.20.0 twin survived a no-args invalidation"

    def test_no_args_deletes_the_v019_hashed_twin(self):
        """A hashed key's twin comes from the legacy derivation, not from rewriting its suffix."""
        backend = _RecordingBackend()

        @cache(backend=backend, ttl=None, namespace=_LONG_NAMESPACE, serializer="auto", l1_enabled=False)
        @_pin_identity
        def fn(x: int) -> dict:
            return {"v": x}

        fn(1)
        backend.store[_V019_LONG_KEY] = b"0.19.0 copy"

        fn.invalidate_cache()

        assert backend.store == {}, "the key 0.19.0 wrote survived a no-args invalidation"

    def test_default_serializer_issues_one_delete_per_recorded_key(self):
        """Code `s` already is the legacy key: no twin is recorded, so no second delete."""
        backend = _RecordingBackend()

        @cache(backend=backend, ttl=60, namespace="twin-default", serializer="default")
        def fn(x: int) -> int:
            return x

        fn(1)
        fn(2)
        written = set(backend.store)
        backend.deleted.clear()

        fn.invalidate_cache()

        assert len(backend.deleted) == 2
        assert set(backend.deleted) == written

    def test_key_function_records_no_twin(self):
        backend = _RecordingBackend()

        @cache(backend=backend, ttl=60, namespace="twin-keyfn", serializer="auto", key=str)
        def fn(x: int) -> int:
            return x

        fn(1)
        (written_key,) = backend.store
        backend.deleted.clear()

        fn.invalidate_cache()

        assert backend.deleted == [written_key]

    @pytest.mark.parametrize(
        ("backend_cls", "tenant"),
        [(ScopedFlakyBackend, _flaky_tenant), (ScopedBackend, _registry_tenant)],
        ids=["local_sweep", "registry_drain"],
    )
    def test_another_tenants_no_args_call_leaves_the_twin(self, backend_cls: type, tenant: Any):
        """Tenant B's whole-function invalidation never deletes tenant A's twin."""
        backend = backend_cls()

        @cache(
            backend=backend,
            ttl=None,
            namespace=f"twin-t{backend_cls is ScopedBackend:d}",
            serializer="auto",
            l1_enabled=False,
        )
        def fn(x: int) -> dict:
            return {"v": x}

        token = tenant.set("a")
        try:
            fn(1)
            (a_key,) = backend.store
            assert _suffix(a_key) == "1a"
            a_twin = _pre_020_key(a_key)
            backend.store[a_twin] = b"tenant a pre-0.20.0 copy"
        finally:
            tenant.reset(token)

        token = tenant.set("b")
        try:
            fn.invalidate_cache()
        finally:
            tenant.reset(token)
        assert a_twin in backend.store, "tenant B's invalidation deleted tenant A's twin"

        token = tenant.set("a")
        try:
            fn.invalidate_cache()
        finally:
            tenant.reset(token)
        assert backend.store == {}

    def test_failed_twin_delete_is_counted_and_retried(self, caplog: pytest.LogCaptureFixture):
        backend = _FailOnKeysBackend()

        @cache(backend=backend, ttl=None, namespace="twin-retry", serializer="auto")
        def fn(x: int) -> dict:
            return {"v": x}

        current_key, legacy_key = self._write_and_seed_twin(fn, backend)
        backend.fail_on = {legacy_key}

        with caplog.at_level(logging.ERROR, logger="cachekit"):
            fn.invalidate_cache()

        records = [r for r in caplog.records if "Failed to delete" in r.getMessage()]
        assert [r.getMessage() for r in records] == [
            "Failed to delete 1 L2 key(s); they stay tracked for the next invalidate_cache()"
        ]
        assert list(backend.store) == [legacy_key]

        backend.fail_on = set()
        fn.invalidate_cache()
        assert backend.store == {}, "the next no-args invalidation did not retry the failed twin delete"
