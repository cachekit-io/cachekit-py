"""Focused cache handling classes following SOLID principles.

This module breaks down the cache decorator into focused,
single-responsibility classes that are easier to test and maintain.
"""

from __future__ import annotations

import functools
import sys
import threading
from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol, Union, runtime_checkable

# TypeGuard requires Python 3.10+, use typing_extensions for 3.9
if sys.version_info >= (3, 10):
    from typing import TypeGuard
else:
    from typing_extensions import TypeGuard

from cachekit.backends.base import BackendError, BaseBackend, TTLInspectableBackend
from cachekit.backends.provider import (
    BackendProviderInterface,
    CacheClientProvider,
    DefaultBackendProvider,
    DefaultCacheClientProvider,
    DefaultLoggerProvider,
    LoggerProvider,
)
from cachekit.config import ConfigurationError, get_settings
from cachekit.di import DIContainer
from cachekit.key_generator import CacheKeyGenerator
from cachekit.serializers.base import SerializationError
from cachekit.serializers.wrapper import SerializationWrapper

if TYPE_CHECKING:
    from cachekit.serializers.base import SerializerProtocol

# Global DI container instance with default registrations
container = DIContainer()
container.register(CacheClientProvider, DefaultCacheClientProvider)
container.register(LoggerProvider, DefaultLoggerProvider)
container.register(BackendProviderInterface, DefaultBackendProvider)

# Dependencies are injected at runtime, not module load time
# This ensures test isolation works properly with DI


def get_client_provider():
    """Get the current CacheClientProvider from DI container."""
    return container.get(CacheClientProvider)


def get_logger_provider():
    """Get the current LoggerProvider from DI container."""
    return container.get(LoggerProvider)


def get_backend_provider():
    """Get the current BackendProviderInterface from DI container."""
    return container.get(BackendProviderInterface)


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


@functools.lru_cache(maxsize=128)
def _get_cached_utility_function(import_path: str):
    """Thread-safe cached import of utility functions (like serialize_for_redis).

    Uses functools.lru_cache for automatic thread-safe caching of utility functions.
    These functions are imported less frequently than serializer classes but still
    benefit from caching to avoid repeated import overhead.

    Args:
        import_path: Full import path (e.g., 'cachekit.serializers.serialize_for_redis')

    Returns:
        Cached utility function
    """
    try:
        module_path, function_name = import_path.rsplit(".", 1)
        module = __import__(module_path, fromlist=[function_name])
        function = getattr(module, function_name)
        get_logger().debug(f"Cached utility import: {import_path}")
        return function
    except (ImportError, AttributeError) as e:
        get_logger().warning(f"Failed to import utility function {import_path}: {e}")
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


class SerializerFactory:
    """Factory class responsible for creating serializer instances.

    Note: Encryption is now a separate layer (not a serializer type).
    Use encryption=True parameter instead of serializer="encrypted".

    Current serializers:
    - DefaultSerializer (default): MessagePack + LZ4 compression + xxHash3-64 checksums
    - ArrowSerializer (arrow): Apache Arrow IPC for DataFrames
    - OrjsonSerializer (orjson): High-performance JSON serialization
    """

    @staticmethod
    def create_serializer(serializer: str = "default", data_sample: Any = None):
        """Create serializer instance based on type.

        Args:
            serializer: Requested serializer type ("default", "arrow", "orjson")
            data_sample: Optional data sample (unused)

        Returns:
            Serializer instance

        Raises:
            ValueError: If unknown serializer is requested

        Note:
            For encryption, use encryption=True parameter (not serializer="encrypted").
            Encryption is a wrapper layer, not a peer serializer.
        """
        from cachekit.serializers import SERIALIZER_REGISTRY

        if serializer in SERIALIZER_REGISTRY:
            try:
                serializer_class = SERIALIZER_REGISTRY[serializer]
                return serializer_class()
            except (ImportError, RuntimeError) as e:
                raise RuntimeError(
                    f"{serializer} serializer not available: {e}. Please ensure dependencies are installed."
                ) from e
        else:
            valid_options = ", ".join(SERIALIZER_REGISTRY.keys())
            raise ValueError(
                f"Unknown serializer: '{serializer}'. "
                f"Valid options: {valid_options}. "
                f"For encryption, use encryption=True parameter (not serializer='encrypted')."
            )

    @staticmethod
    def validate_serializer_for_data(serializer_name: str, data: Any) -> bool:
        """Validate if a serializer can handle specific data.

        Args:
            serializer_name: Name of the serializer to test
            data: Data to validate against

        Returns:
            True if serializer can handle the data, False otherwise
        """
        from cachekit.serializers import SERIALIZER_REGISTRY

        return serializer_name in SERIALIZER_REGISTRY

    @staticmethod
    def get_compatibility_report(data: Any) -> str:
        """Get a detailed compatibility report for data across all serializers.

        Args:
            data: Data to analyze

        Returns:
            Human-readable compatibility report
        """
        from cachekit.serializers import SERIALIZER_REGISTRY

        serializers = ", ".join(SERIALIZER_REGISTRY.keys())
        return f"Data type {type(data).__name__} - Compatible with: {serializers}"


class CacheSerializationHandler:
    """Handles serialization/deserialization with optional encryption layer.

    Architecture:
    - Serializer: Defines HOW to serialize (default/msgpack, future: pickle, json)
    - Encryption: Defines WHETHER to encrypt (security layer on top, orthogonal)
    - Tenant extraction: For multi-tenant encryption key isolation (FAIL CLOSED)

    Modes:
    - encryption=False: Direct serialization (plaintext in Redis)
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
        encryption: bool = False,
        tenant_extractor: Any | None = None,
        single_tenant_mode: bool = False,
        deployment_uuid: Optional[str] = None,
        master_key: Optional[str] = None,
        enable_integrity_checking: bool = True,
    ):
        """Initialize with serializer strategy and optional encryption.

        Args:
            serializer_name: Serializer instance or name. Accepts either:
                            - String name: "default" (MessagePack), "arrow" (DataFrame zero-copy), "orjson" (JSON)
                            - SerializerProtocol instance: Custom serializer implementing the protocol
            encryption: Enable encryption layer (wraps serializer with EncryptionWrapper)
            tenant_extractor: Optional TenantContextExtractor for multi-tenant encryption.
                             Only used if encryption=True.
                             If None: single-tenant mode (uses nil UUID).
                             If provided: multi-tenant mode (extracts tenant_id, FAIL CLOSED).
            single_tenant_mode: Explicitly enable single-tenant mode (requires encryption=True).
                               Mutually exclusive with tenant_extractor.
            deployment_uuid: Optional deployment-specific UUID for single-tenant mode.
                            If not provided, uses env var or persistent file.
            master_key: Optional master key for encryption (hex-encoded). If not provided,
                       reads from REDIS_CACHE_MASTER_KEY environment variable.
            enable_integrity_checking: Enable integrity checking (default: True)
                                      Uses xxHash3-64 (8 bytes) for all serializers.
                                      Set to False for @cache.minimal (speed-first, no checksums)

        Raises:
            ConfigurationError: If encryption config is invalid (missing mode or both modes).
            TypeError: If serializer_name is not a string or SerializerProtocol instance.

        Note:
            FAIL CLOSED security policy: If encryption=True and tenant_extractor provided
            but extraction fails, ValueError propagates to caller (no fallback to shared key).
        """
        self.serializer_name = serializer_name
        self.encryption = encryption
        self.tenant_extractor = tenant_extractor
        self.single_tenant_mode = single_tenant_mode
        self.deployment_uuid = deployment_uuid
        self.master_key = master_key
        self.enable_integrity_checking = enable_integrity_checking
        self._deployment_uuid_value: Optional[str] = None

        # Extract string name for metadata storage (for protocol instances, use class name)
        if isinstance(serializer_name, str):
            self._serializer_string_name = serializer_name
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
                get_logger().info(f"Using deployment UUID from configuration: {validated_uuid}")
                return validated_uuid
            except ValueError as e:
                raise ConfigurationError(
                    f"Invalid deployment_uuid in configuration (must be valid UUID): {settings.deployment_uuid}. Error: {e}"
                ) from e

        # Option 3: Persistent file storage (auto-generated, survives restarts)
        deployment_uuid_file = Path.home() / ".cachekit" / "deployment_uuid"

        if deployment_uuid_file.exists():
            # Read existing UUID from file
            stored_uuid = deployment_uuid_file.read_text().strip()
            try:
                validated_uuid = str(uuid.UUID(stored_uuid))
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
            wrapper = EncryptionWrapper(tenant_id=tenant_id, master_key=master_key_bytes)

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
        """Serialize data for Redis storage with optional tenant context for encryption.

        Args:
            data: Data to serialize
            args: Positional arguments from cached function (for tenant extraction)
            kwargs: Keyword arguments from cached function (for tenant extraction)
            cache_key: Cache key for AAD binding (SECURITY CRITICAL for encryption).
                      Required when encryption is enabled to prevent ciphertext substitution.

        Returns:
            Serialized data wrapped for Redis storage

        Raises:
            ValueError: If tenant extraction fails in multi-tenant mode (FAIL CLOSED)
            ValueError: If cache_key is empty when encryption is enabled
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
                elif self.single_tenant_mode:
                    # MEDIUM-02: Single-tenant mode with deterministic UUID
                    # Uses cached deployment UUID (generated in __init__)
                    if self._deployment_uuid_value is None:
                        raise RuntimeError("deployment_uuid should be set in __init__ for single-tenant mode")
                    tenant_id = self._deployment_uuid_value
                else:
                    # Defensive fallback (should not happen due to validation in __init__)
                    tenant_id = "00000000-0000-0000-0000-000000000000"

                # CRITICAL-03 FIX: Use cached EncryptionWrapper to prevent 360K key copies/hour
                # Gets cached instance (thread-safe LRU, maxsize=256) instead of creating new one
                serializer = self._get_cached_encryption_wrapper(tenant_id)

                # EncryptionWrapper.serialize() requires cache_key for AAD v0x03 binding
                serialized_data, metadata = serializer.serialize(data, cache_key)
            else:
                # No encryption - use base serializer directly (no cache_key needed)
                serializer = self._base_serializer
                serialized_data, metadata = serializer.serialize(data)

            # Convert metadata to dict if needed
            metadata_dict = metadata.to_dict() if hasattr(metadata, "to_dict") else {}
            return SerializationWrapper.wrap_for_redis(serialized_data, metadata_dict, self._serializer_string_name)
        except ValueError:
            # Tenant extraction or cache_key missing - FAIL CLOSED (re-raise, don't catch)
            # This is a security violation: encryption requires valid tenant_id and cache_key
            raise
        except Exception as e:
            # Don't silently fallback - log error and raise to prevent data loss
            get_logger().error(f"Serialization failed with {self.serializer_name}: {e}")
            raise SerializationError(f"Failed to serialize data with {self.serializer_name}: {e}") from e

    def deserialize_data(self, data: str | bytes, cache_key: str = "") -> Any:
        """Deserialize data from Redis storage with cache_key verification.

        Args:
            data: Serialized data from Redis (may be encrypted)
            cache_key: Cache key for AAD verification (SECURITY CRITICAL for encrypted data).
                      Required when data is encrypted to verify ciphertext binding.

        Returns:
            Deserialized Python object

        Raises:
            ValueError: If cache_key is empty when data is encrypted
            SerializationError: If deserialization fails (including AAD mismatch)

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
        try:
            # Unwrap Redis data envelope
            serialized_data, metadata_dict, serializer_name = SerializationWrapper.unwrap_from_redis(data)

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

            # Determine serializer based on whether data is encrypted
            # Check metadata.encrypted flag (not just self.encryption) to handle
            # cases where handler config changed but old encrypted data exists
            if metadata.encrypted:
                # Data is encrypted - use cached EncryptionWrapper for decryption
                # CRITICAL-03 FIX: Use cached instance instead of creating new one
                tenant_id = metadata.tenant_id if metadata.tenant_id else "00000000-0000-0000-0000-000000000000"
                serializer = self._get_cached_encryption_wrapper(tenant_id)

                # EncryptionWrapper.deserialize() requires cache_key for AAD v0x03 verification
                return serializer.deserialize(serialized_data, metadata, cache_key)
            else:
                # Data is not encrypted - use base serializer directly (no cache_key needed)
                return base_serializer.deserialize(serialized_data, metadata)
        except ValueError:
            # cache_key missing for encrypted data - FAIL CLOSED (re-raise)
            raise
        except Exception as e:
            get_logger().error(f"Deserialization failed with {self.serializer_name}: {e}")
            raise SerializationError(f"Failed to deserialize data with {self.serializer_name}: {e}") from e


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
    ):
        """Initialize with dependencies."""
        self.serialization_handler = serialization_handler
        self.key_generator = key_generator
        self._cache_handler = cache_handler

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

            cached_data = self._cache_handler.get(cache_key, refresh_ttl)
            if cached_data is not None:
                get_logger().cache_hit(cache_key, "Backend")
                # Pass cache_key for AAD verification (required for encrypted data)
                deserialized = self.serialization_handler.deserialize_data(cached_data, cache_key)
                # Return a tuple (True, value) to distinguish from "no cache entry"
                return (True, deserialized)
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
            Tuple (True, value) if cache hit, None if cache miss or error

        Note:
            Requires cache_handler to be set via set_cache_handler() before calling.
            For encrypted data, cache_key is used for AAD v0x03 verification.
        """
        try:
            if self._cache_handler is None:
                raise RuntimeError("Cache handler must be set before calling get_cached_value_async")

            cached_data = await self._cache_handler.get_async(cache_key, refresh_ttl)
            if cached_data is not None:
                get_logger().cache_hit(cache_key, "Backend")
                # Pass cache_key for AAD verification (required for encrypted data)
                deserialized = self.serialization_handler.deserialize_data(cached_data, cache_key)
                # Return a tuple (True, value) to distinguish from "no cache entry"
                return (True, deserialized)
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
            self._cache_handler.set(cache_key, serialized_data, ttl)
            get_logger().cache_stored(cache_key, ttl)

            # Return serialized string (wrapped envelope) for L1 cache storage
            return serialized_data
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
        # Check if backend supports TTL inspection (graceful degradation)
        if not supports_ttl_inspection(self.backend):
            get_logger().debug(
                f"Backend {type(self.backend).__name__} doesn't support TTL inspection, skipping TTL refresh for key {key}"
            )
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

    def set(self, key: str, value: Union[str, bytes], ttl: Optional[int] = None, **metadata) -> bool:
        """Set value in cache using backend.

        Args:
            key: Cache key
            value: Bytes value to store (encrypted or plaintext msgpack)
            ttl: Time-to-live in seconds
            **metadata: Additional metadata (ignored, for compatibility)

        Returns:
            True if successfully stored, False otherwise
        """
        # Ensure value is bytes
        if isinstance(value, str):
            value = value.encode("utf-8")

        try:
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
        """Execute async operation with backpressure control and adaptive timeout."""
        if self.backpressure_controller:
            # Apply backpressure control first
            with self.backpressure_controller.acquire():
                return await self._with_timeout_async(operation, *args, **kwargs)
        else:
            # No backpressure control, just apply timeout
            return await self._with_timeout_async(operation, *args, **kwargs)

    async def _with_timeout_async(self, operation, *args, **kwargs):
        """Execute async operation with timeout delegation to backend.

        Timeout handling is delegated to the backend implementation via
        TimeoutConfigurableBackend protocol.
        """
        # Execute operation directly - timeout handled by backend layer
        return await operation(*args, **kwargs)

    async def get_async(self, key: str, refresh_ttl: Optional[int] = None) -> Optional[bytes]:
        """Get value from cache asynchronously using backend."""
        try:
            # Note: BaseBackend methods are sync (not async)
            # We wrap in executor for async compatibility
            value = self._with_backpressure_and_timeout(self.backend.get, key)

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

    async def set_async(self, key: str, value: Union[str, bytes], ttl: Optional[int] = None, **metadata) -> bool:
        """Set value in cache asynchronously using backend."""
        # Ensure value is bytes
        if isinstance(value, str):
            value = value.encode("utf-8")

        try:
            # Note: BaseBackend methods are sync (not async)
            # We wrap in executor for async compatibility
            self._with_backpressure_and_timeout(self.backend.set, key, value, ttl)
            return True
        except BackendError as e:
            get_logger().error(f"Backend error setting key {key}: {e}")
            return False
        except Exception as e:
            get_logger().error(f"Unexpected error setting key {key}: {e}")
            return False

    async def delete_async(self, key: str) -> bool:
        """Delete key from cache asynchronously using backend."""
        try:
            # Note: BaseBackend methods are sync (not async)
            # We wrap in executor for async compatibility
            return self._with_backpressure_and_timeout(self.backend.delete, key)
        except BackendError as e:
            get_logger().error(f"Backend error deleting key {key}: {e}")
            return False
        except Exception as e:
            get_logger().error(f"Unexpected error deleting key {key}: {e}")
            return False
