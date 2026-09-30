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
from typing import Any, ClassVar

import msgpack

from cachekit._rust_serializer import ByteStorage

from .base import PAYLOAD_DECODE_ERRORS, SerializationError, SerializationFormat, SerializationMetadata, unpackb_bounded

# Every envelope ByteStorage has written opens with a fixarray-4 (0x94) and then its payload slot's
# marker: bin8/16/32 in the current encoding, an int array (fixarray, array16, array32) in the legacy one.
_ENVELOPE_PAYLOAD_MARKERS = frozenset((0xC4, 0xC5, 0xC6, *range(0x90, 0xA0), 0xDC, 0xDD))
# Largest declared payload an integrity-off reader verifies; verifying costs a full decompress (see deserialize).
_ENVELOPE_PROBE_MAX_SIZE = 256 * 1024
_CROSS_CONFIG_ERROR = "Cache entry was written with integrity checking on but this reader has integrity checking disabled"

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

    # MessagePack is a language-agnostic wire format — safe under encryption for cross-SDK reads.
    cross_sdk_compatible: ClassVar[bool] = True

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

        # Built with integrity off too: that reader verifies a would-be envelope before refusing
        # it (see deserialize Raises:). Writes still gate on the flag.
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

    def deserialize(self, data: bytes | memoryview, metadata: SerializationMetadata | None = None) -> Any:
        """Deserialize MessagePack bytes with optional ByteStorage unwrapping.

        Args:
            data: Bytes from serialize() (with or without ByteStorage envelope)
            metadata: Optional metadata. Only ``compressed`` is read (see Raises).

        Returns:
            Deserialized Python object

        Raises:
            SerializationError: If data is malformed, not valid MessagePack, or integrity check
                fails. With integrity checking off, also if the entry is a ByteStorage envelope,
                which this reader does not unwrap: ``metadata.compressed`` says so, or the bytes
                have the layout ByteStorage writes, declare at most 256 KiB, and pass
                ``ByteStorage.retrieve()``. Shape alone never refuses a value, unlike
                :class:`AutoSerializer`: the docs send users here to escape that refusal, and a
                look-alike cannot match the checksum by chance. So without a ``compressed=True``
                header, a rotted envelope, one declaring more than 256 KiB, and one in another
                layout come back decoded as their fields, and a value equal to a valid envelope's
                fields is refused on every read.

        Examples:
            >>> serializer = StandardSerializer()
            >>> data, _ = serializer.serialize({"test": 123})
            >>> result = serializer.deserialize(data)
            >>> result == {"test": 123}
            True
        """
        # No bytes() coercion: Rust retrieve accepts the buffer protocol (LAB-770), so
        # unwrap's zero-copy memoryview flows through without a full-payload copy.
        try:
            if self.enable_integrity_checking:
                # Unwrap ByteStorage envelope (decompress + validate integrity)
                msgpack_data, _ = self._byte_storage.retrieve(data)
                return unpackb_bounded(msgpack_data, **self._msgpack_unpack_opts)
            # No unwrap here: an enveloped entry is refused, not decoded (see Raises).
            if metadata is not None and metadata.compressed:
                raise SerializationError(_CROSS_CONFIG_ERROR)
            if isinstance(data, memoryview):
                # Flatten to unsigned bytes as unpackb_bounded does, so the layout test indexes the same
                # bytes the decode reads: a signed view reads the 0x94 lead as -108.
                data = data.cast("B") if data.c_contiguous else bytes(data)
            value = unpackb_bounded(data, **self._msgpack_unpack_opts)
            if self._is_verified_envelope(data, value):
                raise SerializationError(_CROSS_CONFIG_ERROR)
            return value
        except SerializationError:
            # Re-raise SerializationError (integrity check failure) without swallowing
            raise
        except PAYLOAD_DECODE_ERRORS as e:
            raise SerializationError(f"Failed to deserialize MessagePack data: {e}") from e

    def _is_verified_envelope(self, data: bytes | memoryview, value: Any) -> bool:
        """True only when ``data``, already decoded to ``value``, is a ByteStorage envelope that passes ``retrieve()``."""
        # Necessary conditions, never a verdict. The layout test spares ordinary reads the attempt. The
        # size cap bounds it: retrieve() decompresses the whole declared payload before it checks the
        # checksum, and a caller can cache a value that declares a large one. A 0x94 lead that decoded
        # is a 4-element list, so value[2] is the declared original_size.
        if len(data) < 2 or data[0] != 0x94 or data[1] not in _ENVELOPE_PAYLOAD_MARKERS:
            return False
        if not isinstance(value[2], int) or value[2] > _ENVELOPE_PROBE_MAX_SIZE:
            return False
        try:
            self._byte_storage.retrieve(data)
        except Exception:  # any failure (not an envelope, checksum, size) means "not verified"
            return False
        return True


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
