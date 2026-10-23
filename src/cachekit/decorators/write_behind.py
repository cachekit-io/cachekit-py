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
- **Bounds.** A new entry is admitted only while fewer than ``MAX_ENTRIES`` are queued and their bytes
  stay within ``MAX_BYTES``; otherwise ``submit`` refuses and the caller writes synchronously, as without
  the option. A replacement for an entry already queued or being sent is always taken, so a fallback never
  races a deferred write of the same entry: the queue can exceed the bounds by one value per sending
  thread, plus the growth of replaced values. At most ``MAX_WORKERS`` threads send, started on demand and
  ending when the queue is empty. The bounds are sized so a full queue drains within ``EXIT_FLUSH_SECONDS``
  at half a second per write.
- **Invalidation.** ``cancel`` drops matching queued writes and waits for matching sends to finish, so a
  delete issued after it is ordered behind every write the caller made before it.
- **Fork.** A forked child starts empty: its first caller publishes a fresh ``_State`` in one store, and
  the parent's queue is the parent's to send. A child forked without at-fork hooks starts no thread, so
  ``submit`` refuses there.
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

    __slots__ = ("bytes", "closed", "cond", "pending", "pid", "sending", "workers")

    def __init__(self) -> None:
        self.cond = threading.Condition(threading.Lock())
        self.pending: dict[Entry, _Job] = {}
        self.sending: dict[Entry, _Job] = {}  # entry -> the job being sent for it
        self.bytes = 0
        self.workers = 0
        self.closed = False
        self.pid = os.getpid()


# The lock a forked child's first callers take over under. Keyed by PID, so no parent thread can hold it at
# fork; setdefault is atomic with or without the GIL, so racing callers share one (l1_cache's take-over idiom).
_take_over_locks: dict[int, threading.Lock] = {}


class WriteBehind:
    """A decorated function's queue of deferred L2 writes and the threads that send them.

    ``send`` callables run in a copy of the submitting caller's context (tenant contextvars included) and
    must not raise: a raise is passed to ``on_error`` and the write counts as failed.
    """

    def __init__(self, on_error: Callable[[Entry, BaseException], None]) -> None:
        self._on_error = on_error
        self._state = _State()
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
            if old is None and entry not in s.sending and (len(s.pending) >= MAX_ENTRIES or s.bytes + size > MAX_BYTES):
                return False
            s.pending[entry] = job
            s.bytes += size - (old.size if old is not None else 0)
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
                    entry = next((e for e in s.pending if e not in s.sending), None)
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
            s.cond.wait_for(lambda: not s.pending and not s.sending, timeout=max(0.0, deadline - time.monotonic()))
            return [*s.sending, *s.pending]


_writers: weakref.WeakSet[WriteBehind] = weakref.WeakSet()


def _flush_at_exit() -> None:
    """Send what every writer still holds, within EXIT_FLUSH_SECONDS in total. Never blocks past it."""
    deadline = time.monotonic() + EXIT_FLUSH_SECONDS
    dropped: list[Entry] = []
    for writer in list(_writers):
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
