"""Cross-process L1 invalidation on Redis pub/sub.

Publishing. After an invalidation's L2 change succeeded on a key-tracking backend, the decorator
announces it with one ``PUBLISH`` on :data:`CHANNEL`, on the backend's own client: no knob, no new
connection, no thread. The event is a MessagePack map of two strings, ``r`` (the function's registry
id) and ``k`` (the invalidated cache key, absent when every key of the function was invalidated).

Listening. A process with ``CACHEKIT_INVALIDATION_LISTENER_ENABLED`` set runs one listener: redis-py's
own pub/sub worker thread on a one-connection pool cloned from the backend's
(``PerRequestRedisBackend.listener_pool``), started by the first cache operation that reaches such a
backend. Each event is decoded under a size and shape bound, because the bytes are untrusted, and
handed to the evictors registered for its registry id: one per decorated function with an L1.
Delivery is at most once. An event sent while a listener is not subscribed is lost, and the entry
expires by its L1 TTL.

The channel is local to cachekit-py: no other SDK reads or writes it, and a format change takes a
new channel name, never a field negotiation.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any, Optional

import msgpack
import redis

from cachekit import l1_cache
from cachekit.cache_handler import supports_key_tracking
from cachekit.hash_utils import redact_cache_key, redact_error_for_log

logger = logging.getLogger(__name__)

CHANNEL = "cachekit:py:invalidate:v1"

# Cap on each field, in UTF-8 bytes. A receiver rejects longer strings, so a longer key is
# announced as the whole function, which receivers already handle.
_MAX_FIELD_BYTES = 1024
# Cap on a whole event, checked before decoding. Two capped fields fit well inside it.
_MAX_EVENT_BYTES = 4096

# A listener that failed to start is retried by a cache operation this much later, not by every
# one: during an outage each attempt can wait out a connect timeout.
_START_RETRY_SECONDS = 60.0
# How long a start waits for Redis to confirm the subscription.
_SUBSCRIBE_CONFIRM_SECONDS = 5.0

# uWSGI options under which a worker runs Python's at-fork hooks, or imports the app after the fork.
_UWSGI_FORK_OPTIONS = ("py-call-uwsgi-fork-hooks", "py-call-osafterfork", "lazy-apps", "lazy")

Evictor = Callable[[Optional[str]], None]


def encode_event(registry_id: str, key: Optional[str]) -> Optional[bytes]:
    """The event announcing that ``key`` was invalidated, or every key of the function (``None``).

    Returns ``None`` when the registry id itself is over the cap: such an event could only be
    dropped by every receiver.

    Examples:
        >>> import msgpack
        >>> msgpack.unpackb(encode_event("ck:reg:ns:00ff", "ns:ns:func:m.f:args:ab:1s"))
        {'r': 'ck:reg:ns:00ff', 'k': 'ns:ns:func:m.f:args:ab:1s'}
        >>> msgpack.unpackb(encode_event("ck:reg:ns:00ff", None))
        {'r': 'ck:reg:ns:00ff'}

        A key over 1024 UTF-8 bytes widens the event to the whole function:

        >>> msgpack.unpackb(encode_event("ck:reg:ns:00ff", "鍵" * 400))
        {'r': 'ck:reg:ns:00ff'}
        >>> encode_event("ck:reg:" + "x" * 1024, None) is None
        True
    """
    if len(registry_id.encode("utf-8")) > _MAX_FIELD_BYTES:
        return None
    event = {"r": registry_id}
    if key is not None and len(key.encode("utf-8")) <= _MAX_FIELD_BYTES:
        event["k"] = key
    return msgpack.packb(event)


def decode_event(data: object) -> tuple[str, Optional[str]]:
    """The registry id and key (``None``: every key) an event carries. Raises on anything else.

    The bytes are untrusted: any client allowed to ``PUBLISH`` on this Redis can send them. So the
    size is bounded before decoding, and MessagePack may build only maps of at most 4 entries and
    strings of at most 1024 bytes: no bin, array or ext values, and nesting deep enough to exhaust
    the decoder's stack raises. A forged event that passes costs evictions from L1, never more.

    Examples:
        >>> decode_event(encode_event("ck:reg:ns:00ff", "ns:ns:func:m.f:args:ab:1s"))
        ('ck:reg:ns:00ff', 'ns:ns:func:m.f:args:ab:1s')
        >>> decode_event(encode_event("ck:reg:ns:00ff", None))
        ('ck:reg:ns:00ff', None)
        >>> decode_event(b"\\x91\\x01")  # an array
        Traceback (most recent call last):
            ...
        ValueError: 1 exceeds max_array_len(0)
    """
    if not isinstance(data, bytes) or len(data) > _MAX_EVENT_BYTES:
        raise ValueError(f"not an event: bytes of at most {_MAX_EVENT_BYTES} expected")
    event = msgpack.unpackb(
        data,
        raw=False,
        use_list=False,
        max_map_len=4,
        max_str_len=_MAX_FIELD_BYTES,
        max_bin_len=0,
        max_array_len=0,
        max_ext_len=0,
    )
    if not isinstance(event, dict) or not isinstance(event.get("r"), str):
        raise ValueError("not an event: a map with a string registry id expected")
    key = event.get("k")
    if "k" in event and not isinstance(key, str):
        raise ValueError("not an event: the key is not a string")
    return event["r"], key


def publish(backend: Any, registry_id: str, key: Optional[str]) -> None:
    """Announce an invalidation whose L2 change already succeeded. Never raises.

    One ``PUBLISH`` on the backend's shared client, outside its error classification and the
    reliability stack, so a pub/sub failure cannot count against the circuit breaker. It waits for
    Redis's reply, never for delivery. A failure is a WARNING: the invalidation itself stands, and
    peers that missed the event keep their L1 copies until the L1 TTL.

    Args:
        backend: The resolved, key-tracking backend whose client carries the event
        registry_id: The function's unscoped registry id
        key: The invalidated cache key, or ``None`` for the whole function. Callers pass ``None``
            for custom ``key=`` functions, whose keys embed caller identifiers.
    """
    try:
        payload = encode_event(registry_id, key)
        if payload is None:
            logger.warning(
                "Invalidation not announced: registry id %s is over %d bytes (namespace too long)",
                redact_cache_key(registry_id),
                _MAX_FIELD_BYTES,
            )
            return
        receivers = backend._client.publish(CHANNEL, payload)
    except Exception as e:
        logger.warning(
            "Invalidation announcement failed; other processes keep their L1 copies until the L1 TTL: %s",
            redact_error_for_log(e),
        )
        return
    logger.debug("Invalidation announced to %s listener(s)", receivers)


def _pid_lock(locks: dict[int, threading.Lock]) -> threading.Lock:
    """This process's lock in ``locks``. A child gets its own, so a lock a parent thread held at
    fork, and which no thread in the child will ever release, is never waited on; ``setdefault``
    is atomic with or without the GIL, so the threads of one process share one lock."""
    pid = os.getpid()
    lock = locks.get(pid)
    return lock if lock is not None else locks.setdefault(pid, threading.Lock())


# ---- Dispatch: registry id -> the evictors of every live decorated function with that id ----
# Weak: a decorated function holds its own evictor, so one that is discarded drops out.
_evictors: dict[str, weakref.WeakSet[Evictor]] = {}
_dispatch_locks: dict[int, threading.Lock] = {}  # guards _evictors; see _pid_lock


def register(registry_id: str, evict: Evictor) -> None:
    """Route events for ``registry_id`` to ``evict``, called with the key or ``None`` (every key).

    ``evict`` is held weakly: the caller keeps it alive for as long as it should receive events.
    """
    with _pid_lock(_dispatch_locks):
        _evictors.setdefault(registry_id, weakref.WeakSet()).add(evict)


def _evictors_for(registry_id: str) -> list[Evictor]:
    """A snapshot, taken under the lock register() takes: iterating the live set while a decoration
    adds to it would raise, and drop the event."""
    with _pid_lock(_dispatch_locks):
        evictors = _evictors.get(registry_id)
        return list(evictors) if evictors is not None else []


# ---- Listener: one per process, started by a cache operation (owner-PID check, no fork hook) ----
_listener_flag: Optional[bool] = None  # CACHEKIT_INVALIDATION_LISTENER_ENABLED, read on first use
_listener_pid: Optional[int] = None  # the process the running listener belongs to
_listener: Any = None  # (PubSub, worker thread) of that listener
_start_locks: dict[int, threading.Lock] = {}  # see _pid_lock
_start_retry_at = float("-inf")  # time.monotonic() before which a failed start is not retried
_untrackable_warned: dict[int, object] = {}  # PID -> marker: one WARNING per process


def _listener_enabled() -> bool:
    global _listener_flag
    if _listener_flag is None:
        from cachekit.config.singleton import get_settings

        _listener_flag = get_settings().invalidation_listener_enabled
    return _listener_flag


def listener_start_due(backend: object) -> bool:
    """Whether this cache operation should start the process's listener. Never raises.

    With the flag unset, the default, this reads one cached bool. With it set, it compares the
    process id with the listener's owner, so a forked child starts its own listener instead of
    believing it has its parent's; the parent's socket is the parent's. A child forked without
    at-fork hooks (uWSGI without the options in _UWSGI_FORK_OPTIONS) runs no listener at all and
    this logs nothing there: a thread started in such a child can hang in Thread.start(), and a
    log call on a handler lock a parent thread held at fork hangs too. Its L1 heals by TTL.
    """
    try:
        if not _listener_enabled() or _listener_pid == os.getpid() or l1_cache._forked_without_hooks():
            return False
        if not supports_key_tracking(backend):
            _warn_untrackable(backend)
            return False
        return time.monotonic() >= _start_retry_at
    except Exception as e:  # a listener must never fail a cache operation
        if not l1_cache._forked_without_hooks():
            logger.warning("Invalidation listener check failed: %s", redact_error_for_log(e))
        return False


def _warn_untrackable(backend: object) -> None:
    marker = object()
    if _untrackable_warned.setdefault(os.getpid(), marker) is marker:
        logger.warning(
            "CACHEKIT_INVALIDATION_LISTENER_ENABLED is set, but %s does not carry invalidation events: "
            "functions cached on it get no cross-process L1 eviction. Only the tenant-scoped Redis "
            "backend (CACHEKIT_REDIS_URL / REDIS_URL, or RedisBackendProvider) does.",
            type(backend).__name__,
        )


def start_listener(backend: Any) -> None:
    """Start this process's listener on ``backend``'s Redis, unless another thread is starting it.

    Never raises and never waits for another thread. Sync; async callers run it through
    asyncio.to_thread, as it connects and subscribes. A failed start is a WARNING, and a cache
    operation retries it _START_RETRY_SECONDS later. Once started, reconnecting is redis-py's: the
    worker thread's next read reconnects, and the connection's on_connect callback re-subscribes.
    """
    global _listener, _listener_pid, _start_retry_at
    lock = _pid_lock(_start_locks)
    if not lock.acquire(blocking=False):
        return
    try:
        if _listener_pid == os.getpid():
            return
        pubsub = None
        try:
            pubsub = redis.Redis(connection_pool=backend.listener_pool()).pubsub()
            pubsub.subscribe(**{CHANNEL: _on_message})
            _confirm_subscription(pubsub)
            thread = pubsub.run_in_thread(sleep_time=1.0, daemon=True, exception_handler=_on_listener_error)
        except Exception as e:
            if pubsub is not None:
                with contextlib.suppress(Exception):
                    pubsub.close()
            _start_retry_at = time.monotonic() + _START_RETRY_SECONDS
            logger.warning(
                "Invalidation listener failed to start; a cache operation retries in %d s: %s",
                _START_RETRY_SECONDS,
                redact_error_for_log(e),
            )
            return
        thread.name = "cachekit-invalidation-listener"  # no function or key metadata (CWE-532)
        # Replaces a parent's listener in a forked child. Dropping that one does no I/O on the
        # parent's socket: redis-py shuts a connection's socket down only in the process that opened
        # it, and a PubSub reset sends no UNSUBSCRIBE.
        _listener = (pubsub, thread)
        _listener_pid = os.getpid()
    finally:
        lock.release()
    logger.info("Invalidation listener started: pid=%d channel=%s", os.getpid(), CHANNEL)


def _confirm_subscription(pubsub: Any) -> None:
    """Wait for Redis to confirm the SUBSCRIBE. redis-py only sends it, so a subscription Redis refuses
    (an ACL without the channel, the Redis 7 default for a new user) would otherwise surface later,
    in the worker thread, with this process believing it listens."""
    reply = pubsub.get_message(timeout=_SUBSCRIBE_CONFIRM_SECONDS)  # raises on a refusal (NoPermissionError)
    if reply is None or reply.get("type") != "subscribe":
        raise redis.ConnectionError("Redis did not confirm the subscription")


def _on_message(message: dict[str, Any]) -> None:
    """Handle one event in the listener thread. Never raises: a bad event is dropped and the
    listener keeps running. Never logs the registry id or key: a forged event carries the sender's
    text."""
    try:
        registry_id, key = decode_event(message.get("data"))
        evictors = _evictors_for(registry_id)
        if not evictors:
            logger.debug("Invalidation event for a function this process does not cache: dropped")
            return
        for evict in evictors:
            evict(key)
    except Exception as e:
        logger.warning("Invalidation event dropped: %s", redact_error_for_log(e))


def _on_listener_error(error: BaseException, pubsub: Any, thread: Any) -> None:
    """Called by redis-py's worker thread instead of dying. Events published meanwhile are lost.

    A connection error: the thread's next read reconnects and re-subscribes, after a second's wait so
    a Redis that is down is not spun on. A command Redis refused, above all a re-subscription an ACL
    now denies: redis-py would send it again only on its next reconnect, so the listener is retired,
    and a cache operation starts a new one, which confirms its subscription, _START_RETRY_SECONDS later.
    """
    if isinstance(error, redis.ResponseError):
        _retire(thread)
        logger.warning(
            "Invalidation listener stopped: Redis refused it; a cache operation retries in %d s: %s",
            _START_RETRY_SECONDS,
            redact_error_for_log(error),
        )
        return
    logger.warning("Invalidation listener error; retrying in 1 s: %s", redact_error_for_log(error))
    time.sleep(1.0)


def _retire(thread: Any) -> None:
    """Stop ``thread``'s loop and, if it is this process's listener, forget it so a cache operation
    can start another after the retry window."""
    global _listener, _listener_pid, _start_retry_at
    thread.stop()
    if _listener is not None and _listener[1] is thread:
        _start_retry_at = time.monotonic() + _START_RETRY_SECONDS
        _listener, _listener_pid = None, None


def _stop_listener() -> None:
    """Stop and forget this process's listener (tests)."""
    global _listener, _listener_pid
    listener, _listener, _listener_pid = _listener, None, None
    if listener is not None:
        _, thread = listener
        thread.stop()  # its loop closes the PubSub on its way out
        thread.join(timeout=5)


def _warn_if_uwsgi_skips_fork_hooks() -> None:
    """One WARNING when this process runs under uWSGI with none of _UWSGI_FORK_OPTIONS set.

    uWSGI forks its workers from C and runs no Python at-fork hook unless told to, so a worker
    keeps its master's L1 entries and runs no invalidation listener. Called once, at import, which
    is in the master before any worker exists: a worker forked without hooks must not log at all.
    uWSGI registers its ``uwsgi`` module before it imports the app, so this looks it up rather than
    importing it: an import outside uWSGI would run any ``uwsgi.py`` on ``sys.path``.
    """
    if l1_cache._forked_without_hooks():
        return
    options = getattr(sys.modules.get("uwsgi"), "opt", None)
    if not isinstance(options, dict) or any(name in options for name in _UWSGI_FORK_OPTIONS):
        return
    logger.warning(
        "uWSGI forks its workers without running Python's at-fork hooks: each worker keeps the "
        "master's L1 entries and runs no invalidation listener. Set py-call-uwsgi-fork-hooks "
        "(uWSGI 2.0.21+), py-call-osafterfork or lazy-apps."
    )


_warn_if_uwsgi_skips_fork_hooks()
