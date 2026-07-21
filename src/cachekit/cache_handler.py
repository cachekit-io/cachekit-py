"""Focused cache handling classes following SOLID principles.

This module breaks down the cache decorator into focused,
single-responsibility classes that are easier to test and maintain.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import warnings
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional, Protocol, TypeGuard, Union, runtime_checkable

from cachekit.backends.base import BackendError, BaseBackend, BufferHandle, BufferReadableBackend, TTLInspectableBackend
from cachekit.backends.provider import (
    BackendProviderInterface,
    DefaultBackendProvider,
    DefaultLoggerProvider,
    LoggerProvider,
)
from cachekit.config import ConfigurationError, get_settings
from cachekit.di import DIContainer
from cachekit.interop import InteropError
from cachekit.key_generator import CacheKeyGenerator
from cachekit.serializers.base import (
    SerializationError,
    SerializationFormat,
    SerializationMetadata,
    SuspiciousCacheEntryError,
)
from cachekit.serializers.encryption_wrapper import DecryptionAuthenticationError
from cachekit.serializers.wrapper import SerializationWrapper

if TYPE_CHECKING:
    from cachekit.serializers.base import SerializerProtocol

# Serializer string names whose wire format is language-agnostic and therefore safe to
# use under encryption (Issue #134). 'auto' is intentionally excluded — it emits
# Python-specific type tags that no other-language SDK can decode.
CROSS_SDK_SERIALIZER_NAMES = ("default", "std", "standard", "orjson", "arrow")

# Serializer-name aliases collapsed to one canonical frame tag so interchangeable names stay
# cache-compatible: an entry written as 'auto' must read back under 'pythonic' (its documented
# alias) and vice-versa, instead of a serializer-mismatch that recomputes on every read (#167).
_SERIALIZER_NAME_ALIASES = {"std": "default", "standard": "default", "pythonic": "auto"}

# Global DI container instance with default registrations
container = DIContainer()
container.register(LoggerProvider, DefaultLoggerProvider)
container.register(BackendProviderInterface, DefaultBackendProvider)

# Dependencies are injected at runtime, not module load time
# This ensures test isolation works properly with DI


def get_logger_provider():
    """Get the current LoggerProvider from DI container."""
    return container.get(LoggerProvider)


def get_backend_provider():
    """Get the current BackendProviderInterface from DI container."""
    return container.get(BackendProviderInterface)


def redact_cache_key(cache_key: object) -> str:
    """Redact a cache key for log/error messages.

    Cache keys can embed caller-supplied tenant/user identifiers, so they must never reach
    logs verbatim (issue #163). A fixed-length blake2b digest keeps messages correlatable
    across the sync and async cache-set failure paths without leaking the key itself.
    """
    return f"<redacted:{hashlib.blake2b(str(cache_key).encode('utf-8'), digest_size=8).hexdigest()}>"


# Lazy logger initialization to avoid import-time container access
_logger = None


def get_logger():
    """Get or initialize logger lazily."""
    global _logger
    if _logger is None:
        _logger = get_logger_provider().get_logger(__name__)
    return _logger


# Constants for locking
LOCK_TIMEOUT = 10  # Lock expires after 10 seconds to prevent deadlocks
LOCK_BLOCKING_TIMEOUT = 5  # Wait max 5 seconds to acquire the lock
LOCK_RETRY_INTERVAL = 0.1  # Sleep for 100ms between retries after lock fails


def _record_security_counter(name: str, labels: dict[str, str]) -> None:
    """Best-effort security-telemetry counter — must never break the read path.

    The reliability import stays inside the guard deliberately: if the metrics
    stack itself fails to import, that failure is swallowed like any other
    telemetry error rather than taking down cache reads.
    """
    try:
        from cachekit.reliability.async_metrics import get_async_metrics_collector

        get_async_metrics_collector().record_counter(name, labels=labels)
    except Exception:  # noqa: S110 — telemetry must never break the read path
        pass


def handle_decrypt_failure(error: Exception, *, tier: str, cache_key: str, fail_closed: bool) -> str:
    """Classify a decrypt/integrity failure, record telemetry, and apply the fail policy.

    This is the SINGLE implementation of the read-path security policy
    (cachekit-py#170); every catch site delegates here so the sites cannot
    drift. Classification is type-based, the metric is recorded before any
    raise (fail-closed events are always counted), and the fail-open/fail-closed
    decision lives in exactly one place.

    Failure classes — the ``reason`` label on ``cachekit_decrypt_failures_total``:

    - ``auth_tamper``: DecryptionAuthenticationError — AES-GCM tag verification
      failed (tampered ciphertext, wrong key, AAD/cache_key mismatch), tenant
      mismatch, or key-fingerprint mismatch under fail-closed. The signal an
      active attack produces; honored by the fail-closed policy.
    - ``suspicious_envelope``: SuspiciousCacheEntryError — the unauthenticated
      envelope is inconsistent with handler config (plaintext claim under
      encryption, missing tenant_id). Benign during lazy plaintext→encrypted
      migration, so it ALWAYS fails open (miss + evict, LAB-241) — but spikes
      outside a migration window warrant investigation.
    - ``corruption``: any other SerializationError — checksum mismatch, malformed
      frame, serializer mismatch, deserialize failure on authenticated
      plaintext. Not tamper evidence; always fails open.

    Args:
        error: The exception raised by the deserialize path.
        tier: Cache tier where the failure surfaced ("l1" or "l2").
        cache_key: Cache key being read. Logged only in redacted form
            (redact_cache_key, issue #163 — keys can embed tenant/user
            identifiers); metric labels stay bounded and never carry it.
        fail_closed: The handler's resolved encryption fail-closed policy.

    Returns:
        The reason label, when the policy is fail-open.

    Raises:
        DecryptionAuthenticationError: re-raises ``error`` when ``fail_closed``
            is True and the failure is tamper-class (fail closed). Callers do
            their tier-specific cleanup (L1 invalidation happens BEFORE calling
            this; L2 eviction happens after a fail-open return, so a fail-closed
            raise retains the entry as evidence).
    """
    if isinstance(error, DecryptionAuthenticationError):
        reason = "auth_tamper"
    elif isinstance(error, SuspiciousCacheEntryError):
        reason = "suspicious_envelope"
    else:
        reason = "corruption"

    _record_security_counter("cachekit_decrypt_failures_total", {"reason": reason, "tier": tier})

    if fail_closed and isinstance(error, DecryptionAuthenticationError):
        get_logger().error(
            f"{tier.upper()} cache decrypt AUTHENTICATION failure for {redact_cache_key(cache_key)}; "
            f"failing closed (encryption.fail_closed=True): {error}"
        )
        raise error
    get_logger().warning(f"{tier.upper()} cache decrypt/integrity failure ({reason}) for {redact_cache_key(cache_key)}: {error}")
    return reason


def supports_ttl_inspection(backend: BaseBackend) -> TypeGuard[TTLInspectableBackend]:
    """Type guard to check if backend supports TTL inspection and refresh.

    Args:
        backend: Backend instance to check

    Returns:
        True if backend implements TTLInspectableBackend protocol

    Note:
        This uses TypeGuard to enable proper type narrowing in conditional blocks.
        After this check, the type checker knows backend is TTLInspectableBackend.
    """
    return hasattr(backend, "get_ttl") and hasattr(backend, "refresh_ttl")


# Backend type names already warned about, so refresh_ttl_on_get degradation warns at most
# once per backend type per process (avoids per-hit log spam). Tests clear this set.
_TTL_REFRESH_UNSUPPORTED_WARNED: set[str] = set()


def warn_ttl_refresh_unsupported(backend: BaseBackend) -> None:
    """Warn ONCE per backend type that ``refresh_ttl_on_get=True`` has no effect here.

    Backends without TTLInspectableBackend (both get_ttl and refresh_ttl) cannot do
    threshold-based TTL refresh, so the flag is silently ignored. A silent no-op on a
    feature the user explicitly opted into is a footgun — surface it once (LAB-446), while
    still degrading gracefully (the caller returns without failing the cache op).
    """
    name = type(backend).__name__
    if name in _TTL_REFRESH_UNSUPPORTED_WARNED:
        return
    _TTL_REFRESH_UNSUPPORTED_WARNED.add(name)
    warnings.warn(
        f"refresh_ttl_on_get=True has no effect on {name}: it does not support TTL "
        f"inspection (needs both get_ttl and refresh_ttl), so the setting is ignored. Use "
        f"the Redis, CachekitIO, or File backend for TTL refresh.",
        UserWarning,
        stacklevel=3,
    )


def supports_buffer_read(backend: BaseBackend) -> TypeGuard[BufferReadableBackend]:
    """Type guard: backend can return a zero-copy buffer via get_buffer (#171, File/POSIX only).

    Returns:
        True if backend implements BufferReadableBackend (used for the mmap Arrow read fast path).
    """
    return hasattr(backend, "get_buffer")


class SWRCapableBackend(Protocol):
    """Backend with server-signaled stale-while-revalidate reads (LAB-381).

    Reads report whether the entry is in its stale-grace window; writes accept
    the window length. Currently only CachekitIOBackend (the SaaS signals
    freshness on read — see protocol spec/saas-api.md#stale-while-revalidate).
    """

    def get_with_freshness(self, key: str) -> Optional[tuple[bytes, bool]]: ...

    def set(self, key: str, value: bytes, ttl: Optional[int] = None, stale_ttl: Optional[int] = None) -> None: ...


def supports_swr(backend: BaseBackend) -> TypeGuard[SWRCapableBackend]:
    """Type guard: backend supports server-signaled SWR stale-grace reads (LAB-381)."""
    return hasattr(backend, "get_with_freshness")


# Import caching for serializer modules
#
# PERFORMANCE OPTIMIZATION: Dynamic imports are expensive (~100μs per import)
# which matters significantly for a caching library called frequently.
#
# THREAD SAFETY: Multiple threads importing the same serializer simultaneously
# could cause race conditions. RLock prevents this with double-checked locking.
#
# MEMORY EFFICIENCY: Avoids keeping multiple copies of the same serializer
# class in memory across different parts of the application.
#
# GIL CONTENTION: Reduces Python's Global Interpreter Lock contention from
# repeated module loading operations in multi-threaded environments.

_serializer_cache_lock = threading.RLock()
_serializer_cache: dict[str, type] = {}

# Pre-create serializer instances to eliminate 625μs overhead
_serializer_instance_cache = {}
_serializer_instance_lock = threading.RLock()


def _get_cached_serializer_class(serializer_name: str, import_path: str):
    """Thread-safe cached import of serializer classes.

    Args:
        serializer_name: Name of the serializer (e.g., 'rust', 'msgpack')
        import_path: Full import path (e.g., 'cachekit.serializers.RustSerializer')

    Returns:
        Cached serializer class
    """
    cache_key = f"{serializer_name}:{import_path}"

    # Fast path: check if already cached (without lock for read performance)
    if cache_key in _serializer_cache:
        return _serializer_cache[cache_key]

    # Slow path: acquire lock and import
    with _serializer_cache_lock:
        # Double-checked locking pattern - verify still not cached after acquiring lock
        # This prevents race conditions where two threads both pass the first check
        # but only one should actually perform the import
        if cache_key in _serializer_cache:
            return _serializer_cache[cache_key]

        # Import the module and get the class
        # This is the expensive operation we're caching (~100μs per dynamic import)
        try:
            module_path, class_name = import_path.rsplit(".", 1)
            module = __import__(module_path, fromlist=[class_name])
            serializer_class = getattr(module, class_name)

            # Cache the imported class for future use
            # Key format: "serializer_name:full.import.path" for uniqueness
            _serializer_cache[cache_key] = serializer_class
            get_logger().debug(f"Cached serializer import: {serializer_name} -> {import_path}")

            return serializer_class
        except (ImportError, AttributeError) as e:
            get_logger().warning(f"Failed to import serializer {import_path}: {e}")
            raise


def _get_cached_serializer_instance(
    serializer: Union[str, SerializerProtocol], enable_integrity_checking: bool = True
) -> SerializerProtocol:  # type: ignore[name-defined]
    """Get serializer instance with configurable integrity checking.

    This eliminates the expensive SerializerFactory.create_serializer() path
    that was doing validation, imports, and instance creation on every call.

    Now uses the unified get_serializer() factory from serializers/__init__.py,
    which supports pluggable serializers via SERIALIZER_REGISTRY.

    Note: Encryption is now a separate layer (not a serializer type).
    Use encryption=True parameter instead of serializer="encrypted".

    Args:
        serializer: Either a string name ("default", "arrow", "orjson") or SerializerProtocol instance
        enable_integrity_checking: Enable integrity checking (default: True)
            Uses xxHash3-64 for all serializers (Rust ByteStorage for default/auto,
            Python xxhash for arrow/orjson).

    Returns:
        Serializer instance implementing SerializerProtocol

    Raises:
        ValueError: If serializer_name not in SERIALIZER_REGISTRY
        TypeError: If serializer is not a string or SerializerProtocol instance
    """
    # If already a protocol instance, validate and return directly
    if not isinstance(serializer, str):
        from cachekit.serializers.base import SerializerProtocol

        if not isinstance(serializer, SerializerProtocol):
            raise TypeError(
                f"serializer must be a string name or SerializerProtocol instance, got {type(serializer).__name__}. "
                f"Valid string names: 'default', 'arrow', 'orjson'. For custom serializers, implement SerializerProtocol."
            )
        # Return protocol instance directly (no caching for custom instances)
        return serializer

    # String name - use cached lookup with integrity_checking setting
    serializer_name = serializer
    cache_key = f"{serializer_name}:{enable_integrity_checking}"

    # Fast path: check if already cached (lock-free read)
    if cache_key in _serializer_instance_cache:
        return _serializer_instance_cache[cache_key]

    # Slow path: create and cache instance (with lock)
    with _serializer_instance_lock:
        # Double-checked locking pattern
        if cache_key in _serializer_instance_cache:
            return _serializer_instance_cache[cache_key]

        # Use unified serializer factory (supports "default", "arrow", "orjson", etc.)
        try:
            from cachekit.serializers import get_serializer

            instance = get_serializer(serializer_name, enable_integrity_checking=enable_integrity_checking)
        except ValueError as e:
            # Re-raise with additional context about encryption
            raise ValueError(f"{e} For encryption, use encryption=True parameter (not serializer='encrypted').") from e

        # Cache the instance
        _serializer_instance_cache[cache_key] = instance
        return instance


class CacheSerializationHandler:
    """Handles serialization/deserialization with optional encryption layer.

    Architecture:
    - Serializer: Defines HOW to serialize (default/msgpack, future: pickle, json)
    - Encryption: Defines WHETHER to encrypt (security layer on top, orthogonal)
    - Tenant extraction: For multi-tenant encryption key isolation (FAIL CLOSED)

    Modes (encryption is tri-state: None=auto / True=force-on / False=hard opt-out):
    - encryption=None: Auto-detect from CACHEKIT_MASTER_KEY (single-tenant if a key is present)
    - encryption=False: Explicit opt-out — direct serialization (plaintext), even if a master key is set
    - encryption=True, tenant_extractor=None: Single-tenant encrypted (nil UUID)
    - encryption=True, tenant_extractor provided: Multi-tenant encrypted (FAIL CLOSED)

    Examples:
        Basic usage without encryption:

        >>> handler = CacheSerializationHandler(serializer_name="default")
        >>> data = {"user_id": 123, "name": "Alice"}
        >>> serialized = handler.serialize_data(data, cache_key="user:123")
        >>> isinstance(serialized, bytes)
        True

        With encryption (single-tenant mode) - requires resetting the settings singleton
        first to pick up the new environment variable:

        >>> import os
        >>> from cachekit.config.singleton import reset_settings
        >>> reset_settings()  # Clear cached settings
        >>> os.environ["CACHEKIT_MASTER_KEY"] = "a" * 64  # 32-byte hex key
        >>> handler = CacheSerializationHandler(
        ...     serializer_name="default",
        ...     encryption=True,
        ...     single_tenant_mode=True,
        ...     deployment_uuid="00000000-0000-0000-0000-000000000001",
        ... )
        >>> # Encryption requires cache_key for AAD binding
        >>> serialized = handler.serialize_data(
        ...     {"secret": "password"},
        ...     cache_key="user:123:credentials"
        ... )
        >>> isinstance(serialized, bytes)
        True
        >>> reset_settings()  # Cleanup
        >>> del os.environ["CACHEKIT_MASTER_KEY"]
    """

    def __init__(
        self,
        serializer_name: Union[str, SerializerProtocol] = "default",  # type: ignore[name-defined]
        encryption: bool | None = None,
        tenant_extractor: Any | None = None,
        single_tenant_mode: bool = False,
        deployment_uuid: Optional[str] = None,
        master_key: Optional[str] = None,
        enable_integrity_checking: bool = True,
        encryption_fail_closed: bool | None = None,
        interop_mode: bool = False,
    ):
        """Initialize with serializer strategy and optional encryption.

        Args:
            serializer_name: Serializer instance or name. Accepts either:
                            - String name: "default" (MessagePack), "arrow" (DataFrame zero-copy), "orjson" (JSON)
                            - SerializerProtocol instance: Custom serializer implementing the protocol
            encryption: Tri-state encryption control (wraps serializer with EncryptionWrapper):
                        - None (default): auto-detect from CACHEKIT_MASTER_KEY. Single-tenant mode
                          is auto-enabled when a master key is present.
                        - True: force encryption ON (requires a master key + explicit tenant mode).
                        - False: explicit hard opt-out. Never encrypts, even when CACHEKIT_MASTER_KEY
                          is set fleet-wide. This is the deliberate per-function escape hatch.
            tenant_extractor: Optional TenantContextExtractor for multi-tenant encryption.
                             Only used if encryption=True.
                             If None: single-tenant mode (uses nil UUID).
                             If provided: multi-tenant mode (extracts tenant_id, FAIL CLOSED).
            single_tenant_mode: Explicitly enable single-tenant mode (requires encryption=True).
                               Mutually exclusive with tenant_extractor.
            deployment_uuid: Optional deployment-specific UUID for single-tenant mode.
                            If not provided, uses env var or persistent file.
            master_key: Optional master key for encryption (hex-encoded). If not provided,
                       reads from CACHEKIT_MASTER_KEY environment variable.
            enable_integrity_checking: Enable integrity checking (default: True)
                                      Uses xxHash3-64 (8 bytes) for all serializers.
                                      Set to False for @cache.minimal (speed-first, no checksums)
                                      NOTE: xxHash3-64 is CORRUPTION detection only — it is not
                                      cryptographic and offers no tamper resistance. Tamper
                                      resistance requires encryption (AES-256-GCM).
            encryption_fail_closed: Tri-state tamper-failure policy:
                                   - None (default): defer to CACHEKIT_ENCRYPTION_FAIL_CLOSED
                                     env setting (defaults to False = fail open).
                                   - True: raise DecryptionAuthenticationError to the caller on
                                     AES-GCM authentication failure or key-fingerprint mismatch
                                     instead of silently recomputing.
                                   - False: explicit fail-open opt-out (warn + metric + recompute).

        Raises:
            ConfigurationError: If encryption config is invalid (missing mode or both modes).
            TypeError: If serializer_name is not a string or SerializerProtocol instance.

        Note:
            FAIL CLOSED security policy: If encryption=True and tenant_extractor provided
            but extraction fails, ValueError propagates to caller (no fallback to shared key).
        """
        self.serializer_name = serializer_name
        self.enable_integrity_checking = enable_integrity_checking
        self.interop_mode = interop_mode
        self._deployment_uuid_value: Optional[str] = None

        # Interop mode (interop/v1, spec/interop-mode.md): values are ONE plain
        # MessagePack document — no ByteStorage envelope and no CK v3 frame, so
        # there is no stored metadata header at all. Whether an entry is
        # encrypted is decided by THIS handler's configuration, never by
        # sniffing stored bytes (fail closed by construction; the LAB-241
        # downgrade cannot be reintroduced because there is no header to forge).
        if interop_mode:
            if tenant_extractor is not None:
                raise ConfigurationError(
                    "interop mode does not support tenant_extractor (multi-tenant) encryption: "
                    "interop entries store no metadata header, so the read path cannot recover "
                    "a per-call tenant. Use single-tenant encryption with an explicitly shared "
                    "CACHEKIT_DEPLOYMENT_UUID across SDKs instead."
                )
            if isinstance(serializer_name, str) and _SERIALIZER_NAME_ALIASES.get(serializer_name, serializer_name) != "default":
                raise ConfigurationError(
                    f"interop mode requires the default (MessagePack) serializer, got '{serializer_name}': "
                    f"interop/v1 values are plain MessagePack by specification."
                )
            if not isinstance(serializer_name, str):
                raise ConfigurationError(
                    "interop mode does not accept a custom serializer instance: interop/v1 values "
                    "are canonical plain MessagePack produced by the built-in interop encoder."
                )

        # Tri-state encryption resolution. `encryption` is None/True/False:
        #   None  -> auto-detect from CACHEKIT_MASTER_KEY (fleet-wide convergence point)
        #   True  -> explicit force-on (validated below)
        #   False -> explicit hard opt-out; honored even when a master key is present
        #
        # Auto-detection is the ONLY path that may flip encryption on and auto-set
        # single_tenant_mode. An explicit False MUST NOT be promoted to True just
        # because CACHEKIT_MASTER_KEY exists (issue #128).
        if encryption is None:
            encryption = False
            if master_key is None and tenant_extractor is None:
                settings = get_settings()
                if settings.master_key:
                    encryption = True
                    master_key = settings.master_key.get_secret_value()
                    single_tenant_mode = True

        self.encryption = encryption
        self.tenant_extractor = tenant_extractor
        self.single_tenant_mode = single_tenant_mode
        self.deployment_uuid = deployment_uuid
        self.master_key = master_key

        # Tri-state fail-closed resolution (mirrors the `encryption` tri-state, issue #128):
        # an explicit True/False from EncryptionConfig wins; None defers to the fleet-wide
        # CACHEKIT_ENCRYPTION_FAIL_CLOSED env setting (default False = historical fail-open).
        # Resolved independently of self.encryption because an encryption-disabled handler can
        # still decrypt stale encrypted entries (config-drift reads) and must honor the policy.
        if encryption_fail_closed is None:
            encryption_fail_closed = get_settings().encryption_fail_closed
        self.encryption_fail_closed = encryption_fail_closed

        # Config-drift warn-once bookkeeping: first drift read per cache_key warns,
        # the rest only increment cachekit_config_drift_reads_total (log-flood guard).
        # ponytail: bounded set — beyond 1024 distinct drifted keys, counter-only.
        self._drift_warned_keys: set[str] = set()

        # Extract string name for metadata storage (for protocol instances, use class name)
        if isinstance(serializer_name, str):
            # Canonicalize aliases to prevent envelope mismatch on deserialize
            self._serializer_string_name = _SERIALIZER_NAME_ALIASES.get(serializer_name, serializer_name)
        else:
            # Protocol instance - use class name for metadata
            self._serializer_string_name = type(serializer_name).__name__

        # MEDIUM-02: Validate single-tenant mode configuration
        if self.encryption:
            # Require explicit tenant mode (either extractor OR single_tenant_mode)
            if not self.tenant_extractor and not self.single_tenant_mode:
                raise ConfigurationError(
                    "Encryption requires explicit tenant mode. "
                    "Provide tenant_extractor for multi-tenant OR "
                    "set single_tenant_mode=True with deployment_uuid for single-tenant."
                )

            # Prevent both modes from being enabled simultaneously
            if self.tenant_extractor and self.single_tenant_mode:
                raise ConfigurationError(
                    "Cannot use both tenant_extractor and single_tenant_mode. "
                    "Choose multi-tenant (tenant_extractor) OR single-tenant (single_tenant_mode=True)."
                )

            # Issue #134: Encryption requires a cross-SDK-compatible serializer so the
            # encrypted bytes remain decodable by other-language SDKs. The user's
            # serializer is threaded into EncryptionWrapper (see
            # _get_cached_encryption_wrapper); it is NOT silently replaced. We therefore
            # allow any serializer that produces a language-agnostic wire format and reject
            # the rest (notably 'auto' and unmarked custom instances) with a clear error.
            if isinstance(serializer_name, str):
                if serializer_name not in CROSS_SDK_SERIALIZER_NAMES:
                    raise ConfigurationError(
                        f"Encryption requires a cross-SDK-compatible serializer for cross-language "
                        f"interop, got serializer='{serializer_name}'. Allowed under encryption: "
                        f"{', '.join(CROSS_SDK_SERIALIZER_NAMES)}. The 'auto' serializer emits "
                        f"Python-specific types that other SDKs cannot decode, so it cannot be used "
                        f"with encryption."
                    )
            elif not getattr(type(serializer_name), "cross_sdk_compatible", False):
                raise ConfigurationError(
                    f"Encryption requires a cross-SDK-compatible serializer for cross-language interop. "
                    f"The serializer instance '{type(serializer_name).__name__}' does not declare "
                    f"cross_sdk_compatible=True. Custom serializers used with encryption must set the "
                    f"cross_sdk_compatible ClassVar to True and guarantee a language-agnostic wire format."
                )

            # Generate deterministic deployment UUID for single-tenant mode
            if self.single_tenant_mode:
                self._deployment_uuid_value = self._get_deterministic_deployment_uuid(provided_uuid=self.deployment_uuid)
                get_logger().info(
                    "Single-tenant mode initialized",
                    extra={
                        "deployment_uuid": self._deployment_uuid_value,
                        "source": "provided" if self.deployment_uuid else "auto-generated",
                    },
                )

        # Use cached base serializer instance with integrity_checking setting
        # Encryption wrapper is created per-request with tenant_id (if encryption=True)
        if interop_mode:
            # Interop values bypass ByteStorage entirely; integrity checking is
            # meaningless here (tamper protection comes from AES-GCM when
            # encryption is on). InteropSerializer is stateless — no cache needed.
            from cachekit.serializers.interop_serializer import InteropSerializer

            self._base_serializer = InteropSerializer()
        else:
            self._base_serializer = _get_cached_serializer_instance(serializer_name, enable_integrity_checking)

        # CRITICAL-03 FIX: Cache EncryptionWrapper instances per tenant to prevent
        # 360K key copies/hour at 100 req/sec. Uses thread-safe LRU cache (maxsize=256)
        # with double-checked locking pattern (OrderedDict + RLock, NOT functools.lru_cache)
        from collections import OrderedDict

        self._encryption_wrapper_cache: OrderedDict[str, Any] = OrderedDict()  # tenant_id -> EncryptionWrapper
        self._encryption_cache_lock = threading.RLock()
        self._encryption_cache_maxsize = 256

    def _get_deterministic_deployment_uuid(self, provided_uuid: Optional[str]) -> str:
        """Get deployment UUID with determinism guarantee (MEDIUM-02, Criterion 2).

        Deterministic UUID ensures encrypted cache data remains readable after restarts.
        Non-deterministic UUID (e.g., using time.time()) causes complete cache invalidation.

        Priority order:
        1. Explicit provided_uuid (user-controlled, highest priority)
        2. Environment variable CACHEKIT_DEPLOYMENT_UUID (recommended for prod)
        3. Persistent file storage (auto-generated, survives restarts)
        4. NEVER use time.time() or random values (breaks decryption)

        Args:
            provided_uuid: Optional UUID provided by user

        Returns:
            Validated and deterministic deployment UUID

        Raises:
            ConfigurationError: If UUID format is invalid
        """
        import uuid
        from pathlib import Path

        # Option 1: Explicit UUID provided by user
        if provided_uuid:
            try:
                # Validate UUID format
                validated_uuid = str(uuid.UUID(provided_uuid))
                self._require_canonical_tenant_form(provided_uuid, validated_uuid, source="deployment_uuid parameter")
                get_logger().info(f"Using provided deployment UUID: {validated_uuid}")
                return validated_uuid
            except ValueError as e:
                raise ConfigurationError(
                    f"Invalid deployment_uuid format (must be valid UUID): {provided_uuid}. Error: {e}"
                ) from e

        # Option 2: Configuration (recommended for production)
        settings = get_settings()
        if settings.deployment_uuid:
            try:
                validated_uuid = str(uuid.UUID(settings.deployment_uuid))
                self._require_canonical_tenant_form(settings.deployment_uuid, validated_uuid, source="CACHEKIT_DEPLOYMENT_UUID")
                get_logger().info(f"Using deployment UUID from configuration: {validated_uuid}")
                return validated_uuid
            except ValueError as e:
                raise ConfigurationError(
                    f"Invalid deployment_uuid in configuration (must be valid UUID): {settings.deployment_uuid}. Error: {e}"
                ) from e

        # Interop mode never falls through to the machine-local sources below:
        # the persistent-file / freshly-generated UUID is random PER HOST, so
        # two processes (or two SDKs) would silently derive different AES keys
        # — every cross-host read fails auth, entries evict each other in a
        # recompute loop, and on metered-misses billing every miss costs money.
        # Cross-SDK encryption only works with an explicitly shared tenant.
        if self.interop_mode:
            raise ConfigurationError(
                "interop mode with encryption requires an explicitly shared deployment UUID "
                "(deployment_uuid parameter or CACHEKIT_DEPLOYMENT_UUID): the auto-generated "
                "machine-local UUID differs per host, so other processes and SDKs could never "
                "decrypt entries written here. Configure the same canonical lowercase UUID "
                "in every SDK sharing this cache."
            )

        # Option 3: Persistent file storage (auto-generated, survives restarts)
        deployment_uuid_file = Path.home() / ".cachekit" / "deployment_uuid"

        if deployment_uuid_file.exists():
            # Read existing UUID from file
            stored_uuid = deployment_uuid_file.read_text().strip()
            try:
                validated_uuid = str(uuid.UUID(stored_uuid))
                self._require_canonical_tenant_form(stored_uuid, validated_uuid, source=str(deployment_uuid_file))
                get_logger().info(f"Using persistent deployment UUID from {deployment_uuid_file}")
                return validated_uuid
            except ValueError:
                # Corrupted file - regenerate
                get_logger().warning(f"Corrupted deployment UUID file: {deployment_uuid_file}. Regenerating...")

        # Generate new UUID and persist to file
        new_uuid = str(uuid.uuid4())
        try:
            deployment_uuid_file.parent.mkdir(parents=True, exist_ok=True)
            deployment_uuid_file.write_text(new_uuid)
            deployment_uuid_file.chmod(0o600)  # Read/write for owner only
            get_logger().info(f"Generated and persisted new deployment UUID: {new_uuid} at {deployment_uuid_file}")
        except Exception as e:
            get_logger().error(
                f"Failed to persist deployment UUID to {deployment_uuid_file}: {e}. "
                "UUID will be regenerated on next restart (cache will be invalidated)."
            )

        return new_uuid

    def _require_canonical_tenant_form(self, raw: str, canonical: str, source: str) -> None:
        """Interop mode: reject a deployment UUID that is not already canonical.

        The tenant string feeds HKDF key derivation and AAD component 1. Python
        normalizes through uuid.UUID() (lowercases, strips braces/URN); another
        SDK reading the same shared config would use the raw string — different
        derived keys, silent cross-SDK auth failures, mutual entry eviction.
        Interop mode therefore requires the configured value to be byte-equal to
        its canonical lowercase-hyphenated form, so what Python derives from IS
        the literal string every other SDK sees. Auto mode is unaffected
        (normalization there is long-shipped behavior).
        """
        if self.interop_mode and raw.strip() != canonical:
            raise ConfigurationError(
                f"interop mode requires the deployment UUID from {source} to be in canonical "
                f"lowercase-hyphenated form (got {raw!r}, canonical {canonical!r}): other SDKs "
                f"derive tenant keys from the raw string, so any normalization on the Python "
                f"side silently breaks cross-SDK decryption."
            )

    def _get_cached_encryption_wrapper(self, tenant_id: str) -> Any:
        """Get or create cached EncryptionWrapper for tenant_id.

        CRITICAL-03 FIX: Thread-safe LRU cache with double-checked locking.
        Prevents 360K key copies/hour at 100 req/sec by reusing EncryptionWrapper
        instances (which internally cache derived keys).

        Args:
            tenant_id: Tenant identifier for key isolation

        Returns:
            Cached or newly created EncryptionWrapper instance
        """
        # Fast path: check cache without lock (read-only, safe)
        if tenant_id in self._encryption_wrapper_cache:
            # Move to end for LRU (requires lock)
            with self._encryption_cache_lock:
                if tenant_id in self._encryption_wrapper_cache:
                    self._encryption_wrapper_cache.move_to_end(tenant_id)
                    return self._encryption_wrapper_cache[tenant_id]

        # Slow path: create new wrapper with lock
        with self._encryption_cache_lock:
            # Double-checked locking: verify still not cached after acquiring lock
            if tenant_id in self._encryption_wrapper_cache:
                self._encryption_wrapper_cache.move_to_end(tenant_id)
                return self._encryption_wrapper_cache[tenant_id]

            # Create new EncryptionWrapper
            from cachekit.serializers.encryption_wrapper import EncryptionWrapper

            # Convert master_key from hex string to bytes if provided
            master_key_bytes = bytes.fromhex(self.master_key) if self.master_key else None

            # Issue #134: thread the user's base serializer into the wrapper so a
            # cross-SDK-compatible serializer (Arrow/orjson) is actually used under
            # encryption instead of being silently replaced by StandardSerializer.
            # The base serializer is fixed per handler instance, so the per-tenant
            # cache key (tenant_id) remains correct — every wrapper for this handler
            # wraps the same _base_serializer.
            wrapper = EncryptionWrapper(
                serializer=self._base_serializer,
                tenant_id=tenant_id,
                master_key=master_key_bytes,
                fail_closed=self.encryption_fail_closed,
            )

            # Enforce LRU cache size limit
            if len(self._encryption_wrapper_cache) >= self._encryption_cache_maxsize:
                # Remove oldest (first) entry
                self._encryption_wrapper_cache.popitem(last=False)

            # Add new wrapper (at end)
            self._encryption_wrapper_cache[tenant_id] = wrapper
            get_logger().debug(f"Created and cached EncryptionWrapper for tenant: {tenant_id}")

            return wrapper

    def serialize_data(
        self,
        data: Any,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        cache_key: str = "",
    ) -> bytes:
        """Serialize data for cache storage with optional tenant context for encryption.

        Args:
            data: Data to serialize
            args: Positional arguments from cached function (for tenant extraction)
            kwargs: Keyword arguments from cached function (for tenant extraction)
            cache_key: Cache key for AAD binding (SECURITY CRITICAL for encryption).
                      Required when encryption is enabled to prevent ciphertext substitution.

        Returns:
            Serialized data wrapped for cache storage

        Raises:
            ValueError: If tenant extraction fails in multi-tenant mode (FAIL CLOSED)
            ValueError: If cache_key is empty when encryption is enabled
            ValueError: If the serialized envelope exceeds max_value_size
                (CACHEKIT_MAX_VALUE_SIZE) — the L2 oversized-entry ceiling
            SerializationError: If serialization fails

        Note:
            Tenant extraction uses FAIL CLOSED security policy:
            - If tenant_extractor provided: extracts tenant_id from args/kwargs or raises ValueError
            - If single_tenant_mode=True: uses deterministic deployment UUID

        Examples:
            Serialize a dictionary (no encryption):

            >>> handler = CacheSerializationHandler(serializer_name="default")
            >>> data = {"user": "alice", "score": 42}
            >>> result = handler.serialize_data(data, cache_key="scores:alice")
            >>> isinstance(result, bytes)
            True

            Serialize with different data types:

            >>> handler = CacheSerializationHandler()
            >>> handler.serialize_data([1, 2, 3], cache_key="list:test")  # doctest: +ELLIPSIS
            b'...'
            >>> handler.serialize_data("hello", cache_key="str:test")  # doctest: +ELLIPSIS
            b'...'
            >>> handler.serialize_data(None, cache_key="none:test")  # doctest: +ELLIPSIS
            b'...'

            Round-trip serialization:

            >>> handler = CacheSerializationHandler()
            >>> original = {"nested": {"list": [1, 2, 3]}, "flag": True}
            >>> serialized = handler.serialize_data(original, cache_key="complex:data")
            >>> recovered = handler.deserialize_data(serialized, cache_key="complex:data")
            >>> recovered == original
            True
        """
        kwargs = kwargs or {}

        try:
            # Wrap with encryption layer if requested (defines WHETHER to encrypt)
            if self.encryption:
                # Extract tenant_id based on configuration (FAIL CLOSED)
                if self.tenant_extractor:
                    # Multi-tenant mode: MUST extract tenant_id
                    # If extraction fails, ValueError bubbles up (FAIL CLOSED - no fallback)
                    tenant_id = self.tenant_extractor.extract(args, kwargs)
                else:
                    # MEDIUM-02: Single-tenant mode with deterministic UUID
                    # Uses cached deployment UUID (generated in __init__)
                    # Constructor guarantees single_tenant_mode=True here (validated in __init__)
                    if self._deployment_uuid_value is None:
                        raise RuntimeError("deployment_uuid should be set in __init__ for single-tenant mode")
                    tenant_id = self._deployment_uuid_value

                # CRITICAL-03 FIX: Use cached EncryptionWrapper to prevent 360K key copies/hour
                # Gets cached instance (thread-safe LRU, maxsize=256) instead of creating new one
                serializer = self._get_cached_encryption_wrapper(tenant_id)

                # EncryptionWrapper.serialize() requires cache_key for AAD v0x03 binding
                serialized_data, metadata = serializer.serialize(data, cache_key)
            else:
                # No encryption - use base serializer directly (no cache_key needed)
                serializer = self._base_serializer
                serialized_data, metadata = serializer.serialize(data)

            if self.interop_mode:
                # Interop/v1: the stored bytes ARE the document — plain MessagePack,
                # or nonce||ciphertext||tag when encrypted. No CK frame, no metadata
                # header; other SDKs must be able to read these bytes as-is.
                wrapped = serialized_data
            else:
                # Convert metadata to dict if needed
                metadata_dict = metadata.to_dict() if hasattr(metadata, "to_dict") else {}
                wrapped = SerializationWrapper.wrap(serialized_data, metadata_dict, self._serializer_string_name)
        except ValueError:
            # Tenant extraction, cache_key missing, or an interop data-model
            # rejection (InteropError) — FAIL CLOSED / fail loud (re-raise).
            raise
        except Exception as e:
            # Don't silently fallback - log error and raise to prevent data loss
            get_logger().error(f"Serialization failed with {self.serializer_name}: {e}")
            raise SerializationError(f"Failed to serialize data with {self.serializer_name}: {e}") from e

        # L2 oversized-entry ceiling (issue #163): every L2 write flows through here,
        # so this is the single enforcement point for max_value_size. Callers catch the
        # raise and degrade to uncached execution (warning logged, function result still
        # returned) — a cache must never break the wrapped function.
        max_value_size = get_settings().max_value_size
        if len(wrapped) > max_value_size:
            # Redact the raw cache key: it can carry caller-supplied tenant/user identifiers, and
            # this message reaches the fallback warning log path (issue #163 review).
            raise ValueError(
                f"Serialized value for key {redact_cache_key(cache_key)} is {len(wrapped)} bytes, exceeding "
                f"max_value_size ({max_value_size} bytes); refusing to cache. Increase "
                f"CACHEKIT_MAX_VALUE_SIZE to cache larger values."
            )
        return wrapped

    def supports_mmap_read(self) -> bool:
        """True iff reads can use the zero-copy mmap fast path (#171).

        Eligible only for PLAINTEXT Arrow that returns pandas:
        - encrypted values can never mmap (AES-GCM decrypt owns its buffer);
        - non-Arrow serializers gain nothing (they copy at the Rust/C boundary, rebuild objects);
        - the "arrow" return_format yields a table that ALIASES the mapped pages, so closing the
          handle would be a use-after-free — pandas (which copies out via to_pandas) only.

        The backend must also support buffer reads (File/POSIX); that is checked separately, so a
        True here on a non-File backend simply means get_buffer returns None and we fall back.
        """
        return (
            not self.encryption
            and self._serializer_string_name == "arrow"
            and getattr(self._base_serializer, "return_format", None) == "pandas"
        )

    def deserialize_data(self, data: str | bytes | memoryview, cache_key: str = "") -> Any:
        """Deserialize data from cache storage with cache_key verification.

        Args:
            data: Serialized data from cache (may be encrypted)
            cache_key: Cache key for AAD verification (SECURITY CRITICAL for encrypted data).
                      Required when data is encrypted to verify ciphertext binding.

        Returns:
            Deserialized Python object

        Raises:
            ValueError: If cache_key is empty when data is encrypted
            SerializationError: If deserialization fails (including AAD mismatch), or if
                this handler has encryption enabled and the entry's header claims
                plaintext — the header is unauthenticated, so an encryption-enabled
                handler never routes to the plaintext deserializer (fail closed,
                CWE-757 downgrade protection). Callers treat this as a cache miss.

        Examples:
            Basic round-trip (serialize then deserialize):

            >>> handler = CacheSerializationHandler()
            >>> original = {"name": "Bob", "age": 30, "active": True}
            >>> cache_key = "user:bob"
            >>> serialized = handler.serialize_data(original, cache_key=cache_key)
            >>> handler.deserialize_data(serialized, cache_key=cache_key)
            {'name': 'Bob', 'age': 30, 'active': True}

            Handles nested structures:

            >>> handler = CacheSerializationHandler()
            >>> nested = {"users": [{"id": 1}, {"id": 2}], "meta": {"count": 2}}
            >>> serialized = handler.serialize_data(nested, cache_key="users:all")
            >>> result = handler.deserialize_data(serialized, cache_key="users:all")
            >>> result["users"][0]["id"]
            1
            >>> result["meta"]["count"]
            2

            Preserves None and boolean types:

            >>> handler = CacheSerializationHandler()
            >>> data = {"value": None, "flag": False, "count": 0}
            >>> serialized = handler.serialize_data(data, cache_key="types:test")
            >>> result = handler.deserialize_data(serialized, cache_key="types:test")
            >>> result["value"] is None
            True
            >>> result["flag"] is False
            True
        """
        if self.interop_mode:
            return self._deserialize_interop(data, cache_key)

        try:
            # Unwrap cache data envelope
            serialized_data, metadata_dict, serializer_name = SerializationWrapper.unwrap(data)

            # Convert metadata
            serialization_metadata = _get_cached_serializer_class("metadata", "cachekit.serializers.SerializationMetadata")
            metadata = serialization_metadata.from_dict(metadata_dict)

            # Get base serializer
            base_serializer = self._base_serializer

            # Validate serializer compatibility - cached data must match decorator's serializer
            # This prevents deserialization errors when switching serializers
            if serializer_name != self._serializer_string_name and serializer_name != "unknown":
                raise SerializationError(
                    f"Serializer mismatch: cached data uses '{serializer_name}', "
                    f"but decorator configured with '{self._serializer_string_name}'. "
                    f"Cache entry is incompatible. Either flush cache or use cache key namespacing "
                    f"for gradual migrations: @cache(namespace='v2-{self._serializer_string_name}')"
                )

            # SECURITY (LAB-241 / CWE-757): the CK frame header is plaintext and NOT
            # covered by the AES-GCM tag (AAD v0x03 binds tenant/cache_key/format/
            # compressed — not the header itself). A backend-write attacker can plant
            # a frame claiming `encrypted: false` with an arbitrary plaintext payload;
            # an encryption-enabled reader must never let that header downgrade it to
            # the unauthenticated plaintext path. Fail closed — callers treat
            # SerializationError as a miss and evict, so legacy plaintext entries
            # written before encryption was enabled are recomputed and re-stored
            # encrypted rather than silently accepted
            # (see docs/features/zero-knowledge-encryption.md, "Fail-Closed Read Path").
            if self.encryption and not metadata.encrypted:
                # SuspiciousCacheEntryError → telemetry reason "suspicious_envelope":
                # legitimate during lazy plaintext→encrypted migration, an attack
                # signature otherwise. Behavior stays miss+evict either way (LAB-241).
                raise SuspiciousCacheEntryError(
                    "Encryption is enabled but the cache entry's header claims plaintext. "
                    "Refusing the unauthenticated plaintext read path (fail closed): the "
                    "header is not covered by the AES-GCM tag and may be forged. If this "
                    "entry predates enabling encryption, it will be recomputed and "
                    "re-stored encrypted on the next access; see "
                    "docs/features/zero-knowledge-encryption.md for eager-migration guidance."
                )

            # Determine serializer based on whether data is encrypted.
            # metadata.encrypted may still be True while self.encryption is False
            # (handler config changed but old encrypted data exists) — decrypting
            # is safe in that direction because it stays on the authenticated path.
            if metadata.encrypted:
                # SECURITY EVENT (cachekit-py#170, config-drift read): this handler has
                # encryption DISABLED yet is being handed an encrypted entry. Legitimate
                # cause: encryption was recently turned off and stale entries remain until
                # TTL expiry. But the same signature appears when an attacker plants an
                # encrypted entry or when configuration has silently drifted, and the
                # decrypt below falls back to the global CACHEKIT_MASTER_KEY — so this must
                # never happen silently. Warn on every occurrence; decryption itself stays
                # on the authenticated (AES-GCM) path, so a forged entry still fails auth.
                if not self.encryption:
                    _record_security_counter("cachekit_config_drift_reads_total", {"reason": "encryption_disabled"})
                    if cache_key not in self._drift_warned_keys and len(self._drift_warned_keys) < 1024:
                        self._drift_warned_keys.add(cache_key)
                        get_logger().warning(
                            f"Config-drift read for {redact_cache_key(cache_key) if cache_key else 'unknown key'}: "
                            f"handler has encryption disabled but the cache entry is encrypted (tenant "
                            f"'{metadata.tenant_id or 'unknown'}'). Decrypting via the globally configured "
                            f"master key. If encryption was not recently disabled for this function, "
                            f"investigate for misconfiguration or cache tampering. Further reads of this "
                            f"key count on cachekit_config_drift_reads_total without logging."
                        )

                # Data is encrypted - use cached EncryptionWrapper for decryption
                # CRITICAL-03 FIX: Use cached instance instead of creating new one
                if not metadata.tenant_id:
                    # Envelope claims encryption but lacks the tenant needed to derive the
                    # key — malformed or field-stripped. suspicious_envelope telemetry.
                    raise SuspiciousCacheEntryError(
                        "Encrypted cache entry is missing tenant_id in metadata. Cannot decrypt without tenant context."
                    )
                tenant_id = metadata.tenant_id
                serializer = self._get_cached_encryption_wrapper(tenant_id)

                # EncryptionWrapper.deserialize() requires cache_key for AAD v0x03 verification
                return serializer.deserialize(serialized_data, metadata, cache_key)
            else:
                # Data is not encrypted - use base serializer directly (no cache_key needed)
                return base_serializer.deserialize(serialized_data, metadata)
        except (ValueError, SerializationError):
            # ValueError: cache_key missing for encrypted data — FAIL CLOSED
            # SerializationError/EncryptionError: let the outer handler log and handle
            raise
        except Exception as e:
            get_logger().error(f"Deserialization failed with {self.serializer_name}: {e}")
            raise SerializationError(f"Failed to deserialize data with {self.serializer_name}: {e}") from e

    def _deserialize_interop(self, data: str | bytes | memoryview, cache_key: str) -> Any:
        """Interop/v1 read path: config decides encryption, never the stored bytes.

        Interop entries carry no metadata header, so there is nothing to sniff
        and nothing to forge: with encryption enabled the bytes are ALWAYS
        treated as nonce||ciphertext||tag and authenticated before any decode
        (fail closed — same CWE-757 posture as the auto-mode LAB-241 fix);
        without encryption they are decoded as one plain MessagePack document.
        Decode/auth failures raise SerializationError, which callers treat as
        a miss and evict (self-healing overwrite).
        """
        if isinstance(data, str):
            raise SerializationError("interop cache entries are binary; got str from backend")
        try:
            if self.encryption:
                if self._deployment_uuid_value is None:
                    raise SerializationError("interop encryption requires single-tenant mode (deployment UUID missing)")
                tenant_id = self._deployment_uuid_value
                wrapper = self._get_cached_encryption_wrapper(tenant_id)
                # Synthesize the metadata the wrapper needs: interop AAD is pinned
                # to format=msgpack, compressed=False, NO original_type (exactly
                # four AAD components). tenant/fingerprint come from config — an
                # attacker cannot influence them because nothing is read from the
                # stored bytes except the ciphertext itself.
                metadata = SerializationMetadata(
                    serialization_format=SerializationFormat.MSGPACK,
                    compressed=False,
                    original_type=None,
                    encrypted=True,
                    tenant_id=tenant_id,
                    encryption_algorithm="AES-256-GCM",
                    key_fingerprint=wrapper.encryption_key_fingerprint,
                )
                return wrapper.deserialize(data, metadata, cache_key)
            return self._base_serializer.deserialize(data)
        except (ValueError, SerializationError):
            raise
        except Exception as e:
            get_logger().error(f"Interop deserialization failed: {e}")
            raise SerializationError(f"Failed to deserialize interop cache entry: {e}") from e


class CacheOperationHandler:
    """Handles core cache operations - Single Responsibility.

    Orchestrates cache key generation, serialization, and backend storage.
    Follows the Strategy pattern for backend abstraction.

    Examples:
        Create handler with dependencies:

        >>> from cachekit.key_generator import CacheKeyGenerator
        >>> serialization = CacheSerializationHandler()
        >>> key_gen = CacheKeyGenerator()
        >>> handler = CacheOperationHandler(serialization, key_gen)

        Generate cache keys for function calls (format: ns:{namespace}:func:{module}.{name}:args:{hash}:{flags}):

        >>> def get_user(user_id: int): pass
        >>> handler.get_cache_key(get_user, (123,), {}, namespace="users")  # doctest: +ELLIPSIS
        'ns:users:func:...'

        Cache key includes function arguments:

        >>> def search(query: str, limit: int): pass
        >>> key1 = handler.get_cache_key(search, ("hello",), {"limit": 10}, namespace=None)
        >>> key2 = handler.get_cache_key(search, ("world",), {"limit": 10}, namespace=None)
        >>> key1 != key2  # Different args = different keys
        True

        Same args produce same key (deterministic):

        >>> key3 = handler.get_cache_key(search, ("hello",), {"limit": 10}, namespace=None)
        >>> key1 == key3
        True
    """

    def __init__(
        self,
        serialization_handler: CacheSerializationHandler,
        key_generator: CacheKeyGenerator,
        cache_handler: Optional[CacheHandlerStrategy] = None,
        on_deserialize_error: Optional[Callable[[Exception, str], None]] = None,
    ):
        """Initialize with dependencies.

        Args:
            on_deserialize_error: Optional hook invoked as (error, cache_key) when an
                L2 read hits a corrupt/tampered entry (SerializationError). The decorator
                wires this to its metrics pipeline so the cache_get_deserialize signal
                fires from one place for both sync and async paths (#159). Best-effort:
                hook failures are logged and never mask the miss/recompute.
        """
        self.serialization_handler = serialization_handler
        self.key_generator = key_generator
        self._cache_handler = cache_handler
        self.on_deserialize_error = on_deserialize_error

    def _notify_deserialize_error(self, error: Exception, cache_key: str) -> None:
        """Report a corrupt L2 entry to the observability hook (best-effort)."""
        if self.on_deserialize_error is None:
            return
        try:
            self.on_deserialize_error(error, cache_key)
        except Exception as hook_err:  # observability must never break the miss path
            get_logger().warning(f"on_deserialize_error hook failed for {cache_key}: {hook_err}")

    def get_cache_key(
        self,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        namespace: str | None,
        integrity_checking: bool = True,
    ) -> str:
        """Generate cache key for function call.

        Args:
            func: Function being cached
            args: Positional arguments
            kwargs: Keyword arguments
            namespace: Optional namespace prefix
            integrity_checking: Whether integrity checking is enabled (affects cache key)

        Returns:
            Cache key string

        Examples:
            Basic key generation (format: func:{module}.{name}:args:{hash}:{flags}):

            >>> from cachekit.key_generator import CacheKeyGenerator
            >>> handler = CacheOperationHandler(CacheSerializationHandler(), CacheKeyGenerator())
            >>> def my_func(x): return x * 2
            >>> key = handler.get_cache_key(my_func, (42,), {}, namespace=None)
            >>> key.startswith("func:")
            True

            Namespace prefixes the key (format: ns:{namespace}:func:...):

            >>> key_ns = handler.get_cache_key(my_func, (42,), {}, namespace="v2")
            >>> key_ns.startswith("ns:v2:")
            True

            _bypass_cache kwarg is filtered out:

            >>> key1 = handler.get_cache_key(my_func, (1,), {"_bypass_cache": True}, None)
            >>> key2 = handler.get_cache_key(my_func, (1,), {}, None)
            >>> key1 == key2  # _bypass_cache doesn't affect key
            True
        """
        filtered_kwargs = {k: v for k, v in kwargs.items() if k != "_bypass_cache"}
        return self.key_generator.generate_key(func, args, filtered_kwargs, namespace, integrity_checking)

    def get_cached_value(self, cache_key: str, refresh_ttl: Optional[int] = None) -> Optional[Any]:
        """Get value from cache if it exists.

        Args:
            cache_key: Cache key to retrieve (also used for AAD verification if encrypted)
            refresh_ttl: Optional TTL to refresh on hit

        Returns:
            Tuple (True, value) if cache hit, None if cache miss or error

        Note:
            Requires cache_handler to be set via set_cache_handler() before calling.
            For encrypted data, cache_key is used for AAD v0x03 verification.
        """
        try:
            if self._cache_handler is None:
                raise RuntimeError("Cache handler must be set before calling get_cached_value")

            # mmap fast path (#171): plaintext Arrow -> pandas on a buffer-readable backend (File,
            # POSIX) reads zero-copy. The handle is confined to this frame and closed in `finally`,
            # so the mmap never becomes the returned value and never reaches L1 (blocker C). A None
            # from get_buffer (ineligible file, or a non-buffer backend) falls through to bytes.
            if self.serialization_handler.supports_mmap_read():
                handle = self._cache_handler.get_buffer(cache_key)
                if handle is not None:
                    try:
                        get_logger().cache_hit(cache_key, "Backend(mmap)")
                        return (True, self.serialization_handler.deserialize_data(handle.view, cache_key))
                    finally:
                        handle.close()

            cached_data = self._cache_handler.get(cache_key, refresh_ttl)
            if cached_data is not None:
                get_logger().cache_hit(cache_key, "Backend")
                # Pass cache_key for AAD verification (required for encrypted data)
                deserialized = self.serialization_handler.deserialize_data(cached_data, cache_key)
                # Return a tuple (True, value) to distinguish from "no cache entry"
                return (True, deserialized)
            return None
        except SerializationError as e:
            # Single policy point (cachekit-py#170): records the metric and raises when
            # fail-closed — in that case the poisoned entry is deliberately RETAINED as
            # evidence for the operator (eviction only happens on the fail-open return).
            handle_decrypt_failure(
                e, tier="l2", cache_key=cache_key, fail_closed=self.serialization_handler.encryption_fail_closed
            )
            # Fail open: best-effort evict the poisoned entry so subsequent reads don't
            # re-pay full decompress+verify only to fail again; the caller recomputes
            # and re-stores the value (#159).
            try:
                if self._cache_handler is not None:
                    self._cache_handler.delete(cache_key)
            except Exception as del_err:  # best-effort eviction; never mask the miss/recompute
                get_logger().warning(f"Failed to evict poisoned L2 entry {redact_cache_key(cache_key)}: {del_err}")
            self._notify_deserialize_error(e, cache_key)
            return None
        except Exception as e:
            get_logger().warning(f"Backend operation failed for get on {cache_key}: {e}")
            return None

    def get_cached_value_with_freshness(
        self, cache_key: str, refresh_ttl: Optional[int] = None
    ) -> Optional[tuple[tuple[bool, Any], bool]]:
        """SWR variant of :meth:`get_cached_value` (LAB-381): also reports staleness.

        Returns ``((True, value), is_stale)`` on a hit, None on miss/error. The mmap
        fast path is skipped — SWR is CachekitIO-only, which is not buffer-readable.
        Error semantics mirror get_cached_value: the LAB-108 policy point raises
        DecryptionAuthenticationError when fail-closed (poisoned entry retained as
        evidence); fail-open evicts and reads as a miss so the caller recomputes.
        """
        try:
            if self._cache_handler is None:
                raise RuntimeError("Cache handler must be set before calling get_cached_value_with_freshness")

            hit = self._cache_handler.get_with_freshness(cache_key)
            if hit is None:
                return None
            cached_data, is_stale = hit
            get_logger().cache_hit(cache_key, "Backend(stale)" if is_stale else "Backend")
            deserialized = self.serialization_handler.deserialize_data(cached_data, cache_key)
            return ((True, deserialized), is_stale)
        except SerializationError as e:
            # Single policy point (cachekit-py#170): records the metric and raises when
            # fail-closed — in that case the poisoned entry is deliberately RETAINED as
            # evidence for the operator (eviction only happens on the fail-open return).
            handle_decrypt_failure(
                e, tier="l2", cache_key=cache_key, fail_closed=self.serialization_handler.encryption_fail_closed
            )
            try:
                if self._cache_handler is not None:
                    self._cache_handler.delete(cache_key)
            except Exception as del_err:  # best-effort eviction; never mask the miss/recompute
                get_logger().warning(f"Failed to evict poisoned L2 entry {redact_cache_key(cache_key)}: {del_err}")
            self._notify_deserialize_error(e, cache_key)
            return None
        except Exception as e:
            get_logger().warning(f"Backend operation failed for get on {cache_key}: {e}")
            return None

    async def get_cached_value_with_freshness_async(
        self, cache_key: str, refresh_ttl: Optional[int] = None
    ) -> Optional[tuple[tuple[bool, Any, bytes], bool]]:
        """Async SWR variant (LAB-381): staleness + the raw envelope for L1 backfill.

        Returns ``((True, value, raw_bytes), is_stale)`` on a hit — the 3-tuple
        matches :meth:`get_cached_value_async` (LAB-111 routing) so the async
        decorator backfills L1 without re-serializing. None on miss/error; the
        LAB-108 fail-closed policy propagates DecryptionAuthenticationError.
        """
        try:
            if self._cache_handler is None:
                raise RuntimeError("Cache handler must be set before calling get_cached_value_with_freshness_async")

            hit = await self._cache_handler.get_with_freshness_async(cache_key)
            if hit is None:
                return None
            cached_data, is_stale = hit
            get_logger().cache_hit(cache_key, "Backend(stale)" if is_stale else "Backend")
            deserialized = self.serialization_handler.deserialize_data(cached_data, cache_key)
            return ((True, deserialized, cached_data), is_stale)
        except SerializationError as e:
            # Single policy point (cachekit-py#170) — see get_cached_value_async.
            handle_decrypt_failure(
                e, tier="l2", cache_key=cache_key, fail_closed=self.serialization_handler.encryption_fail_closed
            )
            try:
                if self._cache_handler is not None:
                    await self._cache_handler.delete_async(cache_key)
            except Exception as del_err:  # best-effort eviction; never mask the miss/recompute
                get_logger().warning(f"Failed to evict poisoned L2 entry {redact_cache_key(cache_key)}: {del_err}")
            self._notify_deserialize_error(e, cache_key)
            return None
        except Exception as e:
            get_logger().warning(f"Backend operation failed for get on {cache_key}: {e}")
            return None

    async def get_cached_value_async(self, cache_key: str, refresh_ttl: Optional[int] = None) -> Optional[Any]:
        """Get value from cache if it exists (async version).

        Args:
            cache_key: Cache key to retrieve (also used for AAD verification if encrypted)
            refresh_ttl: Optional TTL to refresh on hit

        Returns:
            Tuple (True, value, raw_bytes) if cache hit, None if cache miss or error.
            Unlike the sync variant, the raw serialized envelope is included so the
            async decorator can backfill L1 without re-serializing (re-encrypting).

        Note:
            Requires cache_handler to be set via set_cache_handler() before calling.
            For encrypted data, cache_key is used for AAD v0x03 verification.
        """
        try:
            if self._cache_handler is None:
                raise RuntimeError("Cache handler must be set before calling get_cached_value_async")

            # NOTE: no mmap fast path here yet. The async decorator routes through this method
            # (#159), but buffer reads are sync-only today; the async mmap read is #171 scope.
            cached_data = await self._cache_handler.get_async(cache_key, refresh_ttl)
            if cached_data is not None:
                get_logger().cache_hit(cache_key, "Backend")
                # Pass cache_key for AAD verification (required for encrypted data)
                deserialized = self.serialization_handler.deserialize_data(cached_data, cache_key)
                # Tuple distinguishes a hit from "no cache entry"; raw bytes ride along for L1
                return (True, deserialized, cached_data)
            return None
        except SerializationError as e:
            # Single policy point (cachekit-py#170): records the metric and raises when
            # fail-closed — in that case the poisoned entry is deliberately RETAINED as
            # evidence for the operator (eviction only happens on the fail-open return).
            handle_decrypt_failure(
                e, tier="l2", cache_key=cache_key, fail_closed=self.serialization_handler.encryption_fail_closed
            )
            # Fail open: best-effort evict the poisoned entry so subsequent reads don't
            # re-pay full decompress+verify only to fail again; the caller recomputes
            # and re-stores the value (#159).
            try:
                if self._cache_handler is not None:
                    await self._cache_handler.delete_async(cache_key)
            except Exception as del_err:  # best-effort eviction; never mask the miss/recompute
                get_logger().warning(f"Failed to evict poisoned L2 entry {redact_cache_key(cache_key)}: {del_err}")
            self._notify_deserialize_error(e, cache_key)
            return None
        except Exception as e:
            get_logger().warning(f"Backend operation failed for get on {cache_key}: {e}")
            return None

    def store_result(
        self,
        cache_key: str,
        result: Any,
        ttl: int | None,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        stale_ttl: int | None = None,
    ) -> Optional[bytes]:
        """Store result in backend cache with optional tenant context for encryption.

        Args:
            cache_key: Cache key (also used for AAD binding if encryption enabled)
            result: Result to cache
            ttl: Time-to-live in seconds
            args: Function args (for tenant extraction in encryption)
            kwargs: Function kwargs (for tenant extraction in encryption)

        Returns:
            Serialized bytes (for L1 cache storage), or None if serialization failed

        Note:
            Requires cache_handler to be set via set_cache_handler() before calling.
            For encrypted data, cache_key is bound to ciphertext via AAD v0x03.
        """
        try:
            if self._cache_handler is None:
                raise RuntimeError("Cache handler must be set before calling store_result")

            # Pass cache_key for AAD binding (required for encrypted data)
            serialized_data = self.serialization_handler.serialize_data(result, args, kwargs, cache_key)
            # Only thread the SWR kwarg when set: strategy implementations without
            # **metadata (tests, custom handlers) must keep working unchanged.
            if stale_ttl is not None:
                self._cache_handler.set(cache_key, serialized_data, ttl, stale_ttl=stale_ttl)
            else:
                self._cache_handler.set(cache_key, serialized_data, ttl)
            get_logger().cache_stored(cache_key, ttl)

            # Return serialized string (wrapped envelope) for L1 cache storage
            return serialized_data
        except InteropError:
            # Interop/v1 data-model rejection: fail loud, never "computed but
            # silently never cached" (spec-mandated; matches cachekit-ts).
            raise
        except Exception as e:
            get_logger().warning(f"Failed to store in backend cache: {e}")
            return None

    async def store_result_async(
        self,
        cache_key: str,
        result: Any,
        ttl: int | None,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> Optional[bytes]:
        """Store result in backend cache (async version) with optional tenant context for encryption.

        Args:
            cache_key: Cache key (also used for AAD binding if encryption enabled)
            result: Result to cache
            ttl: Time-to-live in seconds
            args: Function args (for tenant extraction in encryption)
            kwargs: Function kwargs (for tenant extraction in encryption)

        Returns:
            Serialized bytes (for L1 cache storage), or None if serialization failed

        Note:
            Requires cache_handler to be set via set_cache_handler() before calling.
            For encrypted data, cache_key is bound to ciphertext via AAD v0x03.
        """
        try:
            if self._cache_handler is None:
                raise RuntimeError("Cache handler must be set before calling store_result_async")

            # Pass cache_key for AAD binding (required for encrypted data)
            serialized_data = self.serialization_handler.serialize_data(result, args, kwargs, cache_key)
            await self._cache_handler.set_async(cache_key, serialized_data, ttl)
            get_logger().cache_stored(cache_key, ttl)

            # Return serialized string (wrapped envelope) for L1 cache storage
            return serialized_data
        except InteropError:
            # Interop/v1 data-model rejection: fail loud, never "computed but
            # silently never cached" (spec-mandated; matches cachekit-ts).
            raise
        except Exception as e:
            get_logger().warning(f"Failed to store in backend cache: {e}")
            return None

    def set_cache_handler(self, handler: CacheHandlerStrategy):
        """Set a specific cache handler strategy.

        Args:
            handler: Cache handler implementing CacheHandlerStrategy protocol
        """
        self._cache_handler = handler

    @property
    def cache_handler(self) -> Optional[CacheHandlerStrategy]:
        """Get the current cache handler."""
        return self._cache_handler


class CacheInvalidator:
    """Handles cache invalidation - Single Responsibility."""

    def __init__(
        self,
        key_generator: CacheKeyGenerator,
        backend: Optional[BaseBackend] = None,
        integrity_checking: bool = True,
    ):
        """Initialize with key generator and optional backend.

        Args:
            key_generator: Key generator instance
            backend: Optional backend instance (can be set later via set_backend)
            integrity_checking: Whether integrity checking is enabled (affects cache key generation)
        """
        self.key_generator = key_generator
        self._backend = backend
        self.integrity_checking = integrity_checking

    def set_backend(self, backend: BaseBackend):
        """Set the backend instance.

        Args:
            backend: Backend instance implementing BaseBackend protocol
        """
        self._backend = backend

    def invalidate_cache(
        self,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        namespace: str | None,
    ) -> None:
        """Invalidate cache entry.

        Args:
            func: Cached function
            args: Function arguments
            kwargs: Function keyword arguments
            namespace: Optional namespace

        Note:
            Requires backend to be set via set_backend() or constructor before calling.
        """
        if self._backend is None:
            raise RuntimeError("Backend must be set before calling invalidate_cache")
        cache_key = self.key_generator.generate_key(func, args, kwargs, namespace, self.integrity_checking)

        try:
            self._backend.delete(cache_key)
            get_logger().cache_invalidated(cache_key, "Backend")
        except BackendError as e:
            get_logger().error(f"Backend operation failed for invalidation on {cache_key}: {e}")
        except Exception as e:
            get_logger().error(f"Unexpected error invalidating {cache_key}: {e}")

    async def invalidate_cache_async(
        self,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        namespace: str | None,
    ) -> None:
        """Invalidate cache entry (async version).

        Args:
            func: Cached function
            args: Function arguments
            kwargs: Function keyword arguments
            namespace: Optional namespace

        Note:
            Requires backend to be set via set_backend() or constructor before calling.
        """
        if self._backend is None:
            raise RuntimeError("Backend must be set before calling invalidate_cache_async")
        cache_key = self.key_generator.generate_key(func, args, kwargs, namespace, self.integrity_checking)

        try:
            # Note: BaseBackend methods are sync (not async)
            # We call sync method from async context (will be wrapped in executor by caller if needed)
            self._backend.delete(cache_key)
            get_logger().cache_invalidated(cache_key, "Backend")
        except BackendError as e:
            get_logger().error(f"Backend operation failed for invalidation on {cache_key}: {e}")
        except Exception as e:
            get_logger().error(f"Unexpected error invalidating {cache_key}: {e}")


@runtime_checkable
class CacheHandlerStrategy(Protocol):
    """Protocol for cache handlers - supports both standard and pipelined operations."""

    def get(self, key: str, refresh_ttl: Optional[int] = None) -> Optional[bytes]:
        """Get value from cache with optional TTL refresh."""
        ...

    def get_buffer(self, key: str) -> Optional[BufferHandle]:
        """Return a zero-copy buffer handle if the backend supports it (#171), else None."""
        ...

    def set(self, key: str, value: Union[str, bytes], ttl: Optional[int] = None, **metadata) -> bool:
        """Set value in cache with TTL and optional metadata."""
        ...

    def delete(self, key: str) -> bool:
        """Delete key from cache."""
        ...

    async def get_async(self, key: str, refresh_ttl: Optional[int] = None) -> Optional[bytes]:
        """Get value from cache asynchronously."""
        ...

    async def set_async(self, key: str, value: Union[str, bytes], ttl: Optional[int] = None, **metadata) -> bool:
        """Set value in cache asynchronously."""
        ...

    async def delete_async(self, key: str) -> bool:
        """Delete key from cache asynchronously."""
        ...

    def get_with_freshness(self, key: str) -> Optional[tuple[bytes, bool]]:
        """Get value plus SWR staleness (LAB-381); (bytes, is_stale) or None."""
        ...

    async def get_with_freshness_async(self, key: str) -> Optional[tuple[bytes, bool]]:
        """Async variant of get_with_freshness."""
        ...


class StandardCacheHandler:
    """Standard cache handler with backend abstraction.

    Note: L1 (in-memory) caching is handled at the decorator wrapper layer.
    This class handles L2 (backend) abstraction for Redis/HTTP/DynamoDB/etc.

    This class implements the CacheHandlerStrategy protocol and provides:
    - Backpressure control for rate limiting
    - TTL refresh for cache warming
    - Graceful error handling with logging

    Examples:
        With a mock backend (for testing):

        >>> class MockBackend:
        ...     def __init__(self):
        ...         self._store = {}
        ...     def get(self, key):
        ...         return self._store.get(key)
        ...     def set(self, key, value, ttl=None):
        ...         self._store[key] = value
        ...     def delete(self, key):
        ...         return self._store.pop(key, None) is not None
        >>> backend = MockBackend()
        >>> handler = StandardCacheHandler(backend)

        Store and retrieve values:

        >>> handler.set("user:123", b'{"name": "Alice"}', ttl=300)
        True
        >>> handler.get("user:123")
        b'{"name": "Alice"}'

        Delete cached values:

        >>> handler.delete("user:123")
        True
        >>> handler.get("user:123") is None
        True

        TTL refresh threshold (default 50%):

        >>> handler = StandardCacheHandler(backend, ttl_refresh_threshold=0.5)
        >>> handler.ttl_refresh_threshold
        0.5
    """

    def __init__(
        self,
        backend: BaseBackend,
        timeout_provider=None,
        backpressure_controller=None,
        ttl_refresh_threshold=0.5,
    ):
        """Initialize with backend and optional features.

        Args:
            backend: Backend instance (implements BaseBackend protocol)
            timeout_provider: Optional callable that returns timeout value
            backpressure_controller: Optional BackpressureController for request limiting
            ttl_refresh_threshold: Threshold for TTL refresh (0.0-1.0, default 0.5 = 50%)
        """
        self.backend = backend
        self.timeout_provider = timeout_provider
        self.backpressure_controller = backpressure_controller
        self.ttl_refresh_threshold = ttl_refresh_threshold

    def _with_backpressure_and_timeout(self, operation, *args, **kwargs):
        """Execute operation with backpressure control and adaptive timeout."""
        if self.backpressure_controller:
            # Apply backpressure control first
            with self.backpressure_controller.acquire():
                return self._with_timeout(operation, *args, **kwargs)
        else:
            # No backpressure control, just apply timeout
            return self._with_timeout(operation, *args, **kwargs)

    def _with_timeout(self, operation, *args, **kwargs):
        """Execute operation with timeout delegation to backend.

        Timeout handling is delegated to the backend implementation via
        TimeoutConfigurableBackend protocol.
        """
        # Execute operation directly - timeout handled by backend layer
        return operation(*args, **kwargs)

    async def _maybe_refresh_ttl(self, key: str, refresh_ttl: int) -> None:
        """Refresh TTL on key if backend supports it and threshold is met.

        This implements graceful degradation: silently skips if backend doesn't
        support TTL inspection (TTLInspectableBackend protocol).

        Args:
            key: Cache key to potentially refresh
            refresh_ttl: Target TTL value in seconds

        Note:
            Uses TypeGuard pattern for proper type narrowing. Logs at debug level
            when skipping due to lack of backend support.
        """
        # Check if backend supports TTL inspection (graceful degradation).
        # Warn once (not silently no-op) so the ignored flag is discoverable — LAB-446.
        if not supports_ttl_inspection(self.backend):
            warn_ttl_refresh_unsupported(self.backend)
            return

        # Type checker now knows self.backend is TTLInspectableBackend
        try:
            remaining_ttl = await self.backend.get_ttl(key)
            if remaining_ttl is not None and remaining_ttl < refresh_ttl * self.ttl_refresh_threshold:
                await self.backend.refresh_ttl(key, refresh_ttl)
                get_logger().debug(
                    f"Refreshed TTL for {key}: {refresh_ttl}s "
                    f"(remaining: {remaining_ttl}s, threshold: {self.ttl_refresh_threshold})"
                )
        except Exception as e:
            # Log but don't fail the cache operation
            get_logger().debug(f"Failed to refresh TTL for {key}: {e}")

    def get(self, key: str, refresh_ttl: Optional[int] = None) -> Optional[bytes]:
        """Get value from cache using backend.

        Args:
            key: Cache key
            refresh_ttl: Optional TTL to refresh on hit

        Returns:
            Bytes value (encrypted or plaintext msgpack) if found, None if miss
        """
        try:
            value = self._with_backpressure_and_timeout(self.backend.get, key)

            # Note: TTL refresh is async, but we're in sync context
            # TTL refresh will be handled in async path (get_async)
            # For sync operations, we skip TTL refresh to avoid blocking

            return value
        except BackendError as e:
            get_logger().error(f"Backend error getting key {key}: {e}")
            return None
        except Exception as e:
            get_logger().error(f"Unexpected error getting key {key}: {e}")
            return None

    def get_buffer(self, key: str) -> Optional[BufferHandle]:
        """Return a zero-copy buffer handle for key if the backend supports it (#171), else None.

        Mirrors get()'s backpressure/timeout wrapping. Returns None when the backend can't map the
        value (or on any backend error) so the caller transparently falls back to get().
        """
        if not supports_buffer_read(self.backend):
            return None
        try:
            return self._with_backpressure_and_timeout(self.backend.get_buffer, key)
        except BackendError as e:
            get_logger().error(f"Backend error mmapping key {key}: {e}")
            return None
        except Exception as e:
            get_logger().error(f"Unexpected error mmapping key {key}: {e}")
            return None

    def get_with_freshness(self, key: str) -> Optional[tuple[bytes, bool]]:
        """Get value plus SWR staleness from an SWR-capable backend (LAB-381).

        Returns ``(bytes, is_stale)`` on a hit, or None on miss/error (same
        degradation contract as :meth:`get` — an error reads as a miss and the
        caller takes the synchronous recompute path).
        """
        if not supports_swr(self.backend):
            value = self.get(key)
            return (value, False) if value is not None else None
        try:
            return self._with_backpressure_and_timeout(self.backend.get_with_freshness, key)
        except BackendError as e:
            get_logger().error(f"Backend error getting key {key}: {e}")
            return None
        except Exception as e:
            get_logger().error(f"Unexpected error getting key {key}: {e}")
            return None

    async def get_with_freshness_async(self, key: str) -> Optional[tuple[bytes, bool]]:
        """Async variant of :meth:`get_with_freshness` (sync backend call in the thread pool)."""
        if not supports_swr(self.backend):
            value = await self.get_async(key)
            return (value, False) if value is not None else None
        try:
            return await self._with_backpressure_and_timeout_async(self.backend.get_with_freshness, key)
        except BackendError as e:
            get_logger().error(f"Backend error getting key {key}: {e}")
            return None
        except Exception as e:
            get_logger().error(f"Unexpected error getting key {key}: {e}")
            return None

    def set(
        self, key: str, value: Union[str, bytes], ttl: Optional[int] = None, stale_ttl: Optional[int] = None, **metadata
    ) -> bool:
        """Set value in cache using backend.

        Args:
            key: Cache key
            value: Bytes value to store (encrypted or plaintext msgpack)
            ttl: Time-to-live in seconds
            stale_ttl: SWR stale-grace window in seconds past the fresh TTL
                (LAB-381); silently ignored on backends without SWR support.
            **metadata: Additional metadata (ignored, for compatibility)

        Returns:
            True if successfully stored, False otherwise
        """
        # Ensure value is bytes
        if isinstance(value, str):
            value = value.encode("utf-8")

        try:
            if stale_ttl is not None and supports_swr(self.backend):
                self._with_backpressure_and_timeout(self.backend.set, key, value, ttl, stale_ttl)
            else:
                self._with_backpressure_and_timeout(self.backend.set, key, value, ttl)
            return True
        except BackendError as e:
            get_logger().error(f"Backend error setting key {key}: {e}")
            return False
        except Exception as e:
            get_logger().error(f"Unexpected error setting key {key}: {e}")
            return False

    def delete(self, key: str) -> bool:
        """Delete key from cache using backend.

        Args:
            key: Cache key to delete

        Returns:
            True if successfully deleted, False otherwise
        """
        try:
            return self._with_backpressure_and_timeout(self.backend.delete, key)
        except BackendError as e:
            get_logger().error(f"Backend error deleting key {key}: {e}")
            return False
        except Exception as e:
            get_logger().error(f"Unexpected error deleting key {key}: {e}")
            return False

    async def _with_backpressure_and_timeout_async(self, operation, *args, **kwargs):
        """Execute sync operation in thread pool with backpressure control.

        Runs sync backend operations in a thread pool to avoid blocking the
        event loop, while still applying backpressure control.
        """

        def execute_sync():
            if self.backpressure_controller:
                with self.backpressure_controller.acquire():
                    return operation(*args, **kwargs)
            return operation(*args, **kwargs)

        return await asyncio.to_thread(execute_sync)

    async def get_async(self, key: str, refresh_ttl: Optional[int] = None) -> Optional[bytes]:
        """Get value from cache asynchronously using backend.

        Runs sync backend.get() in a thread pool to avoid blocking the event loop.
        """
        try:
            # Run sync backend operation in thread pool
            value = await self._with_backpressure_and_timeout_async(self.backend.get, key)

            # Optionally refresh TTL if value exists and refresh_ttl provided
            # Uses graceful degradation (skips if backend doesn't support TTL inspection)
            if value is not None and refresh_ttl is not None:
                await self._maybe_refresh_ttl(key, refresh_ttl)

            return value
        except BackendError as e:
            get_logger().error(f"Backend error getting key {key}: {e}")
            return None
        except Exception as e:
            get_logger().error(f"Unexpected error getting key {key}: {e}")
            return None

    async def set_async(
        self, key: str, value: Union[str, bytes], ttl: Optional[int] = None, stale_ttl: Optional[int] = None, **metadata
    ) -> bool:
        """Set value in cache asynchronously using backend.

        Runs sync backend.set() in a thread pool to avoid blocking the event loop.
        ``stale_ttl`` opens an SWR stale-grace window (LAB-381); silently ignored
        on backends without SWR support.
        """
        # Ensure value is bytes
        if isinstance(value, str):
            value = value.encode("utf-8")

        try:
            # Run sync backend operation in thread pool
            if stale_ttl is not None and supports_swr(self.backend):
                await self._with_backpressure_and_timeout_async(self.backend.set, key, value, ttl, stale_ttl)
            else:
                await self._with_backpressure_and_timeout_async(self.backend.set, key, value, ttl)
            return True
        except BackendError as e:
            get_logger().error(f"Backend error setting key {key}: {e}")
            return False
        except Exception as e:
            get_logger().error(f"Unexpected error setting key {key}: {e}")
            return False

    async def delete_async(self, key: str) -> bool:
        """Delete key from cache asynchronously using backend.

        Runs sync backend.delete() in a thread pool to avoid blocking the event loop.
        """
        try:
            # Run sync backend operation in thread pool
            return await self._with_backpressure_and_timeout_async(self.backend.delete, key)
        except BackendError as e:
            get_logger().error(f"Backend error deleting key {key}: {e}")
            return False
        except Exception as e:
            get_logger().error(f"Unexpected error deleting key {key}: {e}")
            return False
