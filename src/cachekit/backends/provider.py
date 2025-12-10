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


class DefaultBackendProvider(BackendProviderInterface):
    """Default backend provider using Redis backend.

    Creates RedisBackendProvider singleton with connection pooling.
    Delegates to RedisBackendProvider.get_backend() for per-request wrappers.

    For single-tenant deployments (default), sets tenant_context to "default".
    For multi-tenant deployments, tenant_context must be set externally.
    """

    def __init__(self):
        self._provider = None

    def get_backend(self):
        """Get per-request backend instance from singleton provider."""
        if self._provider is None:
            from cachekit.backends.redis.config import RedisBackendConfig
            from cachekit.backends.redis.provider import RedisBackendProvider, tenant_context

            redis_config = RedisBackendConfig.from_env()
            self._provider = RedisBackendProvider(redis_url=redis_config.redis_url)

            # Set default tenant for single-tenant mode (if not already set)
            if tenant_context.get() is None:
                tenant_context.set("default")

        return self._provider.get_backend()


__all__ = [
    "CacheClientProvider",
    "LoggerProvider",
    "SimpleLogger",
    "DefaultLoggerProvider",
    "BackendProviderInterface",
    "DefaultCacheClientProvider",
    "DefaultBackendProvider",
]
