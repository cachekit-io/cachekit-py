"""Await an executor or HTTP round-trip to completion through cancellation.

Shared by the Redis provider and the CachekitIO backend. It lives here, not in the Redis provider,
because importing that module loads redis-py, and a CachekitIO-only program must not.
"""

from __future__ import annotations

import asyncio
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
        raise cancelled
    return fut.result()
