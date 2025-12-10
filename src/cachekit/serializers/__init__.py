import logging
from threading import Lock
from typing import Any

from cachekit._rust_serializer import ByteStorage

from .arrow_serializer import ArrowSerializer
from .auto_serializer import AutoSerializer
from .base import (
    SerializationError,
    SerializationFormat,
    SerializationMetadata,
    SerializerProtocol,
)
from .encryption_wrapper import EncryptionWrapper
from .orjson_serializer import OrjsonSerializer
from .standard_serializer import StandardSerializer

logger = logging.getLogger(__name__)

# Validate ByteStorage works correctly
test_storage = ByteStorage("msgpack")
test_data = b"test validation data"
envelope = test_storage.store(test_data, None)
retrieved, _ = test_storage.retrieve(envelope)
assert retrieved == test_data, "ByteStorage validation failed"

# Serializer factory with double-checked locking
# Cache key format: "name:integrity_checking" to support different configurations
_serializer_cache: dict[str, Any] = {}
_serializer_lock = Lock()

# Registry maps serializer names to factory functions (not classes directly)
# This allows passing enable_integrity_checking parameter during instantiation
SERIALIZER_REGISTRY = {
    "auto": AutoSerializer,  # Python-specific types (NumPy, pandas, datetime optimization)
    "default": StandardSerializer,  # Language-agnostic MessagePack for multi-language caches
    "std": StandardSerializer,  # Explicit StandardSerializer alias
    "arrow": ArrowSerializer,
    "orjson": OrjsonSerializer,
    "encrypted": EncryptionWrapper,  # AutoSerializer + AES-256-GCM encryption
}


def get_serializer(name: str, enable_integrity_checking: bool = True) -> SerializerProtocol:
    """Get cached serializer instance by name with configurable integrity checking (thread-safe).

    Uses double-checked locking for fast-path performance (lock-free reads
    after first instantiation). This eliminates 625μs overhead from repeated
    serializer instantiation.

    Args:
        name: Serializer name ("default", "std", "auto", "arrow", "orjson")
        enable_integrity_checking: Enable integrity checking (default: True)
            - For "default"/"std": Controls ByteStorage layer (True = LZ4 + xxHash3-64, False = pure MessagePack)
            - For "auto": Controls ByteStorage layer (True = LZ4 + xxHash3-64, False = plain pickle)
            - For "arrow"/"orjson": Controls xxHash3-64 integrity checking (Python xxhash)

    Returns:
        Cached serializer instance configured with integrity checking setting

    Raises:
        ValueError: If name not in SERIALIZER_REGISTRY

    Examples:
        >>> serializer = get_serializer("default")
        >>> isinstance(serializer, StandardSerializer)
        True
        >>> serializer_fast = get_serializer("std", enable_integrity_checking=False)
        >>> serializer_fast.enable_integrity_checking
        False
        >>> get_serializer("invalid")  # doctest: +SKIP
        Traceback (most recent call last):
        ValueError: Unknown serializer: 'invalid'. Valid options: default, std, auto, arrow, orjson
    """
    # Cache key includes integrity_checking setting to support both configurations
    cache_key = f"{name}:{enable_integrity_checking}"

    # Fast path: Check cache without lock (read-only, thread-safe)
    if cache_key in _serializer_cache:
        return _serializer_cache[cache_key]

    # Slow path: Validate and instantiate with lock
    with _serializer_lock:
        # Double-check: Another thread may have created it
        if cache_key in _serializer_cache:
            return _serializer_cache[cache_key]

        # Validate name
        if name not in SERIALIZER_REGISTRY:
            valid_names = ", ".join(SERIALIZER_REGISTRY.keys())
            raise ValueError(
                f"Unknown serializer: '{name}'. "
                f"Valid options: {valid_names}. "
                f"To use a custom serializer, pass instance directly: "
                f"@cache(serializer=MySerializer())"
            )

        # Instantiate with integrity checking configuration
        serializer_class = SERIALIZER_REGISTRY[name]
        if name in ("default", "std", "auto", "arrow", "orjson"):
            # All core serializers use enable_integrity_checking parameter
            serializer = serializer_class(enable_integrity_checking=enable_integrity_checking)
        else:
            # Future serializers without integrity checking support
            serializer = serializer_class()

        # Validate protocol compliance
        if not isinstance(serializer, SerializerProtocol):
            raise TypeError(
                f"Serializer {serializer_class.__name__} must implement "
                f"SerializerProtocol (serialize, deserialize methods required)"
            )

        _serializer_cache[cache_key] = serializer
        return serializer


# Simplified functions using StandardSerializer as default
def serialize(data: Any) -> bytes:
    """Serialize data using StandardSerializer (language-agnostic MessagePack).

    For Python-specific types (NumPy, pandas), use get_serializer("auto") instead.

    Args:
        data: Python object to serialize (must be language-universal type)

    Returns:
        Serialized bytes

    See Also:
        get_serializer("default"): Cached instance with full control over integrity checking
        get_serializer("auto"): AutoSerializer for Python-specific types
    """
    serializer = StandardSerializer()
    serialized_data, _ = serializer.serialize(data)
    return serialized_data


def deserialize(data: bytes) -> Any:
    """Deserialize data using StandardSerializer (language-agnostic MessagePack).

    For Python-specific deserialization, use get_serializer("auto") instead.

    Args:
        data: Serialized bytes (MessagePack format with optional ByteStorage envelope)

    Returns:
        Deserialized Python object

    See Also:
        get_serializer("default"): Cached instance with full control over integrity checking
        get_serializer("auto"): AutoSerializer for Python-specific types
    """
    return StandardSerializer().deserialize(data)


def get_available_serializers() -> dict[str, Any]:
    """Get all available serializer classes.

    Note: EncryptionWrapper is not included as it's a wrapper requiring
    a base serializer, not a standalone serializer type.
    """
    return SERIALIZER_REGISTRY.copy()


def benchmark_serializers() -> dict[str, Any]:
    """Get instantiated serializers for benchmarking."""
    serializers = {}
    for name, cls in get_available_serializers().items():
        try:
            serializers[name] = cls()
        except Exception as e:
            logger.warning(f"Failed to instantiate {name} serializer: {e}")
    return serializers


def get_serializer_info() -> dict[str, dict[str, Any]]:
    """Get information about available serializers."""
    info = {}
    for name, cls in get_available_serializers().items():
        try:
            instance = cls()
            info[name] = {
                "class": cls.__name__,
                "module": cls.__module__,
                "available": True,
                "description": cls.__doc__ or "No description available",
            }
            # Add method info if available
            if hasattr(instance, "get_info"):
                info[name].update(instance.get_info())
        except Exception as e:
            info[name] = {
                "class": cls.__name__,
                "module": cls.__module__,
                "available": False,
                "error": str(e),
            }
    return info


# Export the main interface
__all__ = [
    "ArrowSerializer",
    "AutoSerializer",
    "EncryptionWrapper",
    "OrjsonSerializer",
    "StandardSerializer",
    # Base types
    "SerializationError",
    "SerializationFormat",
    "SerializationMetadata",
    "SerializerProtocol",
    # Factory
    "get_serializer",
    "SERIALIZER_REGISTRY",
    # Utility functions
    "benchmark_serializers",
    "deserialize",
    "get_available_serializers",
    "get_serializer_info",
    "serialize",
]
