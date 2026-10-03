"""Cross-process invalidation listener (cachekit.invalidation): decoding, dispatch, eviction, start.

The listener's Redis side (two processes, forks, reconnects) runs in
tests/integration/test_invalidation_channel_redis.py; these tests pin the parts that need no server.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import multiprocessing
import os
import queue as queue_mod
import subprocess
import sys
import threading
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Optional

import msgpack
import pytest
import redis

from cachekit import cache, hash_utils, invalidation, l1_cache
from cachekit.backends.redis.client import create_connection_pool
from cachekit.backends.redis.provider import PerRequestRedisBackend
from cachekit.config import DecoratorConfig
from cachekit.config.settings import CachekitConfig
from cachekit.config.singleton import reset_settings
from tests.unit.test_key_registry import PlainBackend, ScopedBackend, TrackingBackend, _closure_cell, _registry_ids, _tenant

INVALIDATION_LOGGER = invalidation.__name__
LISTENER_THREAD = "cachekit-invalidation-listener"


@pytest.fixture(autouse=True)
def listener_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test starts with the flag unset, no listener, no retry pending and a fresh WARNING
    window, and leaves no listener behind."""
    monkeypatch.delenv("CACHEKIT_INVALIDATION_LISTENER_ENABLED", raising=False)
    reset_settings()
    monkeypatch.setattr(invalidation, "_start_retry_at", float("-inf"))
    monkeypatch.setattr(invalidation, "_untrackable_warned", {})
    monkeypatch.setattr(invalidation, "_start_locks", {})
    monkeypatch.setattr(invalidation, "_dropped_event_warn", hash_utils._WarnThrottle())
    monkeypatch.setattr(invalidation, "_listener_error_warn", hash_utils._WarnThrottle())
    yield
    invalidation._stop_listener()


def _listen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set CACHEKIT_INVALIDATION_LISTENER_ENABLED for this test, as an operator would."""
    monkeypatch.setenv("CACHEKIT_INVALIDATION_LISTENER_ENABLED", "true")
    reset_settings()


def _listener_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == LISTENER_THREAD]


def _message(data: object) -> dict[str, Any]:
    """What redis-py hands a channel handler."""
    return {"type": "message", "pattern": None, "channel": invalidation.CHANNEL.encode(), "data": data}


def _nested_maps(depth: int) -> bytes:
    """``depth`` one-entry maps nested inside each other: 3 bytes a level."""
    return b"\x81\xa1a" * depth + b"\xc0"


_SIZE = "bytes of at most 4096 expected"
_SHAPE = "a map with a string registry id expected"
_KEY = "the key is not a string"


@pytest.mark.unit
class TestDecodeFloor:
    """decode_event: untrusted bytes are bounded before and during decoding."""

    @pytest.mark.parametrize(
        ("data", "error", "match"),
        [
            pytest.param(b"\x80" + b"x" * 4096, ValueError, _SIZE, id="oversize"),
            pytest.param(
                msgpack.packb({"r": "r" * 1000, "k": "k" * 1000, "a" * 1000: "a" * 1000, "b" * 1000: "b" * 1000}),
                ValueError,
                _SIZE,
                id="oversize-but-decodable",  # within every per-field cap: only the size bound stops it
            ),
            pytest.param("ck:reg:ns:00", ValueError, _SIZE, id="str-not-bytes"),
            pytest.param(None, ValueError, _SIZE, id="none"),
            pytest.param(b"\x91\xa1x", ValueError, r"exceeds max_array_len\(0\)", id="array"),
            pytest.param(b"\x01", ValueError, _SHAPE, id="int"),
            pytest.param(msgpack.packb("ck:reg:ns:00"), ValueError, _SHAPE, id="bare-string"),
            pytest.param(msgpack.packb({"k": "x"}), ValueError, _SHAPE, id="no-r"),
            pytest.param(msgpack.packb({"r": 1}), ValueError, _SHAPE, id="int-r"),
            pytest.param(msgpack.packb({"r": None}), ValueError, _SHAPE, id="nil-r"),
            pytest.param(
                msgpack.packb({"r": b"ck:reg:ns:00"}, use_bin_type=True), ValueError, r"exceeds max_bin_len\(0\)", id="bin-r"
            ),
            pytest.param(  # decodes to a valid event if bin values are allowed
                msgpack.packb({"r": "ck:reg:ns:00", "b": b"x"}, use_bin_type=True),
                ValueError,
                r"exceeds max_bin_len\(0\)",
                id="bin-extra-field",
            ),
            pytest.param(msgpack.packb({"r": "ck:reg:ns:00", "k": 7}), ValueError, _KEY, id="int-k"),
            pytest.param(msgpack.packb({"r": "ck:reg:ns:00", "k": None}), ValueError, _KEY, id="nil-k"),
            pytest.param(
                msgpack.packb({"r": "ck:reg:ns:00", "k": ["a"]}), ValueError, r"exceeds max_array_len\(0\)", id="array-k"
            ),
            pytest.param(_nested_maps(1300), msgpack.exceptions.StackError, None, id="deep"),
            pytest.param(
                msgpack.packb({str(i): "x" for i in range(5)}), ValueError, r"exceeds max_map_len\(4\)", id="five-entries"
            ),
            pytest.param(  # decodes to a valid event if a map may hold five entries
                msgpack.packb({"r": "x", "a": 1, "b": 2, "c": 3, "d": 4}),
                ValueError,
                r"exceeds max_map_len\(4\)",
                id="five-entries-valid-r",
            ),
            pytest.param(msgpack.packb({"r": "x" * 1025}), ValueError, r"exceeds max_str_len\(1024\)", id="long-r"),
            pytest.param(
                msgpack.packb({"r": "ck:reg:ns:00", "t": msgpack.Timestamp(0)}),
                ValueError,
                r"exceeds max_ext_len\(0\)",
                id="ext",
            ),
            pytest.param(b"\x81\xa1r\xa2\xff\xfe", UnicodeDecodeError, "can't decode byte 0xff", id="invalid-utf8"),
            pytest.param(b"\x81\xa1r", ValueError, "incomplete input", id="truncated"),
            pytest.param(
                msgpack.packb({"r": "ck:reg:ns:00"}) + b"\x00", msgpack.exceptions.ExtraData, "extra data", id="trailing-bytes"
            ),
        ],
    )
    def test_rejects(self, data: object, error: type[Exception], match: Optional[str]) -> None:
        """Each case fails on its own bound: the error text names it, so removing that bound fails it."""
        with pytest.raises(error, match=match):
            invalidation.decode_event(data)

    def test_accepts_both_event_kinds_and_ignores_extra_fields(self) -> None:
        assert invalidation.decode_event(msgpack.packb({"r": "a", "k": "b", "x": 1, "y": None})) == ("a", "b")
        assert invalidation.decode_event(msgpack.packb({"r": "a"})) == ("a", None)

    def test_two_fields_at_the_cap_fit_the_size_bound(self) -> None:
        event = invalidation.encode_event("r" * 1024, "k" * 1024)
        assert event is not None and len(event) <= 4096
        assert invalidation.decode_event(event) == ("r" * 1024, "k" * 1024)


@pytest.mark.unit
class TestOnMessage:
    """_on_message: decode, then hand the key to every evictor of the registry id; never raise."""

    def test_event_reaches_every_evictor_of_its_registry_id(self) -> None:
        got: list[tuple[str, Optional[str]]] = []

        def first(key: Optional[str]) -> None:
            got.append(("first", key))

        def second(key: Optional[str]) -> None:
            got.append(("second", key))

        def other(key: Optional[str]) -> None:
            got.append(("other", key))

        invalidation.register("ck:reg:t:aaaa", first)
        invalidation.register("ck:reg:t:aaaa", second)
        invalidation.register("ck:reg:t:bbbb", other)

        invalidation._on_message(_message(invalidation.encode_event("ck:reg:t:aaaa", "k1")))
        invalidation._on_message(_message(invalidation.encode_event("ck:reg:t:aaaa", None)))

        assert sorted(got, key=repr) == [("first", "k1"), ("first", None), ("second", "k1"), ("second", None)]

    def test_unknown_registry_id_is_dropped_at_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG, logger=INVALIDATION_LOGGER):
            invalidation._on_message(_message(invalidation.encode_event("ck:reg:t:never-registered", "k")))
        (record,) = caplog.records
        assert record.levelno == logging.DEBUG and "does not cache" in record.getMessage()
        assert "never-registered" not in caplog.text

    @pytest.mark.parametrize(
        "data",
        [
            msgpack.packb({"r": "SECRET-registry-text", "k": 1}),
            msgpack.packb({"r": "ck:reg:t:aaaa", "k": "SECRET-key-text" * 100}),
            b"SECRET-raw-bytes",
            _nested_maps(1300),
        ],
        ids=["bad-k", "long-k", "garbage", "deep"],
    )
    def test_bad_event_is_dropped_with_a_warning_that_carries_no_event_text(
        self, data: bytes, caplog: pytest.LogCaptureFixture
    ) -> None:
        called: list[Optional[str]] = []

        def evict(key: Optional[str]) -> None:
            called.append(key)

        invalidation.register("ck:reg:t:aaaa", evict)
        with caplog.at_level(logging.DEBUG, logger=INVALIDATION_LOGGER):
            invalidation._on_message(_message(data))  # never raises

        assert called == []
        (record,) = caplog.records
        assert record.levelno == logging.WARNING
        assert record.getMessage().startswith("Invalidation event dropped (drops since the last warning: 1)")
        assert "SECRET" not in caplog.text and "aaaa" not in caplog.text

    def test_bad_events_warn_once_a_window_with_the_count(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Whoever can publish on the channel decides how many bad events arrive: one WARNING a
        window, the rest at DEBUG, and the next WARNING counts them all."""
        with caplog.at_level(logging.DEBUG, logger=INVALIDATION_LOGGER):
            for _ in range(5):
                invalidation._on_message(_message(b"SECRET-garbage"))
            monkeypatch.setattr(hash_utils, "_WARN_INTERVAL_SECONDS", 0.0)  # the window elapses
            invalidation._on_message(_message(b"SECRET-garbage"))
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        debugs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
        assert len(warnings) == 2 and len(debugs) == 4
        assert warnings[0].startswith("Invalidation event dropped (drops since the last warning: 1)")
        assert warnings[1].startswith("Invalidation event dropped (drops since the last warning: 5)")  # none lost
        assert all(m.startswith("Invalidation event dropped: ") for m in debugs)
        assert "SECRET" not in caplog.text

    def test_registration_during_dispatch_drops_no_event(self, caplog: pytest.LogCaptureFixture) -> None:
        """A decoration landing mid-dispatch, here from inside an evictor, must not abort the event:
        dispatch walks a snapshot, so every evictor registered before the event still gets it.
        Walking the live set would raise "Set changed size during iteration" and drop the event."""
        got: list[str] = []
        late: list[Any] = []

        def first(key: Optional[str]) -> None:
            got.append("first")

            def late_evictor(key: Optional[str]) -> None:
                got.append("late")

            late.append(late_evictor)  # keep it alive
            invalidation.register("ck:reg:t:during", late_evictor)

        def second(key: Optional[str]) -> None:
            got.append("second")

        invalidation.register("ck:reg:t:during", first)
        invalidation.register("ck:reg:t:during", second)

        with caplog.at_level(logging.WARNING, logger=INVALIDATION_LOGGER):
            invalidation._on_message(_message(invalidation.encode_event("ck:reg:t:during", "k")))
        assert sorted(got) == ["first", "second"]
        assert caplog.records == []

        got.clear()
        invalidation._on_message(_message(invalidation.encode_event("ck:reg:t:during", "k")))
        assert sorted(got) == ["first", "late", "second"]  # the one first registered meanwhile waits for the next event


@pytest.mark.unit
class TestRegistration:
    """A backed wrapper with an L1 registers its evictor at decoration, weakly."""

    def test_backed_wrapper_with_l1_registers(self) -> None:
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace="reg-backed")
        def f(x: int) -> int:
            return x

        f(1)
        (rid,) = _registry_ids(backend)
        assert invalidation._evictors_for(rid) == [f._cachekit_evict]  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        "kwargs", [{"backend": None}, {"backend": "tracking", "l1_enabled": False}], ids=["l1-only", "no-l1"]
    )
    def test_wrapper_without_shared_l1_does_not_register(self, kwargs: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
        registered: list[str] = []
        monkeypatch.setattr(invalidation, "register", lambda rid, evict: registered.append(rid))
        if kwargs["backend"] == "tracking":
            kwargs = {**kwargs, "backend": TrackingBackend()}

        @cache(ttl=60, namespace="reg-none", **kwargs)
        def f(x: int) -> int:
            return x

        f(1)
        assert registered == []

    def test_discarded_wrapper_drops_out(self) -> None:
        backend = TrackingBackend()

        def f(x: int) -> int:
            return x

        wrapped = cache(backend=backend, ttl=60, namespace="reg-gc")(f)
        wrapped(1)
        (rid,) = _registry_ids(backend)
        assert len(invalidation._evictors_for(rid)) == 1
        del wrapped
        gc.collect()
        assert invalidation._evictors_for(rid) == []


def _l1_keys(fn: Any) -> set[str]:
    l1 = _closure_cell(fn, "_l1_cache").cell_contents
    return set(l1._state.cache)


@pytest.mark.unit
class TestEvict:
    """_evict: L1 only, never _cached_keys (tenant-blind, may race a re-record)."""

    def test_single_key_event_evicts_only_that_key(self) -> None:
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace="evict-one")
        def f(x: int) -> int:
            return x

        f(1)
        f(2)
        k1, k2 = list(backend.store)
        f._cachekit_evict(k1)  # type: ignore[attr-defined]
        assert _l1_keys(f) == {k2}
        assert {key for _, key in _closure_cell(f, "_cached_keys").cell_contents} == {k1, k2}

    def test_whole_function_event_evicts_every_recorded_key_and_keeps_the_records(self) -> None:
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace="evict-all")
        def f(x: int) -> int:
            return x

        for x in range(3):
            f(x)
        records = set(_closure_cell(f, "_cached_keys").cell_contents)
        f._cachekit_evict(None)  # type: ignore[attr-defined]
        assert _l1_keys(f) == set()
        assert _closure_cell(f, "_cached_keys").cell_contents == records
        assert len(backend.store) == 3  # L2 is the publisher's job, never the receiver's

    def test_write_recorded_during_an_event_survives_it(self) -> None:
        """An entry _put_l1 records while _evict runs keeps its record, even when its tracking
        failed and that record is the only thing left that can reach the L2 entry."""
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace="evict-race")
        def f(x: int) -> int:
            return x

        f(1)
        (key1,) = backend.store
        l1 = _closure_cell(f, "_l1_cache").cell_contents
        real = l1.invalidate_many

        def write_meanwhile(keys: Any) -> None:
            backend.fail_track = True
            f(99)  # a miss: L2 write, failed track_key, _put_l1 records it
            backend.fail_track = False
            real(keys)

        l1.invalidate_many = write_meanwhile
        try:
            f._cachekit_evict(None)  # type: ignore[attr-defined]
        finally:
            del l1.invalidate_many
        (key99,) = set(backend.store) - {key1}
        assert key99 in {key for _, key in _closure_cell(f, "_cached_keys").cell_contents}
        assert key99 not in {key for _, key in backend.track_calls if key in backend.sets.get(_, set())}

        backend.sets.clear()  # the registry never saw it: only this process's record reaches it
        f.invalidate_cache()
        assert backend.store == {}

    def test_another_tenants_record_survives_a_tenant_blind_event(self) -> None:
        backend = ScopedBackend()

        @cache(backend=backend, ttl=60, namespace="evict-tenants")
        def f(x: int) -> int:
            return x

        token = _tenant.set("a")
        try:
            f(1)
        finally:
            _tenant.reset(token)
        token = _tenant.set("b")
        try:
            f(2)
            f._cachekit_evict(None)  # type: ignore[attr-defined]
            scopes = {scope for scope, _ in _closure_cell(f, "_cached_keys").cell_contents}
            assert scopes == {"t:a:", "t:b:"}
            assert _l1_keys(f) == set()  # L1 is tenant-blind: both copies go
        finally:
            _tenant.reset(token)

        token = _tenant.set("a")
        try:
            backend.sets.clear()
            f.invalidate_cache()  # tenant a's record still reaches tenant a's entry
        finally:
            _tenant.reset(token)
        assert [k for k in backend.store if k.startswith("t:a:")] == []
        assert len([k for k in backend.store if k.startswith("t:b:")]) == 1


class _ListenerBackend(TrackingBackend):
    """Tracking backend whose listener pool is scripted by the test."""

    def __init__(self, pool_error: Optional[BaseException] = None) -> None:
        super().__init__()
        self.pool_error = pool_error
        self.pool_threads: list[threading.Thread] = []

    def listener_pool(self) -> Any:
        self.pool_threads.append(threading.current_thread())
        if self.pool_error is not None:
            raise self.pool_error
        raise AssertionError("no listener pool in unit tests")


@pytest.mark.unit
class TestListenerStart:
    """When a cache operation starts the listener, and what it does when it cannot."""

    def test_no_preset_and_no_decorator_option_reaches_the_flag(self) -> None:
        assert "invalidation_listener_enabled" not in DecoratorConfig.__dataclass_fields__
        assert CachekitConfig().invalidation_listener_enabled is False

    def test_flag_follows_the_settings_after_a_reset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The flag is read from cachekit's settings on each check, with no copy of its own, so
        reset_settings() reaches it."""
        backend = _ListenerBackend()
        assert invalidation.listener_start_due(backend) is False  # unset, the default
        monkeypatch.setenv("CACHEKIT_INVALIDATION_LISTENER_ENABLED", "true")
        reset_settings()
        assert invalidation.listener_start_due(backend) is True
        monkeypatch.setenv("CACHEKIT_INVALIDATION_LISTENER_ENABLED", "false")
        reset_settings()
        assert invalidation.listener_start_due(backend) is False

    def test_flag_unset_opens_no_thread_and_no_connection(self) -> None:
        backend = _ListenerBackend()

        @cache(backend=backend, ttl=60, namespace="start-off")
        def f(x: int) -> int:
            return x

        for x in range(3):
            f(x)
        assert backend.pool_threads == []
        assert _listener_threads() == []

    async def test_flag_unset_async_opens_no_thread_and_no_connection(self) -> None:
        backend = _ListenerBackend()

        @cache(backend=backend, ttl=60, namespace="start-off-async")
        async def f(x: int) -> int:
            return x

        for x in range(3):
            await f(x)
        assert backend.pool_threads == []

    def test_flag_on_with_a_backend_that_cannot_listen_warns_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _listen(monkeypatch)
        first, second = PlainBackend(), PlainBackend()

        @cache(backend=first, ttl=60, namespace="start-plain-a")
        def f(x: int) -> int:
            return x

        @cache(backend=second, ttl=60, namespace="start-plain-b")
        def g(x: int) -> int:
            return x

        with caplog.at_level(logging.WARNING, logger=INVALIDATION_LOGGER):
            for x in range(3):
                f(x)
                g(x)
        (record,) = caplog.records
        assert "PlainBackend does not carry invalidation events" in record.getMessage()
        assert invalidation._listener_pid is None and _listener_threads() == []

    def test_a_running_listener_does_not_hide_the_warning_for_a_backend_that_cannot_listen(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Another function's listener already runs in this process: a function on a backend that
        carries no events still gets the one-time WARNING."""
        _listen(monkeypatch)
        monkeypatch.setattr(invalidation, "_listener_pid", os.getpid())
        with caplog.at_level(logging.WARNING, logger=INVALIDATION_LOGGER):
            assert invalidation.listener_start_due(_ListenerBackend()) is False  # already listening
            assert invalidation.listener_start_due(PlainBackend()) is False
            assert invalidation.listener_start_due(PlainBackend()) is False
        (record,) = caplog.records
        assert "PlainBackend does not carry invalidation events" in record.getMessage()

    def test_failed_start_warns_and_waits_before_retrying(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _listen(monkeypatch)
        backend = _ListenerBackend(pool_error=redis.ConnectionError("Error 111 connecting to secret-host:6379"))

        @cache(backend=backend, ttl=60, namespace="start-fail")
        def f(x: int) -> int:
            return x

        with caplog.at_level(logging.WARNING, logger=INVALIDATION_LOGGER):
            for x in range(3):
                assert f(x) == x  # the cache operation itself is unaffected
        assert len(backend.pool_threads) == 1  # retried no sooner than _START_RETRY_SECONDS
        (record,) = caplog.records
        assert record.getMessage() == (
            "Invalidation listener failed to start; the next cache operation that reaches Redis after 60 s retries it: "
            "ConnectionError"
        )
        assert "secret-host" not in caplog.text

        monkeypatch.setattr(invalidation, "_start_retry_at", float("-inf"))  # the retry window passed
        f(4)
        assert len(backend.pool_threads) == 2

    def test_start_in_progress_is_not_waited_for(self) -> None:
        backend = _ListenerBackend()
        held, release = threading.Event(), threading.Event()
        starter = threading.Thread(target=_hold, args=(invalidation._pid_lock(invalidation._start_locks), held, release))
        starter.start()  # another thread is starting the listener
        assert held.wait(5)
        try:
            began = time.monotonic()
            invalidation.start_listener(backend)
            assert time.monotonic() - began < 0.5  # the cache operation went on without waiting
        finally:
            release.set()
            starter.join(5)
        assert backend.pool_threads == []

    def test_start_that_raced_a_failed_one_waits_out_the_retry_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A thread that passed listener_start_due just before another thread's start failed finds
        the retry window under the lock, so it does not retry at once."""
        backend = _ListenerBackend()
        monkeypatch.setattr(invalidation, "_start_retry_at", time.monotonic() + 60)  # the other start just failed
        invalidation.start_listener(backend)
        assert backend.pool_threads == []

    async def test_async_start_runs_off_the_loop_and_the_call_does_not_wait_for_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _listen(monkeypatch)
        backend = _ListenerBackend(pool_error=redis.ConnectionError("down"))
        connecting = threading.Event()
        real_pool = backend.listener_pool

        def slow_pool() -> Any:
            connecting.wait(5)  # a start stuck on a slow connect
            return real_pool()

        backend.listener_pool = slow_pool  # type: ignore[method-assign]

        @cache(backend=backend, ttl=60, namespace="start-async")
        async def f(x: int) -> int:
            return x

        assert await f(1) == 1
        assert backend.pool_threads == []  # returned while the start was still connecting
        connecting.set()
        for _ in range(500):
            if backend.pool_threads:
                break
            await asyncio.sleep(0.01)
        assert len(backend.pool_threads) == 1 and backend.pool_threads[0] is not threading.current_thread()

    def test_flag_on_with_a_tracking_backend_that_cannot_clone_a_pool_warns_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A KeyTrackableBackend other than the tenant-scoped Redis backend has no listener pool."""
        _listen(monkeypatch)
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace="start-tracking-no-pool")
        def f(x: int) -> int:
            return x

        with caplog.at_level(logging.WARNING, logger=INVALIDATION_LOGGER):
            for x in range(3):
                f(x)
        (record,) = caplog.records
        assert "TrackingBackend does not carry invalidation events" in record.getMessage()
        assert invalidation._listener_pid is None and invalidation._start_retry_at == float("-inf")

    def test_child_forked_without_hooks_starts_nothing_and_logs_nothing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _listen(monkeypatch)
        monkeypatch.setattr(l1_cache, "_import_pid", -1)  # a fork made from C: no at-fork hook ran here
        monkeypatch.setattr(l1_cache, "_hooked_pid", None)
        monkeypatch.setattr(invalidation, "_listener_pid", os.getppid())  # the parent's listener, inherited
        listening, plain = _ListenerBackend(), PlainBackend()

        with caplog.at_level(logging.DEBUG, logger=INVALIDATION_LOGGER):
            assert invalidation.listener_start_due(listening) is False
            assert invalidation.listener_start_due(plain) is False  # not even the one-time WARNING
        assert listening.pool_threads == [] and caplog.records == []

    def test_a_failing_check_never_fails_the_cache_operation(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def broken() -> bool:
            raise ValueError("settings exploded")

        monkeypatch.setattr(invalidation, "_listener_enabled", broken)

        @cache(backend=TrackingBackend(), ttl=60, namespace="start-broken")
        def f(x: int) -> int:
            return x

        with caplog.at_level(logging.WARNING, logger=INVALIDATION_LOGGER):
            assert f(1) == 1
        assert "Invalidation listener check failed: ValueError" in caplog.text


class _FakeWorker:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True

    def join(self, timeout: Optional[float] = None) -> None:
        pass


class _FakeConnection:
    def __init__(self) -> None:
        self.disconnects = 0

    def disconnect(self) -> None:
        self.disconnects += 1


# What redis-py's get_message() returns for Redis's reply to SUBSCRIBE and to a PING.
_SUBSCRIBED = {"type": "subscribe", "pattern": None, "channel": invalidation.CHANNEL.encode(), "data": 1}
_PONG = {"type": "pong", "pattern": None, "channel": None, "data": b""}


class _FakePubSub:
    """A PubSub talking to a scripted Redis. get_message() takes the next item of ``inbox``: a
    reply is returned, an exception (a refusal) raised, and a message handed to its channel's
    handler, as redis-py does. ping() adds ``pong`` to the inbox, or nothing if it is None. With
    the inbox empty, a read waits out its timeout. The worker hits ``error`` before run_in_thread()
    returns, the earliest it can."""

    def __init__(self, inbox: Optional[list[Any]] = None, pong: Any = _PONG, error: Optional[BaseException] = None) -> None:
        self.inbox = [_SUBSCRIBED] if inbox is None else list(inbox)
        self.pong = pong
        self.error = error
        self.connection = _FakeConnection()
        self.worker = _FakeWorker()
        self.handlers: dict[str, Any] = {}
        self.timeouts: list[float] = []
        self.closed = False

    def subscribe(self, **handlers: Any) -> None:
        self.handlers.update(handlers)

    def ping(self) -> None:
        if self.pong is not None:
            self.inbox.append(self.pong)

    def get_message(self, timeout: float) -> Optional[dict[str, Any]]:
        self.timeouts.append(timeout)
        if not self.inbox:
            time.sleep(timeout)
            return None
        reply = self.inbox.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if reply["type"] == "message":
            self.handlers[invalidation.CHANNEL](reply)
            return None
        return reply

    def run_in_thread(self, sleep_time: float, daemon: bool, exception_handler: Any) -> _FakeWorker:
        self.worker.started = True
        if self.error is not None:
            exception_handler(self.error, self, self.worker)
        return self.worker

    def close(self) -> None:
        self.closed = True


def _serve(monkeypatch: pytest.MonkeyPatch, pubsub: _FakePubSub) -> None:
    """Make the listener's redis.Redis(connection_pool=...).pubsub() return ``pubsub``."""
    monkeypatch.setattr(redis, "Redis", lambda connection_pool: types.SimpleNamespace(pubsub=lambda: pubsub))


_POOL_ONLY = types.SimpleNamespace(listener_pool=lambda: None)  # a backend for start_listener with _serve


@pytest.mark.unit
class TestListenerConfirm:
    """A start waits for Redis to confirm the SUBSCRIBE and answer a PING; a refusal or silence fails it."""

    @pytest.mark.parametrize(
        ("inbox", "pong", "error"),
        [
            pytest.param(
                [redis.exceptions.NoPermissionError("NOPERM no channel")], _PONG, "NoPermissionError", id="subscribe-refused"
            ),
            pytest.param(
                [_SUBSCRIBED], redis.exceptions.NoPermissionError("NOPERM no ping"), "NoPermissionError", id="ping-refused"
            ),
            pytest.param([], _PONG, "TimeoutError", id="no-subscribe-reply"),
            pytest.param([_SUBSCRIBED], None, "TimeoutError", id="no-pong"),
        ],
    )
    def test_a_start_redis_does_not_confirm_fails_and_waits_for_a_later_cache_operation(
        self, inbox: list[Any], pong: Any, error: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(invalidation, "_CONFIRM_SECONDS", 0.2)
        pubsub = _FakePubSub(inbox, pong)
        _serve(monkeypatch, pubsub)
        with caplog.at_level(logging.WARNING, logger=INVALIDATION_LOGGER):
            invalidation.start_listener(_POOL_ONLY)
        assert invalidation._listener is None and invalidation._listener_pid is None
        assert invalidation._start_retry_at > time.monotonic() + 50  # the next cache operation after the window retries
        assert pubsub.closed and not pubsub.worker.started
        (record,) = caplog.records
        assert record.getMessage().startswith("Invalidation listener failed to start")
        assert record.getMessage().endswith(f": {error}") and "NOPERM" not in caplog.text

    def test_one_window_covers_both_replies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The PING gets what the SUBSCRIBE's reply left of _CONFIRM_SECONDS, not a window of its own."""
        monkeypatch.setattr(invalidation, "_CONFIRM_SECONDS", 1.0)
        pubsub = _FakePubSub()
        read = pubsub.get_message

        def late_subscribe_reply(timeout: float) -> Optional[dict[str, Any]]:
            if not pubsub.timeouts:
                time.sleep(0.3)
            return read(timeout)

        pubsub.get_message = late_subscribe_reply  # type: ignore[method-assign]
        _serve(monkeypatch, pubsub)
        invalidation.start_listener(_POOL_ONLY)
        assert invalidation._listener is pubsub.worker
        subscribe_wait, pong_wait = pubsub.timeouts
        assert subscribe_wait > 0.9 and pong_wait < 0.75

    def test_an_event_published_during_the_confirmation_reaches_its_evictors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        got: list[tuple[Optional[str], threading.Thread]] = []

        def evict(key: Optional[str]) -> None:
            got.append((key, threading.current_thread()))

        invalidation.register("ck:reg:t:confirm", evict)
        event = _message(invalidation.encode_event("ck:reg:t:confirm", "k1"))
        pubsub = _FakePubSub([_SUBSCRIBED, event])  # after the SUBSCRIBE's reply, before the PONG
        _serve(monkeypatch, pubsub)
        invalidation.start_listener(_POOL_ONLY)
        assert got == [("k1", threading.current_thread())]  # dispatched by the start itself
        assert invalidation._listener is pubsub.worker and invalidation._listener_pid == os.getpid()

    def test_a_sync_operation_that_starts_the_listener_reads_l2_after_the_confirmation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []

        class Backend(TrackingBackend):
            def listener_pool(self) -> Any:
                return None  # _serve's redis.Redis ignores it

            def get(self, key: str) -> Optional[bytes]:
                order.append("l2-read")
                return super().get(key)

        pubsub = _FakePubSub()
        read = pubsub.get_message

        def recorded(timeout: float) -> Optional[dict[str, Any]]:
            reply = read(timeout)
            if reply is not None:
                order.append(reply["type"])
            return reply

        pubsub.get_message = recorded  # type: ignore[method-assign]
        _serve(monkeypatch, pubsub)
        _listen(monkeypatch)

        @cache(backend=Backend(), ttl=60, namespace="confirm-sync")
        def f(x: int) -> int:
            return x

        assert f(1) == 1
        assert order == ["subscribe", "pong", "l2-read"]
        assert invalidation._listener is pubsub.worker


@pytest.mark.unit
class TestListenerErrors:
    """The worker thread's exception handler: it retries and never changes who owns the listener."""

    def test_a_refused_subscription_waits_then_reconnects_to_subscribe_again(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        pubsub, slept = _FakePubSub(), []
        monkeypatch.setattr(invalidation, "_listener", pubsub.worker)
        monkeypatch.setattr(invalidation, "_listener_pid", os.getpid())
        monkeypatch.setattr(invalidation.time, "sleep", slept.append)
        with caplog.at_level(logging.WARNING, logger=INVALIDATION_LOGGER):
            invalidation._on_listener_error(redis.exceptions.NoPermissionError("NOPERM no channel"), pubsub, pubsub.worker)
        assert slept == [invalidation._RESUBSCRIBE_SECONDS] and pubsub.connection.disconnects == 1
        assert not pubsub.worker.stopped  # the thread's next read reconnects, and on_connect subscribes again
        assert invalidation._listener is pubsub.worker and invalidation._listener_pid == os.getpid()
        assert "Invalidation listener refused by Redis" in caplog.text and "NOPERM" not in caplog.text

    def test_a_connection_error_waits_a_second_and_keeps_the_connection_to_redis_py(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pubsub, slept = _FakePubSub(), []
        monkeypatch.setattr(invalidation, "_listener", pubsub.worker)
        monkeypatch.setattr(invalidation, "_listener_pid", os.getpid())
        monkeypatch.setattr(invalidation.time, "sleep", slept.append)
        invalidation._on_listener_error(redis.ConnectionError("lost"), pubsub, pubsub.worker)
        assert slept == [1.0] and pubsub.connection.disconnects == 0 and not pubsub.worker.stopped
        assert invalidation._listener is pubsub.worker and invalidation._listener_pid == os.getpid()

    def test_connection_errors_warn_once_a_window_with_the_count(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A Redis outage fails the listener once a second, per process: one WARNING a window, the
        rest at DEBUG, and the next WARNING counts them all. Every error still waits its second."""
        pubsub, slept = _FakePubSub(), []
        monkeypatch.setattr(invalidation.time, "sleep", slept.append)
        with caplog.at_level(logging.DEBUG, logger=INVALIDATION_LOGGER):
            for _ in range(5):
                invalidation._on_listener_error(redis.ConnectionError("SECRET lost"), pubsub, pubsub.worker)
            monkeypatch.setattr(hash_utils, "_WARN_INTERVAL_SECONDS", 0.0)  # the window elapses
            invalidation._on_listener_error(redis.ConnectionError("SECRET lost"), pubsub, pubsub.worker)
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        debugs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
        assert len(warnings) == 2 and len(debugs) == 4
        assert warnings[0].startswith("Invalidation listener error (errors since the last warning: 1)")
        assert warnings[1].startswith("Invalidation listener error (errors since the last warning: 5)")  # none lost
        assert all(m.startswith("Invalidation listener error; retrying in 1 s: ") for m in debugs)
        assert slept == [1.0] * 6 and "SECRET" not in caplog.text

    def test_a_refusal_before_the_start_records_the_thread_leaves_a_listener_that_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The worker can fail before run_in_thread() returns, here on an ACL change just after the
        start confirmed the subscription. Its handler changes no ownership, so the start still
        records a live thread, and that thread keeps subscribing again."""
        pubsub = _FakePubSub(error=redis.exceptions.NoPermissionError("NOPERM"))
        _serve(monkeypatch, pubsub)
        monkeypatch.setattr(invalidation.time, "sleep", lambda seconds: None)

        invalidation.start_listener(_POOL_ONLY)

        assert invalidation._listener is pubsub.worker and invalidation._listener_pid == os.getpid()
        assert not pubsub.worker.stopped and pubsub.connection.disconnects == 1


def _hold(lock: threading.Lock, held: threading.Event, release: threading.Event) -> None:
    with lock:
        held.set()
        release.wait(10)


@pytest.mark.unit
@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() not available on this platform")
class TestForkedChildLocks:
    """A child forked while a parent thread holds a start or dispatch lock never waits on it."""

    @pytest.mark.parametrize("held_lock", ["start", "dispatch"])
    def test_child_does_not_block_on_a_lock_held_at_fork(self, held_lock: str) -> None:
        locks = invalidation._start_locks if held_lock == "start" else invalidation._dispatch_locks
        held, release = threading.Event(), threading.Event()
        holder = threading.Thread(target=_hold, args=(invalidation._pid_lock(locks), held, release), daemon=True)
        holder.start()
        assert held.wait(5)
        try:
            ctx = multiprocessing.get_context("fork")
            results = ctx.Queue()

            def child(q: Any) -> None:
                def evict(key: Optional[str]) -> None:
                    pass

                invalidation.register("ck:reg:t:fork", evict)  # takes the dispatch lock
                start_free = invalidation._pid_lock(invalidation._start_locks).acquire(timeout=2)
                q.put({"evictors": len(invalidation._evictors_for("ck:reg:t:fork")), "start_lock_free": start_free})

            process = ctx.Process(target=child, args=(results,))
            process.start()
            try:
                outcome = results.get(timeout=20)
            except queue_mod.Empty:
                outcome = "child blocked on a lock its parent held at fork"
            finally:
                process.join(timeout=10)
                if process.is_alive():
                    process.kill()
            assert outcome == {"evictors": 1, "start_lock_free": True}
        finally:
            release.set()
            holder.join(5)


@pytest.mark.unit
class TestListenerPool:
    """PerRequestRedisBackend.listener_pool: a clone that keeps the transport (CWE-319)."""

    @staticmethod
    def _backend(url: str) -> PerRequestRedisBackend:
        return PerRequestRedisBackend(redis.Redis(connection_pool=create_connection_pool(url)), "default")

    def test_tls_url_keeps_ssl_connection(self) -> None:
        backend = self._backend("rediss://user:pw@cache.example:6380/2")  # pragma: allowlist secret
        source, clone = backend._client.connection_pool, backend.listener_pool()
        assert clone is not source and type(clone) is redis.ConnectionPool
        assert clone.connection_class is redis.SSLConnection
        assert clone.max_connections == 1
        kwargs = clone.connection_kwargs
        assert kwargs["health_check_interval"] == 10 and kwargs["socket_keepalive"] is True
        assert kwargs["decode_responses"] is False
        for name in ("host", "port", "db", "username", "password", "socket_timeout", "socket_connect_timeout"):
            assert kwargs[name] == source.connection_kwargs[name], name
        assert isinstance(clone.make_connection(), redis.SSLConnection)  # builds without connecting

    def test_unix_socket_url_keeps_unix_connection_without_keepalive(self) -> None:
        backend = self._backend("unix:///run/redis/redis.sock?db=3")
        clone = backend.listener_pool()
        assert clone.connection_class is redis.UnixDomainSocketConnection
        assert clone.max_connections == 1 and clone.connection_kwargs["health_check_interval"] == 10
        assert "socket_keepalive" not in clone.connection_kwargs  # a Unix socket connection rejects it
        assert clone.connection_kwargs["path"] == "/run/redis/redis.sock"
        assert isinstance(clone.make_connection(), redis.UnixDomainSocketConnection)  # no TypeError

    def test_plain_tcp_url(self) -> None:
        clone = self._backend("redis://cache.example:6379/0").listener_pool()
        assert clone.connection_class is redis.Connection
        assert clone.connection_kwargs["socket_keepalive"] is True

    async def test_clone_inside_a_with_timeout_window_keeps_the_configured_timeout(self) -> None:
        backend = self._backend("redis://cache.example:6379/0")
        configured = backend._client.connection_pool.connection_kwargs["socket_timeout"]
        async with backend.with_timeout("get", 50):
            assert backend._client.connection_pool.connection_kwargs["socket_timeout"] == 0.05  # the window's
            async with backend.with_timeout("set", 10):  # nested
                assert backend.listener_pool().connection_kwargs["socket_timeout"] == configured
            assert backend.listener_pool().connection_kwargs["socket_timeout"] == configured
        assert backend.listener_pool().connection_kwargs["socket_timeout"] == configured
        assert backend._client.connection_pool.connection_kwargs["socket_timeout"] == configured

    async def test_window_opened_through_another_backend_on_the_same_pool(self) -> None:
        """RedisBackendProvider hands out a new backend object per call over one shared pool."""
        client = redis.Redis(connection_pool=create_connection_pool("redis://cache.example:6379/0"))
        shared, per_request = PerRequestRedisBackend(client, "default"), PerRequestRedisBackend(client, "tenant-a")
        configured = client.connection_pool.connection_kwargs["socket_timeout"]
        async with per_request.with_timeout("get", 50):
            assert shared.listener_pool().connection_kwargs["socket_timeout"] == configured


@pytest.mark.unit
class TestUwsgiWarning:
    """One WARNING under uWSGI unless py-call-uwsgi-fork-hooks runs Python's at-fork hooks in workers."""

    @pytest.fixture
    def fake_uwsgi(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        module = types.ModuleType("uwsgi")
        monkeypatch.setitem(sys.modules, "uwsgi", module)
        return module

    def test_warns_without_any_fork_option(self, fake_uwsgi: Any, caplog: pytest.LogCaptureFixture) -> None:
        fake_uwsgi.opt = {"master": True, "processes": b"4"}
        with caplog.at_level(logging.WARNING, logger=INVALIDATION_LOGGER):
            invalidation._warn_if_uwsgi_skips_fork_hooks()
        (record,) = caplog.records
        message = record.getMessage()
        assert "--enable-threads --py-call-uwsgi-fork-hooks" in message and "osafterfork" not in message

    @pytest.mark.parametrize("option", ["py-call-osafterfork", "lazy-apps", "lazy"])
    def test_no_other_fork_option_silences_it(self, option: str, fake_uwsgi: Any, caplog: pytest.LogCaptureFixture) -> None:
        """py-call-osafterfork aborts every worker on 3.13+; under lazy-apps a worker's thread can hang."""
        fake_uwsgi.opt = {"master": True, "enable-threads": True, option: True}
        with caplog.at_level(logging.WARNING, logger=INVALIDATION_LOGGER):
            invalidation._warn_if_uwsgi_skips_fork_hooks()
        assert len(caplog.records) == 1

    @pytest.mark.parametrize("extra", [{}, {"lazy-apps": True}])
    def test_fork_hooks_silence_it(self, extra: dict[str, bool], fake_uwsgi: Any, caplog: pytest.LogCaptureFixture) -> None:
        fake_uwsgi.opt = {"master": True, "py-call-uwsgi-fork-hooks": True, **extra}
        with caplog.at_level(logging.WARNING, logger=INVALIDATION_LOGGER):
            invalidation._warn_if_uwsgi_skips_fork_hooks()
        assert caplog.records == []

    def test_never_from_a_child_forked_without_hooks(
        self, fake_uwsgi: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake_uwsgi.opt = {}
        monkeypatch.setattr(l1_cache, "_import_pid", -1)
        monkeypatch.setattr(l1_cache, "_hooked_pid", None)
        with caplog.at_level(logging.WARNING, logger=INVALIDATION_LOGGER):
            invalidation._warn_if_uwsgi_skips_fork_hooks()
        assert caplog.records == []

    def test_silent_outside_uwsgi(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        monkeypatch.setitem(sys.modules, "uwsgi", None)  # import uwsgi raises ImportError
        with caplog.at_level(logging.WARNING, logger=INVALIDATION_LOGGER):
            invalidation._warn_if_uwsgi_skips_fork_hooks()
            sys.modules["uwsgi"] = types.ModuleType("uwsgi")  # importable, but no opt: not uWSGI
            invalidation._warn_if_uwsgi_skips_fork_hooks()
        assert caplog.records == []

    def test_a_uwsgi_module_on_the_path_is_never_imported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / "uwsgi.py").write_text("raise RuntimeError('imported a project file named uwsgi.py')\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.delitem(sys.modules, "uwsgi", raising=False)
        with caplog.at_level(logging.WARNING, logger=INVALIDATION_LOGGER):
            invalidation._warn_if_uwsgi_skips_fork_hooks()  # outside uWSGI: no import, no raise
        assert "uwsgi" not in sys.modules and caplog.records == []

    def test_import_warns_once(self) -> None:
        code = (
            "import logging, sys, types\n"
            "logging.basicConfig(level=logging.WARNING, format='%(name)s %(message)s')\n"
            "sys.modules['uwsgi'] = types.SimpleNamespace(opt={})\n"
            "import cachekit, cachekit.invalidation\n"
            "from cachekit import cache\n"
        )
        proc = subprocess.run(  # noqa: S603 - trusted: sys.executable + literal code
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stderr.count("uWSGI forks its workers") == 1, proc.stderr
