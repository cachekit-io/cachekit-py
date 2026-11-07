"""Bridge between @cache.local() decorator and ObjectCache.

Handles sync/async detection, key generation, parameter validation,
and attaches the standard wrapper API (invalidate_cache, cache_clear, cache_info).
"""

from __future__ import annotations

import functools
import inspect
import threading
from collections.abc import Callable, Hashable
from typing import Any

from cachekit._rust_serializer import HitCounter, HitMemo
from cachekit._rust_serializer import memo_key as _memo_key
from cachekit.decorators.single_flight import AsyncFlights, ThreadFlights
from cachekit.decorators.wrapper import CacheInfo, InvalidationOutcome
from cachekit.key_generator import CacheKeyGenerator
from cachekit.object_cache import ObjectCache

_ALLOWED_PARAMS: frozenset[str] = frozenset({"ttl", "max_entries", "namespace", "key"})

# What HitMemo.get returns when it cannot answer a call.
_MISS = object()


class _Names:
    """A function's __module__ and __qualname__ as read once: what CacheKeyGenerator.generate_key reads off func.

    Building a key from this rather than the function binds the key to one read of the names, so a
    rename while the key is built cannot give it names other than the ones its memo admission checked.
    """

    def __init__(self, module: Any, qualname: Any) -> None:
        self.__module__ = module
        self.__qualname__ = qualname


def create_local_wrapper(
    func: Callable[..., Any],
    **kwargs: Any,
) -> Callable[..., Any]:
    """Create a locally-cached wrapper for *func* using ObjectCache.

    Validation and ObjectCache construction happen at decoration time.
    The sync or async wrapper is chosen based on ``inspect.iscoroutinefunction(func)``.

    Args:
        func: The function to wrap.
        **kwargs: Accepts ``ttl``, ``max_entries``, ``namespace``, ``key``.
            Any other keyword raises ``TypeError``.

    Returns:
        A wrapped callable with ``invalidate_cache``, ``ainvalidate_cache``,
        ``cache_clear``, ``cache_info``, and ``__wrapped__`` attached.

    Raises:
        TypeError: If unknown keyword arguments are passed.
        ValueError: If ttl < 1 or max_entries < 1.
    """
    # --- Parameter validation (fail-fast at decoration time) ---
    unknown = set(kwargs) - _ALLOWED_PARAMS
    if unknown:
        raise TypeError(
            f"@cache.local() only accepts: key, max_entries, namespace, ttl. "
            f"Got: {sorted(unknown)}. "
            f"For serialized caching use @cache(), for encryption use @cache.secure()."
        )

    ttl: int = kwargs.get("ttl", 300)  # type: ignore[assignment]
    max_entries: int = kwargs.get("max_entries", 256)  # type: ignore[assignment]
    namespace: str | None = kwargs.get("namespace", None)  # type: ignore[assignment]
    key: Callable[..., str] | None = kwargs.get("key", None)  # type: ignore[assignment]

    if not isinstance(ttl, int):
        raise TypeError(f"ttl must be an int, got {type(ttl).__name__}")
    if not isinstance(max_entries, int):
        raise TypeError(f"max_entries must be an int, got {type(max_entries).__name__}")
    if ttl < 1:
        raise ValueError(f"ttl must be >= 1, got {ttl}")
    if max_entries < 1:
        raise ValueError(f"max_entries must be >= 1, got {max_entries}")

    # --- Build cache and key generator ---
    # On generated keys a hit is one call into the extension (HitMemo.get), which reads the memo the
    # cache keeps in step under its lock, as on @cache(backend=None) (wrapper.py, _enter_l1_only). The
    # memo holds its callers' arguments in memory; every invalidation of this cache runs in this
    # process and drops what it erases. A key= function builds its key on every call: the memo keys a
    # call by its arguments, and key= can read more than them (a context variable, say).
    # The memo answers only while func.__module__ and func.__qualname__ are the objects read here:
    # the key generator reads them on every call, so renaming the function re-keys it as before.
    # Exact str names only: the key generator formats them, and only an immutable name formats the
    # same way every time it is the same object. __module__ can be any object. A callable without
    # names (a functools.partial, an instance with __call__) gets no memo and decorates as before:
    # with key= it caches, without it the key generator refuses it on the call.
    names = getattr(func, "__module__", None), getattr(func, "__qualname__", None)
    hit_counter = HitCounter()
    memo = HitMemo(func, hit_counter, *names) if key is None and type(names[0]) is str and type(names[1]) is str else None
    hit = None if memo is None else memo.get
    object_cache = ObjectCache(max_entries=max_entries, memo=memo)
    key_gen = CacheKeyGenerator()

    # cache_info() counts calls, not lookups: a hit is a call served without running the function,
    # from the cache or by a concurrent call on the same key it joined; a miss is a call that ran it.
    # The cache counts the hits under its lock and hit_counter the lock-free ones; these count the
    # rest, on the miss path only.
    runs = shared = 0
    counts_lock = threading.Lock()

    def _count(*, ran: bool) -> None:
        nonlocal runs, shared
        with counts_lock:
            if ran:
                runs += 1
            else:
                shared += 1

    def _make_key(args: tuple[Any, ...], kw: dict[str, Any], named: Any = func) -> str:
        if key is not None:
            return key(*args, **kw)
        return key_gen.generate_key(
            func=named,
            args=args,
            kwargs=kw,
            namespace=namespace,
            integrity_checking=False,
            serializer_type="local",
        )

    def _call_key(args: tuple[Any, ...], kw: dict[str, Any]) -> tuple[str, Hashable | None]:
        """A call's cache key, and with a memo the memo key to memoise its entry under, or None.

        A call is memoised only when its key was built with the names the memo answers for. The names
        are read once and the key is built from that read, so a rename racing the call, even one undone
        before it returns, cannot leave an entry stored under other names memoised under these."""
        if memo is None:
            return _make_key(args, kw), None
        module, qualname = func.__module__, func.__qualname__
        cache_key = _make_key(args, kw, _Names(module, qualname))
        if module is not names[0] or qualname is not names[1]:
            return cache_key, None
        return cache_key, _memo_key(args, kw)

    # A miss runs the function once for every concurrent miss on its key (single_flight): every
    # caller of one call gets the same object, as a later hit does. One map is used, by kind.
    flights = AsyncFlights()
    thread_flights = ThreadFlights()

    # A no-args invalidate_cache() on a function that takes parameters means the whole function,
    # as on @cache: its zero-argument key was never cached, and a custom key= could not build it.
    # A callable with no signature (a builtin such as max) counts as taking parameters: clearing
    # every entry can never leave one stale.
    try:
        func_has_params = bool(inspect.signature(func).parameters)
    except (TypeError, ValueError):
        func_has_params = True

    # --- Shared helper functions (defined once, attached to either wrapper) ---
    # Each invalidation also forgets the key's call in flight: a read that starts after it never
    # joins a miss that started before it.

    def invalidate_cache(*args: Any, **kw: Any) -> InvalidationOutcome:
        """Remove the cached entry for these arguments, or every entry when a function that takes
        parameters is called with none. There is no L2: always NO_L2."""
        if not args and not kw and func_has_params:
            cache_clear()
            return InvalidationOutcome.NO_L2
        cache_key = _make_key(args, kw)
        flights.forget([cache_key])
        thread_flights.forget([cache_key])
        object_cache.delete(cache_key)
        return InvalidationOutcome.NO_L2

    async def ainvalidate_cache(*args: Any, **kw: Any) -> InvalidationOutcome:
        """Async variant of invalidate_cache (operation is sync but API is async for consistency)."""
        return invalidate_cache(*args, **kw)

    def cache_clear() -> None:
        """Remove all entries for this function. Works for both sync and async."""
        flights.forget(None)
        thread_flights.forget(None)
        object_cache.clear()

    def cache_info() -> CacheInfo:
        """Return cache statistics as a CacheInfo namedtuple.

        A call that joined a concurrent call on its key counts as a hit when it gets the value,
        and not at all when that call raised; only the call that ran the function counts a miss.
        """
        with counts_lock:
            hits, misses = object_cache.hits + hit_counter.count + shared, runs
        return CacheInfo(
            hits=hits,
            misses=misses,
            l1_hits=hits,
            l2_hits=0,
            maxsize=object_cache.max_entries,
            currsize=object_cache.size,
            l2_avg_latency_ms=0.0,
            last_operation_at=None,
            session_id=None,
        )

    # --- Build sync or async wrapper ---

    if inspect.iscoroutinefunction(func):

        async def _fill_async(cache_key: str, memo_key: Hashable | None, args: tuple[Any, ...], kw: dict[str, Any]) -> Any:
            _count(ran=True)
            epoch = object_cache.invalidation_epoch  # before the call: see ObjectCache, "Invalidation epoch"
            result = await func(*args, **kw)
            object_cache.put(cache_key, result, ttl, memo_key=memo_key, epoch=epoch)
            return result

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kw: Any) -> Any:
            # A memoised hit is one call into the extension: no key, no lock (ObjectCache, "Lock-free hits").
            if hit is not None:
                value = hit(args, kw, _MISS)
                if value is not _MISS:
                    return value
            cache_key, memo_key = _call_key(args, kw)
            found, cached_value = object_cache.get(cache_key, memo_key)
            if found:
                return cached_value
            result, joined = await flights.run((cache_key,), functools.partial(_fill_async, cache_key, memo_key, args, kw))
            if joined:
                _count(ran=False)
            return result

        wrapper: Any = async_wrapper
    else:

        def _fill(cache_key: str, memo_key: Hashable | None, args: tuple[Any, ...], kw: dict[str, Any]) -> Any:
            _count(ran=True)
            epoch = object_cache.invalidation_epoch  # before the call: see ObjectCache, "Invalidation epoch"
            result = func(*args, **kw)
            object_cache.put(cache_key, result, ttl, memo_key=memo_key, epoch=epoch)
            return result

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kw: Any) -> Any:
            # As in async_wrapper.
            if hit is not None:
                value = hit(args, kw, _MISS)
                if value is not _MISS:
                    return value
            cache_key, memo_key = _call_key(args, kw)
            found, cached_value = object_cache.get(cache_key, memo_key)
            if found:
                return cached_value
            result, joined = thread_flights.run(
                (cache_key,),
                functools.partial(_fill, cache_key, memo_key, args, kw),
                functools.partial(object_cache.peek, cache_key),
            )
            if joined:
                _count(ran=False)
            return result

        wrapper = sync_wrapper

    # --- Attach API methods ---
    wrapper.invalidate_cache = invalidate_cache  # type: ignore[attr-defined]
    wrapper.ainvalidate_cache = ainvalidate_cache  # type: ignore[attr-defined]
    wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
    wrapper.cache_info = cache_info  # type: ignore[attr-defined]
    wrapper.__wrapped__ = func  # type: ignore[attr-defined]
    return wrapper
