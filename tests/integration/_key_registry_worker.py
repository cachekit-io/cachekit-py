"""Worker for the cross-process key registry test: one decorated function, importable by
module path from every process, so every process derives the same registry id."""

from __future__ import annotations

import os

import redis

from cachekit import cache
from cachekit.backends.redis.provider import PerRequestRedisBackend

NAMESPACE = "key_registry_xproc"


def lookup(x: int) -> int:
    return x * 10


def cached_lookup(client: redis.Redis, tenant: str = "default"):
    return cache(backend=PerRequestRedisBackend(client, tenant), ttl=300, namespace=NAMESPACE)(lookup)


def write(args: list[int]) -> None:
    """Process A: write one cache entry per argument, then exit."""
    client = redis.Redis.from_url(os.environ["CK_TEST_REDIS_URL"])
    fn = cached_lookup(client)
    for x in args:
        fn(x)
