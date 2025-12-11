from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, NamedTuple, TypeVar, Union

from ..backends.errors import BackendError, BackendErrorType
from ..cache_handler import (
    CacheInvalidator,
    CacheOperationHandler,
    CacheSerializationHandler,
    StandardCacheHandler,
    get_backend_provider,
    get_logger,
)
from ..key_generator import CacheKeyGenerator
from ..l1_cache import get_l1_cache
from ..reliability import CircuitBreakerConfig

# Config import removed - using direct DecoratorConfig integration
from .orchestrator import FeatureOrchestrator
from .tenant_context import TenantContextExtractor

if TYPE_CHECKING:
    from ..serializers.base import SerializerProtocol

F = TypeVar("F", bound=Callable[..., Any])

_logger = logging.getLogger(__name__)


class CacheInfo(NamedTuple):
    """Cache statistics for a decorated function.

    Matches functools.lru_cache API for consistency.

    Note on TTL:
    TTL remaining is NOT exposed here because cache_info() is per-function, but TTL is per-key.
    A single decorated function with variable arguments caches multiple independent keys:
        @cache(ttl=3600)
        def query(user_id: int):
            ...
        query(1)  # cached at T0, expires at T0+3600
        query(2)  # cached at T1, expires at T1+3600
    At time T1+100, what's the "TTL remaining"? Different for each key.
    Tracking all TTLs requires per-key overhead. Use Redis TTL commands directly if needed.

    Examples:
        Create CacheInfo with statistics:

        >>> info = CacheInfo(
        ...     hits=100, misses=20, l1_hits=80, l2_hits=20,
        ...     maxsize=None, currsize=None, l2_avg_latency_ms=2.5,
        ...     last_operation_at=1700000000.0, session_id="test-session"
        ... )
        >>> info.hits
        100
        >>> info.l1_hits + info.l2_hits == info.hits
        True

        Access hit ratio:

        >>> total = info.hits + info.misses
        >>> round(info.hits / total, 2)
        0.83
    """

    hits: int  # Total cache hits (L1 + L2)
    misses: int  # Total cache misses
    l1_hits: int  # L1 (in-memory) hits only
    l2_hits: int  # L2 (backend) hits only
    maxsize: int | None  # Not applicable for external cache (always None)
    currsize: int | None  # Not applicable (always None)
    l2_avg_latency_ms: float  # Average L2 (Redis) latency in milliseconds
    last_operation_at: float | None  # Unix timestamp of last cache operation
    session_id: str | None = None  # Function-specific session ID for correlation


class _FunctionStats:
    """Tracks cache performance statistics for a single decorated function.

    Thread Safety:
        Uses RLock for thread-safe counter updates. All methods are safe to call
        from multiple threads concurrently. Statistics are shared across all
        threads calling the same decorated function.

    Attributes:
        _l1_hits: Count of L1 cache hits
        _l2_hits: Count of L2 cache hits (Redis)
        _misses: Count of cache misses
        _l2_cumulative_latency_ms: Sum of L2 operation latencies (ms)
        _lock: RLock for thread-safe updates
        _function_identifier: Module and function name for session ID generation
        session_id: Function-specific session identifier (lazily regenerated after clear)

    Examples:
        Track cache hits and misses:

        >>> stats = _FunctionStats("mymodule.myfunc")
        >>> stats.record_l1_hit()
        >>> stats.record_l2_hit(2.5)
        >>> stats.record_miss()
        >>> info = stats.get_info()
        >>> info.hits
        2
        >>> info.l1_hits
        1
        >>> info.l2_hits
        1
        >>> info.misses
        1

        L2 average latency is tracked:

        >>> stats2 = _FunctionStats("test.func")
        >>> stats2.record_l2_hit(2.0)
        >>> stats2.record_l2_hit(4.0)
        >>> stats2.get_info().l2_avg_latency_ms
        3.0

        Clear resets all statistics:

        >>> stats.clear()
        >>> info = stats.get_info()
        >>> info.hits
        0
    """

    def __init__(self, function_identifier: str = "default", l1_enabled: bool = True):
        """Initialize statistics tracker.

        Args:
            function_identifier: Function identifier for session ID generation.
                                Format: "{module}.{function_name}". Defaults to "default".
            l1_enabled: Whether L1 (in-memory) cache is enabled for this function.
                       Used for rate limit classification headers. Defaults to True.
        """
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._l1_hits = 0
        self._l2_hits = 0
        self._l2_cumulative_latency_ms = 0.0  # Sum of all L2 latencies
        self._l2_cached_avg_ms = 0.0  # Cached average (recalculated on L2 hit)
        self._last_operation_at: float | None = None  # Unix timestamp
        self._function_identifier = function_identifier  # Store for lazy session ID generation
        self._clear_count = 0  # Incremented on each cache_clear() call
        self.session_id: str | None = None  # Lazy-initialized on first access
        self.l1_enabled = l1_enabled  # Rate limit classification flag

    def record_hit(self, source: str):
        """Record a cache hit from L1 or L2 (DEPRECATED - use record_l1_hit/record_l2_hit)."""
        with self._lock:
            self._hits += 1
            if source == "l1":
                self._l1_hits += 1
            elif source == "l2":
                self._l2_hits += 1
            self._last_operation_at = time.time()

    def record_l1_hit(self):
        """Record an L1 (in-memory) cache hit."""
        with self._lock:
            self._hits += 1
            self._l1_hits += 1
            self._last_operation_at = time.time()

    def record_l2_hit(self, latency_ms: float):
        """Record an L2 (Redis) cache hit with latency measurement."""
        with self._lock:
            self._hits += 1
            self._l2_hits += 1
            self._l2_cumulative_latency_ms += latency_ms
            # Recalculate average immediately when new L2 hit recorded
            self._l2_cached_avg_ms = self._l2_cumulative_latency_ms / self._l2_hits
            self._last_operation_at = time.time()

    def record_miss(self):
        """Record a cache miss."""
        with self._lock:
            self._misses += 1
            self._last_operation_at = time.time()

    def _ensure_session_id(self) -> str:
        """Lazily generate session ID if not set.

        Called on first use or after cache_clear(). Generates a new session ID
        by combining current process UUID with function identifier and clear count.

        Returns:
            str: Function-specific session ID with format:
                 "{uuid}:{module}.{func}" (clear_count=0)
                 "{uuid}:{module}.{func}#N" (clear_count>0, where N is the count)

        Note:
            Must be called within self._lock for thread safety.
            Clear count is appended to make session IDs unique after each cache_clear().
        """
        if self.session_id is None:
            from .session import get_session_id

            base_id = f"{get_session_id()}:{self._function_identifier}"
            # Append clear count if cache has been cleared (makes session ID unique)
            if self._clear_count > 0:
                self.session_id = f"{base_id}#{self._clear_count}"
            else:
                self.session_id = base_id
        return self.session_id

    def get_info(self) -> CacheInfo:
        """Get current statistics as CacheInfo."""
        with self._lock:
            # Ensure session ID is initialized (lazy init or post-clear regeneration)
            current_session_id = self._ensure_session_id()

            return CacheInfo(
                hits=self._hits,
                misses=self._misses,
                l1_hits=self._l1_hits,
                l2_hits=self._l2_hits,
                maxsize=None,  # Not applicable for external cache
                currsize=None,  # Not applicable
                l2_avg_latency_ms=self._l2_cached_avg_ms,
                last_operation_at=self._last_operation_at,
                session_id=current_session_id,
            )

    def clear(self):
        """Reset all statistics and regenerate session ID.

        When cache is cleared (via cache_clear()), statistics are reset to zero
        and a new session ID is generated. This prevents backend validation errors
        when session counters decrease (which would otherwise appear as a replay attack).

        Session regeneration happens lazily on next cache operation - session_id is
        set to None here, and will be regenerated on next get_info() call with an
        incremented clear count appended (e.g., "{uuid}:{func}#1", "{uuid}:{func}#2", etc.).
        """
        with self._lock:
            self._hits = 0
            self._misses = 0
            self._l1_hits = 0
            self._l2_hits = 0
            self._l2_cumulative_latency_ms = 0.0
            self._l2_cached_avg_ms = 0.0
            self._last_operation_at = None
            # Increment clear count (makes next session ID unique)
            self._clear_count += 1
            # Clear session ID - will be regenerated with new clear count on next operation
            self.session_id = None


# Lazy logger initialization to avoid import-time container access
def logger():
    """Get the logger instance lazily."""
    return get_logger()


def create_cache_wrapper(
    func: F,
    config: Any = None,  # DecoratorConfig | None (avoid circular import)
    ttl: int | None = None,
    namespace: str | None = None,
    safe_mode: bool = False,
    # Serialization & Security
    serializer: Union[str, SerializerProtocol] = "default",  # type: ignore[name-defined]
    integrity_checking: bool = True,
    encryption: bool = False,
    tenant_extractor: TenantContextExtractor | None = None,
    single_tenant_mode: bool = False,
    deployment_uuid: str | None = None,
    master_key: str | None = None,
    # Performance features
    pipelined: bool = True,
    refresh_ttl_on_get: bool = False,
    ttl_refresh_threshold: float = 0.5,
    fast_mode: bool = False,
    l1_enabled: bool = True,
    backend: Any = None,
    # Reliability features
    circuit_breaker: bool = True,
    circuit_breaker_config: CircuitBreakerConfig | None = None,
    adaptive_timeout: bool = True,
    backpressure: bool = True,
    max_concurrent_requests: int = 100,
    # Monitoring features
    collect_stats: bool = True,
    enable_tracing: bool = True,
    enable_structured_logging: bool = True,
    # L1-only mode flag
    _l1_only_mode: bool = False,
    **kwargs: Any,
) -> F:
    """Create cache wrapper for a function with specified configuration.

    This is the core wrapper factory that creates the actual sync/async
    wrapper functions with all enterprise features configured.

    Args:
        func: Function to wrap with caching
        ttl: Cache time-to-live in seconds (None = no expiration)
        namespace: Cache key namespace prefix
        safe_mode: Enable safe mode (deprecated, use reliability features)
        serializer: Serializer instance or name. Accepts either:
                   - String name: "default" (MessagePack), "arrow" (DataFrame zero-copy)
                   - SerializerProtocol instance: Custom serializer implementing the protocol
        encryption: Enable zero-knowledge encryption layer (AES-256-GCM).
                   Orthogonal to serializer - wraps ANY serializer with encryption.
        tenant_extractor: Optional tenant ID extractor for multi-tenant encryption.
                         Only used if encryption=True.
                         If None: single-tenant mode (uses nil UUID for encryption).
                         If provided: multi-tenant mode (extracts tenant_id from function args/kwargs).
                         FAIL CLOSED: extraction failure raises ValueError (no fallback to shared key).
        single_tenant_mode: Explicitly enable single-tenant mode (requires encryption=True).
                           Mutually exclusive with tenant_extractor. Prevents accidental shared
                           keys in multi-tenant deployments by requiring explicit configuration.
        deployment_uuid: Optional deployment-specific UUID for single-tenant mode.
                        If not provided, uses CACHEKIT_DEPLOYMENT_UUID env var or persistent file.
                        Must be deterministic (same across restarts) to decrypt cached data.
        pipelined: Enable Redis pipelining for performance
        refresh_ttl_on_get: Refresh TTL on cache hit
        ttl_refresh_threshold: Refresh when TTL below this fraction
        fast_mode: Disable monitoring for maximum performance
        l1_enabled: Enable L1 in-memory cache. With encryption=True, L1 stores encrypted bytes
                   (decryption at read time only). Both L1+L2 support any combination with encryption
                   for both performance and security.
        backend: Optional backend (BaseBackend implementation). If None, uses default
                 RedisBackendProvider from DI container. Pass explicit backend for testing
                 or alternative storage (HTTP, DynamoDB, etc.).
        circuit_breaker: Enable circuit breaker for fault tolerance
        circuit_breaker_config: Circuit breaker configuration
        adaptive_timeout: Enable adaptive timeout
        backpressure: Enable backpressure control
        max_concurrent_requests: Max concurrent requests (backpressure)
        collect_stats: Enable statistics collection
        enable_tracing: Enable distributed tracing
        enable_structured_logging: Enable structured logging

    Security Note:
        When encryption=True and tenant_extractor is provided, tenant ID extraction
        uses FAIL CLOSED security policy. If extraction fails, ValueError propagates to caller
        (no fallback to shared encryption key). This ensures cryptographic tenant isolation.
    """
    # Handle DecoratorConfig object (Task 5: config simplification)
    # If config is provided, override all parameters with config values
    if config is not None:
        # Import here to avoid circular dependency
        from ..config.decorator import DecoratorConfig

        if not isinstance(config, DecoratorConfig):
            raise TypeError(f"config must be DecoratorConfig instance, got {type(config)}")

        # Validate config
        config._validate_config()

        # Override all parameters from DecoratorConfig
        ttl = config.ttl if ttl is None else ttl
        namespace = config.namespace if namespace is None else namespace
        # safe_mode is deprecated, not extracted from config
        serializer = config.serializer
        integrity_checking = config.integrity_checking
        refresh_ttl_on_get = config.refresh_ttl_on_get
        ttl_refresh_threshold = config.ttl_refresh_threshold
        backend = config.backend if backend is None else backend

        # L1 cache settings
        l1_enabled = config.l1.enabled

        # Circuit breaker settings
        circuit_breaker = config.circuit_breaker.enabled

        # Timeout settings
        adaptive_timeout = config.timeout.enabled

        # Backpressure settings
        backpressure = config.backpressure.enabled
        max_concurrent_requests = config.backpressure.max_concurrent_requests

        # Monitoring settings
        collect_stats = config.monitoring.collect_stats
        # enable_tracing = config.monitoring.enable_tracing  # Not used after CacheConfig removal
        enable_structured_logging = config.monitoring.enable_structured_logging

        # Encryption settings
        encryption = config.encryption.enabled
        tenant_extractor = config.encryption.tenant_extractor  # type: ignore[assignment]
        single_tenant_mode = config.encryption.single_tenant_mode
        deployment_uuid = config.encryption.deployment_uuid
        master_key = config.encryption.master_key

    # Fast mode: Disable monitoring overhead, keep performance features
    use_circuit_breaker = circuit_breaker and not fast_mode
    use_adaptive_timeout = adaptive_timeout and not fast_mode
    use_backpressure = backpressure and not fast_mode
    use_collect_stats = collect_stats and not fast_mode
    # use_enable_tracing = enable_tracing and not fast_mode  # Not used after CacheConfig removal
    use_enable_structured_logging = enable_structured_logging and not fast_mode
    use_pipelined = pipelined  # Keep enabled: pipelining reduces network roundtrips

    # Initialize handler components
    # Pre-compute function hash at decoration time (50-200μs savings)
    from ..hash_utils import function_hash

    func_hash = function_hash(f"{func.__module__}.{func.__qualname__}")

    # Initialize key generator (uses Blake2b + pickle)
    key_generator = CacheKeyGenerator()

    # Initialize serialization handler with encryption layer if requested
    # Serializer defines HOW to serialize (default=msgpack), encryption defines WHETHER to encrypt
    serialization_handler = CacheSerializationHandler(
        serializer_name=serializer,
        encryption=encryption,
        tenant_extractor=tenant_extractor,
        single_tenant_mode=single_tenant_mode,
        deployment_uuid=deployment_uuid,
        master_key=master_key,
        enable_integrity_checking=integrity_checking,
    )

    # Create cache handler strategy based on pipelined parameter
    # Will be initialized with actual Redis client when first used
    cache_handler_strategy = None

    operation_handler = CacheOperationHandler(serialization_handler, key_generator, cache_handler=cache_handler_strategy)
    invalidator = CacheInvalidator(key_generator, integrity_checking=integrity_checking)

    # Configuration validation (no CacheConfig object needed - using direct variables)
    # Validate encryption configuration if encryption is enabled
    from ..config import validate_encryption_config

    validate_encryption_config(encryption)

    # Note: L1 cache + encryption is supported.
    # L1 stores encrypted bytes (not plaintext), decryption happens at read time only.
    # This maintains security while enabling sub-microsecond cache hits.

    # Initialize feature orchestrator using EXISTING reliability/monitoring modules
    # Convert CircuitBreakerConfig to dict if provided
    cb_config_dict: dict[str, Any] | None = None
    if circuit_breaker_config is not None:
        cb_config_dict = (
            circuit_breaker_config.model_dump()  # type: ignore[union-attr]
            if hasattr(circuit_breaker_config, "model_dump")
            else circuit_breaker_config
        )

    features = FeatureOrchestrator(
        namespace=namespace or "default",
        circuit_breaker_enabled=use_circuit_breaker,
        circuit_breaker_config=cb_config_dict or {},  # Use empty dict as default
        adaptive_timeout_enabled=use_adaptive_timeout,
        backpressure_enabled=use_backpressure,
        backpressure_config={"max_concurrent": max_concurrent_requests} if use_backpressure else None,
        collect_stats=use_collect_stats,
        enable_structured_logging=use_enable_structured_logging,
    )

    # Store backend and handler type for consistent access
    # If explicit backend provided, use it; otherwise get from provider on first use
    _backend = backend if backend is not None else None
    _use_pipelined = use_pipelined

    # FIX: Initialize L1 cache if enabled
    _l1_cache = get_l1_cache(namespace or "default") if l1_enabled else None

    # Create per-function statistics tracker with lazy session ID generation
    # Session ID format: "{process_uuid}:{module}.{function_name}"
    # Generated lazily on first use or regenerated after cache_clear()
    function_identifier = f"{func.__module__}.{func.__qualname__}"

    # Create stats tracker (session ID will be lazy-initialized on first use)
    # Pass l1_enabled for rate limit classification header
    _stats = _FunctionStats(function_identifier=function_identifier, l1_enabled=l1_enabled)

    # L1-only mode: debug log if backend would have been available
    # Helps developers understand that Redis config is being intentionally ignored
    if _l1_only_mode:
        redis_url = os.environ.get("REDIS_URL") or os.environ.get("CACHEKIT_REDIS_URL")
        if redis_url:
            # Truncate URL to avoid logging credentials
            safe_url = redis_url.split("@")[-1] if "@" in redis_url else redis_url[:30]
            _logger.debug(
                "L1-only mode: %s using in-memory cache only (backend=None explicit), ignoring available Redis at %s",
                function_identifier,
                safe_url,
            )

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: PLR0912
        # Bypass check (5-10μs savings)
        if "_bypass_cache" in kwargs:
            del kwargs["_bypass_cache"]
            return func(*args, **kwargs)

        # SET stats context before any backend operations
        from .stats_context import reset_current_function_stats, set_current_function_stats

        token = set_current_function_stats(_stats)

        # Generate correlation ID for request tracking
        correlation_id = features.generate_correlation_id()
        features.set_correlation_id(correlation_id)

        cache_key = None  # Initialize to avoid UnboundLocalError
        func_start_time: float | None = None  # Initialize for exception handlers

        # Create tracing span for cache operation
        span_attributes = {
            "cache.system": "l1_memory" if _l1_only_mode else "redis",
            "cache.operation": "get",
            "cache.namespace": namespace or "default",
            "cache.serializer": serializer,
            "function.name": func.__name__,
            "function.async": False,
        }

        # Key generation - needed for both L1-only and L1+L2 modes
        try:
            if fast_mode:
                # Minimal key generation - no string formatting overhead
                from ..hash_utils import cache_key_hash

                cache_namespace = namespace or "default"
                args_kwargs_str = str(args) + str(kwargs)
                cache_key = cache_namespace + ":" + func_hash + ":" + cache_key_hash(args_kwargs_str)
            else:
                cache_key = operation_handler.get_cache_key(func, args, kwargs, namespace, integrity_checking)
        except Exception as e:
            # Key generation failed - execute function without caching
            features.handle_cache_error(
                error=e,
                operation="key_generation",
                cache_key="<generation_failed>",
                namespace=namespace or "default",
                duration_ms=0.0,
            )
            reset_current_function_stats(token)
            return func(*args, **kwargs)

        # L1-ONLY MODE: Skip backend initialization entirely
        # This is the fix for the sentinel problem: when backend=None is explicitly passed,
        # we should NOT try to get a backend from the provider
        if _l1_only_mode:
            # L1-only mode: Check L1 cache, execute function on miss, store in L1
            if _l1_cache and cache_key:
                l1_found, l1_bytes = _l1_cache.get(cache_key)
                if l1_found and l1_bytes:
                    # L1 cache hit
                    try:
                        # Pass cache_key for AAD verification (required for encryption)
                        l1_value = operation_handler.serialization_handler.deserialize_data(l1_bytes, cache_key=cache_key)
                        _stats.record_l1_hit()
                        reset_current_function_stats(token)
                        return l1_value
                    except Exception:
                        # L1 deserialization failed - invalidate and continue
                        _l1_cache.invalidate(cache_key)

            # L1 cache miss - execute function and store in L1
            _stats.record_miss()
            try:
                result = func(*args, **kwargs)
                # Serialize and store in L1
                try:
                    # Pass cache_key for AAD binding (required for encryption)
                    serialized_bytes = operation_handler.serialization_handler.serialize_data(
                        result, args, kwargs, cache_key=cache_key
                    )
                    if _l1_cache and cache_key and serialized_bytes:
                        _l1_cache.put(cache_key, serialized_bytes, redis_ttl=ttl)
                except Exception as e:
                    # Serialization/storage failed but function succeeded - log and return result
                    logger().debug(f"L1-only mode: serialization/storage failed for {cache_key}: {e}")
                return result
            finally:
                features.clear_correlation_id()
                reset_current_function_stats(token)

        # L1+L2 MODE: Original behavior with backend initialization
        lock_key = f"{cache_key}:lock"

        with features.create_span("redis_cache", span_attributes) as span:
            try:
                # Add cache key to span attributes
                if span:
                    features.set_span_attributes(span, {"cache.key": cache_key})

                # Guard clause: Circuit breaker check - fail fast if circuit is open
                if features.circuit_breaker and not features.should_allow_request():
                    features.log_cache_operation(
                        operation="circuit_breaker_open",
                        key=cache_key,
                        namespace=namespace or "default",
                        serializer="rust",
                        error="Circuit breaker is OPEN",
                        error_type="CircuitBreakerOpen",
                    )
                    # Circuit breaker fail-fast: raise exception immediately
                    raise BackendError(  # noqa: F823, type: ignore[name-defined]
                        "Circuit breaker OPEN - failing fast", error_type=BackendErrorType.TRANSIENT
                    )

                nonlocal _backend
                if _backend is None:
                    _backend = get_backend_provider().get_backend()

                # Setup cache handler strategy on first use with adaptive timeout
                # Note: Both pipelined and non-pipelined use StandardCacheHandler for now
                handler = StandardCacheHandler(
                    _backend,
                    timeout_provider=features.get_timeout,
                    backpressure_controller=features.backpressure,
                    ttl_refresh_threshold=ttl_refresh_threshold,
                )
                operation_handler.set_cache_handler(handler)
                backend = _backend
            except Exception as e:
                # Guard clause: Client creation failed - early return with fallback
                features.handle_cache_error(
                    error=e,
                    operation="client_creation",
                    cache_key=cache_key or "unknown",
                    namespace=namespace or "default",
                    span=span,
                    duration_ms=0.0,
                    serializer="rust",
                )
                # WHY: Early return on backend failure - outside main try-finally, needs explicit cleanup
                reset_current_function_stats(token)
                return func(*args, **kwargs)

        # Guard clause: L1 cache check first - early return eliminates network latency
        if _l1_cache and cache_key:
            l1_found, l1_bytes = _l1_cache.get(cache_key)
            if l1_found and l1_bytes:
                # L1 cache hit (~50ns vs ~1000μs for Redis) - deserialize bytes
                try:
                    l1_value = operation_handler.serialization_handler.deserialize_data(l1_bytes)

                    features.set_operation_context("l1_get", duration_ms=0.001)
                    features.record_success()

                    # Record L1 cache hit metrics
                    if features.collect_stats:
                        features.record_cache_operation(
                            operation="get",
                            namespace=namespace or "default",
                            serializer="l1_memory",
                            success=True,
                            duration_ms=0.001,  # ~1μs for L1 hit
                            size_bytes=len(l1_bytes),
                            hit=True,
                        )

                    features.log_cache_operation(
                        operation="l1_get",
                        key=cache_key,
                        namespace=namespace or "default",
                        serializer="l1_memory",
                        duration_ms=0.001,
                        hit=True,
                        ttl=ttl,
                    )

                    # Record L1 hit for cache_info()
                    _stats.record_l1_hit()

                    # WHY: L1 cache hit returns BEFORE the try-finally block (line ~642-713)
                    # that handles context cleanup. Without this explicit reset, the contextvar
                    # leaks to subsequent calls, causing stats pollution between requests.
                    # ~34ns overhead, but required for correctness. See test_context_leak_regression.py
                    reset_current_function_stats(token)
                    return l1_value
                except Exception as e:
                    # L1 deserialization failed - invalidate and continue to L2
                    logger().warning(f"L1 cache deserialization failed for {cache_key}: {e}")
                    _l1_cache.invalidate(cache_key)

        # Continue with the rest of the sync wrapper logic...
        # Try to get cached value with optional TTL refresh
        start_time = time.time()
        try:
            refresh_ttl = ttl if refresh_ttl_on_get and ttl else None

            # Use operation handler for all cache access (uses backend internally)
            cached_result = operation_handler.get_cached_value(cache_key, refresh_ttl)

            # Record duration for adaptive timeout
            duration = time.time() - start_time
            features.record_duration(duration)

            if cached_result is not None:
                # Cached result is a tuple (True, actual_value)
                features.set_operation_context("get", duration_ms=duration * 1000)
                features.record_success()

                # Record cache hit in span
                if span:
                    features.set_span_attributes(
                        span,
                        {
                            "cache.hit": True,
                            "cache.latency_ms": duration * 1000,
                        },
                    )

                # Record cache hit with structured logging
                size_bytes = len(str(cached_result[1]).encode("utf-8")) if cached_result[1] is not None else 0
                features.log_cache_operation(
                    operation="get",
                    key=cache_key,
                    namespace=namespace or "default",
                    serializer="rust",
                    duration_ms=duration * 1000,
                    hit=True,
                    ttl=ttl,
                )

                # Also record statistics if enabled
                if features.collect_stats:
                    size_bytes = len(str(cached_result[1]).encode("utf-8")) if cached_result[1] is not None else 0
                    features.record_cache_operation(
                        operation="get",
                        namespace=namespace or "default",
                        serializer="rust",
                        success=True,
                        duration_ms=duration * 1000,
                        size_bytes=size_bytes,
                        hit=True,
                    )

                # Record L2 hit with latency for cache_info()
                duration_ms = duration * 1000
                _stats.record_l2_hit(duration_ms)

                # WHY: L2 cache hit returns from try block that lacks finally cleanup
                # (only inner try at line ~567, not the outer try-finally at ~645-720)
                reset_current_function_stats(token)
                return cached_result[1]
        except Exception as e:
            # Cache GET failed - execute function without caching
            get_duration_ms = (time.time() - start_time) * 1000
            features.handle_cache_error(
                error=e,
                operation="cache_get",
                cache_key=cache_key or "unknown",
                namespace=namespace or "default",
                span=span,
                duration_ms=get_duration_ms,
                serializer="rust",
            )
            # WHY: Early return on cache GET failure - same reason as L2 hit path
            reset_current_function_stats(token)
            return func(*args, **kwargs)

        # CACHE MISS - Execute function and cache result
        # Note: Sync wrappers don't support distributed locking (backend protocol is async-only)
        # For thundering herd protection, use async decorators instead
        # Record miss for cache_info()
        _stats.record_miss()

        try:
            # Execute the original function
            func_start_time = time.time() if features.collect_stats else None
            result = func(*args, **kwargs)

            if features.collect_stats and func_start_time is not None:
                func_latency = (time.time() - func_start_time) * 1000
                features.record_duration(func_latency)

            # Serialize and cache the result
            try:
                # Store using operation handler (pass args/kwargs for tenant extraction)
                # Returns serialized bytes for L1 cache storage
                serialized_bytes = operation_handler.store_result(cache_key, result, ttl, args, kwargs)

                # Also store in L1 cache for fast subsequent access (using serialized bytes)
                if _l1_cache and cache_key and serialized_bytes:
                    _l1_cache.put(cache_key, serialized_bytes, redis_ttl=ttl)

                # Record successful cache set
                set_duration_ms = (time.time() - start_time) * 1000
                features.set_operation_context("set", duration_ms=set_duration_ms)
                features.record_success()

                if features.collect_stats:
                    total_latency = (time.time() - start_time) * 1000
                    features.record_cache_operation(
                        operation="set",
                        namespace=namespace or "default",
                        success=True,
                        duration_ms=total_latency,
                        serializer="rust",
                        hit=False,  # Was a miss
                    )

            except Exception as e:
                # Caching failed but function succeeded - return result anyway
                set_duration_ms = (time.time() - start_time) * 1000
                features.handle_cache_error(
                    error=e,
                    operation="cache_set",
                    cache_key=cache_key,
                    namespace=namespace or "default",
                    duration_ms=set_duration_ms,
                    serializer="rust",
                )

            return result

        except BackendError as e:
            # Backend is unavailable - execute without caching (graceful degradation)
            features.handle_cache_error(
                error=e,
                operation="backend_connection",
                cache_key=cache_key,
                namespace=namespace or "default",
                duration_ms=0.0,
                correlation_id=correlation_id,
            )

            # Execute function without any caching
            result = func(*args, **kwargs)
            return result

        except Exception as e:
            # Other exceptions - record and re-raise
            features.record_failure(e)
            if features.collect_stats and "func_start_time" in locals() and func_start_time is not None:
                func_latency = (time.time() - func_start_time) * 1000
                features.record_duration(func_latency)
            raise
        finally:
            # Clear correlation ID after operation
            features.clear_correlation_id()
            # ALWAYS reset stats context, even on exception
            reset_current_function_stats(token)

    @functools.wraps(func)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        # Bypass check (5-10μs savings)
        if "_bypass_cache" in kwargs:
            del kwargs["_bypass_cache"]
            return await func(*args, **kwargs)

        # SET stats context before any backend operations
        from .stats_context import reset_current_function_stats, set_current_function_stats

        token = set_current_function_stats(_stats)

        try:
            # Get cache key early for consistent usage - note this may fail for complex types
            cache_key = None
            func_start_time: float | None = None  # Initialize for exception handlers
            try:
                # Fast key generation path (for simple types)
                if fast_mode:
                    # Ultra-fast key generation for hot paths (10-50μs savings)
                    from ..hash_utils import cache_key_hash

                    cache_namespace = namespace or namespace or "default"
                    args_kwargs_str = str(args) + str(kwargs)
                    cache_key = cache_namespace + ":" + func_hash + ":" + cache_key_hash(args_kwargs_str)
                else:
                    # Standard key generation with type-aware handling
                    cache_key = operation_handler.get_cache_key(func, args, kwargs, namespace, integrity_checking)
            except Exception as e:
                # If key generation fails, execute function without caching - RETURN EARLY
                # This handles unhashable types gracefully
                features.handle_cache_error(
                    error=e,
                    operation="key_generation",
                    cache_key="<generation_failed>",
                    namespace=namespace or "default",
                    duration_ms=0.0,
                )
                return await func(*args, **kwargs)

            # L1-ONLY MODE: Skip backend initialization entirely
            # This is the fix for the sentinel problem: when backend=None is explicitly passed,
            # we should NOT try to get a backend from the provider
            if _l1_only_mode:
                # L1-only mode: Check L1 cache, execute function on miss, store in L1
                if _l1_cache and cache_key:
                    l1_found, l1_bytes = _l1_cache.get(cache_key)
                    if l1_found and l1_bytes:
                        # L1 cache hit
                        try:
                            # Pass cache_key for AAD verification (required for encryption)
                            l1_value = operation_handler.serialization_handler.deserialize_data(l1_bytes, cache_key=cache_key)
                            _stats.record_l1_hit()
                            return l1_value
                        except Exception:
                            # L1 deserialization failed - invalidate and continue
                            _l1_cache.invalidate(cache_key)

                # L1 cache miss - execute function and store in L1
                _stats.record_miss()
                result = await func(*args, **kwargs)
                # Serialize and store in L1
                try:
                    # Pass cache_key for AAD binding (required for encryption)
                    serialized_bytes = operation_handler.serialization_handler.serialize_data(
                        result, args, kwargs, cache_key=cache_key
                    )
                    if _l1_cache and cache_key and serialized_bytes:
                        _l1_cache.put(cache_key, serialized_bytes, redis_ttl=ttl)
                except Exception as e:
                    # Serialization/storage failed but function succeeded - log and return result
                    logger().debug(f"L1-only mode: serialization/storage failed for {cache_key}: {e}")
                return result

            # L1+L2 MODE: Original behavior with backend initialization
            # Guard clause: Circuit breaker check - fail fast if circuit is open
            # This prevents cascading failures
            if not features.should_allow_request():
                # Circuit breaker fail-fast: raise exception immediately
                raise BackendError(  # noqa: F823  # pyright: ignore[reportUnboundVariable]
                    "Circuit breaker OPEN - failing fast", error_type=BackendErrorType.TRANSIENT
                )

            # Guard clause: L1 cache check first - early return eliminates network latency
            if _l1_cache and cache_key:
                l1_found, l1_bytes = _l1_cache.get(cache_key)
                if l1_found and l1_bytes:
                    # L1 cache hit (~50ns vs ~1000μs for Redis) - deserialize bytes
                    try:
                        l1_value = operation_handler.serialization_handler.deserialize_data(l1_bytes)

                        features.set_operation_context("l1_get", duration_ms=0.001)
                        features.record_success()

                        # Record L1 cache hit metrics
                        if features.collect_stats:
                            features.record_cache_operation(
                                operation="get",
                                namespace=namespace or "default",
                                success=True,
                                duration_ms=0.001,  # Sub-microsecond
                            )

                        # Record L1 hit for cache_info()
                        _stats.record_l1_hit()

                        return l1_value
                    except Exception as e:
                        # L1 deserialization failed - invalidate and continue to L2
                        logger().warning(f"L1 cache deserialization failed for {cache_key}: {e}")
                        _l1_cache.invalidate(cache_key)

            # Initialize backend only when needed (lazy init for performance)
            nonlocal _backend
            if _backend is None:
                try:
                    _backend = get_backend_provider().get_backend()
                except Exception as e:
                    # If Redis connection fails, execute function without caching - RETURN EARLY
                    # This prevents the decorator from breaking the application
                    features.handle_cache_error(
                        error=e,
                        operation="client_creation",
                        cache_key=cache_key or "unknown",
                        namespace=namespace or "default",
                        duration_ms=0.0,
                    )
                    return await func(*args, **kwargs)

            # Update operation handler with the backend (sync or async)
            handler = StandardCacheHandler(
                _backend,
                timeout_provider=features.get_timeout,
                backpressure_controller=features.backpressure,
                ttl_refresh_threshold=ttl_refresh_threshold,
            )
            operation_handler.set_cache_handler(handler)

            # Try to get from Redis cache (always measure time for L2 latency tracking)
            start_time = time.perf_counter()
            # Create correlation context for distributed tracing
            correlation_id = None
            if features._enable_structured_logging:
                correlation_id = features.create_correlation_id()

            try:
                # Attempt to retrieve from Redis
                cached_data = await operation_handler.cache_handler.get_async(cache_key)  # type: ignore[attr-defined]

                if cached_data is not None:
                    # Deserialize the cached data
                    result = operation_handler.serialization_handler.deserialize_data(cached_data)

                    # Record cache hit (always compute for L2 latency stats)
                    get_duration_ms = (time.perf_counter() - start_time) * 1000
                    features.set_operation_context("get", duration_ms=get_duration_ms)
                    features.record_success()

                    if features.collect_stats:
                        features.record_cache_operation(
                            operation="get",
                            namespace=namespace or "default",
                            success=True,
                            duration_ms=get_duration_ms,
                        )

                    # Update L1 cache with Redis value (serialized bytes) for subsequent fast access
                    if _l1_cache and cache_key and cached_data:
                        # cached_data is already serialized bytes from Redis
                        cached_bytes = cached_data.encode("utf-8") if isinstance(cached_data, str) else cached_data
                        _l1_cache.put(cache_key, cached_bytes, redis_ttl=ttl)

                    # Handle TTL refresh if configured and threshold met
                    if refresh_ttl_on_get and ttl and hasattr(_backend, "get_ttl") and hasattr(_backend, "refresh_ttl"):
                        try:
                            remaining_ttl = await _backend.get_ttl(cache_key)
                            if remaining_ttl and remaining_ttl < (ttl * ttl_refresh_threshold):
                                # Refresh TTL in background
                                asyncio.create_task(_backend.refresh_ttl(cache_key, ttl))
                        except Exception as e:
                            # TTL refresh is optional, don't fail on error
                            _logger.debug("TTL refresh failed for %s: %s", cache_key, e)
                    elif refresh_ttl_on_get and ttl:
                        logger().debug(f"Backend doesn't support TTL inspection for {cache_key}, skipping refresh")

                    # Record L2 hit with latency for cache_info()
                    _stats.record_l2_hit(get_duration_ms)

                    return result

            except Exception as e:
                # Redis error - record but continue to function execution
                get_duration_ms = (time.perf_counter() - start_time) * 1000
                features.handle_cache_error(
                    error=e,
                    operation="cache_get",
                    cache_key=cache_key or "unknown",
                    namespace=namespace or "default",
                    duration_ms=get_duration_ms,
                    correlation_id=correlation_id,
                )

            # CACHE MISS - Use distributed lock to prevent thundering herd
            # This ensures only one request executes the function while others wait
            lock_key = f"{cache_key}:lock"
            lock_timeout = 30.0  # Lock expires after 30 seconds to prevent deadlock
            blocking_timeout = 5.0  # Wait up to 5 seconds to acquire lock

            # Check if backend supports distributed locking
            if hasattr(_backend, "acquire_lock"):
                try:
                    # Use backend's async lock protocol
                    async with _backend.acquire_lock(
                        lock_key,
                        timeout=lock_timeout,
                        blocking_timeout=blocking_timeout,
                    ) as lock_acquired:
                        if lock_acquired:
                            # Lock acquired - double-check cache
                            # Another request may have populated it while we waited
                            try:
                                cached_data = await operation_handler.cache_handler.get_async(cache_key)  # type: ignore[attr-defined]
                                if cached_data is not None:
                                    # Another request filled the cache while we waited
                                    result = operation_handler.serialization_handler.deserialize_data(cached_data)

                                    # Update L1 cache with serialized bytes
                                    if _l1_cache and cache_key and cached_data:
                                        cached_bytes = (
                                            cached_data.encode("utf-8") if isinstance(cached_data, str) else cached_data
                                        )
                                        _l1_cache.put(cache_key, cached_bytes, redis_ttl=ttl)

                                    return result
                            except Exception as e:
                                # If double-check fails, continue to execute function
                                _logger.debug("Double-check cache failed after lock acquisition: %s", e)
                        else:
                            # Lock timeout - double-check cache before giving up
                            # Another request may have populated it while we waited
                            logger().warning(
                                f"Failed to acquire lock for {cache_key} (lock_key={lock_key}) after {blocking_timeout}s, checking cache"
                            )
                            try:
                                cached_data = await operation_handler.cache_handler.get_async(cache_key)  # type: ignore[attr-defined]
                                if cached_data is not None:
                                    # Cache was populated while waiting - use it
                                    result = operation_handler.serialization_handler.deserialize_data(cached_data)

                                    # Update L1 cache with serialized bytes
                                    if _l1_cache and cache_key and cached_data:
                                        cached_bytes = (
                                            cached_data.encode("utf-8") if isinstance(cached_data, str) else cached_data
                                        )
                                        _l1_cache.put(cache_key, cached_bytes, redis_ttl=ttl)

                                    return result
                            except Exception:
                                # Cache check failed - fall through to execute function
                                logger().warning(
                                    f"Cache check after lock timeout failed for {cache_key}, executing without lock"
                                )

                        # Execute the original function (with or without lock)
                        func_start_time = time.perf_counter() if features.collect_stats else None
                        result = await func(*args, **kwargs)

                        if features.collect_stats and func_start_time is not None:
                            func_latency = (time.perf_counter() - func_start_time) * 1000
                            features.record_duration(func_latency)

                        # Serialize and cache the result
                        try:
                            serialized_data = operation_handler.serialization_handler.serialize_data(result, args, kwargs)

                            # Store in Redis with TTL
                            await operation_handler.cache_handler.set_async(  # type: ignore[attr-defined]
                                cache_key,
                                serialized_data,
                                ttl=ttl,
                            )

                            # Also store in L1 cache for fast subsequent access (using serialized bytes)
                            if _l1_cache and cache_key:
                                serialized_bytes = (
                                    serialized_data.encode("utf-8") if isinstance(serialized_data, str) else serialized_data
                                )
                                _l1_cache.put(cache_key, serialized_bytes, redis_ttl=ttl)

                            # Record successful cache set
                            set_duration_ms = (time.perf_counter() - start_time) * 1000
                            features.set_operation_context("set", duration_ms=set_duration_ms)
                            features.record_success()

                            if features.collect_stats:
                                features.record_cache_operation(
                                    operation="set",
                                    namespace=namespace or "default",
                                    success=True,
                                    duration_ms=set_duration_ms,
                                )

                        except Exception as e:
                            # Caching failed but function succeeded - return result anyway
                            set_duration_ms = (time.perf_counter() - start_time) * 1000
                            features.handle_cache_error(
                                error=e,
                                operation="cache_set",
                                cache_key=cache_key or "unknown",
                                namespace=namespace or "default",
                                duration_ms=set_duration_ms,
                                correlation_id=correlation_id,
                            )

                        return result

                except Exception as e:
                    # Check if this is a lock-related exception or function execution exception
                    # BackendError may wrap function exceptions - check original_exception
                    from cachekit.backends.errors import BackendError

                    # If it's not a Backend error, it's from the function - re-raise
                    if not isinstance(e, BackendError):
                        raise

                    # If it's a BackendError wrapping a non-backend exception, it's from the function
                    if isinstance(e, BackendError) and e.original_exception:
                        if not isinstance(e.original_exception, BackendError):
                            # Function exception wrapped in BackendError - re-raise the original
                            raise e.original_exception from e

                    # Lock operation failed - execute without lock
                    logger().warning(f"Lock operation failed for {cache_key} (lock_key={lock_key}), executing without lock: {e}")
                    # Fall through to execute without locking

            # Execute without locking (either backend doesn't support it or lock failed)
            if not hasattr(_backend, "acquire_lock"):
                logger().debug(f"Backend doesn't support locking for {cache_key}, executing without thundering herd protection")

            try:
                # Execute the original function
                func_start_time = time.perf_counter() if features.collect_stats else None
                result = await func(*args, **kwargs)

                if features.collect_stats and func_start_time is not None:
                    func_latency = (time.perf_counter() - func_start_time) * 1000
                    features.record_duration(func_latency)

                # Serialize and cache the result
                try:
                    serialized_data = operation_handler.serialization_handler.serialize_data(result, args, kwargs)

                    # Store in Redis with TTL
                    await operation_handler.cache_handler.set_async(  # type: ignore[attr-defined]
                        cache_key,
                        serialized_data,
                        ttl=ttl,
                    )

                    # Also store in L1 cache for fast subsequent access (using serialized bytes)
                    if _l1_cache and cache_key:
                        serialized_bytes = (
                            serialized_data.encode("utf-8") if isinstance(serialized_data, str) else serialized_data
                        )
                        _l1_cache.put(cache_key, serialized_bytes, redis_ttl=ttl)

                    # Record successful cache set
                    set_duration_ms = (time.perf_counter() - start_time) * 1000
                    features.set_operation_context("set", duration_ms=set_duration_ms)
                    features.record_success()

                    if features.collect_stats:
                        features.record_cache_operation(
                            operation="set",
                            namespace=namespace or "default",
                            success=True,
                            duration_ms=set_duration_ms,
                        )

                except Exception as e:
                    # Caching failed but function succeeded - return result anyway
                    set_duration_ms = (time.perf_counter() - start_time) * 1000
                    features.handle_cache_error(
                        error=e,
                        operation="cache_set",
                        cache_key=cache_key or "unknown",
                        namespace=namespace or "default",
                        duration_ms=set_duration_ms,
                        correlation_id=correlation_id,
                    )

                return result

            except Exception as e:
                # Function execution failed - record and re-raise
                features.record_failure(e)
                if features.collect_stats and "func_start_time" in locals() and func_start_time is not None:
                    func_latency = (time.perf_counter() - func_start_time) * 1000
                    features.record_duration(func_latency)
                raise
        finally:
            # ALWAYS reset stats context, even on exception
            reset_current_function_stats(token)

    def invalidate_cache(*args: Any, **kwargs: Any) -> None:
        nonlocal _backend

        # L1-ONLY MODE: Skip backend lookup entirely
        # This fixes the sentinel problem: when backend=None is explicitly passed,
        # we should NOT try to get a backend from the provider
        if not _l1_only_mode and _backend is None:
            try:
                _backend = get_backend_provider().get_backend()
            except Exception as e:
                # If backend creation fails, can't invalidate L2
                _logger.debug("Failed to get backend for invalidation: %s", e)

        # Clear both L2 (backend) and L1 cache
        cache_key = operation_handler.get_cache_key(func, args, kwargs, namespace, integrity_checking)

        # Clear L1 cache first
        if _l1_cache and cache_key:
            _l1_cache.invalidate(cache_key)

        # Clear L2 cache via invalidator (skip in L1-only mode)
        if _backend and not _l1_only_mode:
            invalidator.set_backend(_backend)
            invalidator.invalidate_cache(func, args, kwargs, namespace)

    async def ainvalidate_cache(*args: Any, **kwargs: Any) -> None:
        nonlocal _backend

        # L1-ONLY MODE: Skip backend lookup entirely
        # This fixes the sentinel problem: when backend=None is explicitly passed,
        # we should NOT try to get a backend from the provider
        if not _l1_only_mode and _backend is None:
            try:
                _backend = get_backend_provider().get_backend()
            except Exception as e:
                # If backend creation fails, can't invalidate L2
                _logger.debug("Failed to get backend for async invalidation: %s", e)

        # Clear both L2 (backend) and L1 cache
        cache_key = operation_handler.get_cache_key(func, args, kwargs, namespace, integrity_checking)

        # Clear L1 cache first
        if _l1_cache and cache_key:
            _l1_cache.invalidate(cache_key)

        # Clear L2 cache via invalidator (skip in L1-only mode)
        if _backend and not _l1_only_mode:
            invalidator.set_backend(_backend)
            await invalidator.invalidate_cache_async(func, args, kwargs, namespace)

    def check_health() -> dict[str, Any]:
        """Check health status of this cached function's infrastructure."""
        return features.check_health()

    async def acheck_health() -> dict[str, Any]:
        """Async version of check_health."""
        return features.check_health()

    def get_health_status() -> dict[str, Any]:
        """Get current health status for this decorator instance."""
        return features.get_health_status()

    # Add cache_info, cache_clear, and __wrapped__ attributes (stdlib pattern)
    def cache_info() -> CacheInfo:
        """Get cache statistics (matches functools.lru_cache API).

        Returns hit/miss statistics for the decorated function.

        Threading behavior:
            When used with threading/multiprocessing, statistics are tracked
            per-function (shared across all invocations), not per-thread.
            The internal _FunctionStats object is shared by all threads calling
            the same decorated function, with thread-safe locking via RLock.

            For per-thread statistics, create separate decorated instances:

                # Global shared stats
                @cache()
                def shared_func(x): ...

                # Per-thread stats (different function instances)
                def get_thread_func():
                    @cache()
                    def thread_func(x): ...
                    return thread_func

        Returns:
            CacheInfo: Named tuple with hits, misses, maxsize, currsize

        Examples:
            >>> @cache()
            ... def factorial(n):
            ...     return n * factorial(n-1) if n else 1
            >>> factorial(5)
            120
            >>> factorial.cache_info()
            CacheInfo(hits=4, misses=6, maxsize=None, currsize=6)
        """
        return _stats.get_info()

    def cache_clear() -> None:
        """Clear cache statistics and invalidate all cached entries."""
        _stats.clear()
        # Also invalidate actual cache entries
        invalidate_cache() if not inspect.iscoroutinefunction(func) else ainvalidate_cache()

    if inspect.iscoroutinefunction(func):
        async_wrapper.invalidate_cache = ainvalidate_cache  # type: ignore[attr-defined]
        async_wrapper.ainvalidate_cache = ainvalidate_cache  # async version  # type: ignore[attr-defined]
        async_wrapper.check_health = acheck_health  # async version  # type: ignore[attr-defined]
        async_wrapper.get_health_status = get_health_status  # type: ignore[attr-defined]
        async_wrapper.cache_info = cache_info  # type: ignore[attr-defined]
        async_wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
        async_wrapper.__wrapped__ = func  # type: ignore[attr-defined]
        return async_wrapper  # type: ignore[return-value]
    else:
        sync_wrapper.invalidate_cache = invalidate_cache  # type: ignore[attr-defined]
        sync_wrapper.check_health = check_health  # type: ignore[attr-defined]
        sync_wrapper.get_health_status = get_health_status  # type: ignore[attr-defined]
        sync_wrapper.cache_info = cache_info  # type: ignore[attr-defined]
        sync_wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
        sync_wrapper.__wrapped__ = func  # type: ignore[attr-defined]
        return sync_wrapper  # type: ignore[return-value]
