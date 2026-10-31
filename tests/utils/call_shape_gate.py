"""The gate a call-shape test runs a decorated call through: which backend requests block the caller.

A call-shape test pins two things for one decorated call: the ordered list of requests the caller waits on,
and the requests that run in the background after it returns. The backend's transport calls
``CallShapeGate.observe(op)`` for each request before sending it (tests/unit/test_cachekitio_call_shape.py
hooks the fake urllib3 pool, tests/unit/test_redis_call_shape.py the redis-py client).

Blocking is decided by state, never by a clock: a request blocks if the caller is still waiting on it.
The thread a request arrives on cannot tell that for an async caller. The async backend methods send on
the backend's one client through ``asyncio.to_thread``, so the caller's own requests and its background tasks'
all arrive on worker threads. The gate goes by what the caller is doing instead, and by whether anything
waits on the executor job that sent the request.

Async: every request is parked on the gate, its worker thread held until the event loop releases it.
A request from an executor job whose future nothing waits on (the miss path's lock release) is never released
while the caller is pending: as on the sync side, it is held until the caller has returned, so it is background
however long the caller keeps running, unless a timer fires meanwhile (see below).
Any other parked request is judged by the caller. Once the loop
has run everything it can, for a whole settle since the latest request parked, and the caller is still
pending, the caller waits on a parked request: the oldest is released as blocking. With none parked yet,
the gate waits for one to arrive or for the caller to finish, because a request still on its way through a
worker thread has not parked. Once the caller has returned, whatever it left running is background. A task
the caller spawns cannot send before the caller's current step ends, and on every pinned path the caller
returns within a few turns of that step: the miss runs in a single-flight task the caller awaits.
A caller that kept awaiting after spawning one would let the task's request read as blocking: the pinned
list fails, it does not pass by mistake. A wait on a held request that bypasses its job's future (on an
Event the job sets, say) is one the gate cannot see. Unbounded, it hangs until the guard fails the run.
Bounded, its timer fires while the request is held, and the gate books the request blocking. Any timer that
fires then counts, whichever task set it: a helper task can bound the wait for the caller and live on, so neither
the task nor its fate shows whose wait it was. The gate errs toward blocking instead. A caller that sleeps after
its background release, or background work whose timer fires before the caller returns, makes the pinned list
fail rather than pass by mistake. No pinned path has a timer after its background release. A wait bounded
without a loop timer (by counting turns, say) stays invisible.

Sync: a request blocks if it runs on the caller's thread, where the caller cannot return until it does;
a request on any other thread is held until the caller has returned, so it can only be background.

Only single-caller paths are pinned exactly. Herd paths (several callers on one key) vary in lock-poll
count and order from run to run, so they belong to invariant tests, not here.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import Any

# Loop turns a parked request is given to show it is not awaited. The paths below need a few turns per
# task hop; the bound only has to exceed that, and it counts turns, not time. If it were ever too small,
# a background request would read as blocking: the pinned list fails, it does not pass by mistake.
SETTLE_TURNS = 100
GUARD_SECONDS = 10.0  # hang guard only: hitting it fails the test, it never classifies a request
_WRITER = "cachekit-write-behind"  # the threads write_behind=True sends deferred writes from


@dataclass
class Shape:
    blocking: list[str] = field(default_factory=list)  # in the order the caller waited on them
    background: list[str] = field(default_factory=list)


_running = threading.local()  # .job: the _Job a worker thread is running, for the requests it sends


class _Job(asyncio.Future[Any]):
    """An executor job's future as the gate's ``run_in_executor`` hands it back: a real future that settles as the
    loop's own future for the job does, and records whether anything waits on it. Every wait registers a done
    callback (``await``, ``asyncio.wait``, ``gather``, ``shield``), so registering one is what counts."""

    inner: asyncio.Future[Any]  # the loop's own future for the job
    awaited = False

    def __init__(self, loop: asyncio.AbstractEventLoop, on_awaited: Callable[[], None]) -> None:
        super().__init__(loop=loop)
        self._on_awaited = on_awaited

    def add_done_callback(
        self, fn: Callable[[asyncio.Future[Any]], object], /, *, context: contextvars.Context | None = None
    ) -> None:
        if not self.awaited:
            self.awaited = True
            self._on_awaited()
        super().add_done_callback(fn, context=context)

    def run(self, func: Callable[..., Any], *args: Any) -> Any:
        _running.job = self
        try:
            return func(*args)
        finally:
            _running.job = None

    def relay(self, inner: asyncio.Future[Any]) -> None:
        if self.done():
            return
        if inner.cancelled():
            self.cancel()
        elif (exc := inner.exception()) is not None:
            self.set_exception(exc)
        else:
            self.set_result(inner.result())


@dataclass(eq=False)
class _Parked:
    op: str
    job: _Job | None  # None: sent from outside any executor job
    release: Callable[[], None]

    @property
    def awaited(self) -> bool:
        """Whether something may wait on the request. Not when it comes from a job nothing awaits."""
        return self.job is None or self.job.awaited


class CallShapeGate:
    """Classifies each request a backend sends as blocking or background (see the module docstring)."""

    def __init__(self) -> None:
        self.shape = Shape()
        self.errors: list[str] = []  # a handler's failure would surface as a swallowed BackendError
        # Async run: every request, each on a worker thread, waits here until run_async releases it.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiting: asyncio.Future[Any] | None = None  # the caller, while run_async runs it
        self._jobs: list[_Job] = []  # every executor job of the run
        self._parked: list[_Parked] = []
        self._arrived = asyncio.Event()
        self.arrivals: list[str] = []  # every request parked this run, in order
        # Sync run.
        self._caller: threading.Thread | None = None
        self._caller_returned = threading.Event()

    def _park(self, parked: _Parked) -> None:
        self._parked.append(parked)
        self.arrivals.append(parked.op)
        self._arrived.set()

    async def arrival(self, op: str) -> None:
        """Return once ``op`` has parked this run."""
        while op not in self.arrivals:
            await asyncio.sleep(0)

    def _run_in_executor(self, executor: Executor | None, func: Callable[..., Any], *args: Any) -> _Job:
        """The loop's ``run_in_executor`` during an async run (``asyncio.to_thread`` calls it too)."""
        assert self._loop is not None
        job = _Job(self._loop, self._arrived.set)  # a new wait may be the one the caller is held on
        # The class's method: the loop's own attribute is this one while run_async runs.
        job.inner = type(self._loop).run_in_executor(self._loop, executor, job.run, func, *args)
        job.inner.add_done_callback(job.relay)
        self._jobs.append(job)
        return job

    def _call_at(
        self, when: float, callback: Callable[..., object], *args: Any, context: contextvars.Context | None = None
    ) -> asyncio.TimerHandle:
        """The loop's ``call_at`` during an async run (``call_later``, ``asyncio.sleep`` and ``wait_for`` call it too)."""
        assert self._loop is not None
        return type(self._loop).call_at(self._loop, when, self._timer_fired, callback, *args, context=context)

    def _timer_fired(self, callback: Callable[..., object], *args: Any) -> None:
        # A wait on a held request can bypass its job's future (on an Event the job sets): the gate cannot see that wait,
        # but a bounded one leaves a trace, its timer firing while the caller still waits. Nor can the gate tell whose
        # wait a timer bounds (a helper task can bound it for the caller and live on), so any timer counts: an unrelated
        # one fails the run instead of letting a real wait pass.
        if self._waiting is not None and not self._waiting.done():
            for held in [parked for parked in self._parked if not parked.awaited]:
                self._parked.remove(held)
                self.shape.blocking.append(held.op)
                held.release()
        callback(*args)

    def observe(self, op: str) -> None:
        """Classify the request ``op`` the caller's backend is about to send, holding it while that is decided.

        Call it from the transport, on the thread that sends the request, before sending it.
        """
        if self._loop is not None:
            # An async run: the request comes from a worker thread (an executor job), never the loop's own.
            released = threading.Event()
            self._loop.call_soon_threadsafe(self._park, _Parked(op, getattr(_running, "job", None), released.set))
            if not released.wait(GUARD_SECONDS):
                self.errors.append(f"{op} was never released")
        elif threading.current_thread() is self._caller:
            self.shape.blocking.append(op)
        else:
            if not self._caller_returned.wait(GUARD_SECONDS):
                self.errors.append(f"the caller waited on {op}, sent from another thread")
            self.shape.background.append(op)

    def run_sync(self, call: Callable[[], Any]) -> Shape:
        self.shape = Shape()
        before = set(threading.enumerate())
        self._caller = threading.current_thread()
        self._caller_returned.clear()
        try:
            call()
        finally:
            self._caller = None
            self._caller_returned.set()
        for thread in set(threading.enumerate()) - before:
            thread.join(GUARD_SECONDS)
            assert not thread.is_alive(), f"background thread {thread.name} did not finish"
        assert not self.errors, self.errors
        return self.shape

    async def run_async(self, call: Awaitable[Any]) -> Shape:
        self.shape = shape = Shape()
        self.arrivals, self._jobs = [], []
        writers_before = _writers()
        self._loop = loop = asyncio.get_running_loop()
        loop.run_in_executor = self._run_in_executor  # type: ignore[method-assign]
        loop.call_at = self._call_at  # type: ignore[method-assign]
        try:
            self._waiting = caller = asyncio.ensure_future(call)
            while True:
                arrivals = len(self.arrivals)
                await settle()
                if caller.done():
                    break
                if len(self.arrivals) > arrivals:
                    continue  # one arrived mid-settle: it gets a whole settle too before it is judged
                if waited := next((parked for parked in self._parked if parked.awaited), None):
                    # Everything runnable has run and the caller still waits: it waits on this request.
                    self._parked.remove(waited)
                    shape.blocking.append(waited.op)
                    waited.release()
                else:
                    # None the caller can wait on: it is computing, polling, or its request is still on the way
                    # through a worker thread. A request nothing awaits stays parked until the caller returns.
                    await self._next_event({caller})
            caller.result()
            # The caller has returned: whatever it left running is background. Drain it to the end.
            writers: set[threading.Thread] = set()
            exits: list[asyncio.Future[None]] = []
            while True:
                await settle()
                if self._parked:
                    shape.background.extend(parked.op for parked in self._parked)
                    for parked in self._parked:
                        parked.release()
                    self._parked.clear()
                    continue
                # Background work is a Task, an executor job (the lock release runs in a thread), or a write-behind thread.
                # A writer sends outside any task or job, so the drain joins each one the run started, as run_sync does,
                # including one a background task started during the drain. The join runs off the loop, which must stay
                # free to release the writer's requests.
                for writer in _writers() - writers_before - writers:
                    writers.add(writer)
                    exits.append(type(loop).run_in_executor(loop, None, writer.join, GUARD_SECONDS))
                tasks = asyncio.all_tasks() - {asyncio.current_task()}
                jobs = {job.inner for job in self._jobs if not job.inner.done()}
                if not (pending := tasks | jobs | {exited for exited in exits if not exited.done()}):
                    break
                await self._next_event(pending)
        finally:
            for parked in self._parked:  # left by a caller that raised: never strand a worker thread
                parked.release()
            self._parked.clear()
            del loop.run_in_executor, loop.call_at
            self._loop = self._waiting = None
        assert not self.errors, self.errors
        for writer in writers:
            assert not writer.is_alive(), f"background thread {writer.name} did not finish"
        return shape

    async def _next_event(self, tasks: set[Any]) -> None:
        """Wait until one of ``tasks`` finishes, a request is parked, or a job is first waited on."""
        self._arrived.clear()
        arrived = asyncio.ensure_future(self._arrived.wait())
        done, _ = await asyncio.wait({*tasks, arrived}, timeout=GUARD_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        arrived.cancel()
        await asyncio.gather(arrived, return_exceptions=True)
        assert done, "nothing finished and no request arrived"


def _writers() -> set[threading.Thread]:
    return {thread for thread in threading.enumerate() if thread.name == _WRITER}


async def settle() -> None:
    for _ in range(SETTLE_TURNS):
        await asyncio.sleep(0)
