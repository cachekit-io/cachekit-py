"""Bridge between @cache.local() decorator and ObjectCache.

Handles sync/async detection, key generation, parameter validation,
and attaches the standard wrapper API (invalidate_cache, cache_clear, cache_info).
"""

from __future__ import annotations

import asyncio
import functools
import threading
from collections.abc import Callable
from typing import Any

from cachekit.decorators.single_flight import AsyncFlights, ThreadFlights
from cachekit.decorators.wrapper import CacheInfo
from cachekit.key_generator import CacheKeyGenerator
from cachekit.object_cache import ObjectCache

_ALLOWED_PARAMS: frozenset[str] = frozenset({"ttl", "max_entries", "namespace", "key"})


def create_local_wrapper(
    func: Callable[..., Any],
    **kwargs: Any,
) -> Callable[..., Any]:
    """Create a locally-cached wrapper for *func* using ObjectCache.

    Validation and ObjectCache construction happen at decoration time.
    The sync or async wrapper is chosen based on ``asyncio.iscoroutinefunction(func)``.

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
    object_cache = ObjectCache(max_entries=max_entries)
    key_gen = CacheKeyGenerator()

    # cache_info() counts calls, not lookups: a hit is a call served without running the function,
    # from the cache or by a concurrent call on the same key it joined; a miss is a call that ran it.
    # The cache counts the plain hits; these count the rest, on the miss path only.
    runs = shared = 0
    counts_lock = threading.Lock()

    def _count(*, ran: bool) -> None:
        nonlocal runs, shared
        with counts_lock:
            if ran:
                runs += 1
            else:
                shared += 1

    def _make_key(args: tuple[Any, ...], kw: dict[str, Any]) -> str:
        if key is not None:
            return key(*args, **kw)
        return key_gen.generate_key(
            func=func,
            args=args,
            kwargs=kw,
            namespace=namespace,
            integrity_checking=False,
            serializer_type="local",
        )

    # --- Shared helper functions (defined once, attached to either wrapper) ---

    def invalidate_cache(*args: Any, **kw: Any) -> None:
        """Remove a specific cached entry by regenerating its key."""
        object_cache.delete(_make_key(args, kw))

    async def ainvalidate_cache(*args: Any, **kw: Any) -> None:
        """Async variant of invalidate_cache (operation is sync but API is async for consistency)."""
        object_cache.delete(_make_key(args, kw))

    def cache_clear() -> None:
        """Remove all entries for this function. Works for both sync and async."""
        object_cache.clear()

    def cache_info() -> CacheInfo:
        """Return cache statistics as a CacheInfo namedtuple.

        A call that joined a concurrent call on its key counts as a hit when it gets the value,
        and not at all when that call raised; only the call that ran the function counts a miss.
        """
        with counts_lock:
            hits, misses = object_cache.hits + shared, runs
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
    # A miss runs the function once for every concurrent miss on its key (single_flight): every
    # caller of one call gets the same object, as a later hit does.

    if asyncio.iscoroutinefunction(func):
        flights = AsyncFlights()

        async def _fill_async(cache_key: str, args: tuple[Any, ...], kw: dict[str, Any]) -> Any:
            _count(ran=True)
            result = await func(*args, **kw)
            object_cache.put(cache_key, result, ttl)
            return result

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kw: Any) -> Any:
            cache_key = _make_key(args, kw)
            found, cached_value = object_cache.get(cache_key)
            if found:
                return cached_value
            result, joined = await flights.run(cache_key, functools.partial(_fill_async, cache_key, args, kw))
            if joined:
                _count(ran=False)
            return result

        wrapper: Any = async_wrapper
    else:
        thread_flights = ThreadFlights()

        def _fill(cache_key: str, args: tuple[Any, ...], kw: dict[str, Any]) -> Any:
            _count(ran=True)
            result = func(*args, **kw)
            object_cache.put(cache_key, result, ttl)
            return result

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kw: Any) -> Any:
            cache_key = _make_key(args, kw)
            found, cached_value = object_cache.get(cache_key)
            if found:
                return cached_value
            result, joined = thread_flights.run(
                cache_key,
                functools.partial(_fill, cache_key, args, kw),
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
