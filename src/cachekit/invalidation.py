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

import logging
import os
import sys
import threading
import time
import weakref
from collections.abc import Callable
from types import ModuleType
from typing import Any, Optional

import msgpack

from cachekit import l1_cache
from cachekit.cache_handler import supports_key_tracking
from cachekit.config.singleton import get_settings
from cachekit.hash_utils import _WarnThrottle, redact_cache_key, redact_error_for_log

logger = logging.getLogger(__name__)

CHANNEL = "cachekit:py:invalidate:v1"

# Cap on each field, in UTF-8 bytes. A receiver rejects longer strings, so a longer key is
# announced as the whole function, which receivers already handle.
_MAX_FIELD_BYTES = 1024
# Cap on a whole event, checked before decoding. Two capped fields fit well inside it.
_MAX_EVENT_BYTES = 4096

# A listener that failed to start is retried by a cache operation this much later, not by every
# one: during an outage each attempt can wait out a connect timeout, or _CONFIRM_SECONDS.
_START_RETRY_SECONDS = 60.0
# A running listener whose subscription Redis refuses subscribes again this much later.
_RESUBSCRIBE_SECONDS = 60.0
# How long a start waits, in all, for Redis to answer its SUBSCRIBE and its PING.
_CONFIRM_SECONDS = 5.0

# The uWSGI option (2.0.21+) that runs CPython's whole fork protocol, at-fork hooks included, around each
# worker fork. Not py-call-osafterfork: with enable-threads it aborts every worker on Python 3.13+, and on
# earlier versions it does not hold the master's threads off at the fork. Not lazy-apps (or lazy) alone: a
# worker that first imports cachekit after a fork made from C looks like a fresh process, and may start a
# thread that hangs. enable-threads is not checked: uWSGI 2.0.27+ enables threads by default.
_UWSGI_FORK_HOOKS = "py-call-uwsgi-fork-hooks"

Evictor = Callable[[Optional[str]], None]

# One WARNING a minute per kind, process-wide, with the count since the last one; DEBUG between.
# An ACL without the channel (the Redis 7 default for a new user) fails every PUBLISH, so a
# WARNING per invalidation would be a flood.
_publish_failed_warn = _WarnThrottle()
_too_long_warn = _WarnThrottle()
# A forged or foreign publisher controls how many bad events arrive: one WARNING a minute here too.
_dropped_event_warn = _WarnThrottle()
# A listener that cannot reach Redis fails once a second while it retries: one WARNING a minute.
_listener_error_warn = _WarnThrottle()


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
    Redis's reply, never for delivery. A failure leaves the invalidation standing, and peers that
    missed the event keep their L1 copies until the L1 TTL. So does a registry id over the field cap
    (a namespace over 1000 bytes), which no event can carry. Each is logged through its own
    _WarnThrottle: one WARNING a minute with the count since the last, DEBUG between. A key-tracking
    backend with no Redis client (any but the tenant-scoped Redis backend) carries no channel, so
    nothing is announced for it.

    Args:
        backend: The resolved, key-tracking backend whose client carries the event
        registry_id: The function's unscoped registry id
        key: The invalidated cache key, or ``None`` for the whole function. Callers pass ``None``
            for custom ``key=`` functions, whose keys embed caller identifiers.
    """
    try:
        send = getattr(getattr(backend, "_client", None), "publish", None)
        if not callable(send):
            return
        payload = encode_event(registry_id, key)
        if payload is None:
            unannounced = _too_long_warn.claim()
            if not unannounced:
                logger.debug(
                    "Invalidation not announced: registry id %s is over %d bytes",
                    redact_cache_key(registry_id),
                    _MAX_FIELD_BYTES,
                )
                return
            logger.warning(
                "Invalidation not announced (invalidations since the last warning: %d); other processes keep their L1 "
                "copies until the L1 TTL. Registry id %s is over %d bytes (namespace too long)",
                unannounced,
                redact_cache_key(registry_id),
                _MAX_FIELD_BYTES,
            )
            return
        receivers = send(CHANNEL, payload)
    except Exception as e:
        failures = _publish_failed_warn.claim()
        if not failures:
            logger.debug("Invalidation announcement failed for %s: %s", redact_cache_key(registry_id), redact_error_for_log(e))
            return
        logger.warning(
            "Invalidation announcement failed (failures since the last warning: %d); other processes keep their "
            "L1 copies until the L1 TTL. Latest registry %s: %s",
            failures,
            redact_cache_key(registry_id),
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
_listener_pid: Optional[int] = None  # the process the running listener belongs to
_listener: Any = None  # that listener's worker thread (its .pubsub is the subscription)
_start_locks: dict[int, threading.Lock] = {}  # see _pid_lock
_start_retry_at = float("-inf")  # time.monotonic() before which a failed start is not retried
_untrackable_warned: dict[int, object] = {}  # PID -> marker: one WARNING per process


def _listener_enabled() -> bool:
    """CACHEKIT_INVALIDATION_LISTENER_ENABLED, from the settings the rest of cachekit reads."""
    return get_settings().invalidation_listener_enabled


def listener_start_due(backend: object) -> bool:
    """Whether this cache operation should start the process's listener. Never raises.

    With the flag unset, the default, this reads one setting. With it set, it first checks that
    the backend's class carries events: only a class that keeps a key registry and can clone a
    listener pool does (PerRequestRedisBackend), and a function on any other backend gets the
    one-time WARNING, whether or not another function's listener already runs. Then it compares the
    process id with the listener's owner, so a forked child starts its own listener instead of
    believing it has its parent's; the parent's socket is the parent's. A child forked without
    at-fork hooks (uWSGI without py-call-uwsgi-fork-hooks) runs no listener at all and
    this logs nothing there: a thread started in such a child can hang in Thread.start(), and a
    log call on a handler lock a parent thread held at fork hangs too. Its L1 heals by TTL.
    """
    try:
        if not _listener_enabled():
            return False
        if not supports_key_tracking(backend) or not callable(getattr(type(backend), "listener_pool", None)):
            if not l1_cache._forked_without_hooks():
                _warn_untrackable(backend)
            return False
        if _listener_pid == os.getpid() or l1_cache._forked_without_hooks():
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


def _redis() -> ModuleType:
    """redis-py, imported only once a listener needs it.

    A module-level import would load redis-py, and with it hiredis, for every program that imports
    cachekit. cachekit's redis package is imported first so its hiredis decision runs before
    redis-py loads (see cachekit.hiredis_compat).
    """
    import cachekit.backends.redis  # noqa: F401

    # isort: split
    import redis

    return redis


def start_listener(backend: Any) -> None:
    """Start this process's listener on ``backend``'s Redis, unless another thread is starting it.

    Never raises and never waits for another thread. It connects and subscribes, so it is sync, and
    it returns once Redis has confirmed the SUBSCRIBE and answered a PING (_confirm_subscription):
    a refusal fails the start instead of hiding in the worker, and a sync cache operation that
    starts the listener reads L2 only after the subscription is in place. Nothing else waits for
    the start: an async operation runs it in an executor thread without awaiting it, and another
    thread's operation goes on while it is in progress. An event published before the subscription
    is lost, and that L1 entry expires by its L1 TTL.

    A failed start (Redis unreachable, an ACL that refuses the channel or PING, or no reply within
    _CONFIRM_SECONDS) is a WARNING, and the next cache operation that reaches Redis after
    _START_RETRY_SECONDS retries it; the window is checked again under the lock, so a thread that
    raced the failure does not retry at once. Once started, the listener is this process's until it
    exits, and its worker thread alone deals with what Redis does next (_on_listener_error).
    """
    global _listener, _listener_pid, _start_retry_at
    lock = _pid_lock(_start_locks)
    if not lock.acquire(blocking=False):
        return
    try:
        if _listener_pid == os.getpid() or time.monotonic() < _start_retry_at:
            return
        pubsub = None
        try:
            pubsub = _redis().Redis(connection_pool=backend.listener_pool()).pubsub()
            pubsub.subscribe(**{CHANNEL: _on_message})
            _confirm_subscription(pubsub)
            thread = pubsub.run_in_thread(sleep_time=1.0, daemon=True, exception_handler=_on_listener_error)
        except Exception as e:
            if pubsub is not None:
                try:
                    pubsub.close()
                except Exception as close_error:
                    logger.debug("Closing the failed listener's connection failed: %s", redact_error_for_log(close_error))
            _start_retry_at = time.monotonic() + _START_RETRY_SECONDS
            logger.warning(
                "Invalidation listener failed to start; the next cache operation that reaches Redis after %d s retries it: %s",
                _START_RETRY_SECONDS,
                redact_error_for_log(e),
            )
            return
        thread.name = "cachekit-invalidation-listener"  # no function or key metadata (CWE-532)
        # Replaces a parent's listener in a forked child. Dropping that one does no I/O on the
        # parent's socket: redis-py shuts a connection's socket down only in the process that opened
        # it, and a PubSub reset sends no UNSUBSCRIBE.
        _listener = thread
        _listener_pid = os.getpid()
    finally:
        lock.release()
    logger.info("Invalidation listener started: pid=%d channel=%s", os.getpid(), CHANNEL)


def _confirm_subscription(pubsub: Any) -> None:
    """Wait up to _CONFIRM_SECONDS, in all, for Redis to confirm the SUBSCRIBE and then answer a
    PING. Raises on a refusal (NoPermissionError) and on no reply (TimeoutError).

    redis-py only sends the SUBSCRIBE, so without this a refusal (an ACL without the channel, the
    Redis 7 default for a new user) would surface later in the worker. The PING is the listener's
    idle check: an ACL that allows SUBSCRIBE but not PING would otherwise pass here and fail ten
    seconds later. An event that arrives before the PONG goes to its evictors, as in the worker.
    """
    deadline = time.monotonic() + _CONFIRM_SECONDS
    for expected in ("subscribe", "pong"):
        if expected == "pong":
            pubsub.ping()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _redis().TimeoutError(f"Redis sent no {expected} reply in {_CONFIRM_SECONDS:g} s")
            reply = pubsub.get_message(timeout=remaining)  # a refusal raises here (NoPermissionError)
            if reply is not None and reply.get("type") == expected:
                break


def _on_message(message: dict[str, Any]) -> None:
    """Handle one event, in the listener thread (or in the starting thread, before the PONG). Never
    raises: a bad event is dropped and the listener keeps running. Never logs the registry id or
    key: a forged event carries the sender's text."""
    try:
        registry_id, key = decode_event(message.get("data"))
        evictors = _evictors_for(registry_id)
        if not evictors:
            logger.debug("Invalidation event for a function this process does not cache: dropped")
            return
        for evict in evictors:
            evict(key)
    except Exception as e:
        drops = _dropped_event_warn.claim()
        if not drops:
            logger.debug("Invalidation event dropped: %s", redact_error_for_log(e))
            return
        logger.warning(
            "Invalidation event dropped (drops since the last warning: %d); the listener keeps running. Latest: %s",
            drops,
            redact_error_for_log(e),
        )


def _on_listener_error(error: BaseException, pubsub: Any, thread: Any) -> None:
    """Called by redis-py's worker thread instead of dying. Events published meanwhile are lost.

    It handles what goes wrong after a successful start; a refusal at start fails the start
    instead (_confirm_subscription). It never stops the thread or touches who owns the listener, so
    it cannot race the start that records the thread, and its retry needs no cache operation: a
    process serving only L1 hits recovers too. A connection error: the thread's next read
    reconnects, and the connection's on_connect callback subscribes again, after a second's wait so
    a Redis that is down is not spun on. A command Redis refused, a SUBSCRIBE or PING an ACL change
    now denies above all: redis-py sends the SUBSCRIBE again only on a reconnect, so the thread waits
    _RESUBSCRIBE_SECONDS and drops the connection, and its next read reconnects and subscribes again.
    A grant therefore takes effect within that wait, with no restart.
    """
    if isinstance(error, _redis().ResponseError):
        logger.warning(
            "Invalidation listener refused by Redis; subscribing again in %d s: %s",
            _RESUBSCRIBE_SECONDS,
            redact_error_for_log(error),
        )
        time.sleep(_RESUBSCRIBE_SECONDS)
        connection = pubsub.connection
        if connection is not None:
            connection.disconnect()
        return
    errors = _listener_error_warn.claim()
    if not errors:
        logger.debug("Invalidation listener error; retrying in 1 s: %s", redact_error_for_log(error))
    else:
        logger.warning(
            "Invalidation listener error (errors since the last warning: %d); retrying every second. Latest: %s",
            errors,
            redact_error_for_log(error),
        )
    time.sleep(1.0)


def _stop_listener() -> None:
    """Stop and forget this process's listener (tests)."""
    global _listener, _listener_pid
    thread, _listener, _listener_pid = _listener, None, None
    if thread is not None:
        thread.stop()  # its loop closes the PubSub on its way out
        thread.join(timeout=5)


def _uwsgi_forked_this_process(uwsgi: Any) -> bool:
    """Whether uWSGI forked this process from C (a worker, mule or spooler), or cannot say.

    l1_cache._forked_without_hooks misses a process that first imports cachekit after such a fork:
    a worker under lazy-apps, or one whose app imports cachekit on its first request. With --master,
    uWSGI forked every process but the master; without it, masterpid() is 0 and worker_id() stays 0
    in the process that loads the app until it forks the workers.
    """
    try:
        master = uwsgi.masterpid()
        return os.getpid() != master if master else uwsgi.worker_id() > 0
    except Exception:  # not uWSGI's own module: never log when unsure, never fail import cachekit
        return True


def _warn_if_uwsgi_skips_fork_hooks() -> None:
    """One WARNING when this process runs under uWSGI without _UWSGI_FORK_HOOKS set.

    uWSGI forks its workers from C and runs no Python at-fork hook unless told to, so a worker
    keeps the master's L1 entries and runs no invalidation listener. Called once, at import, and
    logs only in a process uWSGI did not fork, the one that loads the app: a process forked without
    hooks must not log at all, since a handler lock a master thread held at the fork would hang it.
    So under lazy-apps, where each worker first imports cachekit after the fork, nothing logs.
    uWSGI registers its ``uwsgi`` module before it imports the app, so this looks it up rather than
    importing it: an import outside uWSGI would run any ``uwsgi.py`` on ``sys.path``.
    """
    uwsgi = sys.modules.get("uwsgi")
    options = getattr(uwsgi, "opt", None)
    if not isinstance(options, dict) or _UWSGI_FORK_HOOKS in options or _uwsgi_forked_this_process(uwsgi):
        return
    logger.warning(
        "uWSGI is running without py-call-uwsgi-fork-hooks, so its worker forks skip all or part of "
        "CPython's fork protocol: a worker keeps the master's L1 entries and runs no invalidation "
        "listener, or under py-call-osafterfork can hang or abort at start. Run uWSGI with "
        "--enable-threads --py-call-uwsgi-fork-hooks (uWSGI 2.0.21+)."
    )


_warn_if_uwsgi_skips_fork_hooks()
