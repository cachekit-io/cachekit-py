"""Bounded write-behind for decorated misses (``write_behind=True``): the L2 write leaves the caller's path.

With the option on, a miss serializes on the caller's thread, hands the backend write to this writer,
puts the value in L1 and returns. The writer sends it from a background thread. The caller gains nothing
by waiting for that write today: a failed L2 write is logged and the value is returned anyway. What it
gives up is "L2 holds the value when the call returns": another process can miss the key until the write
lands, and a process killed before then loses it (the value is recomputed on the next miss).

One writer per decorated function, shared by its sync and async callers. Threads, not event-loop tasks,
for both: a task dies in ``asyncio.run``'s teardown and cannot be flushed at exit, and the backends' sync
calls are already what invalidation uses off the loop.

- **Coalescing.** One pending write per entry (L2 scope, cache key): a newer value replaces a queued
  one, and a write for an entry already being sent waits for that send, so an older value never lands
  after a newer one.
- **Bounds.** A write is queued only while the queue then holds at most ``MAX_ENTRIES`` entries and
  ``MAX_BYTES`` bytes, replacements included. Otherwise ``submit`` refuses, drops the entry's queued value
  (the refused one supersedes it), and the caller writes synchronously, as without the option. The one
  exception is a value for an entry being sent: it is always queued behind that send, since a caller write
  could land before the older send and lose to it. Such values exceed the bounds by at most one per send
  in flight, so by at most ``MAX_WORKERS`` values. At most ``MAX_WORKERS`` threads send, started on demand.
  The bounds are sized so a full queue drains within ``EXIT_FLUSH_SECONDS`` at half a second per write.
- **Parking.** A thread whose queue is empty waits up to ``IDLE_S`` for the next write before it ends, and
  ``submit`` wakes it instead of starting a thread: ``Thread.start()`` waits for the new thread to run, which
  costs a miss more than a local Redis SET does. At most ``MAX_PARKED`` threads wait per process; past that a
  thread ends at once, as before. A closed writer's waiting threads end at once.
- **Invalidation.** ``cancel`` drops matching queued writes and waits for matching sends to finish, so a
  delete issued after it is ordered behind every write the caller made before it. The module-level
  ``cancel`` reaches every writer of a group (one decorated function and namespace, however many times
  it is decorated) or, for an exact key, every writer in the process.
- **Fork.** A forked child starts empty: its first caller publishes a fresh ``_State`` in one store, and
  the parent's queue is the parent's to send. A child forked without at-fork hooks starts no thread, so
  ``submit`` refuses there. A waiting thread holds no lock, so it cannot deadlock a child; it does keep the
  process multi-threaded for up to ``IDLE_S``, but any process with an L1 cache already runs its sweeper
  thread for good (``l1_cache.start_background_cleanup``), so ``os.fork()``'s multi-threaded warning is no
  news there.
- **Exit.** An ``atexit`` hook flushes every writer within ``EXIT_FLUSH_SECONDS`` in total and logs a
  WARNING naming the writes it dropped. It closes nothing: a backend client's own exit handling is left
  alone. ``os._exit`` (multiprocessing workers) and SIGKILL skip it, and lose the queue.
"""

from __future__ import annotations

import atexit
import contextvars
import logging
import os
import threading
import time
import weakref
from collections.abc import Callable
from typing import NamedTuple

from .. import l1_cache
from ..hash_utils import redact_cache_key, redact_error_for_log

_logger = logging.getLogger(__name__)

MAX_WORKERS = 4
# How long a thread with nothing to send waits for the next write. A miss that finds one waiting starts no
# thread; misses further apart than this pay one thread start each, which is noise next to the gap between
# them. Longer only keeps idle threads around.
IDLE_S = 1.0
# Waiting threads per process, across every writer: two writers' full set of MAX_WORKERS, or eight hot
# functions with one each. Past it a thread ends as soon as its queue is empty, which costs the next miss a
# thread start, not a write. The cap keeps an app with hundreds of decorated functions from holding
# hundreds of idle threads.
MAX_PARKED = 8
MAX_ENTRIES = 64
MAX_BYTES = 16 * 1024 * 1024
EXIT_FLUSH_SECONDS = 10.0
# Keys named in the exit WARNING; the count covers the rest.
_EXIT_KEYS_LOGGED = 10

Entry = tuple[str, str]  # (L2 scope, cache key), as _cached_keys records it


class _Job(NamedTuple):
    send: Callable[[], None]
    context: contextvars.Context
    size: int


class _State:
    """Everything a writer mutates, owned by one process and published as one reference.

    A forked child takes over with a fresh state in one store (``WriteBehind._current``), so no thread ever
    pairs the child's PID with the parent's lock, queue or thread count. A worker binds the state it was
    started for and touches no other, so a take-over never moves a send between states.
    """

    __slots__ = ("bytes", "closed", "cond", "idle", "pending", "pid", "sending", "wake", "workers")

    def __init__(self) -> None:
        lock = threading.Lock()
        self.cond = threading.Condition(lock)  # a send ended, the queue drained: cancel and close wait on it
        self.wake = threading.Condition(lock)  # a write was queued or the writer closed: waiting threads
        self.pending: dict[Entry, _Job] = {}
        self.sending: dict[Entry, _Job] = {}  # entry -> the job being sent for it
        self.bytes = 0
        self.workers = 0
        self.idle = 0  # threads waiting on wake; counted in workers too
        self.closed = False
        self.pid = os.getpid()


# The lock a forked child's first callers take over under. Keyed by PID, so no parent thread can hold it at
# fork; setdefault is atomic with or without the GIL, so racing callers share one (l1_cache's take-over idiom).
_take_over_locks: dict[int, threading.Lock] = {}
# Waiting threads per PID, under _writers_lock(): a forked child, whose threads are not copied, counts from 0.
_parked: dict[int, int] = {}


class WriteBehind:
    """A decorated function's queue of deferred L2 writes and the threads that send them.

    ``send`` callables run in a copy of the submitting caller's context (tenant contextvars included) and
    must not raise: a raise is passed to ``on_error`` and the write counts as failed.
    """

    def __init__(self, on_error: Callable[[Entry, BaseException], None], group: str = "") -> None:
        self._on_error = on_error
        self.group = group  # the decorated function's key registry id: its sibling wrappers share it
        self._state = _State()
        with _writers_lock():
            _writers.add(self)

    def _current(self) -> _State:
        """This process's state. A forked child's first caller replaces the parent's, once, with a re-check:
        the parent's threads do not exist here, and its lock may be held."""
        state, pid = self._state, os.getpid()
        if state.pid == pid:
            return state
        with _take_over_locks.setdefault(pid, threading.Lock()):
            state = self._state
            if state.pid != pid:
                state = self._state = _State()
            return state

    def submit(self, entry: Entry, size: int, send: Callable[[], None]) -> bool:
        """Queue ``send`` as the write of ``entry``. False if refused: the caller writes it synchronously."""
        s = self._current()
        if l1_cache._forked_without_hooks():
            return False  # no thread may start here
        job = _Job(send, contextvars.copy_context(), size)
        with s.cond:
            if s.closed:
                return False
            old = s.pending.get(entry)
            old_size = old.size if old is not None else 0
            # A value behind its entry's own send is always taken: a caller write would race that older send
            # and could lose to it. At most one such value per send in flight, so at most MAX_WORKERS.
            if entry not in s.sending and (
                len(s.pending) + (old is None) > MAX_ENTRIES or s.bytes - old_size + size > MAX_BYTES
            ):
                if old is not None:  # superseded by the value the caller now writes itself
                    del s.pending[entry]
                    s.bytes -= old_size
                return False
            s.pending[entry] = job
            s.bytes += size - old_size
            if s.idle:
                s.wake.notify()  # in the same hold as the queueing, so it cannot pass a thread about to wait
            # Threads not busy sending pick up queued entries; start one only if those cannot cover every
            # entry that can be sent now. An entry behind its own send waits for that send's thread.
            runnable = sum(1 for e in s.pending if e not in s.sending)
            if s.workers < MAX_WORKERS and s.workers - len(s.sending) < runnable:
                s.workers += 1
                try:
                    threading.Thread(target=self._work, args=(s,), daemon=True, name="cachekit-write-behind").start()
                except RuntimeError:  # how Thread.start() refuses: "can't start new thread"
                    s.workers -= 1
                    if s.workers == 0:
                        # No thread will ever send it. Only this job can be queued: a queued job always has one.
                        del s.pending[entry]
                        s.bytes -= size
                        return False
        return True

    def _work(self, s: _State) -> None:
        quit_cleanly = False
        try:
            while True:
                with s.cond:
                    entry = _runnable(s)
                    if entry is None and _park(s):
                        try:
                            s.wake.wait_for(lambda: s.closed or _runnable(s) is not None, timeout=IDLE_S)
                            entry = _runnable(s)
                        finally:
                            _unpark(s)
                    if entry is None:
                        # Given up in the same hold as the empty check: a submit in between would see this
                        # thread still counted, start none, and leave its write queued with no thread.
                        s.workers -= 1
                        quit_cleanly = True
                        s.cond.notify_all()
                        return
                    job = s.pending.pop(entry)
                    s.bytes -= job.size
                    s.sending[entry] = job
                try:
                    job.context.run(job.send)
                except BaseException as exc:  # gevent.Timeout and the like too: a dead thread would strand
                    self._on_error(entry, exc)  # the newer value queued behind this send
                finally:
                    with s.cond:
                        del s.sending[entry]
                        s.cond.notify_all()
        finally:
            if not quit_cleanly:  # a raise: give up the thread's place, so the next submit can start one
                with s.cond:
                    s.workers -= 1
                    s.cond.notify_all()

    def cancel(self, match: Callable[[Entry], bool], *, wait: bool = True) -> None:
        """Drop the queued writes of every entry ``match`` accepts, then, with ``wait``, block until the
        sends of those entries in progress at the call have finished. A send that starts later is a write
        made after the call and is not waited for: a key taking misses throughout cannot hold the caller.
        The wait lasts as long as those backend writes take: it has no timeout of its own, since a delete
        sent before a write returns could be overtaken by it. Never call it from a ``send``: that send would
        wait on itself."""
        s = self._current()
        with s.cond:
            for entry in [e for e in s.pending if match(e)]:
                s.bytes -= s.pending.pop(entry).size
            if wait:
                # Jobs, not entries: a later send of the same entry is another job.
                sending = [(e, job) for e, job in s.sending.items() if match(e)]
                s.cond.wait_for(lambda: all(s.sending.get(e) is not job for e, job in sending))

    def close(self, deadline: float) -> list[Entry]:
        """Refuse new writes, wait until the queue drains or ``deadline`` (``time.monotonic``) passes,
        and return the entries still queued or being sent."""
        s = self._current()
        with s.cond:
            s.closed = True
            s.wake.notify_all()  # waiting threads end now; one with a write to send sends it first
            s.cond.wait_for(lambda: not s.pending and not s.sending, timeout=max(0.0, deadline - time.monotonic()))
            return [*s.sending, *s.pending]


def _runnable(s: _State) -> Entry | None:
    """A queued entry no thread is sending, to send next. Call with ``s.cond`` held."""
    return next((e for e in s.pending if e not in s.sending), None)


def _park(s: _State) -> bool:
    """Count a thread of ``s`` as waiting, if the process is under ``MAX_PARKED``. Call with ``s.cond`` held."""
    with _writers_lock():
        if _parked.get(s.pid, 0) >= MAX_PARKED:
            return False
        _parked[s.pid] = _parked.get(s.pid, 0) + 1
    s.idle += 1
    return True


def _unpark(s: _State) -> None:
    s.idle -= 1
    with _writers_lock():
        _parked[s.pid] -= 1


_writers: weakref.WeakSet[WriteBehind] = weakref.WeakSet()
# Guards _writers and _parked: a decoration can add while an invalidation walks it. Per PID, so a child never waits on
# a lock a parent thread held at fork. Lock order: a state's cond first, then this lock (as _park and _unpark take
# them), never the reverse.
_writers_locks: dict[int, threading.Lock] = {}


def _writers_lock() -> threading.Lock:
    return _writers_locks.setdefault(os.getpid(), threading.Lock())


def _all_writers() -> list[WriteBehind]:
    with _writers_lock():
        return list(_writers)


def cancel(match: Callable[[Entry], bool], *, group: str | None = None, wait: bool = True) -> None:
    """``WriteBehind.cancel`` on every writer in this process, or on those of ``group`` only.

    An invalidation reaches the writes of every wrapper that can hold them: sibling decorations of one
    function share its group, and an exact key reaches every writer, so a function sharing keys with
    another (interop) is covered too.
    """
    for writer in _all_writers():
        if group is None or writer.group == group:
            writer.cancel(match, wait=wait)


def _flush_at_exit() -> None:
    """Send what every writer still holds, within EXIT_FLUSH_SECONDS in total. Never blocks past it."""
    deadline = time.monotonic() + EXIT_FLUSH_SECONDS
    dropped: list[Entry] = []
    for writer in _all_writers():
        try:
            dropped += writer.close(deadline)
        except Exception as exc:  # an exit hook has no caller: report, then flush the rest
            _logger.warning("write-behind exit flush failed: %s", redact_error_for_log(exc))
    if dropped:
        redacted = ", ".join(redact_cache_key(key) for _, key in dropped[:_EXIT_KEYS_LOGGED])
        _logger.warning(
            "write-behind: %d deferred L2 write(s) were not sent within %.0f s of exit and are lost; other "
            "processes miss those keys until they are recomputed. Keys: %s",
            len(dropped),
            EXIT_FLUSH_SECONDS,
            redacted,
        )


atexit.register(_flush_at_exit)
