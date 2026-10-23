"""write_behind=True: a miss returns once its value is serialized and in L1; the L2 write follows on a writer thread.

The backend here records every write with the thread it ran on. A write on the caller's thread is one the caller
waited for (the off path, or a fallback at a full queue); a write on a ``cachekit-write-behind`` thread is deferred.
Writer threads are held at a gate the test opens, so "the call returned before its write" is decided by state,
never by a clock: had the caller waited on a gated write, the gate's guard would expire and the test would fail.
"""

from __future__ import annotations

import ast
import asyncio
import contextvars
import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from cachekit import cache
from cachekit.backends.file import FileBackend
from cachekit.backends.file.config import FileBackendConfig
from cachekit.cache_handler import CacheSerializationHandler
from cachekit.config.decorator import DecoratorConfig
from cachekit.config.validation import ConfigurationError
from cachekit.decorators import write_behind as wb
from cachekit.decorators.orchestrator import FeatureOrchestrator
from cachekit.interop import InteropError
from cachekit.serializers.encryption_wrapper import KeyringConfigurationError
from tests.utils.fork_helpers import child_outcome, report, run_fresh

pytestmark = pytest.mark.unit

_GUARD_SECONDS = 10.0  # hang guard only: hitting it fails the test, it never decides an outcome
_WRITER = "cachekit-write-behind"
_MASTER_KEY = "a" * 64  # pragma: allowlist secret — test fixture key
tenant_var: contextvars.ContextVar[str] = contextvars.ContextVar("tenant_var", default="none")


class _GatedBackend:
    """In-memory, non-lockable, non-trackable. Writes from a writer thread wait at ``gate``."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.log: list[tuple[str, str, str]] = []  # (op, key, thread name) in the order they happened
        self.gate = threading.Event()
        self.writing = threading.Event()  # a writer thread is held at the gate
        self.fail_writes = False
        self.tenants: list[str] = []  # tenant_var as each write saw it
        self._lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        name = threading.current_thread().name
        if name == _WRITER:
            self.writing.set()
            if not self.gate.wait(_GUARD_SECONDS):
                raise AssertionError("gate never opened")
        self.tenants.append(tenant_var.get())
        if self.fail_writes:
            raise ConnectionError("backend down")
        with self._lock:
            self.store[key] = value
            self.log.append(("set", key, name))

    def delete(self, key: str) -> bool:
        with self._lock:
            self.log.append(("delete", key, threading.current_thread().name))
            return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {}

    def writes(self, key: str | None = None) -> list[str]:
        """The thread of each write of key (every write, for None), in order."""
        return [t for op, k, t in self.log if op == "set" and (key is None or k == key)]


class _TrackingBackend(_GatedBackend):
    """A KeyTrackableBackend: the registry calls join the log."""

    def __init__(self) -> None:
        super().__init__()
        self.registry: set[str] = set()

    def track_key(self, registry_id: str, key: str) -> None:
        self.log.append(("track", key, threading.current_thread().name))
        self.registry.add(key)

    def drain_tracked(self, registry_id: str, local_keys: Any) -> set[str]:
        keys = self.registry | set(local_keys)
        for key in keys:
            self.delete(key)
        self.registry.clear()
        return keys


def _drain(fn: Any) -> None:
    """Wait until fn's writer holds nothing. The gate must be open."""
    writer: wb.WriteBehind = fn._cachekit_write_behind
    with writer._cond:
        assert writer._cond.wait_for(lambda: not writer._pending and not writer._sending, _GUARD_SECONDS)


def _held(backend: _GatedBackend) -> None:
    assert backend.writing.wait(_GUARD_SECONDS), "no write reached the gate"


@pytest.fixture
def backend() -> _GatedBackend:
    b = _GatedBackend()
    yield b
    b.gate.set()  # never leave a writer thread parked


@pytest.fixture
def one_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wb, "MAX_WORKERS", 1)


class TestOptIn:
    def test_off_by_default_and_in_every_preset(self) -> None:
        assert DecoratorConfig().write_behind is False
        assert not any(
            getattr(DecoratorConfig, preset)(**kwargs).write_behind
            for preset, kwargs in {
                "minimal": {},
                "production": {},
                "dev": {},
                "test": {},
                "secure": {"master_key": _MASTER_KEY, "single_tenant_mode": True},
            }.items()
        )

    def test_must_be_a_bool(self) -> None:
        with pytest.raises(ConfigurationError, match="write_behind must be a bool"):
            DecoratorConfig(write_behind=1)  # type: ignore[arg-type]

    def test_off_writes_on_the_caller(self, backend: _GatedBackend) -> None:
        @cache(backend=backend, ttl=60)
        def fn(x: int) -> int:
            return x * 2

        assert fn(1) == 2
        assert backend.writes() == [threading.current_thread().name]
        assert fn._cachekit_write_behind is None  # type: ignore[attr-defined]

    def test_l1_only_has_no_writer(self) -> None:
        @cache(backend=None, ttl=60, write_behind=True)
        def fn(x: int) -> int:
            return x * 2

        assert fn(1) == 2
        assert fn._cachekit_write_behind is None  # type: ignore[attr-defined]


class TestReturnsBeforeTheWrite:
    def test_sync_miss(self, backend: _GatedBackend) -> None:
        calls = 0

        @cache(backend=backend, ttl=60, write_behind=True)
        def fn(x: int) -> int:
            nonlocal calls
            calls += 1
            return x * 2

        assert fn(1) == 2  # returned with its write still held at the gate
        assert fn(1) == 2 and calls == 1  # an L1 hit: the value was in L1 before the first call returned
        assert backend.store == {}
        backend.gate.set()
        _drain(fn)
        assert backend.writes() == [_WRITER]

    async def test_async_unlocked_miss(self, backend: _GatedBackend) -> None:
        calls = 0

        @cache(backend=backend, ttl=60, write_behind=True)
        async def fn(x: int) -> int:
            nonlocal calls
            calls += 1
            return x * 2

        assert await fn(1) == 2
        assert await fn(1) == 2 and calls == 1
        assert backend.store == {}
        backend.gate.set()
        await asyncio.to_thread(_drain, fn)
        assert backend.writes() == [_WRITER]

    def test_l2_holds_the_value_for_a_cold_reader(self, backend: _GatedBackend) -> None:
        @cache(backend=backend, ttl=60, write_behind=True, namespace="wb")
        def fn(x: int) -> int:
            return x * 2

        fn(1)
        backend.gate.set()
        _drain(fn)

        @cache(backend=backend, ttl=60, l1_enabled=False, namespace="wb")
        def fn(x: int) -> int:  # noqa: F811 - the same function identity, with no L1
            raise AssertionError("recomputed: L2 did not hold the value")

        assert fn(1) == 2


class TestSerializationStaysOnTheCaller:
    """Errors the miss path raises to the caller still do: serialization never leaves its thread."""

    @pytest.mark.parametrize("error", [InteropError("out of model"), KeyringConfigurationError("bad keyring")])
    def test_sync(self, backend: _GatedBackend, monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
        @cache(backend=backend, ttl=60, write_behind=True)
        def fn(x: int) -> int:
            return x * 2

        monkeypatch.setattr(CacheSerializationHandler, "serialize_data", _raise(error))
        with pytest.raises(type(error)):
            fn(1)
        assert backend.log == []

    @pytest.mark.parametrize("error", [InteropError("out of model"), KeyringConfigurationError("bad keyring")])
    async def test_async(self, backend: _GatedBackend, monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
        @cache(backend=backend, ttl=60, write_behind=True)
        async def fn(x: int) -> int:
            return x * 2

        monkeypatch.setattr(CacheSerializationHandler, "serialize_data", _raise(error))
        with pytest.raises(type(error)):
            await fn(1)
        assert backend.log == []

    def test_real_interop_rejection(self, backend: _GatedBackend) -> None:
        @cache(backend=backend, namespace="wbinterop", interop="op", write_behind=True)
        def fn(x: int) -> Any:
            return {x}  # a set is outside the interop data model

        with pytest.raises(InteropError):
            fn(1)
        assert backend.log == []


def _raise(error: Exception) -> Any:
    def serialize_data(*_: Any, **__: Any) -> Any:
        raise error

    return serialize_data


class TestBounds:
    def test_full_queue_falls_back_to_a_caller_write(
        self, backend: _GatedBackend, one_writer: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wb, "MAX_ENTRIES", 1)

        @cache(backend=backend, ttl=60, write_behind=True)
        def fn(x: int) -> int:
            return x * 2

        fn(1)
        _held(backend)  # key 1 is being sent, so the queue is empty
        fn(2)  # queued: the one entry the bound allows
        fn(3)  # refused: written on the caller
        writer: wb.WriteBehind = fn._cachekit_write_behind  # type: ignore[attr-defined]
        assert writer.fallbacks == 1
        assert backend.writes() == [threading.current_thread().name]
        backend.gate.set()
        _drain(fn)
        assert backend.writes().count(_WRITER) == 2

    def test_bytes_bound(self, backend: _GatedBackend, one_writer: None, monkeypatch: pytest.MonkeyPatch) -> None:
        @cache(backend=backend, ttl=60, write_behind=True)
        def fn(x: int) -> bytes:
            return os.urandom(x)  # incompressible: the envelope is at least x bytes

        fn(1)
        _held(backend)
        monkeypatch.setattr(wb, "MAX_BYTES", 500)
        fn(1000)  # its envelope alone is over the bound
        assert fn._cachekit_write_behind.fallbacks == 1  # type: ignore[attr-defined]
        assert backend.writes() == [threading.current_thread().name]

    def test_a_queued_entry_takes_a_newer_value_past_the_bound(
        self, backend: _GatedBackend, one_writer: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No fallback for an entry already queued or being sent: a caller write could race the deferred one."""
        monkeypatch.setattr(wb, "MAX_ENTRIES", 0)
        writer = wb.WriteBehind(lambda *_: None)
        sent: list[int] = []
        held = threading.Event()
        release = threading.Event()

        def send(n: int) -> None:
            held.set()
            assert release.wait(_GUARD_SECONDS)
            sent.append(n)

        monkeypatch.setattr(wb, "MAX_ENTRIES", 1)
        assert writer.submit(("", "k"), 1, lambda: send(1))
        assert held.wait(_GUARD_SECONDS)
        monkeypatch.setattr(wb, "MAX_ENTRIES", 0)
        assert writer.submit(("", "k"), 1, lambda: send(2))  # being sent: queued behind it
        assert writer.submit(("", "k"), 1, lambda: send(3))  # queued: replaced
        assert not writer.submit(("", "other"), 1, lambda: send(4))  # a new entry: refused
        release.set()
        assert writer.close(_deadline()) == []
        assert sent == [1, 3]


def _deadline() -> float:
    return time.monotonic() + _GUARD_SECONDS


class TestCoalescingAndInvalidation:
    def test_newest_value_wins(self, backend: _GatedBackend, one_writer: None) -> None:
        version = 0

        @cache(backend=backend, ttl=60, write_behind=True, l1_enabled=False)
        def fn(x: int) -> int:
            nonlocal version
            version += 1
            return version

        fn(0)
        _held(backend)  # the one writer is held on key 0
        fn(1), fn(1), fn(1)  # no L1, and L2 has nothing yet: three misses, one queued entry
        backend.gate.set()
        _drain(fn)
        assert fn(1) == 4  # what L2 holds: the newest value
        assert len(backend.writes()) == 2  # key 0 and key 1, once each

    def test_invalidate_drops_a_queued_write(self, backend: _GatedBackend, one_writer: None) -> None:
        """The race AC-5 names: an invalidation between the L1 fill and the deferred write."""

        @cache(backend=backend, ttl=60, write_behind=True)
        def fn(x: int) -> int:
            return x * 2

        fn(0)
        _held(backend)
        fn(1)  # in L1, its write queued
        fn.invalidate_cache(1)  # type: ignore[attr-defined]
        backend.gate.set()
        _drain(fn)
        assert list(backend.store) == [_only_key(backend, "set")]  # key 0 only: key 1's write never ran

    def test_invalidate_waits_out_a_write_being_sent(self, backend: _GatedBackend) -> None:
        @cache(backend=backend, ttl=60, write_behind=True)
        def fn(x: int) -> int:
            return x * 2

        fn(1)
        _held(backend)
        done = threading.Thread(target=fn.invalidate_cache, args=(1,))  # type: ignore[attr-defined]
        done.start()
        _until(lambda: len(fn._cachekit_write_behind._cond._waiters) == 1)  # type: ignore[attr-defined]
        assert backend.log == []  # the invalidation waits behind the held write, its delete not sent
        backend.gate.set()
        done.join(_GUARD_SECONDS)
        assert [op for op, *_ in backend.log] == ["set", "delete"]
        assert backend.store == {}

    @pytest.mark.parametrize("tracking", [False, True], ids=["local", "registry"])
    def test_no_args_invalidation_drops_queued_writes(self, one_writer: None, tracking: bool) -> None:
        backend = _TrackingBackend() if tracking else _GatedBackend()

        @cache(backend=backend, ttl=60, write_behind=True)
        def fn(x: int) -> int:
            return x * 2

        try:
            fn(0)
            _held(backend)
            fn(1)
            writer: wb.WriteBehind = fn._cachekit_write_behind  # type: ignore[attr-defined]
            # Opens the gate once the invalidation has dropped key 1's write and waits on key 0's.
            threading.Thread(target=lambda: (_until(lambda: not writer._pending), backend.gate.set())).start()
            fn.invalidate_cache()  # type: ignore[attr-defined]
            _drain(fn)
            assert backend.store == {}
            assert len(backend.writes()) == 1
        finally:
            backend.gate.set()

    def test_a_write_landing_after_the_delete_is_recorded_again(
        self, backend: _GatedBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_invalidate_key's contract: every write records its key after its L2 set. Here a miss queues
        its write and records between the invalidation's cancel and its untrack, so only the writer's
        record after the write leaves the key where a later no-args invalidation finds it."""

        @cache(backend=backend, ttl=60, write_behind=True)
        def fn(x: int) -> int:
            return x * 2

        cancel = wb.WriteBehind.cancel

        def cancel_then_miss(self: wb.WriteBehind, match: Any, *, wait: bool = True) -> None:
            cancel(self, match, wait=wait)
            monkeypatch.setattr(wb.WriteBehind, "cancel", cancel)
            fn(1)

        monkeypatch.setattr(wb.WriteBehind, "cancel", cancel_then_miss)
        fn.invalidate_cache(1)  # type: ignore[attr-defined]
        backend.gate.set()
        _drain(fn)
        assert list(backend.store) != []  # landed after the invalidation's delete
        fn.invalidate_cache()  # type: ignore[attr-defined]
        assert backend.store == {}


def _until(condition: Any) -> None:
    """Wait for a state another thread reaches; the guard only fails a hang."""
    deadline = time.monotonic() + _GUARD_SECONDS
    while not condition():
        assert time.monotonic() < deadline, "state never reached"
        time.sleep(0.001)


def _only_key(backend: _GatedBackend, op: str) -> str:
    (key,) = {k for o, k, _ in backend.log if o == op}
    return key


class TestOnTheWriter:
    def test_tracks_only_after_the_write_with_the_callers_context(self) -> None:
        backend = _TrackingBackend()

        @cache(backend=backend, ttl=60, write_behind=True)
        def fn(x: int) -> int:
            return x * 2

        token = tenant_var.set("tenant-a")
        try:
            fn(1)
        finally:
            tenant_var.reset(token)
        assert backend.registry == set()  # not tracked before the write
        backend.gate.set()
        _drain(fn)
        assert [(op, t) for op, _, t in backend.log] == [("set", _WRITER), ("track", _WRITER)]
        assert backend.tenants == ["tenant-a"]

    def test_a_failed_write_warns_and_counts(self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            FeatureOrchestrator,
            "record_cache_operation",
            lambda self, operation, namespace, success, **_: recorded.append((operation, success)),
        )
        backend = _TrackingBackend()

        @cache(backend=backend, ttl=60, write_behind=True)
        def fn(x: int) -> int:
            return x * 2

        backend.fail_writes = True
        fn(1)
        with caplog.at_level(logging.WARNING, logger="cachekit"):
            backend.gate.set()
            _drain(fn)
        assert ("set", False) in recorded
        assert ("set", True) not in recorded
        assert any("Deferred L2 write failed" in r.getMessage() for r in caplog.records)
        assert backend.registry == set()


class TestPathsThatStaySynchronous:
    def test_async_locked_miss_writes_inside_the_lock(self) -> None:
        events: list[str] = []

        class _Locking(_GatedBackend):
            def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
                events.append("set")
                super().set(key, value, ttl)

            @asynccontextmanager
            async def acquire_lock(self, key: str, timeout: float, blocking_timeout: float | None = None) -> Any:
                events.append("lock")
                yield True
                events.append("unlock")

        locking = _Locking()

        @cache(backend=locking, ttl=60, write_behind=True)
        async def fn(x: int) -> int:
            return x * 2

        assert asyncio.run(fn(1)) == 2
        assert events == ["lock", "set", "unlock"]
        assert _WRITER not in locking.writes()

    def test_streaming_write_stays_on_the_caller(self, tmp_path: Path) -> None:
        pd = pytest.importorskip("pandas")
        pytest.importorskip("pyarrow")
        threads: list[str] = []

        class _Recording(FileBackend):
            def set_streaming(self, *args: Any, **kwargs: Any) -> Any:
                threads.append(threading.current_thread().name)
                return super().set_streaming(*args, **kwargs)

        backend = _Recording(FileBackendConfig(cache_dir=str(tmp_path)))

        @cache(backend=backend, ttl=60, serializer="arrow", write_behind=True)
        def fn(n: int) -> Any:
            return pd.DataFrame({"a": range(n)})

        fn(3)
        assert threads == [threading.current_thread().name]
        assert fn._cachekit_write_behind._pending == {}  # type: ignore[attr-defined]


class TestEncryption:
    def test_the_deferred_write_sends_the_l1_envelope(self, backend: _GatedBackend, monkeypatch: pytest.MonkeyPatch) -> None:
        """With encryption on, the writer sends the very ciphertext the caller serialized and put in L1, and a
        reader with a cold L1 decrypts it without recomputing."""
        from cachekit.l1_cache import L1Cache

        put: dict[str, bytes] = {}
        l1_put = L1Cache.put

        def recording_put(self: L1Cache, key: str, value: bytes, *args: Any, **kwargs: Any) -> Any:
            put[key] = value
            return l1_put(self, key, value, *args, **kwargs)

        monkeypatch.setattr(L1Cache, "put", recording_put)
        options = {"backend": backend, "ttl": 60, "namespace": "enc", "master_key": _MASTER_KEY, "single_tenant_mode": True}

        @cache.secure(write_behind=True, **options)
        def fn(x: int) -> dict[str, int]:
            return {"v": x}

        fn(1)
        backend.gate.set()
        _drain(fn)
        (key,) = backend.store
        assert backend.store[key] is put[key]

        @cache.secure(l1_enabled=False, **options)
        def fn(x: int) -> dict[str, int]:  # noqa: F811 - the same function identity, with no L1
            raise AssertionError("recomputed: L2 did not hold a readable value")

        assert fn(1) == {"v": 1}


def _forked_child_outcome(fork: str) -> object:
    """A writer's state across a fork. fork is "c" for libc's fork(), which runs no at-fork hook, or "os".

    The parent queues a write and starts no thread (MAX_WORKERS 0), so no thread is alive at fork. The
    child misses a second key and reports where its write went: a C-forked child writes it on the caller
    and starts nothing; an os.fork() child queues it on a fresh writer, without the parent's entry.
    """
    import ctypes

    backend = _GatedBackend()

    @cache(backend=backend, ttl=60, write_behind=True, l1_enabled=False)
    def fn(x: int) -> int:
        return x * 2

    wb.MAX_WORKERS = 0
    fn(1)  # queued in the parent, never sent
    wb.MAX_WORKERS = 4
    writer: wb.WriteBehind = fn._cachekit_write_behind  # type: ignore[attr-defined]
    if threading.active_count() != 1:  # not an assert: python -O strips those
        raise RuntimeError(f"a thread would be alive at fork: {threading.enumerate()}")
    r, w = os.pipe()
    child = ctypes.PyDLL(None).fork() if fork == "c" else os.fork()  # PyDLL keeps the GIL through the call
    if child == 0:
        try:
            os.close(r)
            requested: list[str] = []

            class _RecordingThread(threading.Thread):  # keeps the child single-threaded
                def start(self) -> None:
                    requested.append(self.name)

            threading.Thread = _RecordingThread  # type: ignore[misc]  # the child's copy only; it never returns
            fn(2)
            outcome: object = {
                "caller_writes": len(backend.writes()),
                "requested": requested,
                "queued": sorted(key for _, key in writer._pending),
            }
        except BaseException as e:
            outcome = {"error": repr(e)}
        try:
            report(w, outcome)
        finally:
            os._exit(1)
    os.close(w)
    return child_outcome(child, r)


class TestFork:
    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs glibc fork() via ctypes")
    @pytest.mark.parametrize(
        ("fork", "caller_writes", "requested", "queued"),
        [
            pytest.param("c", 1, [], 0, id="c-fork"),
            pytest.param("os", 0, [_WRITER], 1, id="os-fork"),
        ],
    )
    def test_child_never_uses_the_parents_queue(self, fork: str, caller_writes: int, requested: list[str], queued: int) -> None:
        run = run_fresh(_forked_child_outcome, fork)

        assert run.returncode == 0, run.stderr
        outcome = ast.literal_eval(run.stdout)
        assert outcome["caller_writes"] == caller_writes, outcome
        assert outcome["requested"] == requested, outcome
        assert len(outcome["queued"]) == queued, outcome  # the child's own key, never the parent's


_EXIT_SCRIPT = """
import logging, sys, time
from cachekit import cache
from cachekit.backends.file import FileBackend
from cachekit.backends.file.config import FileBackendConfig
from cachekit.decorators import write_behind

logging.basicConfig(level=logging.WARNING)
write_behind.EXIT_FLUSH_SECONDS = {flush_seconds}

class Slow(FileBackend):
    def set(self, key, value, ttl=None):
        time.sleep({write_seconds})
        super().set(key, value, ttl)

@cache(backend=Slow(FileBackendConfig(cache_dir=sys.argv[1])), ttl=600, namespace="exit", write_behind=True)
def fn(x):
    return x * 2

assert fn(21) == 42
print("returned", flush=True)
"""


def _run_exit_script(
    cache_dir: Path, *, flush_seconds: float, write_seconds: float
) -> tuple[subprocess.CompletedProcess[str], float]:
    script = textwrap.dedent(_EXIT_SCRIPT).format(flush_seconds=flush_seconds, write_seconds=write_seconds)
    start = time.monotonic()
    run = subprocess.run(  # noqa: S603 - trusted: sys.executable and a script built here
        [sys.executable, "-c", script, str(cache_dir)], capture_output=True, text=True, timeout=60
    )
    return run, time.monotonic() - start


class TestExit:
    def test_a_clean_exit_flushes_the_queue(self, tmp_path: Path) -> None:
        run, _ = _run_exit_script(tmp_path, flush_seconds=10, write_seconds=0.5)
        assert run.returncode == 0, run.stderr
        assert "returned" in run.stdout
        assert _reader(tmp_path)(21) == 42
        assert "not sent" not in run.stderr

    def test_exit_never_waits_past_the_deadline(self, tmp_path: Path) -> None:
        run, elapsed = _run_exit_script(tmp_path, flush_seconds=1, write_seconds=30)
        assert run.returncode == 0, run.stderr
        assert elapsed < 15, elapsed
        assert "1 deferred L2 write(s) were not sent within 1 s of exit" in run.stderr


def _reader(cache_dir: Path) -> Any:
    """The exit script's fn (same module and qualname, so the same key), reading L2 only."""

    def fn(x: int) -> int:
        raise AssertionError("recomputed: the exit flush did not write L2")

    fn.__module__, fn.__qualname__ = "__main__", "fn"
    return cache(backend=FileBackend(FileBackendConfig(cache_dir=str(cache_dir))), ttl=600, namespace="exit", l1_enabled=False)(
        fn
    )


class TestWriterEdges:
    def test_cancel_waits_only_for_sends_in_progress_at_the_call(self) -> None:
        """A key taking misses throughout an invalidation cannot hold it: a send that starts after the
        cancel is a later write, not one the cancel waits for."""
        writer = wb.WriteBehind(lambda *_: None)
        first, second = threading.Event(), threading.Event()
        assert writer.submit(("", "k"), 1, lambda: first.wait(_GUARD_SECONDS))
        _until(lambda: ("", "k") in writer._sending)
        cancelled = threading.Thread(target=writer.cancel, args=(("", "k").__eq__,))
        cancelled.start()
        _until(lambda: len(writer._cond._waiters) == 1)  # type: ignore[attr-defined]
        assert writer.submit(("", "k"), 1, lambda: second.wait(_GUARD_SECONDS))  # queued behind the first send
        first.set()  # the first send ends, and the second starts at once on the freed thread
        cancelled.join(_GUARD_SECONDS)
        assert not cancelled.is_alive(), "cancel waited on a send that started after it"
        second.set()
        assert writer.close(_deadline()) == []

    def test_a_refused_thread_start_refuses_the_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        writer = wb.WriteBehind(lambda *_: None)

        def refuse(self: threading.Thread) -> None:
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", refuse)
        assert not writer.submit(("", "k"), 1, lambda: None)
        assert writer._pending == {} and writer._workers == 0

    def test_exit_flush_names_what_it_dropped(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        writer = wb.WriteBehind(lambda *_: None)
        release = threading.Event()
        writer.submit(("", "k"), 1, lambda: release.wait(_GUARD_SECONDS))
        monkeypatch.setattr(wb, "_writers", {writer})
        monkeypatch.setattr(wb, "EXIT_FLUSH_SECONDS", 0.1)
        try:
            with caplog.at_level(logging.WARNING, logger="cachekit"):
                wb._flush_at_exit()
        finally:
            release.set()
        assert any("1 deferred L2 write(s) were not sent" in r.getMessage() for r in caplog.records)
        assert not writer.submit(("", "other"), 1, lambda: None)  # closed: callers write synchronously
