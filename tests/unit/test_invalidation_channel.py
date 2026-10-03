"""Cross-process invalidation channel (cachekit.invalidation): what the decorator publishes, and when.

Every invalidation on a key-tracking backend publishes one event, and only after the L2 change it
announces succeeded. These tests pin the decorator's side against the in-memory tracking backend of
test_key_registry; tests/integration/test_invalidation_channel_redis.py runs it on real Redis.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import msgpack
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import NoPermissionError

from cachekit import cache, hash_utils, invalidation
from cachekit.backends.errors import BackendError
from tests.unit.test_key_registry import PlainBackend, TrackingBackend, _LegacyFormat, _registry_ids
from tests.unit.test_key_serializer_suffix import _pre_020_key

WRAPPER_LOGGER = "cachekit.decorators.wrapper"


@pytest.fixture(autouse=True)
def fresh_throttles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test opens its own WARNING windows: the throttles are process-wide."""
    monkeypatch.setattr(invalidation, "_publish_failed_warn", hash_utils._WarnThrottle())
    monkeypatch.setattr(invalidation, "_too_long_warn", hash_utils._WarnThrottle())


def _events(backend: TrackingBackend) -> list[dict[str, str]]:
    """The decoded events the backend's client published, oldest first."""
    assert {channel for channel, _ in backend._client.published} <= {invalidation.CHANNEL}
    return [msgpack.unpackb(payload) for _, payload in backend._client.published]


class _DeleteFails(TrackingBackend):
    """Tracking backend whose delete raises for chosen keys."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_on: set[str] = set()

    def delete(self, key: str) -> bool:
        if key in self.fail_on:
            raise BackendError("delete refused")
        return super().delete(key)


@pytest.mark.unit
class TestDrainPublishes:
    """invalidate_cache() with no args: one {r} event, only after a drain that returned."""

    def test_drain_publishes_the_registry_id_once(self) -> None:
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace="chan-drain")
        def f(x: int) -> int:
            return x

        for x in range(3):
            f(x)
        f.invalidate_cache()

        (rid,) = _registry_ids(backend)
        assert _events(backend) == [{"r": rid}]

    def test_published_after_the_drain_deleted_every_key(self) -> None:
        backend = TrackingBackend()
        seen_at_publish: list[dict[str, bytes]] = []
        publish = backend._client.publish

        def spy(channel: str, message: bytes) -> int:
            seen_at_publish.append(dict(backend.store))
            return publish(channel, message)

        backend._client.publish = spy  # type: ignore[method-assign]

        @cache(backend=backend, ttl=60, namespace="chan-order")
        def f(x: int) -> int:
            return x

        f(1)
        f.invalidate_cache()
        assert seen_at_publish == [{}]

    def test_failed_drain_publishes_nothing(self) -> None:
        backend = TrackingBackend()
        backend.fail_drain = True

        @cache(backend=backend, ttl=60, namespace="chan-drain-fail")
        def f(x: int) -> int:
            return x

        f(1)
        f.invalidate_cache()  # falls back to this process's own keys
        assert backend.store == {}
        assert _events(backend) == []

    def test_legacy_registry_drain_adds_no_event(self) -> None:
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace=_LegacyFormat("chan-legacy"))
        def f(x: int) -> int:
            return x

        f(1)
        (rid,) = _registry_ids(backend)
        f.invalidate_cache()
        assert len(backend.drain_calls) == 2  # the primary set and the pre-fix one
        assert _events(backend) == [{"r": rid}]

    def test_failed_legacy_drain_neither_adds_nor_gates_the_event(self) -> None:
        class LegacyFails(TrackingBackend):
            def drain_tracked(self, registry_id: str, local_keys: Any) -> set[str]:
                if registry_id.startswith("ck:reg:legacy:"):
                    raise BackendError("legacy drain failed")
                return super().drain_tracked(registry_id, local_keys)

        backend = LegacyFails()

        @cache(backend=backend, ttl=60, namespace=_LegacyFormat("chan-legacy-fail"))
        def f(x: int) -> int:
            return x

        f(1)
        (rid,) = _registry_ids(backend)
        f.invalidate_cache()
        assert _events(backend) == [{"r": rid}]

    async def test_async_drain_publishes_once_off_the_event_loop(self) -> None:
        backend = TrackingBackend()
        threads: list[threading.Thread] = []
        publish = backend._client.publish

        def spy(channel: str, message: bytes) -> int:
            threads.append(threading.current_thread())
            return publish(channel, message)

        backend._client.publish = spy  # type: ignore[method-assign]

        @cache(backend=backend, ttl=60, namespace="chan-drain-async")
        async def f(x: int) -> int:
            return x

        await f(1)
        await f.ainvalidate_cache()

        (rid,) = _registry_ids(backend)
        assert _events(backend) == [{"r": rid}]
        assert threads and threads[0] is not threading.current_thread()  # rode the drain's to_thread

    def test_cache_clear_publishes_like_a_drain(self) -> None:
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace="chan-clear")
        def f(x: int) -> int:
            return x

        f(1)
        f.cache_clear()
        (rid,) = _registry_ids(backend)
        assert _events(backend) == [{"r": rid}]


@pytest.mark.unit
class TestSingleKeyPublishes:
    """invalidate_cache(args): one {r, k} event for the current-format key, gated on its delete."""

    @pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
    async def test_one_event_for_the_current_key_when_a_pre_020_twin_exists(self, is_async: bool) -> None:
        backend = TrackingBackend()

        def f(x: int) -> dict[str, int]:
            return {"v": x}

        async def af(x: int) -> dict[str, int]:
            return {"v": x}

        fn = cache(backend=backend, ttl=60, namespace="chan-twin", serializer="auto")(af if is_async else f)
        if is_async:
            await fn(1)
        else:
            fn(1)
        (current_key,) = backend.store
        twin = _pre_020_key(current_key)
        assert twin != current_key
        backend.store[twin] = b"pre-0.20.0 copy"

        if is_async:
            await fn.ainvalidate_cache(1)
        else:
            fn.invalidate_cache(1)

        assert backend.deleted[-2:] == [current_key, twin]  # both deleted ...
        (rid,) = _registry_ids(backend)
        assert _events(backend) == [{"r": rid, "k": current_key}]  # ... one event

    def test_default_serializer_key_publishes_once(self) -> None:
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace="chan-single")
        def f(x: int) -> int:
            return x

        f(7)
        (key,) = backend.store
        f.invalidate_cache(7)
        (rid,) = _registry_ids(backend)
        assert _events(backend) == [{"r": rid, "k": key}]

    def test_zero_parameter_function_publishes_its_one_key(self) -> None:
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace="chan-zero-param")
        def f() -> int:
            return 1

        f()
        (key,) = backend.store
        f.invalidate_cache()  # no parameters: the single-key path, not a drain
        assert backend.drain_calls == []
        (rid,) = _registry_ids(backend)
        assert _events(backend) == [{"r": rid, "k": key}]

    @pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
    async def test_failed_current_key_delete_publishes_nothing(self, is_async: bool) -> None:
        backend = _DeleteFails()

        def f(x: int) -> dict[str, int]:
            return {"v": x}

        async def af(x: int) -> dict[str, int]:
            return {"v": x}

        fn = cache(backend=backend, ttl=60, namespace="chan-del-fail", serializer="auto")(af if is_async else f)
        if is_async:
            await fn(1)
        else:
            fn(1)
        (current_key,) = backend.store
        backend.fail_on = {current_key}

        if is_async:
            await fn.ainvalidate_cache(1)
        else:
            fn.invalidate_cache(1)

        assert current_key in backend.store
        assert _events(backend) == []

    def test_failed_twin_delete_still_publishes_once(self) -> None:
        backend = _DeleteFails()

        @cache(backend=backend, ttl=60, namespace="chan-twin-fail", serializer="auto")
        def f(x: int) -> dict[str, int]:
            return {"v": x}

        f(1)
        (current_key,) = backend.store
        twin = _pre_020_key(current_key)
        backend.store[twin] = b"pre-0.20.0 copy"
        backend.fail_on = {twin}

        f.invalidate_cache(1)
        (rid,) = _registry_ids(backend)
        assert _events(backend) == [{"r": rid, "k": current_key}]

    def test_custom_key_function_publishes_the_registry_id_only(self) -> None:
        """A key= key embeds caller identifiers: it never goes on the wire."""
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace="chan-custom", key=lambda user_id: f"user:{user_id}:alice@example.com")
        def f(user_id: int) -> int:
            return user_id

        f(1)
        f.invalidate_cache(1)
        (rid,) = _registry_ids(backend)
        assert _events(backend) == [{"r": rid}]
        assert b"alice" not in backend._client.published[0][1]

    def test_fast_mode_key_is_published(self) -> None:
        from cachekit.decorators.wrapper import create_cache_wrapper

        backend = TrackingBackend()

        def fn(x: int) -> int:
            return x

        f = create_cache_wrapper(fn, backend=backend, ttl=60, namespace="chan-fast", fast_mode=True)  # internal-only mode
        f(1)
        (key,) = backend.store
        f.invalidate_cache(1)
        (rid,) = _registry_ids(backend)
        assert _events(backend) == [{"r": rid, "k": key}]


@pytest.mark.unit
class TestPublishNeverFailsTheInvalidation:
    """A PUBLISH that fails is a WARNING: the L2 change stands and nothing is raised."""

    @pytest.mark.parametrize(
        "error",
        [
            NoPermissionError("NOPERM this user has no permissions to access the 'cachekit' channel"),
            RedisConnectionError(),
            RuntimeError(),
        ],
        ids=["acl-denied", "connection", "unexpected"],
    )
    @pytest.mark.parametrize("form", ["drain", "single-key"])
    def test_failed_publish_is_a_warning_and_the_invalidation_stands(
        self, error: BaseException, form: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        backend = TrackingBackend()
        backend._client.fail_publish = error

        @cache(backend=backend, ttl=60, namespace="chan-pub-fail")
        def f(x: int) -> int:
            return x

        f(1)
        (key,) = backend.store
        with caplog.at_level(logging.WARNING):
            if form == "drain":
                f.invalidate_cache()
            else:
                f.invalidate_cache(1)

        assert backend.store == {}
        if form == "drain":
            assert backend.sets == {}  # the drain's result stands: no fallback ran
            assert "invalidating local keys only" not in caplog.text
        (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert "Invalidation announcement failed" in warning.getMessage()
        assert type(error).__name__ in warning.getMessage()  # the type, never the message
        assert "NOPERM" not in caplog.text and key not in caplog.text
        assert not any(rid in caplog.text for rid in _registry_ids(backend))

    def test_failed_publishes_warn_once_a_window_with_the_count(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An ACL without the channel fails every PUBLISH: one WARNING a window, not one per call."""
        backend = TrackingBackend()
        backend._client.fail_publish = NoPermissionError("NOPERM")

        @cache(backend=backend, ttl=60, namespace="chan-pub-flood")
        def f(x: int) -> int:
            return x

        with caplog.at_level(logging.DEBUG, logger=invalidation.__name__):
            for x in range(50):
                f(x)
                f.invalidate_cache(x)
            monkeypatch.setattr(hash_utils, "_WARN_INTERVAL_SECONDS", 0.0)  # the window elapses
            f.invalidate_cache(0)
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        debugs = [r for r in caplog.records if r.levelno == logging.DEBUG and "announcement failed" in r.getMessage()]
        assert len(warnings) == 2 and len(debugs) == 49
        assert "failures since the last warning: 1)" in warnings[0]
        assert "failures since the last warning: 50)" in warnings[1]  # none lost from the count
        assert backend.store == {}

    def test_tracking_backend_without_a_redis_client_announces_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        """A third-party KeyTrackableBackend has no channel: no event, and no WARNING per call."""
        backend = TrackingBackend()
        del backend._client

        @cache(backend=backend, ttl=60, namespace="chan-third-party")
        def f(x: int) -> int:
            return x

        f(1)
        with caplog.at_level(logging.DEBUG, logger=invalidation.__name__):
            f.invalidate_cache(1)
            f(2)
            f.invalidate_cache()
        assert backend.store == {} and caplog.records == []

    async def test_failed_async_publish_never_escapes(self) -> None:
        backend = TrackingBackend()
        backend._client.fail_publish = RuntimeError("boom")

        @cache(backend=backend, ttl=60, namespace="chan-pub-fail-async")
        async def f(x: int) -> int:
            return x

        await f(1)
        await f.ainvalidate_cache(1)
        await f(2)
        await f.ainvalidate_cache()
        assert backend.store == {}

    @pytest.mark.parametrize("form", ["drain", "single-key"])
    def test_non_tracking_backend_never_publishes(self, form: str) -> None:
        backend = PlainBackend()

        @cache(backend=backend, ttl=60, namespace="chan-plain")
        def f(x: int) -> int:
            return x

        f(1)
        if form == "drain":
            f.invalidate_cache()
        else:
            f.invalidate_cache(1)
        assert backend.store == {}
        assert backend._client.published == []

    def test_l1_only_never_publishes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[Any] = []
        monkeypatch.setattr(invalidation, "publish", lambda *a: calls.append(a))

        @cache(backend=None, ttl=60, namespace="chan-l1-only")
        def f(x: int) -> int:
            return x

        f(1)
        f.invalidate_cache(1)
        f.invalidate_cache()
        assert calls == []

    def test_registry_id_over_the_cap_is_not_announced(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = TrackingBackend()
        namespace = "n" * 1100

        @cache(backend=backend, ttl=60, namespace=namespace)
        def f(x: int) -> int:
            return x

        f(1)
        with caplog.at_level(logging.WARNING):
            f.invalidate_cache()

        assert backend.store == {}  # the drain itself ran
        assert _events(backend) == []
        assert "registry id <redacted:" in caplog.text and "over 1024 bytes" in caplog.text
        assert namespace not in caplog.text


@pytest.mark.unit
class TestEventEncoding:
    """encode_event: byte-measured caps, {r} when the key cannot travel."""

    def test_key_at_the_cap_travels(self) -> None:
        key = "k" * 1024
        assert msgpack.unpackb(invalidation.encode_event("ck:reg:ns:00", key)) == {"r": "ck:reg:ns:00", "k": key}

    def test_cap_is_measured_in_utf8_bytes(self) -> None:
        cjk = "鍵" * 400  # 400 characters, 1200 bytes
        assert len(cjk) < 1024 < len(cjk.encode("utf-8"))
        assert msgpack.unpackb(invalidation.encode_event("ck:reg:ns:00", cjk)) == {"r": "ck:reg:ns:00"}

    def test_registry_id_over_the_cap_has_no_event(self) -> None:
        assert invalidation.encode_event("ck:reg:" + "鍵" * 400 + ":00", None) is None

    def test_publish_with_an_unencodable_key_is_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = TrackingBackend()
        with caplog.at_level(logging.WARNING, logger=invalidation.__name__):
            invalidation.publish(backend, "ck:reg:ns:00", "lone \ud800 surrogate")
        assert backend._client.published == []
        assert "Invalidation announcement failed" in caplog.text
        assert "\ud800" not in caplog.text
