"""Worker for the cross-process key registry and invalidation channel tests: one decorated
function, importable by module path from every process, so every process derives the same
registry id."""

from __future__ import annotations

import os
import time

import redis

from cachekit import cache
from cachekit.backends.redis.provider import PerRequestRedisBackend

NAMESPACE = "key_registry_xproc"


def lookup(x: int) -> int:
    return x * 10


def cached_lookup(client: redis.Redis, tenant: str = "default", namespace: str = NAMESPACE):
    return cache(backend=PerRequestRedisBackend(client, tenant), ttl=300, namespace=namespace)(lookup)


def write(args: list[int]) -> None:
    """Process A: write one cache entry per argument, then exit."""
    client = redis.Redis.from_url(os.environ["CK_TEST_REDIS_URL"])
    fn = cached_lookup(client)
    for x in args:
        fn(x)


def invalidate(args: list[int], namespace: str = NAMESPACE) -> None:
    """Process A: invalidate one entry per argument (no args: the whole function), print when the
    last invalidation returned, then exit. No listener runs here unless the environment sets one."""
    client = redis.Redis.from_url(os.environ["CK_TEST_REDIS_URL"])
    fn = cached_lookup(client, namespace=namespace)
    if args:
        for x in args:
            fn.invalidate_cache(x)
    else:
        fn.invalidate_cache()
    print(time.time())
