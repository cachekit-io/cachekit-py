"""Invalidation channel on real Redis: what the decorator publishes on cachekit:py:invalidate:v1."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

import msgpack
import pytest
import redis

from cachekit import cache, invalidation
from cachekit.backends.errors import BackendError
from cachekit.backends.redis import provider as provider_module
from cachekit.backends.redis.provider import PerRequestRedisBackend
from tests.integration import _key_registry_worker as worker

pytestmark = pytest.mark.integration


@pytest.fixture
def client(redis_test_client: redis.Redis) -> redis.Redis:
    return redis_test_client


@pytest.fixture
def subscriber(client: redis.Redis) -> Iterator[redis.client.PubSub]:
    """A raw subscription to the channel, confirmed before the test publishes anything."""
    pubsub = client.pubsub()
    pubsub.subscribe(invalidation.CHANNEL)
    confirmation = pubsub.get_message(timeout=5)
    assert confirmation is not None and confirmation["type"] == "subscribe"
    yield pubsub
    pubsub.close()


def _received(pubsub: redis.client.PubSub, wait: float = 0.5) -> list[dict[str, str]]:
    """Every event delivered within ``wait`` seconds, decoded."""
    events: list[dict[str, str]] = []
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        message = pubsub.get_message(timeout=0.05)
        if message is not None and message["type"] == "message":
            events.append(msgpack.unpackb(message["data"]))
    return events


def _registry_id(client: redis.Redis) -> str:
    (key,) = client.keys("t:default:ck:reg:*")
    return key.decode().removeprefix("t:default:")


def _client_as(client: redis.Redis, username: str, password: str) -> redis.Redis:
    """A client for the same server, logged in as another ACL user."""
    pool = client.connection_pool
    kwargs: dict[str, Any] = {**pool.connection_kwargs, "username": username, "password": password}
    return redis.Redis(connection_pool=redis.ConnectionPool(connection_class=pool.connection_class, **kwargs))


class TestDrainAnnouncement:
    def test_three_round_drain_publishes_once(
        self, client: redis.Redis, subscriber: redis.client.PubSub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(provider_module, "_DRAIN_CHUNK", 2)
        fn = cache(backend=PerRequestRedisBackend(client, "default"), ttl=300, namespace="chan_rounds")(worker.lookup)
        for x in range(5):
            fn(x)
        rid = _registry_id(client)
        script_calls = 0
        real_register = client.register_script

        def counting_register(script: str) -> Any:
            real = real_register(script)

            def call(*args: Any, **kwargs: Any) -> Any:
                nonlocal script_calls
                script_calls += 1
                return real(*args, **kwargs)

            return call

        monkeypatch.setattr(client, "register_script", counting_register)
        fn.invalidate_cache()

        assert script_calls == 3  # 2 + 2 + 1 members
        assert _received(subscriber) == [{"r": rid}]

    def test_round_guard_return_publishes_once(
        self, client: redis.Redis, subscriber: redis.client.PubSub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard stops the drain with members left and returns: the one deliberate
        announcement of a drain that left keys behind (they wait for the next drain)."""
        monkeypatch.setattr(provider_module, "_DRAIN_CHUNK", 2)
        monkeypatch.setattr(provider_module, "_DRAIN_MAX_ROUNDS", 2)
        fn = cache(backend=PerRequestRedisBackend(client, "default"), ttl=300, namespace="chan_guard")(worker.lookup)
        for x in range(6):
            fn(x)
        rid = _registry_id(client)

        fn.invalidate_cache()

        assert client.scard(f"t:default:{rid}") == 2  # left for the next drain
        assert _received(subscriber) == [{"r": rid}]

    def test_round_guard_that_raises_publishes_nothing(
        self, client: redis.Redis, subscriber: redis.client.PubSub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the guard raises instead of returning, the drain failed: no announcement."""
        monkeypatch.setattr(provider_module, "_DRAIN_CHUNK", 2)
        monkeypatch.setattr(provider_module, "_DRAIN_MAX_ROUNDS", 2)
        real_drain = PerRequestRedisBackend.drain_tracked

        def guard_raises(self: PerRequestRedisBackend, registry_id: str, local_keys: Any) -> set[str]:
            real_drain(self, registry_id, local_keys)
            if self._client.scard(self._scoped_key(registry_id)):
                raise BackendError("drain stopped at the round guard", operation="drain_tracked")
            return set()

        monkeypatch.setattr(PerRequestRedisBackend, "drain_tracked", guard_raises)
        fn = cache(backend=PerRequestRedisBackend(client, "default"), ttl=300, namespace="chan_guard_raise")(worker.lookup)
        for x in range(6):
            fn(x)

        fn.invalidate_cache()
        assert _received(subscriber) == []

    def test_failed_straggler_unlink_publishes_nothing(
        self, client: redis.Redis, subscriber: redis.client.PubSub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(provider_module, "_DRAIN_CHUNK", 2)
        fn = cache(backend=PerRequestRedisBackend(client, "default"), ttl=300, namespace="chan_straggler")(worker.lookup)
        for x in range(5):
            fn(x)
        for reg in client.keys("*ck:reg:*"):
            client.delete(reg)  # every key is a straggler: 3 UNLINK batches of <= 2
        real_unlink = client.unlink
        calls = 0

        def flaky_unlink(*keys: str) -> int:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise redis.ConnectionError("lost")
            return real_unlink(*keys)

        monkeypatch.setattr(client, "unlink", flaky_unlink)
        fn.invalidate_cache()  # falls back to this process's own keys

        assert calls == 2
        assert _received(subscriber) == []

    def test_publish_denied_by_acl_leaves_the_drain_standing(
        self, client: redis.Redis, subscriber: redis.client.PubSub, caplog: pytest.LogCaptureFixture
    ) -> None:
        user, password = "ck-chan-nopub", "ck-chan-nopub-pw"  # pragma: allowlist secret - throwaway ACL user
        client.acl_setuser(
            user, enabled=True, passwords=[f"+{password}"], keys=["*"], channels=["*"], commands=["+@all", "-publish"]
        )
        try:
            restricted = _client_as(client, user, password)
            fn = cache(backend=PerRequestRedisBackend(restricted, "default"), ttl=300, namespace="chan_acl")(worker.lookup)
            for x in range(3):
                fn(x)
            with caplog.at_level(logging.WARNING):
                fn.invalidate_cache()

            assert not client.keys("t:default:*")  # every key and the tracking set are gone
            assert "invalidating local keys only" not in caplog.text
            assert "Invalidation announcement failed" in caplog.text and "NoPermissionError" in caplog.text
            assert _received(subscriber) == []
            restricted.close()
        finally:
            client.acl_deluser(user)


class TestSingleKeyAnnouncement:
    def test_one_event_for_the_current_key(self, client: redis.Redis, subscriber: redis.client.PubSub) -> None:
        fn = cache(backend=PerRequestRedisBackend(client, "default"), ttl=300, namespace="chan_single", serializer="auto")(
            worker.lookup
        )
        fn(1)
        (scoped,) = [k for k in client.keys("t:default:*") if b":ck:reg:" not in k]
        current_key = scoped.decode().removeprefix("t:default:")
        rid = _registry_id(client)

        fn.invalidate_cache(1)

        assert _received(subscriber) == [{"r": rid, "k": current_key}]

    def test_custom_key_announces_the_function(self, client: redis.Redis, subscriber: redis.client.PubSub) -> None:
        fn = cache(
            backend=PerRequestRedisBackend(client, "default"), ttl=300, namespace="chan_custom", key=lambda x: f"user-{x}"
        )(worker.lookup)
        fn(1)
        rid = _registry_id(client)

        fn.invalidate_cache(1)

        assert _received(subscriber) == [{"r": rid}]
