"""Backend provider interfaces and default implementations.

This module defines abstract interfaces for backend providers and concrete
implementations for dependency injection. Follows protocol-based design
for maximum flexibility and testing capability.
"""

from __future__ import annotations

import logging
import os

from cachekit.config.validation import ConfigurationError

logger = logging.getLogger(__name__)


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
    """Default backend provider with environment-based auto-detection.

    Resolves the cache backend from CACHEKIT_-prefixed environment variables.
    Raises ConfigurationError if multiple CACHEKIT_-prefixed backend variables
    are set simultaneously (ambiguous configuration).

    Priority (first match wins):
        1. CACHEKIT_API_KEY         → CachekitIOBackend
        2. CACHEKIT_REDIS_URL       → RedisBackend
        3. CACHEKIT_MEMCACHED_SERVERS → MemcachedBackend
        4. CACHEKIT_FILE_CACHE_DIR  → FileBackend
        5. REDIS_URL (no prefix)    → RedisBackend (12-factor fallback)
        6. Nothing set              → None (L1-only)

    Conflict rules:
        - Multiple CACHEKIT_-prefixed backend vars → ConfigurationError
        - Non-prefixed REDIS_URL never conflicts (different intent signal)

    Unchanged behavior:
        - Explicit backend= parameter always takes precedence
        - set_default_backend() always takes precedence
    """

    # Environment variables that signal a specific CACHEKIT backend
    _CACHEKIT_BACKEND_VARS: dict[str, str] = {
        "CACHEKIT_API_KEY": "CachekitIO",
        "CACHEKIT_REDIS_URL": "Redis",
        "CACHEKIT_MEMCACHED_SERVERS": "Memcached",
        "CACHEKIT_FILE_CACHE_DIR": "File",
    }

    def __init__(self):
        self._provider = None
        self._resolved = False

    def get_backend(self):
        """Get backend instance via environment auto-detection.

        Returns:
            Backend instance, or None if no backend is configured (L1-only).

        Raises:
            ConfigurationError: If multiple CACHEKIT_-prefixed backend variables are set.
        """
        if not self._resolved:
            self._provider = self._resolve_provider()
            self._resolved = True

        if self._provider is None:
            return None
        return self._provider.get_backend()

    def _resolve_provider(self):
        """Auto-detect backend provider from environment variables.

        Returns:
            Backend provider instance, or None for L1-only.

        Raises:
            ConfigurationError: If multiple CACHEKIT_-prefixed backend variables are set.
        """
        # Detect all CACHEKIT_-prefixed backend signals
        detected = {var: label for var, label in self._CACHEKIT_BACKEND_VARS.items() if os.environ.get(var)}

        # Conflict: 2+ CACHEKIT_-prefixed backend vars is ambiguous
        if len(detected) > 1:
            vars_str = ", ".join(f"{var} ({label})" for var, label in sorted(detected.items()))
            raise ConfigurationError(
                f"Ambiguous backend configuration: multiple CACHEKIT_ backend variables set: {vars_str}\n\n"
                "Set exactly one CACHEKIT_ backend variable, or use explicit backend= parameter."
            )

        # Single CACHEKIT_-prefixed var detected
        if detected:
            var = next(iter(detected))
            return self._create_provider(var)

        # Fallback: non-prefixed REDIS_URL (12-factor convention, never conflicts)
        if os.environ.get("REDIS_URL"):
            return self._create_redis_provider()

        # Nothing configured → L1-only
        logger.debug("No backend environment variables detected — L1-only mode")
        return None

    def _create_provider(self, env_var: str):
        """Create the appropriate backend provider for the given env var."""
        if env_var == "CACHEKIT_API_KEY":
            return self._create_cachekitio_provider()
        if env_var == "CACHEKIT_REDIS_URL":
            return self._create_redis_provider()
        if env_var == "CACHEKIT_MEMCACHED_SERVERS":
            return self._create_memcached_provider()
        if env_var == "CACHEKIT_FILE_CACHE_DIR":
            return self._create_file_provider()
        raise ConfigurationError(f"Unknown backend env var: {env_var}")  # pragma: no cover

    def _create_cachekitio_provider(self):
        """Create CachekitIO backend (wraps in a simple provider)."""
        from cachekit.backends.cachekitio import CachekitIOBackend

        backend = CachekitIOBackend()
        return _StaticBackendProvider(backend)

    def _create_redis_provider(self):
        """Create Redis backend provider with tenant context."""
        from cachekit.backends.redis.config import RedisBackendConfig
        from cachekit.backends.redis.provider import RedisBackendProvider, tenant_context

        redis_config = RedisBackendConfig.from_env()
        provider = RedisBackendProvider(redis_url=redis_config.redis_url)

        # Set default tenant for single-tenant mode (if not already set)
        if tenant_context.get() is None:
            tenant_context.set("default")

        return provider

    def _create_memcached_provider(self):
        """Create Memcached backend."""
        from cachekit.backends.memcached import MemcachedBackend
        from cachekit.backends.memcached.config import MemcachedBackendConfig

        config = MemcachedBackendConfig.from_env()
        backend = MemcachedBackend(config)
        return _StaticBackendProvider(backend)

    def _create_file_provider(self):
        """Create File backend."""
        from cachekit.backends.file import FileBackend
        from cachekit.backends.file.config import FileBackendConfig

        config = FileBackendConfig.from_env()
        backend = FileBackend(config)
        return _StaticBackendProvider(backend)


class _StaticBackendProvider:
    """Wraps a pre-created backend instance as a provider.

    Used for backends that don't have their own provider class
    (CachekitIO, Memcached, File). Unlike RedisBackendProvider which
    creates per-request wrappers, these backends are stateless enough
    to share a single instance.
    """

    def __init__(self, backend):
        self._backend = backend

    def get_backend(self):
        return self._backend


__all__ = [
    "CacheClientProvider",
    "LoggerProvider",
    "SimpleLogger",
    "DefaultLoggerProvider",
    "BackendProviderInterface",
    "DefaultCacheClientProvider",
    "DefaultBackendProvider",
]
