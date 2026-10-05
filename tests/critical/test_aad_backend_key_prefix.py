"""CRITICAL PATH TEST: the AAD binds the backend's key prefix.

protocol ``spec/encryption.md`` defines the AAD ``cache_key`` as the logical backend key: every
client-side prefix, the backend's own included, followed by the call key. So an encrypted entry
copied to another key prefix on the same store must fail authentication and be recomputed, never
decrypted under the other prefix. A backend with no prefix keeps the AAD it always had.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from cachekit import cache
from cachekit.backends.file import FileBackend, FileBackendConfig
from cachekit.backends.memcached.backend import MemcachedBackend
from cachekit.backends.memcached.config import MemcachedBackendConfig
from cachekit.backends.redis.provider import PerRequestRedisBackend, tenant_context
from cachekit.cache_handler import CacheSerializationHandler
from cachekit.serializers.encryption_wrapper import EncryptionWrapper
from tests.utils.memcached_helpers import mock_hash_client

pytestmark = pytest.mark.critical

_KEY = "ab" * 32


@pytest.fixture
def memcached_store() -> Iterator[dict[str, bytes]]:
    """Every MemcachedBackend built in the test shares this dict as its server."""
    store: dict[str, bytes] = {}
    with patch("pymemcache.client.hash.HashClient") as cls:
        client = mock_hash_client()
        client.set.side_effect = lambda k, v, expire=0, noreply=True: store.__setitem__(k, v)
        client.get.side_effect = lambda k: store.get(k)
        client.delete.side_effect = lambda k, noreply=True: store.pop(k, None) is not None
        cls.return_value = client
        yield store


def _secure(backend: Any, calls: list[str], label: str, namespace: str, *, is_async: bool = False, **kwargs: Any) -> Any:
    """A secure cached function. Same name and namespace on every call, so every one of them
    builds the same cache key; ``label`` tells which one computed a value."""
    decorator = cache.secure(master_key=_KEY, ttl=300, namespace=namespace, backend=backend, **kwargs)
    if is_async:

        async def lookup(x: int) -> str:
            calls.append(label)
            return f"{label}:{x}"

    else:

        def lookup(x: int) -> str:
            calls.append(label)
            return f"{label}:{x}"

    return decorator(lookup)


async def _call(fn: Any, x: int) -> Any:
    result = fn(x)
    return await result if inspect.isawaitable(result) else result


def _text(key: str | bytes) -> str:
    return key.decode() if isinstance(key, bytes) else key


def _redis_entry(client: Any, tenant: str) -> str:
    """The one cache entry under ``tenant``'s prefix (the key registry also lives there)."""
    [key] = [_text(k) for k in client.keys(f"t:{tenant}:ns:*")]
    return key


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
async def test_memcached_entry_moved_to_another_key_prefix_fails_authentication(
    memcached_store: dict[str, bytes], is_async: bool
) -> None:
    calls: list[str] = []
    ns = f"aad_prefix_mc_{is_async}"

    def backend(prefix: str) -> MemcachedBackend:
        return MemcachedBackend(MemcachedBackendConfig(key_prefix=prefix))

    write = _secure(backend("app-a:"), calls, "a", ns, is_async=is_async, l1_enabled=False)
    same_prefix = _secure(backend("app-a:"), calls, "a-again", ns, is_async=is_async, l1_enabled=False)
    other_prefix = _secure(backend("app-b:"), calls, "b", ns, is_async=is_async, l1_enabled=False)

    assert await _call(write, 1) == "a:1"
    assert await _call(same_prefix, 1) == "a:1"  # same prefix: the round trip authenticates
    assert calls == ["a"]

    [key] = [k for k in memcached_store if k.startswith("app-a:")]
    memcached_store["app-b:" + key.removeprefix("app-a:")] = memcached_store[key]

    assert await _call(other_prefix, 1) == "b:1"  # the moved entry failed authentication
    assert calls == ["a", "b"]


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
async def test_tenant_scoped_redis_entry_moved_to_another_tenant_fails_authentication(
    redis_test_client: Any, is_async: bool
) -> None:
    """Same master key and single-tenant mode on both sides: only the ``t:{tenant}:`` prefix differs."""
    client = redis_test_client
    calls: list[str] = []
    ns = f"aad_prefix_redis_{is_async}"
    write = _secure(PerRequestRedisBackend(client, "tenant-a"), calls, "a", ns, is_async=is_async, l1_enabled=False)
    same = _secure(PerRequestRedisBackend(client, "tenant-a"), calls, "a-again", ns, is_async=is_async, l1_enabled=False)
    other = _secure(PerRequestRedisBackend(client, "tenant-b"), calls, "b", ns, is_async=is_async, l1_enabled=False)

    assert await _call(write, 1) == "a:1"
    assert await _call(same, 1) == "a:1"
    assert calls == ["a"]

    key = _redis_entry(client, "tenant-a")
    client.set(key.replace("t:tenant-a:", "t:tenant-b:", 1), client.get(key))

    assert await _call(other, 1) == "b:1"
    assert calls == ["a", "b"]


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
async def test_shared_l1_entry_of_another_tenant_is_a_miss_not_tamper(redis_test_client: Any, is_async: bool) -> None:
    """L1 is keyed by the bare cache key, so behind a context-following backend it holds one tenant's
    entry for all of them. Its AAD binds that tenant's prefix: another tenant's read is an L1 miss
    that goes on to L2, and never raises, even under fail_closed."""
    calls: list[str] = []
    backend = PerRequestRedisBackend(redis_test_client, "default", follow_context=True)
    fn = _secure(backend, calls, "fn", f"aad_prefix_l1_{is_async}", is_async=is_async, fail_closed=True)

    async def as_tenant(tenant: str) -> Any:
        token = tenant_context.set(tenant)
        try:
            return await _call(fn, 1), tenant
        finally:
            tenant_context.reset(token)

    assert await as_tenant("tenant-a") == ("fn:1", "tenant-a")
    assert await as_tenant("tenant-b") == ("fn:1", "tenant-b")
    assert calls == ["fn", "fn"]  # tenant-b was not served tenant-a's L1 entry
    assert await as_tenant("tenant-a") == ("fn:1", "tenant-a")  # tenant-a's own L2 entry
    assert calls == ["fn", "fn"]


def test_entry_written_without_the_prefix_fails_authentication(memcached_store: dict[str, bytes]) -> None:
    """No fallback: an entry whose AAD binds the bare key (what earlier releases wrote through a
    prefixed backend) is not retried with that key. It fails authentication and is recomputed."""
    calls: list[str] = []
    fn = _secure(MemcachedBackend(MemcachedBackendConfig(key_prefix="app:")), calls, "fn", "aad_prefix_legacy", l1_enabled=False)
    assert fn(1) == "fn:1"
    [key] = [k for k in memcached_store if k.startswith("app:")]

    legacy = CacheSerializationHandler(encryption=True, single_tenant_mode=True, master_key=_KEY)
    memcached_store[key] = legacy.serialize_data("legacy", cache_key=key.removeprefix("app:"))

    assert fn(1) == "fn:1"
    assert calls == ["fn", "fn"]


def test_unprefixed_backend_aad_binds_the_bare_cache_key(tmp_path: Any) -> None:
    """A backend with no key prefix keeps the AAD earlier releases built. The AAD is built from the
    bare cache key, and a handler with no backend attached (so no prefix) decrypts the stored entry
    under that key: AES-GCM authenticates only if the AAD bytes are identical."""
    backend = FileBackend(FileBackendConfig(cache_dir=str(tmp_path)))
    calls: list[str] = []
    aad_keys: list[str] = []
    real_create_aad = EncryptionWrapper._create_aad

    def spy(self: EncryptionWrapper, metadata: Any, cache_key: str) -> bytes:
        aad_keys.append(cache_key)
        return real_create_aad(self, metadata, cache_key)

    fn = _secure(backend, calls, "fn", "aad_prefix_none", l1_enabled=False)
    with patch.object(EncryptionWrapper, "_create_aad", spy):
        assert fn(1) == "fn:1"
    [cache_key] = aad_keys
    assert cache_key.startswith("ns:aad_prefix_none:func:")  # the bare key, nothing in front

    handler = CacheSerializationHandler(encryption=True, single_tenant_mode=True, master_key=_KEY)
    stored = backend.get(cache_key)
    assert stored is not None
    assert handler.deserialize_data(stored, cache_key=cache_key) == "fn:1"
