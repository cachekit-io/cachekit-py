"""Cancellation and interrupt handling shared by the Redis backends and the CachekitIO backend.

It lives here, not in the Redis provider, because importing that module loads redis-py, and a CachekitIO-only program must
not.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from collections.abc import Iterator
from types import TracebackType
from typing import Optional, TypeVar

T = TypeVar("T")


async def _await_uninterrupted(fut: asyncio.Future[T]) -> T:
    """Await ``fut`` to completion even if the current task is cancelled meanwhile.

    ``asyncio.to_thread`` work is uninterruptible once an executor thread picks it up, and a
    still-queued work item is dropped if its future is cancelled first — so a cancelled awaiter
    either loses the outcome of a round-trip that still completes, or loses the round-trip
    itself. ``asyncio.wait`` never cancels its inputs and never unwraps their result, so keep
    waiting on ``fut`` until it is really done, absorbing every cancellation, then re-raise the
    last one: callers read ``fut`` for the real outcome before letting it propagate.

    Prefer a plain future (``loop.run_in_executor``): ``all_tasks()`` sweeps such as ``asyncio.run``
    teardown cancel Tasks out from under the drain, and the outcome is lost again. A Task
    (``asyncio.ensure_future``) is the route for native coroutines such as an async HTTP request;
    it gives up only that sweep, so the caller must treat a cancelled ``fut`` as having no outcome.
    """
    cancelled: Optional[asyncio.CancelledError] = None
    while not fut.done():
        try:
            await asyncio.wait({fut})
        except asyncio.CancelledError as exc:
            cancelled = exc
    if cancelled is not None:
        try:
            # Without the traceback it was raised with: asyncio.wait's frames on it hold ``fut``, and once ``fut`` has
            # failed, its exception's frames, whose locals can hold a credential, stay reachable from the cancel.
            raise cancelled.with_traceback(None)
        finally:
            # This frame is on the cancel's traceback too: holding the cancel here is a reference cycle, and holding
            # ``fut`` keeps it reachable again. Callers keep their own reference to ``fut``.
            del fut, cancelled
    try:
        return fut.result()
    finally:
        del fut  # a failed ``fut``'s exception has this frame on its traceback: a reference cycle


def _raised_during_request(exc: BaseException, handled_before: BaseException | None) -> Iterator[BaseException]:
    """``exc``, then each exception its ``__cause__`` and ``__context__`` reach, once each, stopping at ``handled_before``.

    ``handled_before`` is the exception the caller was already handling when the request began: the request's exceptions
    chain it as ``__context__``, but its traceback is the caller's. ``exc`` itself comes first even when it is
    ``handled_before`` (a reused ``gevent.Timeout``), because its traceback now runs through the request too.
    """
    yield exc
    seen = {id(exc), id(handled_before)}
    pending = [exc.__cause__, exc.__context__]
    while pending:
        chained = pending.pop()
        if chained is None or id(chained) in seen:
            continue
        seen.add(id(chained))
        yield chained
        pending += [chained.__cause__, chained.__context__]


def _clear_interrupted(
    exc: BaseException, handled_before: BaseException | None, handled_traceback: TracebackType | None
) -> None:
    """Clear the locals of every finished frame on the traceback of ``exc``, an interrupt leaving a request, and of each
    exception it chained during the request (CWE-532).

    An interrupt (a worker timeout's SystemExit, KeyboardInterrupt, gevent.Timeout) is not a failure a backend classifies:
    it propagates as itself, but its traceback, and those of the exceptions it chained, run through the client library's
    request frames, whose locals can hold a credential. ``traceback.clear_frames`` keeps each frame's file and line, and
    skips a frame still executing, such as the backend's own, which must therefore hold no credential in a local.
    ``handled_before`` is the exception the caller was handling when the request began: unless it is the interrupt, its
    frames are left alone, and it gets back ``handled_traceback``, the traceback it came in with, in case the request
    re-raised it.
    """
    for raised in _raised_during_request(exc, handled_before):
        traceback.clear_frames(raised.__traceback__)
    if handled_before is not None and handled_before is not exc:
        handled_before.__traceback__ = handled_traceback


class _ClearedOnInterrupt:
    """Around a request: an exception that is not an ``Exception`` leaves it with its frames cleared (see
    ``_clear_interrupted``) and propagates as itself.

    The block's ``except Exception`` handlers run inside it, so an interrupt landing in one, which chains the request's
    failure, is cleared too. A CancelledError is cleared the same way.
    """

    __slots__ = ("_handled_before", "_handled_traceback")

    def __enter__(self) -> None:
        self._handled_before = sys.exc_info()[1]
        # Off the exception, not sys.exc_info(): on Python 3.10 that keeps the traceback it was caught with after the
        # caller clears it.
        self._handled_traceback = None if self._handled_before is None else self._handled_before.__traceback__

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        handled_before, handled_traceback = self._handled_before, self._handled_traceback
        self._handled_before = self._handled_traceback = None
        if exc is not None and not isinstance(exc, Exception):
            _clear_interrupted(exc, handled_before, handled_traceback)
