"""Backend provider interfaces and default implementations.

This module defines abstract interfaces for backend providers and concrete
implementations for dependency injection. Follows protocol-based design
for maximum flexibility and testing capability.
"""


class CacheClientProvider:
    """Abstract interface for Redis client providers."""

    def get_sync_client(self):
        """Get synchronous Redis client."""
        raise NotImplementedError

    async def get_async_client(self):
        """Get asynchronous Redis client."""
        raise NotImplementedError


class LoggerProvider:
    """Abstract interface for logger providers."""

    def get_logger(self, name: str):
        """Get logger instance."""
        raise NotImplementedError


class SimpleLogger:
    """Simple logger wrapper that adds cache-specific logging methods."""

    def __init__(self, logger):
        self._logger = logger

    def debug(self, message: str):
        """Log a debug message."""
        self._logger.debug(message)

    def info(self, message: str, extra=None):
        """Log an info message with optional structured data."""
        self._logger.info(message)

    def warning(self, message: str):
        """Log a warning message."""
        self._logger.warning(message)

    def error(self, message: str):
        """Log an error message."""
        self._logger.error(message)

    def cache_hit(self, key: str, source: str = "Redis"):
        """Log cache hits."""
        self._logger.debug(f"{source} cache hit for key: {key}")

    def cache_miss(self, key: str):
        """Log cache misses."""
        self._logger.debug(f"Cache miss for key: {key}")

    def cache_stored(self, key: str, ttl=None):
        """Log cache storage operations."""
        ttl_info = f" with TTL {ttl}" if ttl else ""
        self._logger.debug(f"Cached result for key: {key}{ttl_info}")

    def cache_invalidated(self, key: str, source: str = "Redis"):
        """Log cache invalidation."""
        self._logger.debug(f"Invalidated {source} cache for key: {key}")


class DefaultLoggerProvider(LoggerProvider):
    """Default logger provider using standard Python logging."""

    def get_logger(self, name: str):
        import logging

        return SimpleLogger(logging.getLogger(name))


class BackendProviderInterface:
    """Abstract interface for backend providers."""

    def get_backend(self):
        """Get backend instance."""
        raise NotImplementedError


class DefaultCacheClientProvider(CacheClientProvider):
    """Default Redis client provider with thread-local caching for performance.

    Uses get_cached_redis_client() to provide thread affinity optimization,
    eliminating ~1-2ms overhead per request in high-frequency scenarios.
    """

    def get_sync_client(self):
        from cachekit.backends.redis.client import get_cached_redis_client

        return get_cached_redis_client()

    async def get_async_client(self):
        from cachekit.backends.redis.client import get_cached_async_redis_client

        return await get_cached_async_redis_client()


class PooledClientProvider(CacheClientProvider):
    """Redis client provider with per-instance pools bound to an explicit URL.

    Unlike DefaultCacheClientProvider (process-global pool configured from the
    environment), each instance owns its own connection pools built from the
    URL it was given — the URL a caller passes is the URL that gets used.
    This is what makes RedisBackend(redis_url=...) honour its argument instead
    of silently connecting wherever CACHEKIT_REDIS_URL/REDIS_URL point (#222).

    No connection is made at construction time; pools connect lazily on first use.
    """

    def __init__(self, redis_url: str, config=None):
        from cachekit.backends.redis.client import create_connection_pool
        from cachekit.backends.redis.config import RedisBackendConfig

        self._redis_url = redis_url
        self._config = config or RedisBackendConfig.from_env()
        self._pool = create_connection_pool(redis_url, self._config)
        self._async_pool = None

    def get_sync_client(self):
        import redis

        return redis.Redis(connection_pool=self._pool)

    async def get_async_client(self):
        import redis.asyncio as redis_async

        from cachekit.backends.redis.client import create_async_connection_pool

        if self._async_pool is None:
            # Created lazily inside a running loop. No await between the check
            # and the assignment, so single-loop use is race-free.
            self._async_pool = create_async_connection_pool(self._redis_url, self._config)
        return redis_async.Redis(connection_pool=self._async_pool)


class DefaultBackendProvider(BackendProviderInterface):
    """Default backend provider with env-based auto-detection.

    Selection is by a single, unambiguous environment signal. Priority order:
        1. CACHEKIT_API_KEY            → CachekitIOBackend (SaaS)
        2. CACHEKIT_REDIS_URL          → RedisBackend
        3. CACHEKIT_MEMCACHED_SERVERS  → MemcachedBackend
        4. CACHEKIT_FILE_CACHE_DIR     → FileBackend
        5. REDIS_URL, or nothing set   → RedisBackend (12-factor / localhost default)

    Setting more than one of the four prefixed selectors (1-4) raises
    ``ConfigurationError`` — auto-detection must be unambiguous; pass
    ``backend=`` explicitly to override. The non-prefixed ``REDIS_URL`` is only a
    fallback and never counts as a conflict (12-factor convention).

    CachekitIO/Memcached/File backends are stateless singletons (cached). Redis
    backends are per-request tenant-scoped wrappers (not cached —
    RedisBackendProvider.get_backend() reads the tenant_context ContextVar). For
    single-tenant deployments (default), tenant_context is set to "default".
    """

    # Prefixed selectors in priority order. REDIS_URL is the implicit fallback
    # and intentionally excluded so it never triggers a conflict.
    _SELECTORS = (
        ("CACHEKIT_API_KEY", "cachekitio"),
        ("CACHEKIT_REDIS_URL", "redis"),
        ("CACHEKIT_MEMCACHED_SERVERS", "memcached"),
        ("CACHEKIT_FILE_CACHE_DIR", "file"),
    )

    def __init__(self):
        self._cachekitio_backend = None
        self._redis_provider = None
        self._memcached_backend = None
        self._file_backend = None

    def _detect(self):
        """Return the chosen backend key, or None to use the Redis fallback.

        Raises ConfigurationError if more than one prefixed selector is set.
        """
        import os

        matched = [(env_var, key) for env_var, key in self._SELECTORS if os.environ.get(env_var)]
        if len(matched) > 1:
            from cachekit.config.validation import ConfigurationError

            names = ", ".join(env_var for env_var, _ in matched)
            raise ConfigurationError(
                f"Ambiguous backend auto-detection: multiple selectors set ({names}). "
                "Set exactly one of CACHEKIT_API_KEY / CACHEKIT_REDIS_URL / "
                "CACHEKIT_MEMCACHED_SERVERS / CACHEKIT_FILE_CACHE_DIR, or pass backend= explicitly."
            )
        return matched[0][1] if matched else None

    def get_backend(self):
        """Get backend instance, auto-detected from environment on first call."""
        choice = self._detect()

        if choice == "cachekitio":
            if self._cachekitio_backend is None:
                from cachekit.backends.cachekitio import CachekitIOBackend

                self._cachekitio_backend = CachekitIOBackend()
            return self._cachekitio_backend

        if choice == "memcached":
            if self._memcached_backend is None:
                from cachekit.backends.memcached import MemcachedBackend, MemcachedBackendConfig

                self._memcached_backend = MemcachedBackend(MemcachedBackendConfig.from_env())
            return self._memcached_backend

        if choice == "file":
            if self._file_backend is None:
                from cachekit.backends.file import FileBackend, FileBackendConfig

                self._file_backend = FileBackend(FileBackendConfig.from_env())
            return self._file_backend

        # choice == "redis" (explicit CACHEKIT_REDIS_URL) or None (REDIS_URL / localhost fallback).
        # Tenant-scoped: call the provider each time so it re-reads tenant_context.
        if self._redis_provider is None:
            from cachekit.backends.redis.config import RedisBackendConfig
            from cachekit.backends.redis.provider import RedisBackendProvider, tenant_context

            redis_config = RedisBackendConfig.from_env()
            self._redis_provider = RedisBackendProvider(redis_url=redis_config.redis_url)

            # Set default tenant for single-tenant mode (if not already set)
            if tenant_context.get() is None:
                tenant_context.set("default")

        return self._redis_provider.get_backend()


__all__ = [
    "CacheClientProvider",
    "LoggerProvider",
    "SimpleLogger",
    "DefaultLoggerProvider",
    "BackendProviderInterface",
    "DefaultCacheClientProvider",
    "DefaultBackendProvider",
    "PooledClientProvider",
]
