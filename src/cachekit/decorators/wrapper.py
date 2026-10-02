from __future__ import annotations

import asyncio
import contextlib
import contextvars
import copy
import functools
import inspect
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any, NamedTuple, TypeVar, Union

from cachekit.hash_utils import redact_error_for_log

from .. import invalidation
from ..backends.errors import BackendError, UnsupportedTenantError
from ..cache_handler import (
    CacheHit,
    CacheOperationHandler,
    CacheSerializationHandler,
    StandardCacheHandler,
    TenantResolutionError,
    _supports_multi_delete,
    get_backend_provider,
    get_logger,
    handle_decrypt_failure,
    redact_cache_key,
    supports_key_tracking,
    supports_locking,
    supports_swr,
    supports_ttl_inspection,
    warn_ttl_refresh_unsupported,
)
from ..config.validation import ConfigurationError, hide_secret
from ..interop import (
    InteropError,
    bind_flat_args,
    ensure_interop_backend_compatible,
    generate_interop_key,
    validate_interop_config,
)
from ..key_generator import CacheKeyGenerator
from ..l1_cache import DEFAULT_L1_TTL_SECONDS, get_l1_cache
from ..object_cache import ObjectCache
from ..reliability import CircuitBreakerConfig
from ..serializers import SERIALIZER_REGISTRY
from ..serializers.base import SerializationError
from ..serializers.encryption_wrapper import (
    DecryptionAuthenticationError,
    EncryptionWrapper,
    KeyringConfigurationError,
    TenantMismatchError,
)

# Config import removed - using direct DecoratorConfig integration
from .orchestrator import FeatureOrchestrator
from .tenant_context import TenantContextExtractor

if TYPE_CHECKING:
    from pydantic import SecretStr

    from ..backends.base import BaseBackend
    from ..serializers.base import SerializerProtocol


def _resolve_lazy_backend() -> BaseBackend:
    """Backend for a decorator that was applied without ``backend=``.

    Consulted at FIRST CALL, not at decoration, so ``set_default_backend()``
    takes effect regardless of whether it ran before or after the module holding
    the decorated function was imported (LAB-4457). The result is kept for the
    life of the wrapper and shared by every later call, so it must not capture
    anything request-scoped — the env-resolved Redis backend reads the tenant per
    operation for exactly this reason (LAB-4773).
    """
    from ..config.decorator import get_default_backend

    default = get_default_backend()
    return default if default is not None else get_backend_provider().get_backend()


F = TypeVar("F", bound=Callable[..., Any])

_logger = logging.getLogger(__name__)

# Cap on concurrent L1-only SWR background refreshes per wrapped function.
# Bounds resource usage when many distinct keys go stale together; at capacity
# the refresh is skipped (stale keeps being served) and a later hit retries.
_L1_SWR_MAX_CONCURRENT_REFRESHES = 32

# At most one WARNING per wrapped function per window for each background failure the caller
# never sees (key tracking, a failed refresh, a refresh that could not run); failures in
# between log at DEBUG and are counted into the next WARNING. A registry outage fails every L2
# write and a failing upstream every refresh, so one WARNING each would be a log flood.
_WARN_INTERVAL_SECONDS = 60.0
# Why a refresh never ran when its arguments cannot be snapshotted: every call of that shape
# skips it, so the entry is recomputed only in the foreground once it expires.
_NOT_DEEP_COPYABLE = ": arguments not deep-copyable, so refresh-ahead cannot run for this call"

# Keys per multi-key L2 delete in a whole-function invalidation: bounds each server-side
# command, like the Redis registry drain's chunk.
_DELETE_BATCH = 10_000

# Backed-mode (L2) SWR revalidation pool — deliberately separate from the L1
# constant above so the two features can be tuned independently (LAB-381 panel).
_L2_SWR_MAX_CONCURRENT_REFRESHES = 32


def _ttl_refresh_done_callback(task: asyncio.Task, cache_key: str) -> None:
    """Callback for background TTL refresh tasks to handle errors.

    This callback logs errors from fire-and-forget TTL refresh tasks
    instead of letting them be silently dropped.

    Args:
        task: The completed asyncio Task
        cache_key: The cache key being refreshed (for logging context)
    """
    try:
        exc = task.exception()
        if exc is not None:
            _logger.debug("Background TTL refresh failed for %s: %s", redact_cache_key(cache_key), redact_error_for_log(exc))
    except asyncio.CancelledError:
        # Task was cancelled (e.g., during shutdown) - this is expected, don't log
        pass


class _WarnThrottle:
    """One WARNING per _WARN_INTERVAL_SECONDS for one kind of failure; claim() counts the rest.

    Fork-safe by the owner-PID idiom (see _l2_swr_try_begin): a forked child's first claim
    replaces the lock, which a parent thread that did not survive the fork may hold, and drops
    the parent's count. Sibling threads racing that swap cost at worst one extra WARNING, once
    per fork.
    """

    __slots__ = ("_count", "_lock", "_pid", "_warned_at")

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self._lock = threading.Lock()
        self._warned_at, self._count = float("-inf"), 0
        self._pid = os.getpid()

    def claim(self) -> int:
        """Count one failure. Returns 0 if it should log at DEBUG, else the failures since the
        last WARNING, this one included, for the WARNING it should log.

        The window is claimed under the lock and the caller logs outside it: concurrent
        failures then emit one WARNING, and a slow log sink never serializes the failing callers.
        """
        if self._pid != os.getpid():
            self._reset()
        with self._lock:
            self._count += 1
            now = time.monotonic()
            if now - self._warned_at < _WARN_INTERVAL_SECONDS:
                return 0
            count, self._count, self._warned_at = self._count, 0, now
            return count


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

    def _reset_for_new_process(self) -> None:
        """Discard state inherited across fork(): fresh lock, zeroed counters, new session.

        Only called from the post-fork handler while the child is still
        single-threaded. The lock must be replaced, not acquired: a parent
        thread holding it at fork time leaves it permanently locked in the
        child. session_id=None re-derives lazily from the child's own
        process UUID (see decorators.session), so the child never reports
        under the parent's session ID with reset counters — which the
        server's anti-replay validation would reject.
        """
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._l1_hits = 0
        self._l2_hits = 0
        self._l2_cumulative_latency_ms = 0.0
        self._l2_cached_avg_ms = 0.0
        self._last_operation_at = None
        self._clear_count = 0
        self.session_id = None


# Process-global registry: one _FunctionStats per function identifier.
# Session IDs are derived from process UUID + module.qualname and are thus
# stable across re-decorations — a fresh counter object per decoration
# (factory / per-call patterns) would send counters that go backwards under
# an unchanged session ID, which the server's anti-replay validation rejects
# by silently stripping the session tag. Strong references are deliberate:
# counters must outlive any individual wrapper so a rebuilt wrapper
# continues the same monotonic sequence.
_function_stats_registry: dict[str, _FunctionStats] = {}
_function_stats_registry_lock = threading.Lock()


def _get_function_stats(function_identifier: str, l1_enabled: bool) -> _FunctionStats:
    """Get or create the shared stats tracker for a function identifier.

    Re-decorating the same function reuses the existing tracker. The most
    recent decoration's l1_enabled wins: the flag only feeds the rate-limit
    classification header, and the newest decoration reflects the current
    configuration.
    """
    with _function_stats_registry_lock:
        stats = _function_stats_registry.get(function_identifier)
        if stats is None:
            stats = _FunctionStats(function_identifier=function_identifier, l1_enabled=l1_enabled)
            _function_stats_registry[function_identifier] = stats
        else:
            stats.l1_enabled = l1_enabled
        return stats


def _reset_stats_after_fork() -> None:
    """Reset every registered stats tracker in a newly forked child.

    The child inherits the parent's counters and cached session IDs;
    reporting them would either continue the parent's session from another
    process or reset counters under the parent's session ID — both corrupt
    server-side session telemetry. The child is single-threaded here, so
    wholesale lock replacement is safe.
    """
    global _function_stats_registry_lock
    _function_stats_registry_lock = threading.Lock()
    for stats in _function_stats_registry.values():
        stats._reset_for_new_process()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_stats_after_fork)


# Lazy logger initialization to avoid import-time container access
def logger():
    """Get the logger instance lazily."""
    return get_logger()


def create_cache_wrapper(
    func: F,
    config: Any = None,  # DecoratorConfig | None (avoid circular import)
    ttl: int | None = None,
    stale_ttl: int | None = None,
    namespace: str | None = None,
    # Serialization & Security
    serializer: Union[str, SerializerProtocol] = "default",  # type: ignore[name-defined]
    integrity_checking: bool = True,
    encryption: bool | None = None,
    tenant_extractor: TenantContextExtractor | None = None,
    single_tenant_mode: bool = False,
    deployment_uuid: str | None = None,
    master_key: str | SecretStr | None = None,
    encryption_fail_closed: bool | None = None,
    # Performance features
    refresh_ttl_on_get: bool = False,
    ttl_refresh_threshold: float = 0.5,
    fast_mode: bool = False,
    l1_enabled: bool = True,
    backend: Any = None,
    # Reliability features
    circuit_breaker: bool = True,
    backpressure: bool = True,
    max_concurrent_requests: int = 100,
    # Monitoring features
    collect_stats: bool = True,
    enable_tracing: bool = True,
    enable_structured_logging: bool = True,
    # Interop mode (interop/v1): explicit cross-SDK operation name (None = auto mode)
    interop: str | None = None,
    # L1-only mode flag
    _l1_only_mode: bool = False,
) -> F:
    """Create cache wrapper for a function with specified configuration.

    This is the core wrapper factory that creates the actual sync/async
    wrapper functions with all enterprise features configured.

    Args:
        func: Function to wrap with caching
        ttl: Cache time-to-live in seconds (None = no expiration)
        namespace: Cache key namespace prefix
        serializer: Serializer instance or name. Accepts either:
                   - String name: "default" (MessagePack), "arrow" (DataFrame zero-copy)
                   - SerializerProtocol instance: Custom serializer implementing the protocol
        encryption: Tri-state zero-knowledge encryption control (AES-256-GCM), orthogonal
                   to serializer - wraps ANY serializer with encryption.
                   - None (default): no intent stated. Plaintext when no master key is present;
                     ConfigurationError at construction when one is, from master_key= or
                     CACHEKIT_MASTER_KEY (see the activation table in
                     docs/features/zero-knowledge-encryption.md).
                   - True: force encryption ON (key inline or from CACHEKIT_MASTER_KEY).
                   - False: explicit per-function opt-out — never encrypts, even when
                     CACHEKIT_MASTER_KEY is set (issue #128).
        tenant_extractor: Optional tenant ID extractor for multi-tenant encryption.
                         Only used if encryption=True.
                         If None: single-tenant mode (tenant_id "default" unless deployment_uuid /
                         CACHEKIT_DEPLOYMENT_UUID is set).
                         If provided: multi-tenant mode (extracts tenant_id from function args/kwargs).
                         A failed extraction is logged and the result is not written to the cache
                         (no fallback to a shared key).
        single_tenant_mode: Explicitly enable single-tenant mode (requires encryption=True).
                           Mutually exclusive with tenant_extractor. Prevents accidental shared
                           keys in multi-tenant deployments by requiring explicit configuration.
        deployment_uuid: Optional explicit tenant_id override for single-tenant mode (validated
                        UUID). Falls back to CACHEKIT_DEPLOYMENT_UUID, then to the protocol literal
                        "default" — the cross-SDK default, so a py/rs/ts client on one master key
                        share ciphertext with no tenant configured at all.
        encryption_fail_closed: Tri-state tamper-failure policy. None (default) defers to
                        CACHEKIT_ENCRYPTION_FAIL_CLOSED (default False = fail open). True raises
                        DecryptionAuthenticationError to the caller on AES-GCM authentication
                        failure or key-fingerprint mismatch instead of silently recomputing.
        refresh_ttl_on_get: Refresh TTL on cache hit
        ttl_refresh_threshold: Refresh when TTL below this fraction
        fast_mode: Disable monitoring for maximum performance
        l1_enabled: Enable L1 in-memory cache. With encryption=True, L1 stores encrypted bytes
                   (decryption at read time only). Both L1+L2 support any combination with encryption
                   for both performance and security.
        backend: Optional backend (BaseBackend implementation). If None, resolved on first
                 call from set_default_backend() or the DI backend provider (env auto-detection).
                 Held for the wrapper's lifetime, so it must be safe to share across requests.
        circuit_breaker: Enable circuit breaker for fault tolerance. Its settings come from
                        config.circuit_breaker; without config= the breaker runs its defaults.
        backpressure: Enable backpressure control
        max_concurrent_requests: Max concurrent requests (backpressure)
        collect_stats: Enable statistics collection
        enable_tracing: Enable distributed tracing
        enable_structured_logging: Enable structured logging
        interop: interop/v1 cross-SDK operation name. When set, switches this
                function to canonical {namespace}:{operation}:{args_hash} keys
                and plain-MessagePack values shared byte-identically with
                cachekit-rs / cachekit-ts. Requires namespace; mutually
                exclusive with key= and fast_mode. None (default) = auto mode.

    Security Note:
        When encryption=True and tenant_extractor is provided: a failed extraction is logged
        and the result is not written to the cache. There is no fallback to a shared key.
        tenant_extractor is NOT a tenancy boundary — cache keys carry no tenant component, so
        two tenants calling with identical arguments address the same entry. Give each tenant
        its own namespace or deployment, or make the tenant id a keyword argument of the cached
        function so it is part of the args hash. See docs/features/zero-knowledge-encryption.md.
    """
    circuit_breaker_config: CircuitBreakerConfig | None = None  # None = reliability defaults

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
        stale_ttl = config.stale_ttl if stale_ttl is None else stale_ttl
        namespace = config.namespace if namespace is None else namespace
        serializer = config.serializer
        integrity_checking = config.integrity_checking
        refresh_ttl_on_get = config.refresh_ttl_on_get
        ttl_refresh_threshold = config.ttl_refresh_threshold
        backend = config.backend if backend is None else backend

        # L1 cache settings
        l1_enabled = config.l1.enabled
        l1_max_size_mb = config.l1.max_size_mb

        # Circuit breaker settings: the nested knobs configure the live (reliability) breaker
        circuit_breaker = config.circuit_breaker.enabled
        circuit_breaker_config = CircuitBreakerConfig(
            failure_threshold=config.circuit_breaker.failure_threshold,
            success_threshold=config.circuit_breaker.success_threshold,
            timeout_seconds=config.circuit_breaker.recovery_timeout,
            half_open_requests=config.circuit_breaker.half_open_requests,
        )

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
        master_key = hide_secret(config.encryption.master_key)
        encryption_fail_closed = config.encryption.fail_closed

        # Custom key function (escape hatch for complex types)
        custom_key_func = config.key

        # Interop mode (config carries it through DecoratorConfig validation)
        interop = config.interop if interop is None else interop
    else:
        custom_key_func = None
        l1_max_size_mb = None

    # Re-scope custom_key_func for closure
    if "custom_key_func" not in dir():
        custom_key_func = None

    # Fast mode: Disable monitoring overhead, keep performance features
    use_circuit_breaker = circuit_breaker and not fast_mode
    use_backpressure = backpressure and not fast_mode
    use_collect_stats = collect_stats and not fast_mode
    # use_enable_tracing = enable_tracing and not fast_mode  # Not used after CacheConfig removal
    use_enable_structured_logging = enable_structured_logging and not fast_mode

    # Initialize handler components
    # Pre-compute function hash at decoration time (50-200μs savings)
    from ..hash_utils import blake3_hash, function_hash

    func_hash = function_hash(f"{func.__module__}.{func.__qualname__}")

    # Rebind a str namespace to its exact str value before any use (LAB-6197). A str
    # subclass renders through its own __format__/__eq__/startswith: a (str, Enum) member
    # formats as "NS.USERS" on Python 3.11+ but "users" on 3.10, which split registry ids and
    # key= keys across versions and could slip a crafted value past the "ck" check below.
    # _legacy_namespace keeps the pre-fix f-string rendering for the registry drain below.
    _legacy_namespace: str | None = None
    if isinstance(namespace, str):
        _legacy_namespace = f"{namespace}"
        namespace = str.__str__(namespace)

    # INTEROP MODE (interop/v1, protocol spec/interop-mode.md): validate loudly at
    # decoration time. These checks also cover direct create_cache_wrapper callers
    # that bypass DecoratorConfig validation. Rebinds interop to the exact str value it
    # checked; namespace is already exact from the block above.
    _interop_sig: inspect.Signature | None = None
    if interop is not None:
        try:
            interop, namespace = validate_interop_config(interop, namespace, has_custom_key=custom_key_func is not None)
        except InteropError as e:
            raise ConfigurationError(str(e)) from e
        if fast_mode:
            raise ConfigurationError(
                "interop mode and fast_mode are mutually exclusive: fast-mode keys are not the canonical interop/v1 key format."
            )
        if _l1_only_mode:
            raise ConfigurationError(
                "interop mode requires a shared backend (backend=None is L1-only, in-process): "
                "L1-only mode stores raw Python objects, so the cross-SDK value contract "
                "(plain MessagePack, closed data model) would silently not be enforced."
            )
        # Backend known at decoration time -> guard now; lazily-resolved backends
        # are re-checked per call (see the wrappers below).
        ensure_interop_backend_compatible(backend)
        _interop_sig = inspect.signature(func)

    # Key registry id: names this function's server-side tracking set on a KeyTrackableBackend.
    # 64-bit hash, not func_hash's 32: a registry collision makes one function's invalidation
    # drain another's keys. namespace=None and namespace="default" write different auto-mode
    # keys, so they get different sets (None -> empty segment). The "ck" namespace is reserved:
    # a key written under it could take the ck:reg: shape and overwrite a tracking set.
    if namespace == "ck" or (namespace or "").startswith("ck:"):
        raise ConfigurationError("namespace 'ck' (and 'ck:*') is reserved for cachekit's key registry")
    _registry_hash = blake3_hash(f"{func.__module__}.{func.__qualname__}", digest_size=8)
    _registry_id = f"ck:reg:{namespace if namespace is not None else ''}:{_registry_hash}"
    # Pre-fix releases named the set with the namespace's f-string rendering. Auto-mode keys
    # did not move, so entries tracked under the old name are still served; the no-args drain
    # empties that set too, or they would outlive invalidate_cache() (LAB-5288 precedent).
    # Interop is skipped: 0.20.0 shipped the registry with interop's exact-str rebind, so no
    # release wrote a non-exact interop set. This drains the set name 0.20.x wrote: remove
    # it only in a major release whose notes declare upgrades from 0.20.x unsupported (the
    # rule get_legacy_cache_key follows).
    _legacy_registry_id = (
        f"ck:reg:{_legacy_namespace}:{_registry_hash}"
        if interop is None and _legacy_namespace is not None and _legacy_namespace != namespace
        else None
    )

    # ENCRYPTION + L1-ONLY (LAB-4665, protocol spec/intent-presets.md § L1 Posture rule 3:
    # "secure MUST hold only ciphertext" in L1). Encryption is a serializer layer, and the
    # L1-only ObjectCache path below never serializes — so @cache.secure(backend=None)
    # reported encryption.enabled=True while holding plaintext. Refuse at decoration; a
    # "serialized L1 without L2" mode does not exist and a warning would keep the leak.
    # `encryption` is the pre-resolution tri-state: None is not refused here. With a master
    # key present, None is refused by CacheSerializationHandler instead (no stated intent,
    # L1-only included); with no key, it is plaintext, which L1-only stores correctly.
    _encrypting_serializer = isinstance(serializer, EncryptionWrapper) or (
        isinstance(serializer, str) and SERIALIZER_REGISTRY.get(serializer) is EncryptionWrapper
    )
    if _l1_only_mode and (encryption or _encrypting_serializer):
        raise ConfigurationError(
            "encryption requires a backend: backend=None is L1-only and stores raw Python "
            "objects, which cannot be ciphertext. Drop backend=None (the backend then "
            "resolves from set_default_backend(), else the one CACHEKIT_* selector set: "
            "CACHEKIT_API_KEY, CACHEKIT_REDIS_URL, CACHEKIT_MEMCACHED_SERVERS or "
            "CACHEKIT_FILE_CACHE_DIR, else REDIS_URL / localhost Redis; see "
            "docs/backends/README.md) or pass one explicitly to keep @cache.secure / "
            "encryption=True."
        )

    # Store backend and handler type for consistent access
    # If explicit backend provided, use it; otherwise get from provider on first use
    _backend = backend if backend is not None else None

    # ---- Backed-mode stale-while-revalidate (LAB-381, spec/saas-api.md#stale-while-revalidate) ----
    # Past-TTL SWR: the backend keeps serving an entry for a stale-grace window past
    # its fresh TTL and labels the read stale; we return the stale value immediately
    # and re-run the wrapped function in the background. Requires an SWR-capable
    # backend (CachekitIO — the server signals freshness on read).
    _max_total_ttl = 2_592_000  # 30-day storage cap, shared with the stale window (spec)

    def _l2_freshness_capable() -> bool:
        """Freshness capability of the backend as RESOLVED so far. The read paths
        call this at call time because provider-backed decorators (no backend=
        argument, e.g. @cache.production with CACHEKIT_API_KEY set) resolve
        _backend on first call (LAB-557). Class-level check: an instance-level
        hasattr reads Mock/proxy objects as capable."""
        return _backend is not None and supports_swr(_backend)

    # Decoration-time snapshot for SWR activation (explicit stale_ttl validation,
    # io()'s swr_by_default): a stale window fails at decoration as documented, so
    # provider-backed decorators get the read-side bound but cannot enable SWR.
    _l2_swr_capable_at_decoration = _l2_freshness_capable()
    _stale_ttl: int | None = None
    if stale_ttl is not None:
        # Type-check BEFORE the zero opt-out test: bool is an int subclass and
        # False == 0 == 0.0, so without this ordering True silently means a
        # 1-second window and False/0.0 silently opt out unvalidated.
        if isinstance(stale_ttl, bool) or not isinstance(stale_ttl, int) or stale_ttl < 0:
            raise ConfigurationError(f"stale_ttl must be a non-negative integer, got {stale_ttl!r}")
        if stale_ttl != 0:  # integer 0 = explicit SWR opt-out
            if ttl is None or ttl <= 0:
                raise ConfigurationError("stale_ttl requires a positive ttl (the stale window starts where freshness ends)")
            if ttl + stale_ttl > _max_total_ttl:
                raise ConfigurationError(f"ttl + stale_ttl must not exceed {_max_total_ttl} seconds (30-day storage cap)")
            if not _l2_swr_capable_at_decoration:
                raise ConfigurationError(
                    "stale_ttl requires an SWR-capable backend (CachekitIO) known at decoration time. "
                    "Other backends have no read-side freshness signal — remove stale_ttl, switch to "
                    "@cache.io, or pass backend=CachekitIOBackend() explicitly."
                )
            _stale_ttl = stale_ttl
    elif (
        config is not None
        and getattr(config, "swr_by_default", False)
        and ttl is not None
        and ttl > 0
        and _l2_swr_capable_at_decoration
    ):
        # Preset default (io()): stale window = ttl, capped so the total stays
        # within the 30-day bound. stale_ttl=0 opts out explicitly. A ttl at or
        # above the cap leaves no window headroom -> no default (never negative).
        _default_window = min(ttl, _max_total_ttl - ttl)
        _stale_ttl = _default_window if _default_window > 0 else None

    _l2_swr_active = _stale_ttl is not None

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
        encryption_fail_closed=encryption_fail_closed,
        interop_mode=interop is not None,
    )

    # Create cache handler strategy (initialized with actual Redis client when first used)
    cache_handler_strategy = None

    operation_handler = CacheOperationHandler(serialization_handler, key_generator, cache_handler=cache_handler_strategy)

    # Configuration validation (no CacheConfig object needed - using direct variables)
    # Validate encryption configuration if encryption is enabled
    from ..config import validate_encryption_config

    validate_encryption_config(encryption, master_key=master_key)

    # Note: L1 cache + encryption is supported.
    # L1 stores encrypted bytes (not plaintext), decryption happens at read time only.
    # This maintains security while enabling sub-microsecond cache hits.

    # Initialize feature orchestrator using EXISTING reliability/monitoring modules
    features = FeatureOrchestrator(
        namespace=namespace or "default",
        circuit_breaker_enabled=use_circuit_breaker,
        circuit_breaker_config=circuit_breaker_config,
        backpressure_enabled=use_backpressure,
        backpressure_config={"max_concurrent": max_concurrent_requests} if use_backpressure else None,
        collect_stats=use_collect_stats,
        enable_structured_logging=use_enable_structured_logging,
    )

    # Corrupt/tampered L2 entries are evicted inside get_cached_value(_async); this hook
    # makes both sync and async paths emit the same cache_get_deserialize metric (#159).
    # The entry is a miss, not a backend failure, so it must not count toward the
    # circuit breaker: a refused plaintext entry during a plaintext→encrypted migration,
    # or a handful planted by a backend writer, would otherwise open it and switch off
    # caching for this function.
    def _on_l2_deserialize_error(error: Exception, key: str) -> None:
        features.handle_cache_error(
            error=error,
            operation="cache_get_deserialize",
            cache_key=key,
            namespace=namespace or "default",
            duration_ms=0.0,
            count_toward_breaker=False,
        )

    operation_handler.on_deserialize_error = _on_l2_deserialize_error

    # Initialize L1 cache if enabled. The per-decorator budget (config.l1.max_size_mb)
    # applies only when this namespace's cache is first created — namespaces share one
    # L1Cache, so give functions with distinct budgets distinct namespaces (issue #163).
    _l1_cache = get_l1_cache(namespace or "default", max_size_mb=l1_max_size_mb) if l1_enabled else None

    # L1-only mode: use ObjectCache for raw Python object storage (no serialization).
    # This preserves types (tuples, sets, frozensets) that MessagePack would degrade.
    # L1CacheConfig is honored here (#207): max_size_mb bounds bytes (best-effort
    # object-graph estimate, not entry count) and swr_enabled/swr_threshold_ratio
    # drive background refresh via get_with_swr.
    from ..config.nested import L1CacheConfig
    from ..config.singleton import get_settings

    _l1_config: L1CacheConfig = config.l1 if config is not None else L1CacheConfig()
    # max_size_mb=None inherits the global CACHEKIT_L1_MAX_SIZE_MB setting (issue #163),
    # mirroring L1CacheManager's resolution for the L2-backed path.
    _l1_budget_mb: int = _l1_config.max_size_mb if _l1_config.max_size_mb is not None else get_settings().l1_max_size_mb
    # l1_enabled already merges the decorator param with config.l1.enabled (see
    # config handling above) — with it False in L1-only mode there is no cache
    # at all and the wrappers call the function directly.
    _object_cache: ObjectCache | None = (
        ObjectCache(
            max_entries=None,
            max_size_bytes=_l1_budget_mb * 1024 * 1024,
            swr_threshold_ratio=_l1_config.swr_threshold_ratio,
        )
        if _l1_only_mode and l1_enabled
        else None
    )

    # SWR needs a TTL: freshness is measured against ttl * swr_threshold_ratio.
    # With ttl=None entries never go stale, so there is nothing to revalidate.
    _l1_swr_active = _object_cache is not None and _l1_config.swr_enabled and ttl is not None and ttl > 0

    # Background revalidation machinery — mirrors the L1-only SWR shapes below:
    # per-key in-flight dedup + a bounded slot pool so a burst of distinct stale
    # keys can't spawn unbounded work. Cross-client single-flight rides the
    # backend's async lock as a non-blocking lease (contested = serve stale, no
    # wait, no retry — _try_acquire_lock already treats 409 AND 200+null as
    # contested, LAB-240). The lease is best-effort per spec.
    _l2_swr_inflight: set[str] = set()
    _l2_swr_tasks: set[asyncio.Task[None]] = set()
    _l2_swr_slots = threading.BoundedSemaphore(_L2_SWR_MAX_CONCURRENT_REFRESHES)
    _l2_swr_pid = os.getpid()  # owner process — a forked child must not inherit scheduler state
    _l2_swr_lease_seconds = 30.0  # same server-side lease bound as the miss-path lock

    def _put_l1(cache_key: str, serialized_data: Any, l1_ttl: int | None) -> None:
        """The only L1 write in this wrapper: put the serialized bytes (str payloads encoded),
        then record the key in _cached_keys.

        Recording after the put makes "in L1 => in _cached_keys" hold by construction, so a
        whole-function invalidation that trims _cached_keys before evicting L1 cannot miss an
        entry. The key is recorded even when there is nothing to put (L1 disabled, a streamed
        value): _cached_keys also drives the L2 deletes of process-local invalidation.

        Every open _watch_records() set is told about the key BEFORE it is recorded, so a
        concurrent whole-function invalidation cannot drop a record it was not told about.
        """
        if _l1_cache and serialized_data:
            _b = serialized_data.encode("utf-8") if isinstance(serialized_data, str) else serialized_data
            _l1_cache.put(cache_key, _b, redis_ttl=l1_ttl)
        entry = (_l2_scope(), cache_key)
        if _drain_watches:
            for watch in _drain_watches.copy().values():
                watch.add(entry)
        _cached_keys.add(entry)

    def _is_trackable() -> bool:
        """Whether the backend as RESOLVED so far keeps a server-side key registry.

        Asked at call time: provider-backed decorators resolve _backend at first call, and a
        fresh process whose first act is invalidate_cache() must still drain the registry.
        """
        return _backend is not None and supports_key_tracking(_backend)

    def _track_and_record(cache_key: str) -> None:
        """Record a SUCCESSFUL L2 write in the backend's key registry. Never raises.

        Call only after the L2 write returned success, and never inline on an event loop
        (async callers use asyncio.to_thread). A key whose tracking fails stays in
        _cached_keys, and this process's next drain by the same tenant deletes it from there.
        Other processes' drains cannot see it, so the failure is a WARNING — throttled to one per
        _WARN_INTERVAL_SECONDS, carrying the count of failures since the last one.
        """
        if not _is_trackable():
            return
        try:
            _backend.track_key(_registry_id, cache_key)  # type: ignore[union-attr]
        except Exception as e:
            failures = _track_warn.claim()
            if not failures:
                _logger.debug("Key tracking failed for %s: %s", redact_cache_key(cache_key), redact_error_for_log(e))
                return
            _logger.warning(
                "Key tracking failed in registry %s (failures since the last warning: %d); other processes' "
                "drains miss those keys until their TTL. Latest key %s: %s",
                redact_cache_key(_registry_id),
                failures,
                redact_cache_key(cache_key),
                redact_error_for_log(e),
            )

    async def _track_and_record_async(cache_key: str) -> None:
        """_track_and_record off the event loop, skipping the thread hop when nothing tracks."""
        if _is_trackable():
            await asyncio.to_thread(_track_and_record, cache_key)

    def _l1_backfill_ttl(fresh_for: int | None) -> Any:
        """L1 TTL for a backfill from an L2 read, bounded by the server's remaining
        freshness (LAB-557, spec/saas-api.md#remaining-freshness).

        Unbounded, a read near the end of the server's freshness window restarts
        the clock and serves from L1 as fresh past the server's fresh_until.
        fresh_for=None (pre-signal server / no expiry / non-SWR backend) keeps
        legacy behavior; fresh_for=0 makes L1Cache.put skip the entry entirely
        (effective expiry <= now — nothing fresh remains to record).

        The signal may only ever SHORTEN the L1 lifetime: with ttl=None the
        legacy baseline is L1Cache's own default (DEFAULT_L1_TTL_SECONDS), so a
        long server remainder is clamped to it — returning raw fresh_for would
        EXTEND local service up to the 30-day cap and turn the bound into the
        very freshness-extension it exists to prevent (expert-panel finding,
        CWE-613: server-side DELETE-as-revocation relies on the ≤300s ageout).
        """
        if fresh_for is None:
            return ttl
        return min(DEFAULT_L1_TTL_SECONDS, fresh_for) if ttl is None else min(ttl, fresh_for)

    async def _l2_double_check(
        cache_key: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> tuple[CacheHit | None, bool, int | None]:
        """Post-lock L2 double-check read, freshness-aware on a capable backend
        (LAB-557): a hit found after a lock wait gets the same stale-exclusion
        and remaining-freshness bound on its L1 BACKFILL as the primary hit path
        (the entry another client just wrote is usually full-window fresh, but
        an L2 read error on the primary path can land here with the OLD entry
        still live and late in its window). Deliberate asymmetry: a stale hit
        here is served WITHOUT scheduling a revalidation — spec-permitted
        (subsequent stale reads MAY re-trigger), and this path is already a
        double-fault rarity. Returns (cached_result, is_stale, fresh_for);
        miss/error = (None, False, None), same degradation contract as
        get_cached_value_async.
        """
        if _l2_freshness_capable():
            hit = await operation_handler.get_cached_value_with_freshness_async(cache_key, args, kwargs)
            return hit if hit is not None else (None, False, None)
        return await operation_handler.get_cached_value_async(cache_key, args=args, kwargs=kwargs), False, None

    def _l1_backfill_from_l2(cache_key: str, cached_data: Any, is_stale: bool, fresh_for: int | None) -> None:
        """Backfill L1 from an L2 hit's raw envelope, holding both LAB-557
        invariants at every call site in lockstep: a stale-labelled hit is never
        recorded (spec: local caches MUST NOT record stale as fresh), and a
        fresh hit's local lifetime is bounded by _l1_backfill_ttl.

        Best-effort: the hit is already decoded, so the one refusal L1Cache.put
        documents — TypeError on a non-bytes envelope from an out-of-contract
        backend — is logged and skipped; every caller sits inside an `except
        Exception` that would otherwise demote the served hit into a recompute on
        each call (LAB-348). Anything else is an L1 bug and propagates.
        """
        if not (_l1_cache and cache_key and cached_data and not is_stale):
            return
        try:
            _put_l1(cache_key, cached_data, _l1_backfill_ttl(fresh_for))
        except TypeError as exc:
            logger().warning(f"L1 backfill skipped for {redact_cache_key(cache_key)}: {redact_error_for_log(exc)}")

    def _record_l2_hit_async(size_bytes: int, get_duration_ms: float) -> None:
        """Record the telemetry for an async L2 hit — the uncontended read and
        both post-lock double-check hits (LAB-3769) share this so a
        thundering-herd hit is never invisible to cache_operations_total /
        cache_info() just because it arrived via the lock's double-check.

        size_bytes is the length CacheHit already measured (LAB-3757), not a
        recompute from the envelope. CacheHit.size_bytes is set on every hit
        including the mmap fast path, where envelope is None and there are no
        bytes to measure -- so this label never depends on holding the bytes.

        Best-effort, for the same reason _l1_backfill_from_l2 is (LAB-348):
        every call site sits inside an `except Exception` that falls through to
        a recompute, so a throwing metrics collector would silently turn a hit
        already in hand into a full recompute — under exactly the stampede the
        lock exists to absorb. Telemetry never costs a served hit.

        The two clauses are deliberately split rather than narrowed to the
        collector's own error types. Narrowing does NOT fail fast here: an
        unexpected raise would land in the caller's `except Exception`, which
        logs at DEBUG under "Double-check cache failed after lock acquisition"
        and recomputes — quieter than this, misattributed, and a recompute per
        contended hit. So the unexpected case is caught too, and made loud
        instead: ERROR with the exception type named, which is the signal a
        narrow clause was meant to produce.
        """
        try:
            # Local stat first, external collector second: this is pure arithmetic
            # under a lock and cannot realistically refuse, whereas the collector can
            # — and once the hit is served anyway, a collector refusal must not leave
            # cache_info() omitting a hit the caller was handed. Losing the counter to
            # someone else's registry error is the same invisibility this helper exists
            # to remove.
            _stats.record_l2_hit(get_duration_ms)
            features.set_operation_context("get", duration_ms=get_duration_ms)
            features.record_success()
            if features.collect_stats:
                features.record_cache_operation(
                    operation="get",
                    namespace=namespace or "default",
                    serializer="rust",
                    success=True,
                    duration_ms=get_duration_ms,
                    size_bytes=size_bytes,
                )
        except (ValueError, TypeError) as exc:
            # The collector's documented refusals: duplicated timeseries, a label set
            # that disagrees with the registered metric, a non-numeric observation.
            logger().warning(f"L2 hit telemetry skipped: {redact_error_for_log(exc)}")
        except Exception as exc:
            # Not a collector refusal — a bug in the telemetry stack. Still must not
            # cost the served hit, so surface it at ERROR with its type rather than
            # letting the caller demote this hit into a recompute.
            logger().error(f"L2 hit telemetry failed unexpectedly ({type(exc).__name__}): {redact_error_for_log(exc)}")

    def _warn_refresh(throttle: _WarnThrottle, event: str, cache_key: str, exc: BaseException, reason: str = "") -> None:
        """Log a background refresh that failed or never ran: a throttled WARNING, DEBUG between.

        The caller was already served the cached value and must never see the failure (spec:
        revalidation failure must never surface to callers), so this line is the only signal.
        The function is named by its digest, like the key: a dynamically created function's
        __qualname__ can carry caller data (CWE-532).
        """
        failures = throttle.claim()
        if not failures:
            _logger.debug("%s for %s%s: %s", event, redact_cache_key(cache_key), reason, redact_error_for_log(exc))
            return
        _logger.warning(
            "%s in function %s (%d since the last warning)%s; callers keep the cached value until it expires. Latest key %s: %s",
            event,
            redact_cache_key(function_identifier),
            failures,
            reason,
            redact_cache_key(cache_key),
            redact_error_for_log(exc),
        )

    def _l2_swr_try_begin(cache_key: str) -> bool:
        """Claim a revalidation slot for this key; False = already in flight or at capacity.

        The check-then-add on _l2_swr_inflight is not atomic across OS threads; a rare
        duplicate schedule is benign (the backend lease or last-write-wins between two
        freshly computed values absorbs it — spec explicitly allows duplicates).
        """
        nonlocal _l2_swr_inflight, _l2_swr_tasks, _l2_swr_slots, _l2_swr_pid
        if _l2_swr_pid != os.getpid():
            # Forked child: inherited in-flight keys would never clear (the parent
            # threads that call _l2_swr_end don't survive fork), permanently starving
            # those keys of revalidation, and the inherited semaphore may carry
            # consumed slots or a lock captured mid-acquire (even a non-blocking
            # acquire would then hang). Replace wholesale. Sibling threads racing
            # this swap are as benign as the dedup race above: worst case one
            # duplicate schedule or one lost slot, both self-healing.
            _l2_swr_inflight = set()
            _l2_swr_tasks = set()
            _l2_swr_slots = threading.BoundedSemaphore(_L2_SWR_MAX_CONCURRENT_REFRESHES)
            _l2_swr_pid = os.getpid()
        if cache_key in _l2_swr_inflight:
            return False
        if not _l2_swr_slots.acquire(blocking=False):
            return False
        _l2_swr_inflight.add(cache_key)
        return True

    def _l2_swr_end(cache_key: str) -> None:
        _l2_swr_inflight.discard(cache_key)
        _l2_swr_slots.release()

    async def _l2_swr_recompute_store_async(cache_key: str, call_args: tuple[Any, ...], call_kwargs: dict[str, Any]) -> None:
        result = await func(*call_args, **call_kwargs)
        serialized_data = operation_handler.serialization_handler.serialize_data(
            result, call_args, call_kwargs, cache_key=cache_key
        )
        stored = await operation_handler.cache_handler.set_async(  # type: ignore[attr-defined]
            cache_key, serialized_data, ttl=ttl, stale_ttl=_stale_ttl
        )
        # Refresh L1 with the new fresh bytes (mirrors the miss-path store).
        _put_l1(cache_key, serialized_data, ttl)
        if stored:
            await _track_and_record_async(cache_key)

    async def _l2_swr_revalidate_async(cache_key: str, call_args: tuple[Any, ...], call_kwargs: dict[str, Any]) -> None:
        """Background revalidation for async functions. Failures never reach the caller, only
        the log (_warn_refresh): the caller already got the stale value; the entry hard-expires
        at evict_at and the next request takes the ordinary synchronous miss path (spec
        degradation)."""
        try:
            if supports_locking(_backend):
                async with _backend.acquire_lock(cache_key, timeout=_l2_swr_lease_seconds, blocking_timeout=None) as got_lease:
                    if not got_lease:
                        return  # another client is revalidating — stale already served
                    await _l2_swr_recompute_store_async(cache_key, call_args, call_kwargs)
            else:
                await _l2_swr_recompute_store_async(cache_key, call_args, call_kwargs)
        except Exception as exc:  # noqa: BLE001 — spec: revalidation failure must never surface to callers
            _warn_refresh(_refresh_failed_warn, "SWR revalidation failed", cache_key, exc)
        finally:
            _l2_swr_end(cache_key)

    def _l2_swr_revalidate_sync(cache_key: str, call_args: tuple[Any, ...], call_kwargs: dict[str, Any]) -> None:
        """Background revalidation for sync functions (daemon thread).

        ponytail: per-process single-flight only — the distributed lease API is
        async-only; the lease is a spec SHOULD and duplicate revalidation is benign
        (last-write-wins between fresh values). Add a sync lease if cross-client
        duplicate recomputes ever measurably matter.
        """
        try:
            result = func(*call_args, **call_kwargs)
            serialized_data = operation_handler.serialization_handler.serialize_data(
                result, call_args, call_kwargs, cache_key=cache_key
            )
            stored = operation_handler.cache_handler.set(  # type: ignore[attr-defined]
                cache_key, serialized_data, ttl=ttl, stale_ttl=_stale_ttl
            )
            _put_l1(cache_key, serialized_data, ttl)
            if stored:
                _track_and_record(cache_key)
        except Exception as exc:  # noqa: BLE001 — spec: revalidation failure must never surface to callers
            _warn_refresh(_refresh_failed_warn, "SWR revalidation failed", cache_key, exc)
        finally:
            _l2_swr_end(cache_key)

    def _l2_swr_schedule(cache_key: str, call_args: tuple[Any, ...], call_kwargs: dict[str, Any], *, is_async: bool) -> None:
        """Kick off background revalidation for a stale hit (at most one per key).

        Arguments are deep-copied before scheduling (same contract as the L1-only
        SWR path): the cache key was computed from the arguments at call time, and
        the refresh runs later — it must not see mutations the caller makes after
        receiving the stale value, or it would store the new state under the old
        key. Not-copyable arguments skip the refresh (stale keeps being served; a
        later hit retries). Any scheduling failure releases the slot so the key
        never becomes permanently unrevalidatable.
        """
        if not _l2_swr_try_begin(cache_key):
            return
        try:
            call_args, call_kwargs = copy.deepcopy((call_args, call_kwargs))
        except Exception as exc:
            _l2_swr_end(cache_key)
            _warn_refresh(_refresh_skipped_warn, "SWR revalidation skipped", cache_key, exc, _NOT_DEEP_COPYABLE)
            return
        try:
            if is_async:
                task = asyncio.create_task(_l2_swr_revalidate_async(cache_key, call_args, call_kwargs))
                _l2_swr_tasks.add(task)  # strong ref until done (same pattern as _l1_swr_tasks)
                task.add_done_callback(_l2_swr_tasks.discard)
            else:
                # Snapshot the caller's context (captured in-request, where e.g. a
                # ContextVarExtractor's tenant var is set) so the daemon thread sees
                # the same contextvars the async path inherits via create_task —
                # without this, encryption tenant extraction fails off-request and
                # the refresh silently no-ops (LAB-381 panel, MAJ).
                ctx = contextvars.copy_context()
                threading.Thread(
                    target=ctx.run,
                    args=(_l2_swr_revalidate_sync, cache_key, call_args, call_kwargs),
                    daemon=True,
                    name="cachekit-swr-revalidate",  # no key material (CWE-532)
                ).start()
        except Exception as exc:  # e.g. Thread.start() RuntimeError under resource pressure
            _l2_swr_end(cache_key)
            _warn_refresh(_refresh_unstarted_warn, "SWR revalidation could not be scheduled", cache_key, exc)

    # Create per-function statistics tracker with lazy session ID generation
    # Session ID format: "{process_uuid}:{module}.{function_name}"
    # Generated lazily on first use or regenerated after cache_clear()
    function_identifier = f"{func.__module__}.{func.__qualname__}"

    # Detect whether the wrapped function accepts parameters.
    # Used to distinguish "invalidate the zero-arg entry" from "invalidate ALL entries".
    _func_has_params = bool(inspect.signature(func).parameters)

    def _interop_cache_key(call_args: tuple[Any, ...], call_kwargs: dict[str, Any]) -> str:
        """Interop/v1 key for this call: {namespace}:{operation}:{args_hash}.

        Raises InteropError on out-of-model arguments — interop keygen never
        degrades to uncached execution (the cross-SDK contract requires loud
        rejection; a value that hashes here and errors on another SDK is a
        silent-consistency bug).
        """
        assert _interop_sig is not None and interop is not None and namespace is not None  # noqa: S101
        flat = bind_flat_args(_interop_sig, call_args, call_kwargs)
        return generate_interop_key(namespace, interop, flat)

    # The generated key is the only one carrying a serializer code, so it alone has a
    # pre-0.20.0 twin. One flag, read by both functions below, so the twin can never be
    # computed for a key the write path did not generate.
    _generated_key_mode = interop is None and custom_key_func is None and not fast_mode

    def _resolve_cache_key(call_args: tuple[Any, ...], call_kwargs: dict[str, Any]) -> str:
        """Single key derivation shared by the read/write and invalidate paths (LAB-4387)."""
        # Standard key generation with type-aware handling
        if _generated_key_mode:
            return operation_handler.get_cache_key(func, call_args, call_kwargs, namespace, integrity_checking)
        # Interop: decoration rejects it together with key= or fast_mode, so it never
        # competes with the branches below
        if interop is not None:
            return _interop_cache_key(call_args, call_kwargs)
        # Custom key function (escape hatch for complex types)
        if custom_key_func is not None:
            custom_key = custom_key_func(*call_args, **call_kwargs)
            if not isinstance(custom_key, str):
                raise TypeError(f"key function must return str, got {type(custom_key).__name__}")
            return f"{namespace or 'default'}:{custom_key}"
        # fast_mode: the only mode _generated_key_mode leaves once interop and key= are out.
        # Minimal key generation - no string formatting overhead (10-50μs savings)
        from ..hash_utils import cache_key_hash

        return (namespace or "default") + ":" + func_hash + ":" + cache_key_hash(str(call_args) + str(call_kwargs))

    def _resolve_invalidation_keys(call_args: tuple[Any, ...], call_kwargs: dict[str, Any]) -> list[str]:
        """The key _resolve_cache_key derives, plus its pre-0.20.0 twin on the generated-key path."""
        cache_key = _resolve_cache_key(call_args, call_kwargs)
        if not _generated_key_mode:
            return [cache_key]
        legacy_key = operation_handler.get_legacy_cache_key(func, call_args, call_kwargs, namespace, integrity_checking)
        return [cache_key] if legacy_key == cache_key else [cache_key, legacy_key]

    # Track the cache keys this process wrote or read for this function (for no-args
    # invalidation). Key normalization (hashing of long keys) makes prefix matching
    # unreliable, so actual keys are tracked. Keys written only by other processes are not in
    # this set; on a KeyTrackableBackend the calling tenant's server-side registry reaches
    # them (_drain_all).
    # The set is not bounded: an entry is dropped only by invalidation, never on TTL expiry.
    # Each entry is (L2 key prefix, cache key): a tenant-scoped backend holds one L2 entry
    # per tenant under the same cache key, and an invalidation may delete — and stop
    # tracking — only the calling tenant's (LAB-4773).
    _cached_keys: set[tuple[str, str]] = set()

    def _l2_scope() -> str:
        """Key prefix the resolved backend applies in THIS context ("" when it applies none)."""
        return getattr(_backend, "key_prefix", None) or ""

    def _evict(key: str | None) -> None:
        """Another process's invalidation, from the listener thread: evict ``key``, or every key
        this wrapper recorded (``None``), from L1.

        Never trims _cached_keys. The event is tenant-blind and can race a re-record of the same key
        (see _watch_records), so a trim could drop this process's only record of a live L2 entry;
        a record left behind costs one redundant delete later.
        """
        if _l1_cache is None:  # never registered without one
            return
        if key is not None:
            _l1_cache.invalidate(key)
        else:
            _l1_cache.invalidate_many({cached for _, cached in set(_cached_keys)})

    # One set per open _watch_records(), keyed by (pid, token): _put_l1 adds every entry it
    # records to each, so a whole-function invalidation spares entries re-recorded meanwhile.
    _drain_watches: dict[tuple[int, object], set[tuple[str, str]]] = {}
    # Background-failure WARNING throttles, one per kind, so a frequent kind never hides a rarer one.
    _track_warn = _WarnThrottle()  # key tracking failed
    _refresh_failed_warn = _WarnThrottle()  # a background refresh raised
    _refresh_skipped_warn = _WarnThrottle()  # arguments not deep-copyable: refresh-ahead cannot run
    _refresh_unstarted_warn = _WarnThrottle()  # the refresh thread or task could not be started

    # Shared stats tracker from the process-global registry (session ID lazy-initialized
    # on first use). Re-decoration reuses the same counters — see _get_function_stats.
    _stats = _get_function_stats(function_identifier, l1_enabled)

    # L1-only SWR: strong refs to in-flight refresh tasks. asyncio only keeps weak
    # refs to tasks, so a fire-and-forget refresh could be GC'd mid-flight otherwise.
    _l1_swr_tasks: set[asyncio.Task[None]] = set()

    # Per-key suppression alone doesn't bound refresh concurrency: a workload
    # crossing the SWR threshold on many distinct keys at once would spawn one
    # task/thread per key. This semaphore caps in-flight refreshes per wrapped
    # function; at capacity the refresh is skipped (stale keeps being served)
    # and a later qualifying hit retries.
    _l1_swr_slots = threading.BoundedSemaphore(_L1_SWR_MAX_CONCURRENT_REFRESHES)
    _l1_swr_pid = os.getpid()  # owner process — see _l2_swr_try_begin's fork note

    def _l1_swr_acquire(
        cache_key: str, version: int, call_args: tuple[Any, ...], call_kwargs: dict[str, Any]
    ) -> tuple[Any, Any] | None:
        """Reserve a refresh slot and snapshot the live arguments.

        The cache key was computed from the arguments as they were at call
        time; the refresh runs later, so it must not see mutations the caller
        makes after receiving the stale value (it would store the new state
        under the old key). Returns deep-copied (args, kwargs), or None when at
        capacity or the arguments can't be copied — in both cases this exact
        refresh (version) is cancelled so a later call retries, and the caller
        must not schedule a refresh.
        """
        nonlocal _l1_swr_tasks, _l1_swr_slots, _l1_swr_pid
        assert _object_cache is not None  # noqa: S101 - only called when scheduling a refresh
        if _l1_swr_pid != os.getpid():
            # Forked child: same wholesale reset as _l2_swr_try_begin — the inherited
            # semaphore is parent state (consumed slots, possibly a poisoned lock).
            _l1_swr_tasks = set()
            _l1_swr_slots = threading.BoundedSemaphore(_L1_SWR_MAX_CONCURRENT_REFRESHES)
            _l1_swr_pid = os.getpid()
        if not _l1_swr_slots.acquire(blocking=False):
            _object_cache.cancel_refresh(cache_key, version)
            return None
        try:
            return copy.deepcopy((call_args, call_kwargs))
        except Exception as exc:
            _l1_swr_slots.release()
            _object_cache.cancel_refresh(cache_key, version)
            _warn_refresh(_refresh_skipped_warn, "L1-only SWR refresh skipped", cache_key, exc, _NOT_DEEP_COPYABLE)
            return None

    def _l1_swr_task_done(task: asyncio.Task[None], cache_key: str) -> None:
        _l1_swr_tasks.discard(task)
        if task.cancelled():  # e.g. loop shutdown: nothing failed
            return
        exc = task.exception()
        if exc is not None:
            _warn_refresh(_refresh_failed_warn, "L1-only SWR refresh failed", cache_key, exc)

    async def _l1_swr_refresh_async(
        cache_key: str, version: int, call_args: tuple[Any, ...], call_kwargs: dict[str, Any]
    ) -> None:
        """Background SWR refresh for async functions in L1-only mode.

        Only ever scheduled with a slot held via _l1_swr_acquire; releases it.
        """
        assert _object_cache is not None and ttl is not None  # noqa: S101 - _l1_swr_active guarantees both
        try:
            try:
                result = await func(*call_args, **call_kwargs)
            except BaseException:
                _object_cache.cancel_refresh(cache_key, version)  # let a later call retry
                raise  # logged by _l1_swr_task_done
            _object_cache.complete_refresh(cache_key, version, result, ttl=ttl)
        finally:
            _l1_swr_slots.release()

    def _l1_swr_refresh_sync(cache_key: str, version: int, call_args: tuple[Any, ...], call_kwargs: dict[str, Any]) -> None:
        """Background SWR refresh for sync functions in L1-only mode (runs on a daemon thread).

        Only ever scheduled with a slot held via _l1_swr_acquire; releases it.
        """
        assert _object_cache is not None and ttl is not None  # noqa: S101 - _l1_swr_active guarantees both
        try:
            try:
                result = func(*call_args, **call_kwargs)
            except Exception as exc:
                _object_cache.cancel_refresh(cache_key, version)  # let a later call retry
                _warn_refresh(_refresh_failed_warn, "L1-only SWR refresh failed", cache_key, exc)
                return
            _object_cache.complete_refresh(cache_key, version, result, ttl=ttl)
        finally:
            _l1_swr_slots.release()

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

        cache_key = None  # Initialize to avoid UnboundLocalError

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
            cache_key = _resolve_cache_key(args, kwargs)
        except Exception as e:
            if interop is not None:
                # Interop/v1: out-of-model arguments MUST be rejected with an
                # error — never silently degrade to uncached execution.
                reset_current_function_stats(token)
                raise
            # Key generation failed - execute function without caching
            features.handle_cache_error(
                error=e,
                operation="key_generation",
                cache_key="<generation_failed>",
                namespace=namespace or "default",
                duration_ms=0.0,
                count_toward_breaker=False,  # pre-admission, never reached the backend
            )
            reset_current_function_stats(token)
            return func(*args, **kwargs)

        # L1-ONLY MODE: Store raw Python objects (no serialization).
        # Preserves types (tuples, sets, frozensets) that MessagePack would degrade.
        if _l1_only_mode and _object_cache is None:
            # L1 disabled in L1-only mode -> no cache anywhere; call through
            try:
                return func(*args, **kwargs)
            finally:
                reset_current_function_stats(token)
        if _l1_only_mode and _object_cache:
            if _l1_swr_active and ttl is not None:
                found, cached_value, needs_refresh, version = _object_cache.get_with_swr(cache_key, ttl)
            else:
                found, cached_value = _object_cache.get(cache_key)
                needs_refresh, version = False, 0
            if found:
                _stats.record_l1_hit()
                if needs_refresh:
                    # SWR: serve the stale value now, refresh on a daemon thread
                    # (sync functions have no event loop to schedule a task on)
                    snapshot = _l1_swr_acquire(cache_key, version, args, kwargs)
                    if snapshot is not None:
                        refresh_args, refresh_kwargs = snapshot
                        try:
                            threading.Thread(
                                target=_l1_swr_refresh_sync,
                                args=(cache_key, version, refresh_args, refresh_kwargs),
                                name="cachekit-swr-refresh",  # no function or key metadata (CWE-532)
                                daemon=True,
                            ).start()
                        except RuntimeError as exc:
                            # Thread couldn't start (resource pressure) — release
                            # the slot and this exact refresh so a later call retries
                            _l1_swr_slots.release()
                            _object_cache.cancel_refresh(cache_key, version)
                            _warn_refresh(_refresh_unstarted_warn, "L1-only SWR refresh could not be started", cache_key, exc)
                reset_current_function_stats(token)
                return cached_value

            # Cache miss - execute function and store raw result
            _stats.record_miss()
            try:
                result = func(*args, **kwargs)
                _object_cache.put(cache_key, result, ttl=ttl if ttl is not None else 31536000)
                _cached_keys.add((_l2_scope(), cache_key))
                return result
            finally:
                reset_current_function_stats(token)

        # L1+L2 MODE: Original behavior with backend initialization

        # Tenant scope, before the breaker check and outside every degrade try (LAB-5713): an
        # unsupported tenant id type is a caller bug, so its UnsupportedTenantError reaches the
        # caller before the function runs, whatever the breaker state, and never counts a failure
        # on the breaker every tenant of this function shares. "" until the backend is resolved;
        # the first call checks right after resolving it, below. Sits outside the main
        # try/finally, so the raise path restores the context itself.
        try:
            _l2_scope()
        except Exception:
            reset_current_function_stats(token)
            raise

        nonlocal _backend

        # Interop fail-closed guard (CWE-636): a key-prefixing backend would make
        # this SDK read/write a key other SDKs cannot see. Re-checked on every call
        # because the backend is lazily resolved and a prefix could appear
        # dynamically. It runs before the L1 lookup, because an L1 hit returns early
        # and must not skip it (LAB-5351). ensure_interop_backend_compatible(None)
        # is a no-op, so until the backend is resolved an interop call skips L1 and
        # is checked right after resolution, below. The raise paths sit outside the
        # main try/finally, so they restore the stats context themselves (see
        # test_context_leak_regression.py).
        interop_checked = False
        if interop is not None and _backend is not None:
            try:
                ensure_interop_backend_compatible(_backend)
            except Exception:
                reset_current_function_stats(token)
                raise
            interop_checked = True

        # Guard clause: L1 cache check first - early return eliminates network latency.
        # It runs before the breaker's admission check and records no breaker outcome:
        # the breaker tracks backend health, and an L1 hit never reaches the backend,
        # so it is served whatever the breaker state (LAB-5351).
        if _l1_cache and cache_key and (interop is None or interop_checked):
            l1_found, l1_bytes = _l1_cache.get(cache_key)
            if l1_found and l1_bytes:
                # L1 cache hit (~50ns vs ~1000μs for Redis) - deserialize bytes
                try:
                    l1_value = operation_handler.serialization_handler.deserialize_data(l1_bytes, cache_key, args, kwargs)

                    features.set_operation_context("l1_get", duration_ms=0.001)

                    # Record L1 cache hit metrics
                    if features.collect_stats:
                        features.record_cache_operation(
                            operation="get",
                            namespace=namespace or "default",
                            serializer="l1_memory",
                            success=True,
                            duration_ms=0.001,  # ~1μs for L1 hit
                            size_bytes=len(l1_bytes),
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
                except TenantMismatchError:
                    # L1 is keyed by the bare cache key and holds only this process's own
                    # authenticated writes and backfills, so another tenant's envelope here is
                    # a keying collision, not tamper evidence: an L1 miss, no auth_tamper and no
                    # raise. L2, which may hold this tenant's own entry, applies the policy.
                    _l1_cache.invalidate(cache_key)
                except TenantResolutionError:
                    # No tenant in the caller's context: nothing to decrypt as, and nothing wrong
                    # with the entry, which stays. The L2 read below misses for the same reason.
                    logger().debug(f"L1 read skipped for {redact_cache_key(cache_key)}: caller's tenant unresolved")
                except SerializationError as e:
                    # Poisoned L1 must not outlive remediation of the durable L2 copy —
                    # invalidate BEFORE the policy decision (a fail-closed raise would
                    # otherwise keep re-raising from stale process-local L1 after the
                    # operator fixes L2). L2 remains the retained evidence.
                    _l1_cache.invalidate(cache_key)
                    try:
                        # Single policy point (cachekit-py#170): metric + fail policy.
                        handle_decrypt_failure(
                            e, tier="l1", cache_key=cache_key, fail_closed=serialization_handler.encryption_fail_closed
                        )
                    except DecryptionAuthenticationError:
                        reset_current_function_stats(token)
                        raise
                    # Fail open: fall through to L2
                except KeyringConfigurationError:
                    # LOCAL keyring config fault — not a poisoned L1 entry, so
                    # neither the invalidate nor the "deserialization failed"
                    # message below is true, and swallowing it here degrades a
                    # misconfigured keyring into a silent L2 fall-through. Same
                    # re-raise as the L2 sites in cache_handler.py.
                    #
                    # The sync wrapper has no outer `finally`, so a raising exit
                    # must reset the stats token by hand — exactly as the
                    # DecryptionAuthenticationError sibling above does. (The
                    # async L1 guard needs no reset; its wrapper's outer
                    # `finally` covers every exit path.)
                    reset_current_function_stats(token)
                    raise
                except Exception as e:
                    # L1 deserialization failed - invalidate and continue to L2
                    logger().warning(
                        f"L1 cache deserialization failed for {redact_cache_key(cache_key)}: {redact_error_for_log(e)}"
                    )
                    _l1_cache.invalidate(cache_key)

        # Guard clause: the circuit breaker rejected this call (OPEN, or HALF_OPEN
        # with its probe budget spent) - run the function uncached. This sits
        # outside the try below on purpose: that except records a failure, and a
        # rejection is not one. Recorded, every rejected call would push the OPEN
        # window forward and reopen HALF_OPEN, so the breaker never recovers.
        if not features.should_allow_request():
            features.log_cache_operation(
                operation="circuit_breaker_open",
                key=cache_key,
                namespace=namespace or "default",
                serializer="rust",
                error="Circuit breaker rejected the request",
                error_type="CircuitBreakerOpen",
            )
            reset_current_function_stats(token)
            return func(*args, **kwargs)

        with features.create_span("redis_cache", span_attributes) as span:
            try:
                # Add cache key to span attributes
                if span:
                    features.set_span_attributes(span, {"cache.key": cache_key})

                if _backend is None:
                    _backend = _resolve_lazy_backend()
                    _l2_scope()  # first call: the tenant check above ran before the backend existed

                # Setup cache handler strategy on first use
                handler = StandardCacheHandler(
                    _backend,
                    backpressure_controller=features.backpressure,
                    ttl_refresh_threshold=ttl_refresh_threshold,
                )
                operation_handler.set_cache_handler(handler)
            except UnsupportedTenantError:
                # From the first-call check above, or from a provider that checks the tenant while
                # building the backend (RedisBackendProvider.get_backend): a caller bug, not a
                # client failure, so never degraded or counted (LAB-5713).
                reset_current_function_stats(token)
                raise
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

        # First interop call: the check above had no backend to check (see there).
        if interop is not None and not interop_checked:
            try:
                ensure_interop_backend_compatible(_backend)
            except Exception:
                reset_current_function_stats(token)
                raise

        if _l1_cache and invalidation.listener_start_due(_backend):
            invalidation.start_listener(_backend)

        # Continue with the rest of the sync wrapper logic...
        # Try to get cached value with optional TTL refresh
        start_time = time.time()
        try:
            refresh_ttl = ttl if refresh_ttl_on_get and ttl else None

            # Use operation handler for all cache access (uses backend internally).
            # A freshness-capable backend (CachekitIO) always takes the freshness
            # read — not just when SWR is configured — so every hit carries the
            # server's staleness label (LAB-381/LAB-557); a stale hit is served
            # immediately and (with SWR active) revalidated on a background daemon
            # thread below. The freshness path drops refresh_ttl, which is a
            # documented no-op on the sync path anyway (StandardCacheHandler.get),
            # and skips the mmap fast path (CachekitIO is not buffer-readable).
            _sync_l2_stale = False
            _sync_l2_fresh_for: int | None = None
            if _l2_freshness_capable():
                _fresh_hit = operation_handler.get_cached_value_with_freshness(cache_key, args, kwargs)
                cached_result = _fresh_hit[0] if _fresh_hit is not None else None
                _sync_l2_stale = _fresh_hit[1] if _fresh_hit is not None else False
                _sync_l2_fresh_for = _fresh_hit[2] if _fresh_hit is not None else None
            else:
                cached_result = operation_handler.get_cached_value(cache_key, refresh_ttl, args, kwargs)

            duration = time.time() - start_time

            if cached_result is not None:
                # Cache hit: envelope is None on the mmap fast path; size_bytes is set on every path
                result, cached_data, size_bytes = cached_result.value, cached_result.envelope, cached_result.size_bytes
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
                    features.record_cache_operation(
                        operation="get",
                        namespace=namespace or "default",
                        serializer="rust",
                        success=True,
                        duration_ms=duration * 1000,
                        size_bytes=size_bytes,
                    )

                # Backfill L1 with the L2 envelope for subsequent fast access — stale-exclusion
                # + remaining-freshness bound (LAB-557), as on the async path (LAB-348).
                _l1_backfill_from_l2(cache_key, cached_data, _sync_l2_stale, _sync_l2_fresh_for)

                # Record L2 hit with latency for cache_info()
                duration_ms = duration * 1000
                _stats.record_l2_hit(duration_ms)

                # SWR: stale hit — serve now, revalidate on a daemon thread.
                # Gated on _l2_swr_active: without a configured stale window this
                # decorator serves the mixed-reader hit but owns no revalidation.
                if _sync_l2_stale and _l2_swr_active:
                    _l2_swr_schedule(cache_key, args, kwargs, is_async=False)

                # WHY: L2 cache hit returns from try block that lacks finally cleanup
                # (only inner try at line ~567, not the outer try-finally at ~645-720)
                reset_current_function_stats(token)
                return result
        except (DecryptionAuthenticationError, KeyringConfigurationError):
            # Both propagate from get_cached_value* and must reach the caller: a
            # fail-closed tamper failure (raised only when encryption.fail_closed=True;
            # the metric and error log were recorded there), and a LOCAL keyring config
            # fault (same contract as the L1 guard above). The generic clause below
            # would log either as a cache error, count it on the breaker, and recompute
            # uncached on every call (LAB-4841).
            reset_current_function_stats(token)
            raise
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
            result = func(*args, **kwargs)

            # Serialize and cache the result
            try:
                # Store using operation handler (pass args/kwargs for tenant extraction)
                # Returns serialized bytes for L1 cache storage
                outcome = operation_handler.store_result(cache_key, result, ttl, args, kwargs, stale_ttl=_stale_ttl)

                # Also store in L1 cache for fast subsequent access (using serialized bytes)
                _put_l1(cache_key, outcome.envelope, ttl)
                if outcome.stored:
                    _track_and_record(cache_key)

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
                    )

            except (InteropError, KeyringConfigurationError):
                # Interop/v1 data-model rejection (spec-mandated; matches cachekit-ts),
                # or a LOCAL keyring config fault, which a cold key first hits on this
                # write (see the L2 read): fail loud, never "computed but silently
                # never cached".
                raise
            except Exception as e:
                # Caching failed but function succeeded - return result anyway
                set_duration_ms = (time.time() - start_time) * 1000
                features.handle_cache_error(
                    error=e,
                    operation="cache_set",
                    cache_key=cache_key or "unknown",
                    namespace=namespace or "default",
                    duration_ms=set_duration_ms,
                    serializer="rust",
                )

            return result

        # No `except BackendError` degradation here: the store's own failures are caught
        # above, so only the function can raise one this far, and rerunning the function
        # for it would repeat its side effects (LAB-5360). It is the function's exception.
        except KeyringConfigurationError:
            # From the write, or a nested cached call's: a local config fault, not a
            # backend failure. Counting it would open the breaker, and an open breaker
            # skips the L2 read and write that raise it, so every later call would run
            # uncached without a word.
            raise
        except Exception as e:
            # Other exceptions - record and re-raise
            features.record_failure(e)
            raise
        finally:
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
            try:
                cache_key = _resolve_cache_key(args, kwargs)
            except Exception as e:
                if interop is not None:
                    # Interop/v1: out-of-model arguments MUST be rejected with an
                    # error — never silently degrade to uncached execution.
                    raise
                # If key generation fails, execute function without caching - RETURN EARLY
                # This handles unhashable types gracefully
                features.handle_cache_error(
                    error=e,
                    operation="key_generation",
                    cache_key="<generation_failed>",
                    namespace=namespace or "default",
                    duration_ms=0.0,
                    count_toward_breaker=False,  # pre-admission, never reached the backend
                )
                return await func(*args, **kwargs)

            # L1-ONLY MODE: Store raw Python objects (no serialization).
            # Preserves types (tuples, sets, frozensets) that MessagePack would degrade.
            if _l1_only_mode and _object_cache is None:
                # L1 disabled in L1-only mode -> no cache anywhere; call through
                # (outer finally resets stats context)
                return await func(*args, **kwargs)
            if _l1_only_mode and _object_cache:
                if _l1_swr_active and ttl is not None:
                    found, cached_value, needs_refresh, version = _object_cache.get_with_swr(cache_key, ttl)
                else:
                    found, cached_value = _object_cache.get(cache_key)
                    needs_refresh, version = False, 0
                if found:
                    _stats.record_l1_hit()
                    if needs_refresh:
                        # SWR: serve the stale value now, refresh in the background
                        # without blocking the caller
                        snapshot = _l1_swr_acquire(cache_key, version, args, kwargs)
                        if snapshot is not None:
                            refresh_args, refresh_kwargs = snapshot
                            refresh_task = asyncio.create_task(
                                _l1_swr_refresh_async(cache_key, version, refresh_args, refresh_kwargs)
                            )
                            _l1_swr_tasks.add(refresh_task)
                            refresh_task.add_done_callback(functools.partial(_l1_swr_task_done, cache_key=cache_key))
                    return cached_value

                # Cache miss - execute function and store raw result
                _stats.record_miss()
                result = await func(*args, **kwargs)
                _object_cache.put(cache_key, result, ttl=ttl if ttl is not None else 31536000)
                _cached_keys.add((_l2_scope(), cache_key))
                return result

            # L1+L2 MODE: Original behavior with backend initialization
            # Tenant scope, before the breaker check (LAB-5713): see sync_wrapper. "" until the
            # backend is resolved; the first call checks right after resolving it, below. The
            # outer finally resets the stats context.
            _l2_scope()

            nonlocal _backend

            # Interop fail-closed guard (CWE-636): see sync_wrapper. Checked before
            # the L1 lookup once the backend is resolved; until then an interop call
            # skips L1 and is checked right after resolution, below, so backend
            # resolution stays behind admission (LAB-5351). The raise propagates;
            # the outer finally resets the stats context.
            interop_checked = False
            if interop is not None and _backend is not None:
                ensure_interop_backend_compatible(_backend)
                interop_checked = True

            # Guard clause: L1 cache check first - early return eliminates network latency.
            # Before admission and recording no breaker outcome, as in sync_wrapper (LAB-5351).
            if _l1_cache and cache_key and (interop is None or interop_checked):
                l1_found, l1_bytes = _l1_cache.get(cache_key)
                if l1_found and l1_bytes:
                    # L1 cache hit (~50ns vs ~1000μs for Redis) - deserialize bytes
                    try:
                        l1_value = operation_handler.serialization_handler.deserialize_data(l1_bytes, cache_key, args, kwargs)

                        features.set_operation_context("l1_get", duration_ms=0.001)

                        # Record L1 cache hit metrics (same labels as the sync L1 hit)
                        if features.collect_stats:
                            features.record_cache_operation(
                                operation="get",
                                namespace=namespace or "default",
                                serializer="l1_memory",
                                success=True,
                                duration_ms=0.001,  # Sub-microsecond
                                size_bytes=len(l1_bytes),
                            )

                        # Record L1 hit for cache_info()
                        _stats.record_l1_hit()

                        return l1_value
                    except TenantMismatchError:
                        # Another tenant's envelope in L1: an L1 miss — see the sync L1 guard above.
                        _l1_cache.invalidate(cache_key)
                    except TenantResolutionError:
                        # Caller's tenant unresolved: the entry stays — see the sync L1 guard above.
                        logger().debug(f"L1 read skipped for {redact_cache_key(cache_key)}: caller's tenant unresolved")
                    except SerializationError as e:
                        # Poisoned L1 must not outlive remediation of the durable L2 copy —
                        # invalidate BEFORE the policy decision (a fail-closed raise would
                        # otherwise keep re-raising from stale process-local L1 after the
                        # operator fixes L2). L2 remains the retained evidence.
                        _l1_cache.invalidate(cache_key)
                        # Single policy point (cachekit-py#170): metric + fail policy.
                        # Explicit local re-raise mirrors the sync L1/L2 fail-closed
                        # guards and the async lock-path guard: a fail-closed tamper raise
                        # must reach the caller, never be demoted to a fail-open recompute
                        # if a future edit wraps this read path in a broad `except
                        # Exception` (defense-in-depth, LAB-108). No manual stats reset —
                        # the async wrapper's outer `finally` covers every exit path.
                        try:
                            handle_decrypt_failure(
                                e, tier="l1", cache_key=cache_key, fail_closed=serialization_handler.encryption_fail_closed
                            )
                        except DecryptionAuthenticationError:
                            raise
                        # Fail open: fall through to L2
                    except KeyringConfigurationError:
                        # LOCAL keyring config fault — see the sync L1 guard above.
                        raise
                    except Exception as e:
                        # L1 deserialization failed - invalidate and continue to L2
                        logger().warning(
                            f"L1 cache deserialization failed for {redact_cache_key(cache_key)}: {redact_error_for_log(e)}"
                        )
                        _l1_cache.invalidate(cache_key)

            # Guard clause: the circuit breaker rejected this call (OPEN, or HALF_OPEN
            # with its probe budget spent) - run the function uncached, as
            # sync_wrapper does. Not recorded as a failure: a rejection is not one.
            # The outer finally resets the stats context.
            if not features.should_allow_request():
                features.log_cache_operation(
                    operation="circuit_breaker_open",
                    key=cache_key,
                    namespace=namespace or "default",
                    serializer="rust",
                    error="Circuit breaker rejected the request",
                    error_type="CircuitBreakerOpen",
                )
                return await func(*args, **kwargs)

            # Initialize backend only when needed (lazy init for performance)
            if _backend is None:
                try:
                    _backend = _resolve_lazy_backend()
                except UnsupportedTenantError:
                    raise  # a caller bug, not a client failure: see sync_wrapper
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
                _l2_scope()  # first call: the tenant check above ran before the backend existed

            # First interop call: the check above had no backend to check (see there).
            if interop is not None and not interop_checked:
                ensure_interop_backend_compatible(_backend)

            if _l1_cache and invalidation.listener_start_due(_backend):
                # Connects and subscribes: in an executor thread, and not awaited, so this call never
                # waits on it. start_listener never raises; concurrent starts give way to the first.
                asyncio.get_running_loop().run_in_executor(None, invalidation.start_listener, _backend)

            # Update operation handler with the backend (sync or async)
            handler = StandardCacheHandler(
                _backend,
                backpressure_controller=features.backpressure,
                ttl_refresh_threshold=ttl_refresh_threshold,
            )
            operation_handler.set_cache_handler(handler)

            # Try to get from Redis cache (always measure time for L2 latency tracking)
            start_time = time.perf_counter()

            try:
                # Route through the operation handler so corrupt/tampered entries inherit
                # eviction + the cache_get_deserialize metric instead of persisting (#159),
                # and fail-closed tamper errors propagate (LAB-108). A freshness-capable
                # backend (CachekitIO) always takes the freshness read — not just when SWR
                # is configured — so every hit carries the server's staleness label and
                # remaining-freshness bound (LAB-381/LAB-557): a stale hit is never
                # backfilled to L1, and a fresh hit's backfill can't outlive fresh_until.
                _l2_is_stale = False
                _l2_fresh_for: int | None = None
                if _l2_freshness_capable():
                    _fresh_hit = await operation_handler.get_cached_value_with_freshness_async(cache_key, args, kwargs)
                    cached_result = _fresh_hit[0] if _fresh_hit is not None else None
                    _l2_is_stale = _fresh_hit[1] if _fresh_hit is not None else False
                    _l2_fresh_for = _fresh_hit[2] if _fresh_hit is not None else None
                else:
                    cached_result = await operation_handler.get_cached_value_async(cache_key, args=args, kwargs=kwargs)

                if cached_result is not None:
                    # Cache hit: envelope is the raw serialized bytes for L1 backfill
                    result, cached_data = cached_result.value, cached_result.envelope

                    # Record cache hit (always compute for L2 latency stats)
                    get_duration_ms = (time.perf_counter() - start_time) * 1000
                    _record_l2_hit_async(cached_result.size_bytes, get_duration_ms)

                    # Update L1 cache with the L2 value (serialized bytes) for subsequent
                    # fast access — stale-exclusion + remaining-freshness bound (LAB-557).
                    _l1_backfill_from_l2(cache_key, cached_data, _l2_is_stale, _l2_fresh_for)

                    # Handle TTL refresh if configured and threshold met
                    if refresh_ttl_on_get and ttl and supports_ttl_inspection(_backend):
                        try:
                            remaining_ttl = await _backend.get_ttl(cache_key)
                            if remaining_ttl and remaining_ttl < (ttl * ttl_refresh_threshold):
                                # Refresh TTL in background with error callback
                                task = asyncio.create_task(_backend.refresh_ttl(cache_key, ttl))
                                task.add_done_callback(lambda t: _ttl_refresh_done_callback(t, cache_key))
                        except Exception as e:
                            # TTL refresh is optional, don't fail on error
                            _logger.debug("TTL refresh failed for %s: %s", redact_cache_key(cache_key), redact_error_for_log(e))
                    elif refresh_ttl_on_get and ttl:
                        # Backend can't inspect TTL: warn once instead of silently ignoring
                        # the opted-in flag (LAB-446). Still degrades gracefully.
                        warn_ttl_refresh_unsupported(_backend)

                    # SWR: stale hit — value already in hand; revalidate in the
                    # background so no request pays the recompute at a TTL boundary.
                    # Gated on _l2_swr_active (not just the read gate above): a
                    # decorator without a configured stale window serves a
                    # stale-labelled mixed-reader hit but owns no revalidation.
                    if _l2_is_stale and _l2_swr_active:
                        _l2_swr_schedule(cache_key, args, kwargs, is_async=True)

                    return result

            except (DecryptionAuthenticationError, KeyringConfigurationError):
                # Fail-closed tamper failure propagated from get_cached_value_async —
                # the tamper metric, error log, evidence retention, and fail policy all
                # fired inside handle_decrypt_failure (cachekit-py#170, LAB-108) — or a
                # LOCAL keyring config fault (see the sync L2 read). Either must reach
                # the caller: the generic clause below would demote it to a fail-open
                # "record and recompute".
                raise
            except Exception as e:
                # Backend/network error - record but continue to function execution
                get_duration_ms = (time.perf_counter() - start_time) * 1000
                features.handle_cache_error(
                    error=e,
                    operation="cache_get",
                    cache_key=cache_key or "unknown",
                    namespace=namespace or "default",
                    duration_ms=get_duration_ms,
                )

            # CACHE MISS - Use distributed lock to prevent thundering herd
            # This ensures only one request executes the function while others wait.
            # LockableBackend protocol contract: pass the bare cache_key. Each backend
            # owns its internal lock-namespace derivation (Redis: ``<key>:lock``; SaaS:
            # ``POST /v1/cache/{key}/lock``). Appending here would pollute the SaaS
            # 7-segment canonical key and 400 at the edge.
            lock_timeout = 30.0  # Lock expires after 30 seconds to prevent deadlock
            blocking_timeout = 5.0  # Wait up to 5 seconds to acquire lock

            # Check if backend supports distributed locking
            if supports_locking(_backend):
                func_error: Exception | None = None
                try:
                    # Use backend's async lock protocol
                    async with _backend.acquire_lock(
                        cache_key,
                        timeout=lock_timeout,
                        blocking_timeout=blocking_timeout,
                    ) as lock_acquired:
                        if lock_acquired:
                            # Lock acquired - double-check cache
                            # Another request may have populated it while we waited.
                            # Routed through the operation handler: corrupt entries evict (#159),
                            # stale hits skip L1, fresh backfill bounded by fresh_for (LAB-557).
                            try:
                                _dc_start = time.perf_counter()
                                cached_result, _dc_stale, _dc_fresh_for = await _l2_double_check(cache_key, args, kwargs)
                                if cached_result is not None:
                                    # Another request filled the cache while we waited
                                    result, cached_data = cached_result.value, cached_result.envelope
                                    _dc_duration_ms = (time.perf_counter() - _dc_start) * 1000
                                    _record_l2_hit_async(cached_result.size_bytes, _dc_duration_ms)
                                    _l1_backfill_from_l2(cache_key, cached_data, _dc_stale, _dc_fresh_for)
                                    return result
                            except (DecryptionAuthenticationError, KeyringConfigurationError):
                                # Fail-closed tamper raise from get_cached_value_async
                                # (cachekit-py#170) or a LOCAL keyring config fault (see
                                # the sync L2 read) — must not be demoted to a recompute
                                # by the generic clause below. The lock clause's
                                # `except Exception` then delivers a KeyringConfigurationError:
                                # re-raised as is from a finally-only lock, and unwrapped
                                # through original_exception from a Redis lock's BackendError.
                                raise
                            except Exception as e:
                                # If double-check fails, continue to execute function
                                _logger.debug(
                                    "Double-check cache failed after lock acquisition for %s: %s",
                                    redact_cache_key(cache_key),
                                    redact_error_for_log(e),
                                )
                        else:
                            # Lock timeout - double-check cache before giving up
                            # Another request may have populated it while we waited
                            logger().warning(
                                f"Failed to acquire lock for {redact_cache_key(cache_key)} after {blocking_timeout}s, checking cache"
                            )
                            try:
                                # Routed through the operation handler: corrupt entries evict (#159),
                                # stale hits skip L1, fresh backfill bounded by fresh_for (LAB-557).
                                _dc_start = time.perf_counter()
                                cached_result, _dc_stale, _dc_fresh_for = await _l2_double_check(cache_key, args, kwargs)
                                if cached_result is not None:
                                    # Cache was populated while waiting - use it
                                    result, cached_data = cached_result.value, cached_result.envelope
                                    _dc_duration_ms = (time.perf_counter() - _dc_start) * 1000
                                    _record_l2_hit_async(cached_result.size_bytes, _dc_duration_ms)
                                    _l1_backfill_from_l2(cache_key, cached_data, _dc_stale, _dc_fresh_for)
                                    return result
                            except (DecryptionAuthenticationError, KeyringConfigurationError):
                                # Same as the lock-acquired double-check above.
                                raise
                            except Exception:
                                # Cache check failed - fall through to execute function
                                logger().warning(
                                    f"Cache check after lock timeout failed for {redact_cache_key(cache_key)}, executing without lock"
                                )

                        # Execute the original function (with or without lock). Its exception
                        # is held here, outside the lock's error handling, and re-raised once
                        # the lock is released: a Redis lock wraps whatever leaves its body in
                        # a BackendError, and the clause below would take a function's own
                        # BackendError for a lock failure and run the function again (LAB-5360).
                        try:
                            result = await func(*args, **kwargs)
                        except Exception as e:
                            func_error = e
                        else:
                            # Serialize and cache the result
                            try:
                                serialized_data = operation_handler.serialization_handler.serialize_data(
                                    result, args, kwargs, cache_key=cache_key
                                )

                                # Store in Redis with TTL
                                stored = await operation_handler.cache_handler.set_async(  # type: ignore[attr-defined]
                                    cache_key,
                                    serialized_data,
                                    ttl=ttl,
                                    stale_ttl=_stale_ttl,
                                )

                                # Also store in L1 cache for fast subsequent access (using serialized bytes)
                                _put_l1(cache_key, serialized_data, ttl)
                                if stored:
                                    await _track_and_record_async(cache_key)

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
                                        serializer="rust",
                                    )

                            except (InteropError, KeyringConfigurationError):
                                # Interop/v1 data-model rejection (spec-mandated), or a LOCAL
                                # keyring config fault (see the sync write): fail loud. It
                                # leaves through the lock clause below as the double-check's does.
                                raise
                            except Exception as e:
                                # Caching failed but function succeeded - return result anyway
                                set_duration_ms = (time.perf_counter() - start_time) * 1000
                                features.handle_cache_error(
                                    error=e,
                                    operation="cache_set",
                                    cache_key=cache_key or "unknown",
                                    namespace=namespace or "default",
                                    duration_ms=set_duration_ms,
                                    # A value the serializer (or encryption) rejects is not a
                                    # backend failure; the sync write does not count it either.
                                    count_toward_breaker=not isinstance(e, SerializationError),
                                )

                            return result

                except DecryptionAuthenticationError:
                    # Fail-closed tamper failure from the lock double-check reads — must
                    # propagate to the caller, never demote to "lock failed, execute
                    # without lock". (The generic clause below would also re-raise it,
                    # but only as a side effect of the BackendError check; this clause
                    # makes the security dependency explicit.)
                    raise
                except Exception as e:
                    # The function's exceptions never get here (held in func_error above).
                    # What does is a lock failure, or a cache error the lock body re-raised
                    # on purpose: fail-closed DecryptionAuthenticationError, InteropError,
                    # KeyringConfigurationError. Those leave a finally-only lock as they are.
                    if func_error is not None:
                        # The lock failed while releasing after the function raised. The
                        # function's exception wins and is raised below, outside this handler:
                        # raised in here, it would take the release error as its __context__.
                        logger().warning(
                            f"Lock release failed for {redact_cache_key(cache_key)} after the function raised; "
                            f"the lock may be held until its timeout: {redact_error_for_log(e)}"
                        )
                    elif not isinstance(e, BackendError):
                        raise
                    elif e.original_exception and not isinstance(e.original_exception, BackendError):
                        # A Redis lock wraps them in a BackendError; unwrap and re-raise the original.
                        raise e.original_exception from e
                    else:
                        # Lock operation failed - execute without lock
                        logger().warning(
                            f"Lock operation failed for {redact_cache_key(cache_key)}, executing without lock: {redact_error_for_log(e)}"
                        )
                        # Fall through to execute without locking

                if func_error is not None:
                    # The function's own exception, unchanged and never recorded (as before).
                    raise func_error

            # Execute without locking (either backend doesn't support it or lock failed)
            if not supports_locking(_backend):
                logger().debug(
                    f"Backend doesn't support locking for {redact_cache_key(cache_key)}, executing without thundering herd protection"
                )

            try:
                # Execute the original function
                result = await func(*args, **kwargs)

                # Serialize and cache the result
                try:
                    serialized_data = operation_handler.serialization_handler.serialize_data(
                        result, args, kwargs, cache_key=cache_key
                    )

                    # Store in Redis with TTL
                    stored = await operation_handler.cache_handler.set_async(  # type: ignore[attr-defined]
                        cache_key,
                        serialized_data,
                        ttl=ttl,
                        stale_ttl=_stale_ttl,
                    )

                    # Also store in L1 cache for fast subsequent access (using serialized bytes)
                    _put_l1(cache_key, serialized_data, ttl)
                    if stored:
                        await _track_and_record_async(cache_key)

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
                            serializer="rust",
                        )

                except (InteropError, KeyringConfigurationError):
                    # Interop/v1 data-model rejection (spec-mandated), or a LOCAL
                    # keyring config fault (see the sync write): fail loud.
                    raise
                except Exception as e:
                    # Caching failed but function succeeded - return result anyway
                    set_duration_ms = (time.perf_counter() - start_time) * 1000
                    features.handle_cache_error(
                        error=e,
                        operation="cache_set",
                        cache_key=cache_key or "unknown",
                        namespace=namespace or "default",
                        duration_ms=set_duration_ms,
                        count_toward_breaker=not isinstance(e, SerializationError),  # as the locked write above
                    )

                return result

            except KeyringConfigurationError:
                # From the write, or a nested cached call's: never counted (see the sync wrapper).
                raise
            except Exception as e:
                # Function execution failed - record and re-raise
                features.record_failure(e)
                raise
        finally:
            # ALWAYS reset stats context, even on exception
            reset_current_function_stats(token)

    @contextlib.contextmanager
    def _watch_records() -> Iterator[set[tuple[str, str]]]:
        """Yield a set that collects every entry _put_l1 records until the block exits.

        A whole-function invalidation trims _cached_keys after its L2 deletes. A concurrent
        miss can rewrite a key in between, and _put_l1's re-record of an already-present key
        changes nothing, so without this set the trim drops the only local record of the new
        value. _put_l1 adds to the set BEFORE it records: re-adding any trimmed key found in
        the set afterwards therefore covers a record that races the trim itself.
        """
        pid = os.getpid()
        for stale in [o for o in _drain_watches.copy() if o[0] != pid]:
            _drain_watches.pop(stale, None)  # a forked child's inherited, orphaned watches
        owner, watch = (pid, object()), set[tuple[str, str]]()
        _drain_watches[owner] = watch
        try:
            yield watch
        finally:
            _drain_watches.pop(owner, None)

    def _delete_l2(keys: list[str]) -> tuple[set[str], Union[str, None]]:
        """L2-delete ``keys``: return those not confirmed deleted, and the redacted error of a
        multi-key call that fell back (rendered, so no traceback outlives it). Never raises.

        One multi-key call on a backend that has one. If that call raises as a whole, every
        key's outcome is unknown, so the batch falls back to per-key deletes: one bad batch
        cannot abort the sweep, and each key still gets its own verdict.
        """
        batch_error: Union[str, None] = None
        if _supports_multi_delete(_backend):
            try:
                return _backend._delete_many(keys), None
            except Exception as e:
                batch_error = redact_error_for_log(e)
        failed: set[str] = set()
        for key in keys:
            try:
                _backend.delete(key)  # type: ignore[union-attr]
            except Exception as e:
                _logger.debug("Failed to delete L2 key %s: %s", redact_cache_key(key), redact_error_for_log(e))
                failed.add(key)  # keep key tracked for retry
        return failed, batch_error

    def _local_invalidate_all() -> None:
        """Invalidate every key THIS process knows (_cached_keys): L2 delete, then trim, then L1.

        The whole-function path for backends without a key registry, and the fallback when a
        drain fails. Keys go to L2 in batches of at most _DELETE_BATCH: one multi-key call per
        batch where the backend supports it (the backend may split it; Memcached sends per
        server, 1,000 keys a send), else one call per key. Per batch, the L2 delete
        comes first: evicting L1 first would let an L2-hit backfill landing in between
        re-cache the old value in L1. A key whose delete failed, or that was re-recorded
        while this runs, stays in _cached_keys for the next attempt. Keys other processes
        wrote and this one never saw stay in L2 until their TTL. Another tenant's entry keeps
        its L2 value and stays tracked; only its L1 copy is evicted, because L1 is not
        tenant-scoped.

        Failed deletes are logged once per call with their count: the call never raises, so
        this record is the caller's only signal, and a per-key record would flood during an
        outage.
        """
        scope = _l2_scope()
        failed = 0
        fallbacks, last_fallback = 0, ""
        has_l2 = _backend is not None and not _l1_only_mode
        with _watch_records() as watch:
            snap = list(_cached_keys)  # snapshot: other threads add while this runs
            for start in range(0, len(snap), _DELETE_BATCH):
                batch = snap[start : start + _DELETE_BATCH]
                # Another tenant's L2 entry is not the caller's to delete: it stays tracked.
                mine = [key for entry_scope, key in batch if entry_scope == scope]
                undeleted, batch_error = _delete_l2(mine) if has_l2 and mine else (set(), None)
                failed += len(undeleted)
                if batch_error is not None:
                    fallbacks, last_fallback = fallbacks + 1, batch_error
                for entry in batch:
                    entry_scope, key = entry
                    if entry_scope == scope and key not in undeleted:
                        _cached_keys.discard(entry)
                        if entry in watch:  # rewritten meanwhile: its new value may still be in L2
                            _cached_keys.add(entry)
                    if _object_cache:
                        _object_cache.delete(key)
                    elif _l1_cache:
                        _l1_cache.invalidate(key)
        if fallbacks:
            # Once per call, like the ERROR below. A backend whose multi-key delete always fails
            # (e.g. an ACL that denies UNLINK) otherwise silently pays one round trip per key.
            _logger.warning(
                "Multi-key L2 delete failed for %d batch(es); deleted those keys one by one. Latest error: %s",
                fallbacks,
                last_fallback,
            )
        if failed:
            # ERROR, as for a single key: every failed entry may still be served from L2.
            _logger.error("Failed to delete %d L2 key(s); they stay tracked for the next invalidate_cache()", failed)

    def _drain_all() -> None:
        """Whole-function invalidation. Sync; ainvalidate_cache runs it via asyncio.to_thread.

        On a KeyTrackableBackend, drain the server-side registry: every key ANY process wrote
        for this function is deleted from L2, plus the keys this process knows that the
        registry missed. The backend scopes the registry and its keys to the calling tenant,
        and only the calling tenant's _cached_keys entries go to the drain; other tenants'
        entries are handled as in _local_invalidate_all(). Any failure falls back to
        _local_invalidate_all().

        The trim keeps every entry _put_l1 records while the drain is in flight. Such an entry
        may carry a value written after the drain unlinked it, and if that write's track_key
        failed, this process's record is the only thing left that can reach it: it stays in
        _cached_keys for the next drain.

        A drain that returned is announced once, whatever the legacy set's drain did; the local
        fallback is never announced: peers evicting their L1 would re-read the L2 entries it
        could not reach.
        """
        if not _is_trackable():
            _local_invalidate_all()
            return
        try:
            with _watch_records() as watch:  # opened before the snapshot: later records are watched
                snap = set(_cached_keys)  # this process's view, taken before the drain
                scope = _l2_scope()
                mine = {entry for entry in snap if entry[0] == scope}
                deleted = _backend.drain_tracked(_registry_id, {key for _, key in mine})  # type: ignore[union-attr]
                if _legacy_registry_id is not None:
                    # Its own try: the primary drain already deleted keys that other wrappers
                    # may hold in the shared L1, so its result must still be applied below.
                    # A failed legacy drain leaves its members in the old set for the next one.
                    try:
                        deleted |= _backend.drain_tracked(_legacy_registry_id, ())  # type: ignore[union-attr]
                    except Exception as e:
                        _logger.warning("Legacy key registry drain failed: %s", redact_error_for_log(e))
                # Trim BEFORE evicting: _put_l1 puts then records, so a concurrent write can
                # never leave an L1 entry whose key is no longer in _cached_keys.
                trim = mine - watch
                _cached_keys.difference_update(trim)
                _cached_keys.update(trim & watch)  # recorded while the trim ran
                if _l1_cache:
                    _l1_cache.invalidate_many(deleted | {key for _, key in snap})
        except Exception as e:
            _logger.warning("Key registry drain failed, invalidating local keys only: %s", redact_error_for_log(e))
            _local_invalidate_all()
        else:
            invalidation.publish(_backend, _registry_id, None)

    def _invalidate_key(cache_key: str) -> bool:
        """Single-key invalidation: untrack, L2 delete, then L1. Sync; ainvalidate_cache runs it
        via asyncio.to_thread, like _drain_all. Returns whether the L2 delete returned normally.

        Untrack BEFORE the delete: every write path calls _put_l1, which re-tracks the key, after
        its L2 set, so a concurrent write landing after the delete can never be left in L2 untracked.
        A delete that does not return normally, whatever it raises, re-tracks the key, so a later
        no-args invalidate_cache() retries it.
        """
        entry = (_l2_scope(), cache_key)
        _cached_keys.discard(entry)
        deleted = False
        try:
            if _backend and not _l1_only_mode:
                try:
                    _backend.delete(cache_key)
                    deleted = True
                except Exception as e:
                    # ERROR: a failed delete keeps serving stale data (for interop, to OTHER SDKs too).
                    _logger.error("Failed to delete L2 key %s: %s", redact_cache_key(cache_key), redact_error_for_log(e))
                finally:
                    # finally, not except: a BaseException (gevent Timeout, KeyboardInterrupt) must re-track too.
                    if not deleted:
                        _cached_keys.add(entry)
        finally:
            if _object_cache:
                _object_cache.delete(cache_key)
            elif _l1_cache:
                _l1_cache.invalidate(cache_key)
        return deleted

    def _invalidate_keys(cache_keys: list[str]) -> None:
        """_invalidate_key per key: each logs its own failure, so one never skips the next.

        Announced once, for the first (current-format) key and only if its L2 delete returned
        normally: L1 only ever holds current-format keys, and a peer evicting after a failed
        delete would re-read the entry it left. The pre-0.20.0 twin's delete neither adds nor
        gates the announcement. A key= function's key embeds caller identifiers, so its event
        names the whole function instead.
        """
        current_deleted = _invalidate_key(cache_keys[0])
        for twin in cache_keys[1:]:
            _invalidate_key(twin)
        if current_deleted and _is_trackable():
            invalidation.publish(_backend, _registry_id, None if custom_key_func is not None else cache_keys[0])

    def invalidate_cache(*args: Any, **kwargs: Any) -> None:
        nonlocal _backend

        # L1-ONLY MODE: Skip backend lookup entirely
        # This fixes the sentinel problem: when backend=None is explicitly passed,
        # we should NOT try to get a backend from the provider
        if not _l1_only_mode and _backend is None:
            try:
                _backend = _resolve_lazy_backend()
            except Exception as e:
                # If backend creation fails, can't invalidate L2
                _logger.debug("Failed to get backend for invalidation: %s", redact_error_for_log(e))

        # Same interop guard as reads and writes: a key-prefixing backend would delete
        # {prefix}{key} and leave the bare entry other SDKs read in place.
        if interop is not None:
            ensure_interop_backend_compatible(_backend)

        # Fix #59: When called with no args on a parameterized function,
        # invalidate ALL cached entries for this function.
        # Without this, it generates a key for zero-arg call (never cached) → no-op.
        if not args and not kwargs and _func_has_params:
            _drain_all()
            return

        # Single-key invalidation (specific args provided, or zero-param function).
        # Same derivation as the write path (LAB-4387), plus the pre-0.20.0 twin (LAB-5288).
        _invalidate_keys(_resolve_invalidation_keys(args, kwargs))

    async def ainvalidate_cache(*args: Any, **kwargs: Any) -> None:
        nonlocal _backend

        # L1-ONLY MODE: Skip backend lookup entirely
        # This fixes the sentinel problem: when backend=None is explicitly passed,
        # we should NOT try to get a backend from the provider
        if not _l1_only_mode and _backend is None:
            try:
                _backend = _resolve_lazy_backend()
            except Exception as e:
                # If backend creation fails, can't invalidate L2
                _logger.debug("Failed to get backend for async invalidation: %s", redact_error_for_log(e))

        if interop is not None:  # interop guard, as in invalidate_cache
            ensure_interop_backend_compatible(_backend)

        # Fix #59: When called with no args on a parameterized function,
        # invalidate ALL cached entries for this function.
        if not args and not kwargs and _func_has_params:
            # Off the event loop: every L2 call in here is a sync Redis/backend round-trip.
            await asyncio.to_thread(_drain_all)
            return

        # Single-key invalidation (specific args provided, or zero-param function).
        # Same derivation as the write path (LAB-4387), plus the pre-0.20.0 twin (LAB-5288). The
        # sync deletes run off the loop; never a backend's delete_async. CachekitIO's async client
        # follows the running loop, but the Redis provider caches one async client per provider,
        # whose pool stays bound to the first event loop that used it (asyncio.run per job).
        await asyncio.to_thread(_invalidate_keys, _resolve_invalidation_keys(args, kwargs))

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
            Statistics are tracked per function identity (``module.qualname``),
            shared across all threads and all decorator applications, with
            thread-safe locking via RLock. Re-applying a decorator to the same
            function reuses the existing counters rather than resetting them —
            the session ID is stable across re-decorations, and counters that
            reset under an unchanged session ID would trip the server's
            anti-replay validation. Consequently, decorating the same function
            twice (even with different options) reports combined statistics
            from one shared tracker.

        Fork behavior:
            A forked child process starts with zeroed counters and derives a
            new session ID from its own process UUID; the parent's statistics
            are unaffected.

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
        # In L1-only mode, invalidation is synchronous (no backend I/O needed)
        # so cache_clear() works for both sync and async functions.
        if inspect.iscoroutinefunction(func) and not _l1_only_mode:
            raise TypeError(
                "cache_clear() cannot clear cache for async functions with a backend. Use 'await fn.ainvalidate_cache()' instead."
            )
        invalidate_cache()

    # Other processes' invalidations reach this function's L1 through the process's listener. Only a
    # backed wrapper with an L1 registers: in L1-only mode nothing is shared, so nothing is announced.
    # Registered weakly: the wrapper's _cachekit_evict attribute is what keeps _evict alive.
    if _l1_cache is not None and not _l1_only_mode:
        invalidation.register(_registry_id, _evict)

    if inspect.iscoroutinefunction(func):
        async_wrapper._cachekit_evict = _evict  # type: ignore[attr-defined]
        async_wrapper.invalidate_cache = ainvalidate_cache  # type: ignore[attr-defined]
        async_wrapper.ainvalidate_cache = ainvalidate_cache  # async version  # type: ignore[attr-defined]
        async_wrapper.check_health = acheck_health  # async version  # type: ignore[attr-defined]
        async_wrapper.get_health_status = get_health_status  # type: ignore[attr-defined]
        async_wrapper.cache_info = cache_info  # type: ignore[attr-defined]
        async_wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
        async_wrapper.__wrapped__ = func  # type: ignore[attr-defined]
        return async_wrapper  # type: ignore[return-value]
    else:
        sync_wrapper._cachekit_evict = _evict  # type: ignore[attr-defined]
        sync_wrapper.invalidate_cache = invalidate_cache  # type: ignore[attr-defined]
        sync_wrapper.check_health = check_health  # type: ignore[attr-defined]
        sync_wrapper.get_health_status = get_health_status  # type: ignore[attr-defined]
        sync_wrapper.cache_info = cache_info  # type: ignore[attr-defined]
        sync_wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
        sync_wrapper.__wrapped__ = func  # type: ignore[attr-defined]
        return sync_wrapper  # type: ignore[return-value]
