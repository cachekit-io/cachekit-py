"""HTTP client factory with connection pooling and thread-local caching."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from cachekit.backends.cachekitio.config import CachekitIOBackendConfig

# Global pool instances (shared across threads)
_async_client_instance: httpx.AsyncClient | None = None
_client_lock = threading.Lock()

# Thread-local client cache for performance (both sync and async)
_thread_local = threading.local()


def get_async_http_client(config: CachekitIOBackendConfig) -> httpx.AsyncClient:
    """Get asynchronous HTTP client with connection pooling.

    Creates global singleton async client with connection pooling and keepalive.

    Args:
        config: cachekit.io backend configuration

    Returns:
        httpx.AsyncClient: Async HTTP client with connection pooling
    """
    global _async_client_instance

    if _async_client_instance is None:
        with _client_lock:
            if _async_client_instance is None:
                # Try to enable HTTP/3 if aioquic is installed
                client_kwargs = {
                    "base_url": config.api_url,
                    "timeout": config.timeout,
                    "http2": True,  # Enable HTTP/2
                    "limits": httpx.Limits(
                        max_connections=config.connection_pool_size,
                        max_keepalive_connections=config.connection_pool_size,
                    ),
                    "headers": {
                        "Authorization": f"Bearer {config.api_key.get_secret_value()}",
                        "Content-Type": "application/octet-stream",
                    },
                }

                # Try HTTP/3 (requires aioquic dependency)
                try:
                    import aioquic  # noqa: F401  # type: ignore[import-untyped,import-not-found]

                    # HTTP/3 is available via httpx[http3] - need custom transport
                    # For now, just use HTTP/2 (HTTP/3 support requires more setup)
                    # client_kwargs["http3"] = True
                except ImportError:
                    pass  # HTTP/3 not available, fall back to HTTP/2

                _async_client_instance = httpx.AsyncClient(**client_kwargs)

    return _async_client_instance


def get_cached_async_http_client(config: CachekitIOBackendConfig) -> httpx.AsyncClient:
    """Get thread-local async HTTP client instance.

    Creates a NEW async client instance per thread for thread safety.
    Each thread gets its own connection pool.

    Args:
        config: cachekit.io backend configuration

    Returns:
        httpx.AsyncClient: Thread-local async HTTP client instance
    """
    if not hasattr(_thread_local, "async_client") or _thread_local.async_client is None:
        # Create NEW async client per thread (not shared reference)
        client_kwargs = {
            "base_url": config.api_url,
            "timeout": config.timeout,
            "http2": True,  # Enable HTTP/2
            "limits": httpx.Limits(
                max_connections=config.connection_pool_size,
                max_keepalive_connections=config.connection_pool_size,
            ),
            "headers": {
                "Authorization": f"Bearer {config.api_key.get_secret_value()}",
                "Content-Type": "application/octet-stream",
            },
        }

        # Try HTTP/3 (requires aioquic dependency)
        try:
            import aioquic  # noqa: F401  # type: ignore[import-untyped,import-not-found]

            # HTTP/3 is available via httpx[http3] - need custom transport
            # For now, just use HTTP/2 (HTTP/3 support requires more setup)
            # client_kwargs["http3"] = True
        except ImportError:
            pass  # HTTP/3 not available, fall back to HTTP/2

        _thread_local.async_client = httpx.AsyncClient(**client_kwargs)
    return _thread_local.async_client


def get_sync_http_client(config: CachekitIOBackendConfig) -> httpx.Client:
    """Get synchronous HTTP client with connection pooling (per-thread).

    Creates a NEW sync client per thread for thread safety.
    Each thread gets its own connection pool.

    Args:
        config: cachekit.io backend configuration

    Returns:
        httpx.Client: Thread-local sync HTTP client instance
    """
    if not hasattr(_thread_local, "sync_client") or _thread_local.sync_client is None:
        # Create NEW sync client per thread (thread-safe, no event loop required)
        _thread_local.sync_client = httpx.Client(
            base_url=config.api_url,
            timeout=config.timeout,
            http2=True,  # Enable HTTP/2
            limits=httpx.Limits(
                max_connections=config.connection_pool_size,
                max_keepalive_connections=config.connection_pool_size,
            ),
            headers={
                "Authorization": f"Bearer {config.api_key.get_secret_value()}",
                "Content-Type": "application/octet-stream",
            },
        )
    return _thread_local.sync_client


async def close_async_client() -> None:
    """Close async client instance (useful for cleanup)."""
    global _async_client_instance
    if _async_client_instance is not None:
        await _async_client_instance.aclose()
        _async_client_instance = None

    # Close thread-local client
    if hasattr(_thread_local, "async_client") and _thread_local.async_client is not None:
        await _thread_local.async_client.aclose()
        _thread_local.async_client = None


def close_sync_client() -> None:
    """Close sync client instance (useful for cleanup)."""
    # Close thread-local sync client
    if hasattr(_thread_local, "sync_client") and _thread_local.sync_client is not None:
        _thread_local.sync_client.close()
        _thread_local.sync_client = None


def reset_global_client() -> None:
    """Reset global client instance (useful for testing).

    Note: This does not properly close clients. Use close_*_client() for proper cleanup.
    """
    global _async_client_instance
    with _client_lock:
        _async_client_instance = None

    # Reset thread-local caches
    if hasattr(_thread_local, "async_client"):
        _thread_local.async_client = None
    if hasattr(_thread_local, "sync_client"):
        _thread_local.sync_client = None


__all__ = [
    "get_async_http_client",
    "get_cached_async_http_client",
    "get_sync_http_client",
    "close_async_client",
    "close_sync_client",
    "reset_global_client",
]
