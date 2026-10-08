"""BackpressureController.acquire() as a class-based context manager.

Every rejection scenario runs against the new controller and against ``_LegacyController``,
a verbatim copy of the ``@contextmanager`` generator it replaced, with identical expectations:
the rewrite must not change what callers observe.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Generator

import pytest

from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.reliability import load_control
from cachekit.reliability.load_control import BackpressureController


class _LegacyController(BackpressureController):
    """The previous generator implementation, kept as the behavioural oracle."""

    @contextlib.contextmanager
    def acquire(self) -> Generator[None, None, None]:  # type: ignore[override]
        with self._lock:
            if self._queue_depth >= self.queue_size:
                self._rejected_count += 1
                load_control.cache_operations.labels(  # type: ignore[attr-defined]
                    operation="backpressure", status="rejected", serializer="", namespace=""
                ).inc()
                raise BackendError("Request queue full", error_type=BackendErrorType.TRANSIENT)
            self._queue_depth += 1
        acquired = False
        try:
            acquired = self._semaphore.acquire(timeout=self.timeout)
            if not acquired:
                with self._lock:
                    self._rejected_count += 1
                raise BackendError("Failed to acquire permit", error_type=BackendErrorType.TIMEOUT)
            with self._lock:
                self._queue_depth -= 1
            yield
        except Exception:
            if not acquired:
                with self._lock:
                    self._queue_depth -= 1
            raise
        finally:
            if acquired:
                self._semaphore.release()


CONTROLLERS = pytest.mark.parametrize("cls", [BackpressureController, _LegacyController], ids=["class", "legacy"])


def _rejected_metric() -> float:
    return load_control.cache_operations.labels(  # type: ignore[attr-defined]
        operation="backpressure", status="rejected", serializer="", namespace=""
    )._value.get()


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition never held"
        time.sleep(0.001)


@contextlib.contextmanager
def _holder(controller: BackpressureController) -> Generator[threading.Thread, None, None]:
    """A thread holding the controller's only permit until the block exits."""
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with controller.acquire():
            held.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    assert held.wait(5)
    try:
        yield thread
    finally:
        release.set()
        thread.join(5)


def _idle(controller: BackpressureController) -> None:
    assert controller.queue_depth == 0
    assert controller._semaphore._value == controller.max_concurrent


@pytest.mark.unit
class TestRejectionSemanticsMatchLegacy:
    @CONTROLLERS
    def test_success(self, cls):
        controller = cls(max_concurrent=2)
        with controller.acquire():
            assert controller._semaphore._value == 1
            assert controller.queue_depth == 0
        _idle(controller)
        assert controller.rejected_count == 0

    @CONTROLLERS
    @pytest.mark.parametrize("exc", [ValueError, KeyboardInterrupt], ids=["exception", "base_exception"])
    def test_exception_in_block_releases_permit(self, cls, exc):
        controller = cls(max_concurrent=1)
        with pytest.raises(exc):
            with controller.acquire():
                raise exc()
        _idle(controller)
        assert controller.rejected_count == 0

    @CONTROLLERS
    def test_full_queue(self, cls):
        controller = cls(max_concurrent=1, queue_size=1, timeout=5.0)
        before = _rejected_metric()
        waiter_done = threading.Event()

        def wait_in_queue() -> None:
            with controller.acquire():
                pass
            waiter_done.set()

        with _holder(controller):
            waiter = threading.Thread(target=wait_in_queue)
            waiter.start()
            _wait_for(lambda: controller.queue_depth == 1)
            with pytest.raises(BackendError) as info:
                with controller.acquire():
                    pytest.fail("entered the block on a full queue")
            assert controller.queue_depth == 1  # the rejected caller never joined the queue
        waiter.join(5)

        assert waiter_done.is_set()
        assert "Request queue full" in str(info.value)
        assert info.value.error_type == BackendErrorType.TRANSIENT
        assert controller.rejected_count == 1
        assert _rejected_metric() == before + 1
        _idle(controller)

    @CONTROLLERS
    def test_permit_timeout(self, cls):
        controller = cls(max_concurrent=1, queue_size=10, timeout=0.05)
        before = _rejected_metric()
        with _holder(controller):
            with pytest.raises(BackendError) as info:
                with controller.acquire():
                    pytest.fail("entered the block without a permit")
            assert controller.queue_depth == 0
        assert "Failed to acquire permit" in str(info.value)
        assert info.value.error_type == BackendErrorType.TIMEOUT
        assert controller.rejected_count == 1
        assert _rejected_metric() == before  # a timeout counts in rejected_count only, as before
        _idle(controller)

    @CONTROLLERS
    def test_contention_accounting(self, cls):
        """Many threads, two permits: every call either ran or was rejected, and nothing leaks."""
        controller = cls(max_concurrent=2, queue_size=3, timeout=0.01)
        ran, rejected = [], []
        lock = threading.Lock()
        start = threading.Barrier(16)

        def worker() -> None:
            start.wait()
            for _ in range(20):
                try:
                    with controller.acquire():
                        time.sleep(0.001)
                    outcome = ran
                except BackendError:
                    outcome = rejected
                with lock:
                    outcome.append(1)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)

        assert len(ran) + len(rejected) == 16 * 20
        assert rejected, "the permits were never contended; the test proves nothing"
        assert controller.rejected_count == len(rejected)
        _idle(controller)


@pytest.mark.unit
class TestPermitObject:
    def test_not_a_generator_context_manager(self):
        cm = BackpressureController().acquire()
        assert not isinstance(cm, contextlib._GeneratorContextManager)
        assert hasattr(cm, "__enter__") and hasattr(cm, "__exit__")

    def test_fresh_object_per_call(self):
        controller = BackpressureController()
        assert controller.acquire() is not controller.acquire()

    def test_queue_check_runs_on_enter_not_on_call(self):
        controller = BackpressureController(max_concurrent=1, queue_size=0)
        cm = controller.acquire()  # a full queue, but nothing is checked yet
        assert controller.rejected_count == 0
        with pytest.raises(BackendError, match="Request queue full"):
            with cm:
                pass
        assert controller.rejected_count == 1

    def test_usable_as_decorator(self):
        controller = BackpressureController(max_concurrent=2)
        seen = []

        @controller.acquire()
        def guarded(x: int) -> int:
            seen.append(controller._semaphore._value)
            return x * 2

        assert guarded(1) == 2
        assert guarded(2) == 4
        assert seen == [1, 1]
        _idle(controller)


class _InterruptingLock:
    """A lock whose Nth entry raises KeyboardInterrupt, as a signal landing there would."""

    def __init__(self, interrupt_on: int) -> None:
        self._lock = threading.Lock()
        self._entries = 0
        self._interrupt_on = interrupt_on

    def __enter__(self) -> None:
        self._entries += 1
        if self._entries == self._interrupt_on:
            raise KeyboardInterrupt
        self._lock.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self._lock.release()


@pytest.mark.unit
class TestInterruptAfterAcquire:
    @CONTROLLERS
    def test_interrupt_while_leaving_the_queue_keeps_the_permit(self, cls):
        """An interrupt after the permit is acquired, before the block starts, must not lose the permit.

        Entry 1 is queue admission; entry 2 is leaving the queue once the permit is held.
        """
        controller = cls(max_concurrent=1, timeout=0.05)
        controller._lock = _InterruptingLock(interrupt_on=2)
        with pytest.raises(KeyboardInterrupt):
            with controller.acquire():
                pytest.fail("entered the block after an interrupt")
        assert controller._semaphore._value == 1, "the acquired permit leaked"
        with controller.acquire():  # the single permit is still usable
            pass
        if cls is BackpressureController:
            assert controller.queue_depth == 0  # the old generator left the queue count one too high here
