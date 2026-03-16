"""cachekit.io backend implementation for cachekit."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import httpx

from cachekit.backends.cachekitio.client import get_cached_async_http_client, get_sync_http_client
from cachekit.backends.cachekitio.config import CachekitIOBackendConfig
from cachekit.backends.cachekitio.error_handler import classify_http_error
from cachekit.backends.errors import BackendError
from cachekit.decorators.stats_context import get_current_function_stats
from cachekit.logging import get_structured_logger

if TYPE_CHECKING:
    from cachekit.decorators.wrapper import _FunctionStats

# Module-level logger
_logger = get_structured_logger(__name__)


def _inject_metrics_headers(stats: _FunctionStats | None) -> dict[str, str]:
    """Extract cache metrics and format as HTTP headers.

    Extracts L1 hits, L2 hits, and misses from the provided function statistics,
    calculates the L1 hit rate with zero-division protection, and merges session
    headers for complete request tracing. Also injects L1 status for rate limit
    classification.

    Args:
        stats: Function statistics tracker, or None for graceful degradation.

    Returns:
        dict[str, str]: Headers dictionary containing:
            - X-CacheKit-Session-ID: Process-scoped session identifier
            - X-CacheKit-L1-Hits: Count of L1 cache hits
            - X-CacheKit-L2-Hits: Count of L2 cache hits
            - X-CacheKit-Misses: Count of cache misses
            - X-CacheKit-L1-Hit-Rate: L1 hit rate (0.000 to 1.000)
            - X-CacheKit-L1-Status: Rate limit classification ("hit", "miss", or "disabled")
            - X-CacheKit-Session-Start: Process start timestamp (ms)

    Behavior on None stats:
        Returns empty dict to prevent downstream errors. This allows graceful
        degradation when statistics are not available.

    Examples:
        >>> from cachekit.decorators.wrapper import _FunctionStats
        >>> stats = _FunctionStats(function_identifier="test.module.func", l1_enabled=True)
        >>> stats.record_l1_hit()
        >>> stats.record_l1_hit()
        >>> stats.record_l2_hit(2.5)
        >>> headers = _inject_metrics_headers(stats)
        >>> headers["X-CacheKit-L1-Hits"]
        '2'
        >>> headers["X-CacheKit-L2-Hits"]
        '1'
        >>> float(headers["X-CacheKit-L1-Hit-Rate"])
        0.667
        >>> headers["X-CacheKit-L1-Status"]
        'miss'

        >>> # Zero-division protection: 0 total hits = 0.000 rate
        >>> empty_stats = _FunctionStats(function_identifier="test.module.empty", l1_enabled=True)
        >>> headers = _inject_metrics_headers(empty_stats)
        >>> headers["X-CacheKit-L1-Hit-Rate"]
        '0.000'
        >>> headers["X-CacheKit-L1-Status"]
        'miss'

        >>> # L1 disabled
        >>> disabled_stats = _FunctionStats(function_identifier="test.module.disabled", l1_enabled=False)
        >>> headers = _inject_metrics_headers(disabled_stats)
        >>> headers["X-CacheKit-L1-Status"]
        'disabled'

        >>> # Graceful degradation with None
        >>> headers = _inject_metrics_headers(None)
        >>> headers
        {}
    """
    # Graceful degradation: return empty dict if stats is None
    if stats is None:
        return {}

    # Extract metrics from stats
    info = stats.get_info()
    l1_hits = info.l1_hits
    l2_hits = info.l2_hits
    misses = info.misses

    # Calculate L1 hit rate with zero-division guard
    total_hits = l1_hits + l2_hits
    if total_hits > 0:
        l1_hit_rate = l1_hits / total_hits
    else:
        l1_hit_rate = 0.0

    # Format L1 hit rate to 3 decimal places
    l1_hit_rate_str = f"{l1_hit_rate:.3f}"

    # Determine L1 status for rate limit classification
    # Conservative approach: report "miss" for enabled (counts as backend op)
    # or "disabled" if L1 is not enabled
    if stats.l1_enabled:
        l1_status = "miss"  # Conservative: treat all as backend ops
    else:
        l1_status = "disabled"

    # Get session headers with exception safety
    try:
        from cachekit.backends.cachekitio.session import get_session_start_ms

        if info.session_id:
            # Use function-specific session ID from info (regenerated after cache_clear)
            session_headers = {
                "X-CacheKit-Session-ID": info.session_id,
                "X-CacheKit-Session-Start": str(get_session_start_ms()),
            }
        else:
            # Fallback to process-level session (backward compatibility)
            from cachekit.backends.cachekitio.session import get_session_headers

            session_headers = get_session_headers()
    except Exception as e:
        # Session header generation failed - continue without session headers
        # This ensures backend requests never fail due to session tracking issues
        _logger.debug(f"Session header generation failed: {e}")
        session_headers = {}

    # Build metrics headers
    metrics_headers = {
        "X-CacheKit-L1-Hits": str(l1_hits),
        "X-CacheKit-L2-Hits": str(l2_hits),
        "X-CacheKit-Misses": str(misses),
        "X-CacheKit-L1-Hit-Rate": l1_hit_rate_str,
        "X-CacheKit-L1-Status": l1_status,
    }

    # Merge and return
    return {**session_headers, **metrics_headers}


class CachekitIOBackend:
    """Distributed cache backend via Cloudflare Workers.

    Implements BaseBackend protocol with proper error handling,
    connection pooling, and circuit breaker integration.

    Example:
        >>> from cachekit import cache
        >>> from cachekit.backends.cachekitio import CachekitIOBackend
        >>> # Load from env: CACHEKIT_API_KEY=ck_live_...
        >>> # Usage:
        >>> # @cache(backend=CachekitIOBackend())
        >>> # def expensive_function(x):
        >>> #     return x * 2
        >>> CachekitIOBackend.__name__
        'CachekitIOBackend'
    """

    def __init__(
        self,
        api_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Initialize cachekit.io backend.

        Args:
            api_url: Override API endpoint URL
            api_key: Override API key (ck_live_...)
            timeout: Override request timeout

        If all are None, loads from environment via pydantic-settings.
        """
        if all(x is None for x in [api_url, api_key, timeout]):
            # Load from environment
            self._config = CachekitIOBackendConfig.from_env()  # type: ignore[call-arg]
        else:
            # Use provided values
            if api_url is None or api_key is None:
                raise ValueError("Both api_url and api_key required if using manual config")
            self._config = CachekitIOBackendConfig(
                api_url=api_url,
                api_key=api_key,  # type: ignore[arg-type]
                timeout=timeout or 5.0,
            )

        # Get HTTP clients (hybrid sync/async architecture)
        # Sync client: per-thread, thread-safe, no event loop required
        # Async client: per-thread, event loop safe
        self._sync_client = get_sync_http_client(self._config)
        self._async_client = get_cached_async_http_client(self._config)

    def _request_sync(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make sync HTTP request with error handling and metrics injection.

        Args:
            method: HTTP method (GET, HEAD, PUT, DELETE, POST, PATCH)
            endpoint: API endpoint (relative to base_url/v1/cache/)
            **kwargs: Additional request arguments

        Returns:
            httpx.Response: HTTP response

        Raises:
            BackendError: Classified error for circuit breaker

        Notes:
            Automatically injects cache metrics headers (L1/L2 hits, session ID) when
            called from within a @cache decorated function. If no stats available in
            context, headers are not injected (backward compatible).
        """
        # Inject metrics headers if stats available in context
        stats = get_current_function_stats()

        if stats is not None:
            metrics_headers = _inject_metrics_headers(stats)
            # Merge with existing headers
            if "headers" in kwargs:
                kwargs["headers"] = {**kwargs["headers"], **metrics_headers}
            else:
                kwargs["headers"] = metrics_headers

        url = f"/v1/cache/{endpoint}"
        try:
            response = self._sync_client.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            raise classify_http_error(
                exc,
                response=exc.response,
                operation=method.lower(),
            ) from exc
        except Exception as exc:
            raise classify_http_error(
                exc,
                operation=method.lower(),
            ) from exc

    async def _request_async(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make async HTTP request with error handling and metrics injection.

        Args:
            method: HTTP method (GET, HEAD, PUT, DELETE, POST, PATCH)
            endpoint: API endpoint (relative to base_url/v1/cache/)
            **kwargs: Additional request arguments

        Returns:
            httpx.Response: HTTP response

        Raises:
            BackendError: Classified error for circuit breaker

        Notes:
            Automatically injects cache metrics headers (L1/L2 hits, session ID) when
            called from within a @cache decorated function. If no stats available in
            context, headers are not injected (backward compatible).
        """
        # Inject metrics headers if stats available in context
        stats = get_current_function_stats()

        if stats is not None:
            metrics_headers = _inject_metrics_headers(stats)
            # Merge with existing headers
            if "headers" in kwargs:
                kwargs["headers"] = {**kwargs["headers"], **metrics_headers}
            else:
                kwargs["headers"] = metrics_headers

        url = f"/v1/cache/{endpoint}"
        try:
            response = await self._async_client.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            raise classify_http_error(
                exc,
                response=exc.response,
                operation=method.lower(),
            ) from exc
        except Exception as exc:
            raise classify_http_error(
                exc,
                operation=method.lower(),
            ) from exc

    # ==================== BaseBackend Protocol (Sync) ====================
    # These sync methods use sync httpx.Client (thread-safe, no event loop required)

    def get(self, key: str) -> bytes | None:
        """Retrieve value from cache (sync).

        Args:
            key: Cache key

        Returns:
            Cached bytes value or None if not found

        Raises:
            BackendError: If operation fails (network, auth, etc.)
        """
        try:
            response = self._request_sync("GET", key)
            return response.content
        except BackendError as exc:
            # 404 is not an error (cache miss)
            if exc.original_exception and isinstance(exc.original_exception, httpx.HTTPStatusError):
                if exc.original_exception.response.status_code == 404:
                    return None
            raise

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        """Store value in cache (sync).

        Args:
            key: Cache key
            value: Bytes to cache
            ttl: Time-to-live in seconds (optional)

        Raises:
            BackendError: If operation fails
        """
        headers = {}
        if ttl is not None:
            headers["X-TTL"] = str(ttl)

        self._request_sync("PUT", key, content=value, headers=headers)

    def delete(self, key: str) -> bool:
        """Delete key from cache (sync).

        Args:
            key: Cache key

        Returns:
            True if deleted, False if key didn't exist

        Raises:
            BackendError: If operation fails
        """
        try:
            self._request_sync("DELETE", key)
            return True
        except BackendError as exc:
            # 404 means key didn't exist (not an error for delete)
            if exc.original_exception and isinstance(exc.original_exception, httpx.HTTPStatusError):
                if exc.original_exception.response.status_code == 404:
                    return False
            raise

    def exists(self, key: str) -> bool:
        """Check if key exists in cache (sync).

        Args:
            key: Cache key

        Returns:
            True if key exists, False otherwise

        Raises:
            BackendError: If operation fails
        """
        try:
            # Use HEAD request (idiomatic HTTP for existence checks)
            self._request_sync("HEAD", key)
            return True
        except BackendError as exc:
            # 404 means doesn't exist
            if exc.original_exception and isinstance(exc.original_exception, httpx.HTTPStatusError):
                if exc.original_exception.response.status_code == 404:
                    return False
            raise

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        """Check cachekit.io backend health (sync).

        Pings backend to verify connectivity and measures latency.

        Returns:
            Tuple of (is_healthy, details_dict)
            is_healthy: True if backend is responsive
            details_dict: Contains latency_ms, backend_type, api_url
        """
        try:
            start = time.time()
            response = self._request_sync("GET", "health")
            latency_ms = (time.time() - start) * 1000

            data = response.json()
            return (
                True,
                {
                    "backend_type": "saas",
                    "latency_ms": round(latency_ms, 2),
                    "api_url": self._config.api_url,
                    "version": data.get("version", "unknown"),
                },
            )
        except Exception as exc:
            return (
                False,
                {
                    "backend_type": "saas",
                    "latency_ms": -1,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )

    # ==================== Async Backend Methods (Primary Implementation) ====================

    async def get_async(self, key: str) -> bytes | None:
        """Retrieve value from cache (async).

        Args:
            key: Cache key

        Returns:
            Cached bytes value or None if not found

        Raises:
            BackendError: If operation fails (network, auth, etc.)
        """
        try:
            response = await self._request_async("GET", key)
            return response.content
        except BackendError as exc:
            # 404 is not an error (cache miss)
            if exc.original_exception and isinstance(exc.original_exception, httpx.HTTPStatusError):
                if exc.original_exception.response.status_code == 404:
                    return None
            raise

    async def set_async(self, key: str, value: bytes, ttl: int | None = None) -> None:
        """Store value in cache (async).

        Args:
            key: Cache key
            value: Bytes to cache
            ttl: Time-to-live in seconds (optional)

        Raises:
            BackendError: If operation fails
        """
        headers = {}
        if ttl is not None:
            headers["X-TTL"] = str(ttl)

        await self._request_async("PUT", key, content=value, headers=headers)

    async def delete_async(self, key: str) -> bool:
        """Delete key from cache (async).

        Args:
            key: Cache key

        Returns:
            True if deleted, False if key didn't exist

        Raises:
            BackendError: If operation fails
        """
        try:
            await self._request_async("DELETE", key)
            return True
        except BackendError as exc:
            # 404 means key didn't exist (not an error for delete)
            if exc.original_exception and isinstance(exc.original_exception, httpx.HTTPStatusError):
                if exc.original_exception.response.status_code == 404:
                    return False
            raise

    async def exists_async(self, key: str) -> bool:
        """Check if key exists in cache (async).

        Args:
            key: Cache key

        Returns:
            True if key exists, False otherwise

        Raises:
            BackendError: If operation fails
        """
        try:
            # Use HEAD request (idiomatic HTTP for existence checks)
            await self._request_async("HEAD", key)
            return True
        except BackendError as exc:
            # 404 means doesn't exist
            if exc.original_exception and isinstance(exc.original_exception, httpx.HTTPStatusError):
                if exc.original_exception.response.status_code == 404:
                    return False
            raise

    async def health_check_async(self) -> tuple[bool, dict[str, Any]]:
        """Check cachekit.io backend health (async).

        Pings backend to verify connectivity and measures latency.

        Returns:
            Tuple of (is_healthy, details_dict)
            is_healthy: True if backend is responsive
            details_dict: Contains latency_ms, backend_type, api_url
        """
        try:
            start = time.time()
            response = await self._request_async("GET", "health")
            latency_ms = (time.time() - start) * 1000

            data = response.json()
            return (
                True,
                {
                    "backend_type": "saas",
                    "latency_ms": round(latency_ms, 2),
                    "api_url": self._config.api_url,
                    "version": data.get("version", "unknown"),
                },
            )
        except Exception as exc:
            return (
                False,
                {
                    "backend_type": "saas",
                    "latency_ms": -1,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )

    # ==================== LockableBackend Protocol ====================

    async def acquire_lock(self, lock_key: str, timeout: int = 5) -> str | None:
        """Acquire distributed lock.

        Args:
            lock_key: Lock identifier
            timeout: Lock timeout in seconds

        Returns:
            Lock ID if acquired, None if failed
        """
        try:
            payload = json.dumps({"timeout_ms": timeout * 1000})
            response = await self._request_async(
                "POST",
                f"{lock_key}/lock",
                content=payload.encode(),
                headers={"Content-Type": "application/json"},
            )
            data = response.json()
            return data.get("lock_id")
        except BackendError:
            return None

    async def release_lock(self, lock_key: str, lock_id: str) -> bool:
        """Release distributed lock.

        Args:
            lock_key: Lock identifier
            lock_id: Lock ID from acquire_lock

        Returns:
            True if released, False otherwise
        """
        try:
            # DELETE /v1/cache/{key}/lock?lock_id=xxx
            await self._request_async("DELETE", f"{lock_key}/lock?lock_id={lock_id}")
            return True
        except BackendError:
            return False

    # ==================== TTLInspectableBackend Protocol ====================

    async def get_ttl(self, key: str) -> int | None:
        """Get remaining TTL for key in seconds.

        Args:
            key: Cache key

        Returns:
            TTL in seconds, None if key doesn't exist or has no expiry
        """
        try:
            response = await self._request_async("GET", f"{key}/ttl")
            data = response.json()
            return data.get("ttl")
        except BackendError:
            return None

    async def refresh_ttl(self, key: str, ttl: int) -> bool:
        """Refresh/update TTL for existing key.

        Args:
            key: Cache key
            ttl: New TTL in seconds

        Returns:
            True if updated, False otherwise
        """
        try:
            payload = json.dumps({"ttl": ttl})
            await self._request_async(
                "PATCH",
                f"{key}/ttl",
                content=payload.encode(),
                headers={"Content-Type": "application/json"},
            )
            return True
        except BackendError:
            return False

    # ==================== TimeoutConfigurableBackend Protocol ====================

    def with_timeout(self, timeout: float) -> CachekitIOBackend:
        """Create new backend instance with different timeout.

        Args:
            timeout: New timeout in seconds

        Returns:
            New CachekitIOBackend instance with updated timeout
        """
        return CachekitIOBackend(
            api_url=self._config.api_url,
            api_key=self._config.api_key.get_secret_value(),
            timeout=timeout,
        )
