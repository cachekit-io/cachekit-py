"""Bridge between @cache.local() decorator and ObjectCache.

Handles sync/async detection, key generation, parameter validation,
and attaches the standard wrapper API (invalidate_cache, cache_clear, cache_info).
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable

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
        """Return cache statistics as a CacheInfo namedtuple."""
        return CacheInfo(
            hits=object_cache.hits,
            misses=object_cache.misses,
            l1_hits=object_cache.hits,
            l2_hits=0,
            maxsize=object_cache.max_entries,
            currsize=object_cache.size,
            l2_avg_latency_ms=0.0,
            last_operation_at=None,
            session_id=None,
        )

    # --- Build sync or async wrapper ---

    if asyncio.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kw: Any) -> Any:
            cache_key = _make_key(args, kw)
            found, cached_value = object_cache.get(cache_key)
            if found:
                return cached_value
            result = await func(*args, **kw)
            object_cache.put(cache_key, result, ttl)
            return result

        wrapper: Any = async_wrapper
    else:

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kw: Any) -> Any:
            cache_key = _make_key(args, kw)
            found, cached_value = object_cache.get(cache_key)
            if found:
                return cached_value
            result = func(*args, **kw)
            object_cache.put(cache_key, result, ttl)
            return result

        wrapper = sync_wrapper

    # --- Attach API methods ---
    wrapper.invalidate_cache = invalidate_cache  # type: ignore[attr-defined]
    wrapper.ainvalidate_cache = ainvalidate_cache  # type: ignore[attr-defined]
    wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
    wrapper.cache_info = cache_info  # type: ignore[attr-defined]
    wrapper.__wrapped__ = func  # type: ignore[attr-defined]
    return wrapper
