"""In-process single-flight: concurrent misses on one key share one call.

The first caller to miss a key starts the call, and every caller that misses the same key while it
runs waits for that call's outcome instead of starting its own. A map entry lives only while its
call is in flight and goes when the call settles, success or failure, so a failed call pins nothing
and the next miss starts a fresh one. The distributed lock dedups fills across processes; this
dedups inside one process, before the lock, so a herd in one process costs one trip.

One map per decorated function: ``AsyncFlights`` for coroutine functions, ``ThreadFlights`` for
plain ones. Both are shared by every thread and guarded by a lock, and a forked child starts with an
empty map (owner-PID check, as ``_RefreshPool`` does). A flight key is a tuple whose last item is
the cache key, so an invalidation can drop the key's calls in flight (``forget``).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import threading
from collections.abc import Callable, Collection, Coroutine
from types import TracebackType
from typing import Any, TypeVar

_T = TypeVar("_T")

FlightKey = tuple[Any, ...]


def _not_cancelling() -> bool:
    """Whether nobody has asked to cancel the current task. Python 3.10 cannot tell, so it says no."""
    cancelling = getattr(asyncio.current_task(), "cancelling", None)
    return cancelling is not None and cancelling() == 0


def _forget(flights: dict[FlightKey, Any], cache_keys: Collection[str] | None) -> None:
    if cache_keys is None:
        flights.clear()
        return
    for key in [k for k in flights if k[-1] in cache_keys]:
        del flights[key]


class _AsyncFlight:
    """An async call in flight: its task, and how many callers are waiting on it."""

    __slots__ = ("task", "waiters")

    def __init__(self, task: asyncio.Task[Any]) -> None:
        self.task = task
        self.waiters = 0  # touched only on the task's own loop, so no lock


class AsyncFlights:
    """One coroutine function's misses in flight: flight key -> the call running.

    The call runs in its own task, in a copy of the starting caller's context, and every caller,
    the starter included, waits on it through ``asyncio.shield``. Cancelling a caller (its timeout,
    a dropped client) cancels only that caller's wait while others still wait: the call runs on and
    they get its value. When the last waiting caller is cancelled, nobody wants the call any more,
    so it is cancelled as an unshared call would be, and that caller returns once it has unwound.
    A caller whose shared call was cancelled under it, without being cancelled itself, runs its
    own call, unshared, as it would have run alone.

    Callers share a call only on the event loop running it: a caller never awaits a task bound to
    another thread's loop. A caller that finds its key's call running on another loop runs its own
    call, unshared. A call left on a loop that no longer runs (stopped, or closed with the task
    pending) gives up its key to the next caller's call.
    """

    __slots__ = ("_flights", "_lock", "_pid")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flights: dict[FlightKey, _AsyncFlight] = {}
        self._pid = os.getpid()

    async def run(
        self,
        key: FlightKey,
        call: Callable[[], Coroutine[Any, Any, _T]],
        on_join: Callable[[], object] | None = None,
    ) -> tuple[_T, bool]:
        """Await ``key``'s call in flight on this loop, or start it: ``(value, shared)``.

        ``shared`` is True when another caller started the call. ``call`` is invoked only by a
        caller that starts one. ``on_join`` runs each time a caller joins, before it waits. The
        call's exception is raised in every caller sharing it.
        """
        if self._pid != os.getpid():
            # Forked child: the parent's calls never settle here, and its lock may be held.
            self._lock, self._flights, self._pid = threading.Lock(), {}, os.getpid()
        loop = asyncio.get_running_loop()
        with self._lock:
            flight = self._flights.get(key)
        if flight is not None and not flight.task.done():
            task_loop = flight.task.get_loop()
            if task_loop is loop and flight.task is not asyncio.current_task():
                if on_join is not None:
                    on_join()
                try:
                    return await self._wait(key, flight), True
                except asyncio.CancelledError:
                    if not (flight.task.cancelled() and _not_cancelling()):
                        raise
                # The shared call was cancelled (it raised CancelledError, or something cancelled
                # it), but nobody cancelled this caller: it runs its own call, as it would have alone.
                return await call(), False
            if task_loop is loop or task_loop.is_running():
                # Re-entered from inside its own call, which would wait on itself; or the call runs
                # on another thread's loop, which this caller must not await.
                return await call(), False
        # Created outside the lock: an eager task factory runs the call up to its first await
        # inside create_task, and that may be a cached call on this same function.
        flight = _AsyncFlight(loop.create_task(call()))
        with self._lock:
            self._flights[key] = flight  # replaces only a settled call, or one on a loop that no longer runs
        flight.task.add_done_callback(functools.partial(self._drop, key, flight))
        return await self._wait(key, flight), False

    def forget(self, cache_keys: Collection[str] | None) -> None:
        """Make the next miss on these cache keys (every key, for None) start a new call.

        For invalidation: a read that starts after it must not join a call that started before it.
        Callers already waiting keep their call.
        """
        with self._lock:
            _forget(self._flights, cache_keys)

    async def _wait(self, key: FlightKey, flight: _AsyncFlight) -> Any:
        flight.waiters += 1
        try:
            return await asyncio.shield(flight.task)
        except asyncio.CancelledError:
            if flight.waiters == 1 and not flight.task.done():
                # The last caller was cancelled: the call is too, and has unwound before this
                # caller's cancellation goes on, as when it ran inline.
                self._abandon(key, flight)
                await asyncio.wait((flight.task,))
            raise
        finally:
            flight.waiters -= 1
            if not flight.waiters and not flight.task.done():
                self._abandon(key, flight)  # the last caller left another way (GeneratorExit): no wait

    def _abandon(self, key: FlightKey, flight: _AsyncFlight) -> None:
        """Cancel a call nobody waits on. Out of the map first, so no caller joins a cancelled call."""
        self._drop(key, flight)
        with contextlib.suppress(RuntimeError):  # its loop is closed: it never runs again anyway
            flight.task.cancel()

    def _drop(self, key: FlightKey, flight: _AsyncFlight, _task: object = None) -> None:
        with self._lock:
            if self._flights.get(key) is flight:  # a newer call may already hold the key
                del self._flights[key]


class _Flight:
    """A sync call in flight: the thread running it, a lock it holds until it settles, its outcome."""

    __slots__ = ("error", "ok", "owner", "settled", "traceback", "value")

    def __init__(self) -> None:
        self.owner = threading.get_ident()
        self.settled = threading.Lock()
        self.settled.acquire()
        self.ok = False
        self.value: Any = None
        self.error: Exception | None = None
        self.traceback: TracebackType | None = None


class ThreadFlights:
    """One plain function's misses in flight across threads: flight key -> the call running.

    A caller that finds its key's call in flight blocks until it settles, then returns its value or
    raises its exception. A call interrupted by a non-``Exception`` (``KeyboardInterrupt``,
    ``SystemExit``) did not fail, so its waiters retry and one of them starts the next call.
    """

    __slots__ = ("_flights", "_lock", "_pid")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flights: dict[FlightKey, _Flight] = {}
        self._pid = os.getpid()

    def run(self, key: FlightKey, call: Callable[[], _T], recheck: Callable[[], tuple[bool, _T]]) -> tuple[_T, bool]:
        """Wait for ``key``'s call in flight, or start it: ``(value, shared)``.

        ``shared`` is True when another caller's call produced the value. ``call`` must store its
        value where ``recheck`` finds it before returning. ``recheck`` runs under the map's lock
        when no call is in flight, and must neither block nor call back into this map.
        """
        mine: _Flight | None = None
        try:
            while True:
                if self._pid != os.getpid():
                    # Forked child: the parent's calls never settle here, and its lock may be held.
                    self._lock, self._flights, self._pid = threading.Lock(), {}, os.getpid()
                with self._lock:
                    flight = self._flights.get(key)
                    if flight is None:
                        # The caller's own lookup missed, but a call may have settled since: it stored
                        # its value before leaving the map, so this re-check, under the lock, sees it.
                        found, value = recheck()
                        if found:
                            return value, True
                        mine = self._flights[key] = _Flight()  # inside the try: settled however this ends
                        break
                if flight.owner == threading.get_ident():
                    return call(), False  # re-entered from inside its own call, which would wait on itself
                with flight.settled:  # blocks until the call settles
                    pass
                if flight.ok:
                    return flight.value, True
                if flight.error is not None:
                    # Raised from the traceback saved when it settled, as asyncio's Future.result()
                    # does: raised bare, every waiting thread would add its frames to one traceback.
                    raise flight.error.with_traceback(flight.traceback)
                # Interrupted, not failed: loop to start the next call, or join one another thread started.
            value = call()
            mine.ok, mine.value = True, value
            return value, False
        except Exception as e:
            if mine is not None and not mine.ok:
                mine.error, mine.traceback = e, e.__traceback__
            raise
        finally:
            if mine is not None:
                with self._lock:
                    if self._flights.get(key) is mine:  # forgotten, or a forked child's map never held it
                        del self._flights[key]
                mine.settled.release()

    def forget(self, cache_keys: Collection[str] | None) -> None:
        """Make the next miss on these cache keys (every key, for None) start a new call.

        For invalidation: a read that starts after it must not join a call that started before it.
        Callers already waiting keep their call.
        """
        with self._lock:
            _forget(self._flights, cache_keys)
