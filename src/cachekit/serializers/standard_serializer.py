"""StandardSerializer - Language-agnostic MessagePack serialization.

Minimal, pure MessagePack serializer for multi-language cache interoperability.
Designed for seamless data exchange between Python, PHP, JavaScript, and other languages.

Supports ONLY language-universal primitives:
- None, bool, int, float, str, bytes
- list, tuple, dict
- datetime, date, time (MessagePack extension 0xC0)

Explicitly rejects Python-specific types for safety and interoperability:
- NumPy arrays → Use AutoSerializer (Python-only) or ArrowSerializer (Python/JS/Java/R, NOT PHP)
- pandas DataFrames/Series → Use ArrowSerializer (60%+ faster, multi-language)
- Pydantic models → Convert with .model_dump()
- ORM models → Extract fields explicitly
- Custom classes → Convert to dict

Security: Uses strict isinstance() checks (no hasattr()) to prevent arbitrary code execution.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

import msgpack

from cachekit._rust_serializer import ByteStorage

from .base import SerializationError, SerializationFormat, SerializationMetadata

# Error message constants for unsupported types (Task 2)
NUMPY_ERROR_MESSAGE = (
    "StandardSerializer does not support NumPy arrays (Python-specific type). "
    "Options:\n"
    "  1. AutoSerializer: Python-only caching with automatic NumPy optimization\n"
    "  2. ArrowSerializer: Multi-language support (Python/JavaScript/Java/R, NOT PHP) with 60%+ faster serialization"
)

PANDAS_ERROR_MESSAGE = (
    "StandardSerializer does not support pandas DataFrames or Series (Python-specific types). "
    "Use ArrowSerializer for multi-language DataFrame support (Python/JavaScript/Java/R, NOT PHP). "
    "ArrowSerializer is 60%+ faster than pickle for DataFrames and designed for cross-language interoperability."
)

PYDANTIC_ERROR_MESSAGE = (
    "StandardSerializer does not support Pydantic models (Python-specific type). "
    "Convert to dict before caching:\n\n"
    "    result = model.model_dump()  # Converts Pydantic model to dict\n"
    "    cache.set(key, result)       # Cache the dict\n\n"
    "This ensures compatibility with non-Python languages accessing the same cache."
)

ORM_ERROR_MESSAGE = (
    "StandardSerializer does not support ORM models like SQLAlchemy or Django models (Python-specific types). "
    "Extract fields explicitly to a dict:\n\n"
    "    result = {'id': user.id, 'name': user.name, 'email': user.email}\n"
    "    cache.set(key, result)\n\n"
    "This ensures compatibility with non-Python languages accessing the same cache."
)

CUSTOM_CLASS_ERROR_MESSAGE = (
    "StandardSerializer does not support custom classes (Python-specific types). "
    "Supported types: None, bool, int, float, str, bytes, list, tuple, dict, datetime, date, time\n\n"
    "Options:\n"
    "  1. Convert to dict manually: result = {'field1': obj.field1, 'field2': obj.field2}\n"
    "  2. Use dataclasses.asdict() for dataclasses: result = dataclasses.asdict(obj)\n"
    "  3. Use AutoSerializer if you only need Python-to-Python caching\n\n"
    "StandardSerializer prioritizes multi-language compatibility over convenience."
)


def _standard_default(obj: Any) -> Any:
    """Custom encoder for datetime types (MessagePack extension 0xC0).

    Handles ONLY datetime/date/time using ISO-8601 string format.
    Extension code 0xC0 chosen for language-agnostic datetime representation.

    Explicitly rejects Python-specific types with actionable error messages.

    Args:
        obj: Object to encode

    Returns:
        MessagePack-compatible representation with __datetime__ marker

    Raises:
        TypeError: For unsupported types with actionable guidance
    """
    # Language-universal datetime support (MessagePack extension 0xC0)
    if isinstance(obj, datetime):
        return {"__datetime__": True, "value": obj.isoformat()}
    if isinstance(obj, date):
        return {"__date__": True, "value": obj.isoformat()}
    if isinstance(obj, time):
        return {"__time__": True, "value": obj.isoformat()}

    # Security: Use isinstance() checks instead of hasattr() to prevent arbitrary code execution
    # hasattr() can trigger __getattr__ which may execute malicious code

    # NumPy array detection (strict isinstance check)
    if type(obj).__module__ == "numpy" and type(obj).__name__ == "ndarray":
        raise TypeError(NUMPY_ERROR_MESSAGE)

    # Pandas DataFrame/Series detection (strict isinstance check)
    if type(obj).__module__ == "pandas.core.frame" and type(obj).__name__ == "DataFrame":
        raise TypeError(PANDAS_ERROR_MESSAGE)
    if type(obj).__module__ == "pandas.core.series" and type(obj).__name__ == "Series":
        raise TypeError(PANDAS_ERROR_MESSAGE)

    # Pydantic model detection (check for BaseModel in class hierarchy)
    if "BaseModel" in (base.__name__ for base in type(obj).__mro__):
        raise TypeError(PYDANTIC_ERROR_MESSAGE)

    # ORM model detection (check for common ORM base class names)
    orm_base_names = {"Model", "DeclarativeBase", "Base"}
    if any(base.__name__ in orm_base_names for base in type(obj).__mro__):
        raise TypeError(ORM_ERROR_MESSAGE)

    # Custom class detection (has __dict__ but not a builtin type)
    if hasattr(type(obj), "__dict__") and type(obj).__module__ != "builtins":
        raise TypeError(CUSTOM_CLASS_ERROR_MESSAGE)

    # Generic MessagePack error (fallback)
    raise TypeError(
        f"Object of type {type(obj).__name__} is not supported by StandardSerializer. "
        f"Supported types: None, bool, int, float, str, bytes, list, tuple, dict, datetime, date, time"
    )


def _standard_object_hook(obj: Any) -> Any:
    """Custom decoder for datetime types (MessagePack extension 0xC0).

    Restores datetime/date/time from ISO-8601 strings.

    Args:
        obj: Object from MessagePack decoder

    Returns:
        Restored Python object or original obj if not a datetime marker
    """
    if isinstance(obj, dict):
        if obj.get("__datetime__"):
            value = obj.get("value")
            if value is None:
                raise SerializationError("Invalid datetime format: missing 'value' field in cached data")
            return datetime.fromisoformat(value)
        if obj.get("__date__"):
            value = obj.get("value")
            if value is None:
                raise SerializationError("Invalid date format: missing 'value' field in cached data")
            return date.fromisoformat(value)
        if obj.get("__time__"):
            value = obj.get("value")
            if value is None:
                raise SerializationError("Invalid time format: missing 'value' field in cached data")
            return time.fromisoformat(value)

    return obj


class StandardSerializer:
    """Language-agnostic MessagePack serializer for multi-language cache interoperability.

    Implements SerializerProtocol via structural subtyping (PEP 544).
    No inheritance required - protocol compliance validated at runtime.

    Designed for seamless data exchange between Python, PHP, JavaScript, and other languages.
    Uses pure MessagePack format (no Python-specific types) with optional ByteStorage wrapper
    for compression and integrity checking.

    Supported Types (Language-Universal):
    - Primitives: None, bool, int, float, str, bytes
    - Collections: list, tuple, dict
    - Temporal: datetime, date, time (ISO-8601 via MessagePack extension 0xC0)

    NOT Supported (Use AutoSerializer or ArrowSerializer):
    - NumPy arrays (Python-specific)
    - pandas DataFrames/Series (Python-specific)
    - Pydantic models (Python-specific)
    - ORM models (Python-specific)
    - Custom classes (Python-specific)
    - UUID (Python-specific)
    - set/frozenset (Python-specific)

    Features:
    - Pure MessagePack (language-agnostic wire format)
    - Optional LZ4 compression via ByteStorage
    - Optional xxHash3-64 integrity checking via ByteStorage
    - ISO-8601 datetime encoding (cross-language compatible)
    - Explicit type checking (prevents silent data corruption)

    Use Cases:
    - Multi-language microservices sharing Redis cache
    - PHP/JavaScript frontend + Python backend
    - Cross-language API response caching
    - Language-agnostic session storage

    Protocol Compliance:
        serialize(obj) -> tuple[bytes, SerializationMetadata]
        deserialize(data, metadata=None) -> Any

    Examples:
        >>> serializer = StandardSerializer()
        >>> data, meta = serializer.serialize({"user_id": 123, "name": "Alice"})
        >>> isinstance(data, bytes)
        True
        >>> meta.format
        <SerializationFormat.MSGPACK: 'msgpack'>
        >>> result = serializer.deserialize(data)
        >>> result == {"user_id": 123, "name": "Alice"}
        True

        >>> # Datetime support (ISO-8601)
        >>> from datetime import datetime
        >>> dt = datetime(2024, 1, 15, 12, 30, 0)
        >>> data, _ = serializer.serialize({"timestamp": dt})
        >>> result = serializer.deserialize(data)
        >>> result["timestamp"] == dt
        True

        >>> # NumPy arrays rejected with helpful error
        >>> import numpy as np
        >>> serializer.serialize(np.array([1, 2, 3]))  # doctest: +SKIP
        Traceback (most recent call last):
        TypeError: StandardSerializer does not support NumPy arrays...
    """

    def __init__(self, enable_integrity_checking: bool = True):
        """Initialize StandardSerializer.

        Args:
            enable_integrity_checking: Enable ByteStorage for LZ4 compression and xxHash3-64 integrity (default: True)
                When True: Wraps MessagePack with ByteStorage (compression + integrity checks)
                When False: Pure MessagePack (no compression, no integrity checks)

        Examples:
            >>> serializer = StandardSerializer()  # Default: integrity ON
            >>> serializer_fast = StandardSerializer(enable_integrity_checking=False)  # Speed-first
        """
        self.enable_integrity_checking = enable_integrity_checking

        if self.enable_integrity_checking:
            self._byte_storage = ByteStorage("msgpack")

        # MessagePack configuration for cross-language compatibility
        self._msgpack_pack_opts = {
            "use_bin_type": True,  # Use bin type for bytes (MessagePack spec compliance)
            "strict_types": False,  # Allow mixed types (more flexible)
            "default": _standard_default,  # Handle datetime/date/time
        }
        self._msgpack_unpack_opts = {
            "use_list": True,  # Decode arrays as lists (not tuples)
            "raw": False,  # Decode strings properly (not bytes)
            "object_hook": _standard_object_hook,  # Restore datetime/date/time
        }

    def serialize(self, obj: Any) -> tuple[bytes, SerializationMetadata]:
        """Serialize object to pure MessagePack bytes with optional ByteStorage wrapper.

        Supports ONLY language-universal types (primitives, collections, datetime).
        Rejects Python-specific types (NumPy, pandas, Pydantic, ORM models, custom classes).

        Args:
            obj: Python object to serialize (must be language-universal type)

        Returns:
            Tuple of (serialized bytes, metadata)
            Format (integrity ON): ByteStorage envelope with LZ4 compression + xxHash3-64 checksum
            Format (integrity OFF): Pure MessagePack bytes

        Raises:
            TypeError: If object type is not supported (with actionable error message)
            SerializationError: If serialization fails (data encoding error)

        Examples:
            >>> serializer = StandardSerializer()
            >>> data, meta = serializer.serialize({"test": 123})
            >>> isinstance(data, bytes)
            True
            >>> meta.format.value
            'msgpack'
        """
        try:
            # Serialize to pure MessagePack
            msgpack_data = msgpack.packb(obj, **self._msgpack_pack_opts)

            # Conditionally add ByteStorage wrapper (compression + integrity)
            if self.enable_integrity_checking:
                envelope = self._byte_storage.store(msgpack_data, "msgpack")  # type: ignore[assignment]
            else:
                envelope = msgpack_data

            metadata = SerializationMetadata(
                serialization_format=SerializationFormat.MSGPACK,
                compressed=self.enable_integrity_checking,  # LZ4 compression when ByteStorage enabled
                encrypted=False,  # Encryption is EncryptionWrapper's responsibility
                original_type="msgpack",
            )
            return envelope, metadata  # type: ignore[return-value]
        except TypeError:
            # TypeError = unsupported type (propagate error message from _standard_default)
            raise
        except ValueError as e:
            # ValueError = data encoding error
            raise SerializationError(f"Failed to serialize object to MessagePack: {e}") from e

    def deserialize(self, data: bytes, metadata: SerializationMetadata | None = None) -> Any:
        """Deserialize MessagePack bytes with optional ByteStorage unwrapping.

        Args:
            data: Bytes from serialize() (with or without ByteStorage envelope)
            metadata: Optional metadata (ignored - MessagePack is self-describing)

        Returns:
            Deserialized Python object

        Raises:
            SerializationError: If data is malformed, not valid MessagePack, or integrity check fails

        Examples:
            >>> serializer = StandardSerializer()
            >>> data, _ = serializer.serialize({"test": 123})
            >>> result = serializer.deserialize(data)
            >>> result == {"test": 123}
            True
        """
        try:
            if self.enable_integrity_checking:
                # Unwrap ByteStorage envelope (decompress + validate integrity)
                msgpack_data, _ = self._byte_storage.retrieve(data)
            else:
                # No ByteStorage - data is pure MessagePack
                msgpack_data = data

            # Deserialize MessagePack
            return msgpack.unpackb(msgpack_data, **self._msgpack_unpack_opts)
        except SerializationError:
            # Re-raise SerializationError (integrity check failure) without swallowing
            raise
        except (msgpack.exceptions.UnpackException, ValueError, TypeError) as e:
            raise SerializationError(f"Failed to deserialize MessagePack data: {e}") from e


# Default instance for convenience
standard_serializer = StandardSerializer()


# Convenience functions
def serialize(obj: Any) -> bytes:
    """Serialize object using standard serializer."""
    data, _metadata = standard_serializer.serialize(obj)
    return data


def deserialize(data: bytes) -> Any:
    """Deserialize data using standard serializer."""
    return standard_serializer.deserialize(data)
