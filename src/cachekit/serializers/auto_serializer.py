# pyright: reportOptionalMemberAccess=false
# pyright: reportInvalidTypeForm=false
"""AutoSerializer - Intelligent type-detecting serializer.

Auto-detects and optimizes:
- NumPy arrays (NUMPY_RAW format, zero-copy)
- Pandas DataFrames (columnar format, 60%+ faster than pickle)
- Pandas Series (metadata preservation)
- datetime/date/time (ISO-8601)
- UUID (string representation)
- set/frozenset (type-safe roundtrip)

Uses MessagePack as the default format with graceful degradation for optional dependencies.

Type Checking Note:
Optional imports (numpy, pandas) are guarded at runtime by HAS_NUMPY, HAS_PANDAS flags.
Type checker cannot statically verify these; suppressed via pyright config comments above.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

import msgpack

if TYPE_CHECKING:
    import numpy as np
    import pandas as pd

# Optional imports with feature flags
try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    np = None  # type: ignore[assignment]

try:
    import pandas as pd

    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
    pd = None  # type: ignore[assignment]

# Optional: ArrowSerializer for fast DataFrame serialization
try:
    from .arrow_serializer import ArrowSerializer

    HAS_ARROW_SERIALIZER = True
except ImportError:
    HAS_ARROW_SERIALIZER = False
    ArrowSerializer = None  # type: ignore[assignment,misc]

from cachekit._rust_serializer import ByteStorage

from .base import SerializationError, SerializationFormat, SerializationMetadata

logger = logging.getLogger(__name__)

# Error message constants for unsupported types
PYDANTIC_ERROR_MESSAGE = (
    "AutoSerializer does not support Pydantic models. Use .model_dump() to convert to dict: result = model.model_dump()"
)

ORM_ERROR_MESSAGE = (
    "AutoSerializer does not support ORM models (SQLAlchemy, Django, etc.). Convert to dict or implement a custom serializer."
)

CUSTOM_CLASS_ERROR_MESSAGE = (
    "AutoSerializer does not support custom classes. "
    "Supported types: dict, list, str, int, float, bool, None, bytes, "
    "datetime, date, time, UUID, set, frozenset, NumPy arrays, pandas DataFrames.\n"
    "Options:\n"
    "  1. Convert to dict manually\n"
    "  2. Use dataclasses.asdict() for dataclasses\n"
    "  3. Write a custom serializer implementing SerializerProtocol"
)


def _safe_hasattr(obj: Any, attr: str) -> bool:
    """Safe hasattr that prevents arbitrary code execution via __getattr__.

    Standard hasattr() can trigger side effects if the object implements
    __getattr__ or if the attribute is a property with side effects.

    This implementation uses object.__getattribute__() to bypass custom
    __getattr__ implementations, preventing malicious code execution.

    Args:
        obj: Object to check for attribute
        attr: Attribute name to check

    Returns:
        True if attribute exists, False otherwise (including on errors)

    Security:
        Prevents DoS attacks via expensive property evaluation or
        malicious __getattr__ implementations.

    Example:
        >>> class Evil:
        ...     def __getattr__(self, name):
        ...         import os
        ...         os.system('rm -rf /')  # Malicious!
        ...         return lambda: {}
        >>> _safe_hasattr(Evil(), 'model_dump')  # Safe - returns False
        False
    """
    try:
        # Use object.__getattribute__ to bypass custom __getattr__
        # This prevents triggering malicious code in __getattr__ implementations
        object.__getattribute__(obj, attr)
        return True
    except AttributeError:
        # Attribute doesn't exist - this is the normal case
        return False
    except Exception:
        # Any other exception means we can't trust the object
        return False


def _auto_default(obj: Any) -> Any:
    """Custom encoder for types not natively supported by MessagePack.

    Handles:
    - datetime/date/time → ISO-8601 strings
    - UUID → string representation
    - set/frozenset → list (with type marker for roundtrip)

    Provides helpful errors for:
    - Pydantic models (suggest .model_dump())
    - ORM models (suggest conversion to dict)
    - Custom classes (suggest alternatives)

    Args:
        obj: Object to encode

    Returns:
        MessagePack-compatible representation

    Raises:
        TypeError: For unsupported types with actionable guidance
    """
    # Existing: datetime/date/time support (KEEP)
    if isinstance(obj, datetime):
        return {"__datetime__": True, "value": obj.isoformat()}
    if isinstance(obj, date):
        return {"__date__": True, "value": obj.isoformat()}
    if isinstance(obj, time):
        return {"__time__": True, "value": obj.isoformat()}

    # NEW: UUID support
    if isinstance(obj, UUID):
        return {"__uuid__": True, "value": str(obj)}

    # NEW: set/frozenset support (type-safe roundtrip)
    if isinstance(obj, (set, frozenset)):
        return {"__set__": True, "value": list(obj), "frozen": isinstance(obj, frozenset)}

    # NEW: Helpful error detection for common unsupported types
    if _safe_hasattr(obj, "model_dump"):  # Pydantic BaseModel
        raise TypeError(PYDANTIC_ERROR_MESSAGE)

    if _safe_hasattr(obj, "__tablename__"):  # SQLAlchemy/ORM model
        raise TypeError(ORM_ERROR_MESSAGE)

    if _safe_hasattr(obj, "__dict__") and type(obj).__module__ != "builtins":
        # Custom class (has __dict__ but not a builtin type)
        raise TypeError(CUSTOM_CLASS_ERROR_MESSAGE)

    # Generic MessagePack error (fallback)
    raise TypeError(f"Object of type {type(obj).__name__} is not MessagePack serializable")


def _auto_object_hook(obj: Any) -> Any:
    """Custom decoder for types encoded by _auto_default().

    Restores:
    - datetime/date/time from ISO-8601 strings
    - UUID from string representation
    - set/frozenset from list (type-safe roundtrip)

    Args:
        obj: Object from MessagePack decoder

    Returns:
        Restored Python object or original obj if not a special marker
    """
    if isinstance(obj, dict):
        # Strict validation to prevent collision with user dicts like {'__time__': UUID(...)}
        # Only decode if marker is exactly True (not any truthy value)
        # Raise error if marker is True but structure is malformed (corrupted cache data)

        if obj.get("__datetime__") is True:
            if "value" not in obj:
                raise SerializationError("Invalid datetime format: missing 'value' field in cached data")
            return datetime.fromisoformat(obj["value"])

        if obj.get("__date__") is True:
            if "value" not in obj:
                raise SerializationError("Invalid date format: missing 'value' field in cached data")
            return date.fromisoformat(obj["value"])

        if obj.get("__time__") is True:
            if "value" not in obj:
                raise SerializationError("Invalid time format: missing 'value' field in cached data")
            return time.fromisoformat(obj["value"])

        if obj.get("__uuid__") is True:
            if "value" not in obj:
                raise SerializationError("Invalid UUID format: missing 'value' field in cached data")
            value = obj["value"]
            try:
                return UUID(value)
            except (ValueError, TypeError) as e:
                raise SerializationError(f"Invalid UUID format in cached data: {value}") from e

        if obj.get("__set__") is True:
            if "value" not in obj:
                raise SerializationError("Invalid set format: missing 'value' field in cached data")
            value_list = obj["value"]
            if not isinstance(value_list, list):
                raise SerializationError(f"Invalid set format: expected list, got {type(value_list).__name__}")

            if obj.get("frozen"):
                return frozenset(value_list)
            else:
                return set(value_list)

    return obj


class AutoSerializer:
    """Intelligent serializer with automatic type detection.

    Implements SerializerProtocol via structural subtyping (PEP 544).
    No inheritance required - protocol compliance validated at runtime.

    Features:
    - MessagePack as default format
    - Automatic NumPy array detection and optimization
    - Automatic DataFrame detection and optimization
    - datetime/date/time support (ISO-8601)
    - UUID support (string representation)
    - set/frozenset support (type-safe roundtrip)
    - LZ4 compression via Rust layer
    - xxHash3-64 checksums for integrity
    - ZERO backwards compatibility (greenfield)

    Named "Auto" to be transparent about auto-detection behavior.
    Users understand: "This serializer makes intelligent guesses about optimization."

    Protocol Compliance:
        serialize(obj) -> tuple[bytes, SerializationMetadata]
        deserialize(data, metadata=None) -> Any

    Examples:
        Basic roundtrip with dict:

        >>> serializer = AutoSerializer()
        >>> data, meta = serializer.serialize({"user": "alice", "score": 100})
        >>> isinstance(data, bytes)
        True
        >>> meta.format.value
        'msgpack'
        >>> result = serializer.deserialize(data, meta)
        >>> result == {"user": "alice", "score": 100}
        True

        UUID preservation:

        >>> from uuid import UUID
        >>> original = {"id": UUID("12345678-1234-5678-1234-567812345678")}
        >>> data, meta = serializer.serialize(original)
        >>> result = serializer.deserialize(data, meta)
        >>> result["id"] == original["id"]
        True
        >>> isinstance(result["id"], UUID)
        True

        Set/frozenset roundtrip:

        >>> original = {"tags": {"a", "b", "c"}, "frozen": frozenset([1, 2])}
        >>> data, meta = serializer.serialize(original)
        >>> result = serializer.deserialize(data, meta)
        >>> result["tags"] == {"a", "b", "c"}
        True
        >>> isinstance(result["frozen"], frozenset)
        True

        Datetime support:

        >>> from datetime import datetime
        >>> dt = datetime(2024, 6, 15, 10, 30, 0)
        >>> data, meta = serializer.serialize({"created": dt})
        >>> result = serializer.deserialize(data, meta)
        >>> result["created"] == dt
        True

        Disable integrity checking for speed:

        >>> fast_serializer = AutoSerializer(enable_integrity_checking=False)
        >>> data, _ = fast_serializer.serialize({"fast": True})
        >>> fast_serializer.deserialize(data)
        {'fast': True}
    """

    def __init__(
        self,
        default_format: str = "msgpack",
        enable_integrity_checking: bool = True,
        use_rust: bool | None = None,  # DEPRECATED
    ):
        """Initialize AutoSerializer.

        Args:
            default_format: Serialization format (currently only "msgpack" supported)
            enable_integrity_checking: Enable ByteStorage for compression and integrity checks (default: True)
                When True: LZ4 compression + xxHash3-64 integrity checking via ByteStorage
                When False: Plain MessagePack (no compression, no integrity checks)
                Note: Setting enable_integrity_checking=False disables integrity checking for @cache.minimal
            use_rust: DEPRECATED - use enable_integrity_checking instead

        Raises:
            ValueError: If default_format is not recognized
        """
        # Handle deprecated use_rust parameter
        if use_rust is not None:
            import warnings

            warnings.warn(
                "use_rust parameter is deprecated, use enable_integrity_checking instead",
                DeprecationWarning,
                stacklevel=2,
            )
            enable_integrity_checking = use_rust

        if default_format not in ("msgpack",):
            raise ValueError(f"Unsupported default_format: '{default_format}'. Supported formats: 'msgpack'")

        self.default_format = default_format
        self.enable_integrity_checking = enable_integrity_checking

        if self.enable_integrity_checking:
            self._byte_storage = ByteStorage(default_format)

        # Initialize ArrowSerializer for fast DataFrame serialization (if available)
        if HAS_ARROW_SERIALIZER:
            self._arrow_serializer = ArrowSerializer()  # type: ignore[misc]
        else:
            self._arrow_serializer = None

        # MessagePack configuration for speed
        self._msgpack_pack_opts = {
            "use_bin_type": True,  # Use bin type for bytes (faster)
            "strict_types": False,  # Allow mixed types (more flexible)
            "default": _auto_default,  # Handle datetime, UUID, set, frozenset
        }
        self._msgpack_unpack_opts = {
            "use_list": True,  # Need lists for DataFrame serialization format
            "raw": False,  # Decode strings properly
            "object_hook": _auto_object_hook,  # Restore datetime, UUID, set, frozenset
        }

    def serialize(self, obj: Any) -> tuple[bytes, SerializationMetadata]:
        """Serialize object to bytes using auto detection.

        Auto-detects:
        - NumPy arrays (efficient binary serialization, if numpy installed)
        - Pandas DataFrames (ArrowSerializer if available, else columnar msgpack)
        - Everything else (MessagePack)

        Args:
            obj: Object to serialize

        Returns:
            Tuple[bytes, SerializationMetadata]: Serialized data with metadata
        """
        # NumPy detection (only if numpy installed)
        if HAS_NUMPY and isinstance(obj, np.ndarray):  # type: ignore[union-attr]
            data = self._serialize_numpy(obj)
            metadata = SerializationMetadata(serialization_format=SerializationFormat.MSGPACK, original_type="numpy")
            return data, metadata

        # DataFrame detection (delegate to ArrowSerializer if available)
        if HAS_PANDAS and isinstance(obj, pd.DataFrame):  # type: ignore[union-attr]
            if self._arrow_serializer is not None:
                # Use ArrowSerializer for 50-100x faster DataFrame serialization
                return self._arrow_serializer.serialize(obj)
            else:
                # Fallback to msgpack columnar format
                data = self._serialize_dataframe(obj)
                metadata = SerializationMetadata(serialization_format=SerializationFormat.MSGPACK, original_type="dataframe")
                return data, metadata

        # Series detection (only if pandas installed)
        if HAS_PANDAS and isinstance(obj, pd.Series):  # type: ignore[union-attr]
            data = self._serialize_series(obj)
            metadata = SerializationMetadata(serialization_format=SerializationFormat.MSGPACK, original_type="series")
            return data, metadata

        # Default: MessagePack (always available)
        data = self._serialize_msgpack(obj)
        metadata = SerializationMetadata(serialization_format=SerializationFormat.MSGPACK, original_type="msgpack")
        return data, metadata

    def deserialize(self, data: bytes, metadata: Optional[SerializationMetadata] = None) -> Any:
        """Deserialize bytes back to Python object.

        Automatically detects format from envelope and deserializes accordingly.

        Args:
            data: Serialized bytes from serialize()
            metadata: Optional metadata (contains original_type for format detection)

        Returns:
            Any: Deserialized Python object
        """
        # Check for custom NumPy format
        if data.startswith(b"NUMPY_RAW"):
            return self._deserialize_numpy(data)

        # Use metadata for format detection if available
        if metadata and hasattr(metadata, "original_type"):
            detected_format = metadata.original_type

            # For specialized formats, call type-specific deserializers
            if detected_format == "numpy":
                return self._deserialize_numpy(data)
            elif detected_format == "arrow":
                # Arrow-serialized DataFrame (delegate to ArrowSerializer)
                if self._arrow_serializer is not None:
                    return self._arrow_serializer.deserialize(data, metadata)
                else:
                    raise SerializationError(
                        "Cannot deserialize Arrow format: ArrowSerializer not available. "
                        "Install with: pip install 'cachekit[data]'"
                    )
            elif detected_format == "dataframe":
                # For DataFrame and Series, we need to handle both Rust envelope and direct data
                if self.enable_integrity_checking and len(data) > 4:
                    try:
                        # Try Rust envelope first
                        original_data, _ = self._byte_storage.retrieve(data)
                        unpacked_data = msgpack.unpackb(original_data, **self._msgpack_unpack_opts)
                        return self._deserialize_dataframe(unpacked_data)
                    except Exception as e:
                        # Fall back to direct unpacking
                        logger.debug(f"Rust envelope parsing failed for DataFrame, falling back to direct unpacking: {e}")
                # Direct msgpack data
                unpacked_data = msgpack.unpackb(data, **self._msgpack_unpack_opts)
                return self._deserialize_dataframe(unpacked_data)
            elif detected_format == "series":
                # For DataFrame and Series, we need to handle both Rust envelope and direct data
                if self.enable_integrity_checking and len(data) > 4:
                    try:
                        # Try Rust envelope first
                        original_data, _ = self._byte_storage.retrieve(data)
                        unpacked_data = msgpack.unpackb(original_data, **self._msgpack_unpack_opts)
                        return self._deserialize_series(unpacked_data)
                    except Exception as e:
                        # Fall back to direct unpacking
                        logger.debug(f"Rust envelope parsing failed for Series, falling back to direct unpacking: {e}")
                # Direct msgpack data
                unpacked_data = msgpack.unpackb(data, **self._msgpack_unpack_opts)
                return self._deserialize_series(unpacked_data)

        # For Rust-envelope formats, use the Rust layer
        if self.enable_integrity_checking:
            try:
                # Use Rust layer for decompression and validation
                original_data, format_id = self._byte_storage.retrieve(data)

                # Use metadata if available, otherwise fall back to format_id from envelope
                if metadata and hasattr(metadata, "original_type"):
                    detected_format = metadata.original_type
                else:
                    detected_format = format_id

                # Deserialize based on detected format
                if detected_format == "numpy":
                    return self._deserialize_numpy(original_data)
                elif detected_format == "dataframe":
                    # Unpack the msgpack data first, then pass to DataFrame deserializer
                    unpacked_data = msgpack.unpackb(original_data, **self._msgpack_unpack_opts)
                    return self._deserialize_dataframe(unpacked_data)
                elif detected_format == "series":
                    # Unpack the msgpack data first, then pass to Series deserializer
                    unpacked_data = msgpack.unpackb(original_data, **self._msgpack_unpack_opts)
                    return self._deserialize_series(unpacked_data)
                else:  # msgpack
                    return msgpack.unpackb(original_data, **self._msgpack_unpack_opts)
            except SerializationError:
                # Re-raise SerializationError (corruption detection) without swallowing
                raise
            except Exception as e:
                # If Rust envelope parsing fails for other reasons, try Python-only deserialization
                logger.debug(f"Rust envelope parsing failed, falling back to Python-only deserialization: {e}")

        # Check for Arrow IPC format before msgpack fall-through
        # Arrow data may have xxHash3-64 checksum prefix (8 bytes) or be direct Arrow IPC
        if len(data) >= 14 and data[8:14] == b"ARROW1":
            # Has xxHash3-64 checksum prefix (ArrowSerializer with integrity checking)
            if self._arrow_serializer is not None:
                return self._arrow_serializer.deserialize(data, metadata)
            else:
                raise SerializationError(
                    "Cannot deserialize Arrow format: ArrowSerializer not available. Install with: pip install 'cachekit[data]'"
                )
        elif len(data) >= 6 and data[:6] == b"ARROW1":
            # Direct Arrow IPC (without integrity checking)
            if self._arrow_serializer is not None:
                return self._arrow_serializer.deserialize(data, metadata)
            else:
                raise SerializationError(
                    "Cannot deserialize Arrow format: ArrowSerializer not available. Install with: pip install 'cachekit[data]'"
                )

        # Python-only path (no Rust compression) - direct msgpack deserialization
        try:
            return msgpack.unpackb(data, **self._msgpack_unpack_opts)
        except SerializationError:
            # Re-raise SerializationError (corruption detection) without swallowing
            raise
        except Exception:
            # If msgpack fails for other reasons, try NumPy-specific deserialization
            return self._deserialize_numpy(data)

    def _serialize_numpy(self, arr: np.ndarray) -> bytes:  # type: ignore[name-defined]
        """Serialize NumPy array with metadata, bypassing Rust ByteStorage.

        Requires: numpy installed (HAS_NUMPY=True)

        Raises:
            RuntimeError: If numpy not installed

        Skips Rust layer because:
        - Compression ineffective on NumPy's random data patterns
        - Native serialization already fast (~2ms)
        - Rust overhead adds ~8ms with minimal benefit
        """
        if not HAS_NUMPY:
            raise RuntimeError("NumPy not installed. Install with: pip install cachekit[data]")

        # Create minimal binary format: [dtype_len][dtype_str][shape_len][shape_data][raw_bytes]
        dtype_str = str(arr.dtype).encode("utf-8")
        dtype_len = len(dtype_str).to_bytes(2, byteorder="little")

        # Encode shape as packed integers
        shape_data = b"".join(dim.to_bytes(4, byteorder="little") for dim in arr.shape)
        shape_len = len(shape_data).to_bytes(2, byteorder="little")

        # Combine: header + raw numpy bytes (zero-copy from NumPy)
        return b"NUMPY_RAW" + dtype_len + dtype_str + shape_len + shape_data + arr.tobytes()

    def _deserialize_numpy(self, data: bytes) -> np.ndarray:
        """Deserialize NumPy array from NUMPY_RAW binary format.

        Requires: numpy installed (HAS_NUMPY=True)

        Raises:
            RuntimeError: If numpy not installed
            SerializationError: If data format is invalid or unrecognized
        """
        if not HAS_NUMPY:
            raise RuntimeError("NumPy not installed. Install with: pip install cachekit[data]")

        if not data.startswith(b"NUMPY_RAW"):
            raise SerializationError("Invalid NumPy data format - expected NUMPY_RAW header")

        try:
            offset = 9  # len(b'NUMPY_RAW')

            # Read dtype
            dtype_len = int.from_bytes(data[offset : offset + 2], byteorder="little")
            offset += 2
            dtype_str = data[offset : offset + dtype_len].decode("utf-8")
            offset += dtype_len

            # Read shape
            shape_len = int.from_bytes(data[offset : offset + 2], byteorder="little")
            offset += 2
            shape_data = data[offset : offset + shape_len]
            offset += shape_len

            # Reconstruct shape from packed integers
            shape = []
            for i in range(0, len(shape_data), 4):
                dim = int.from_bytes(shape_data[i : i + 4], byteorder="little")
                shape.append(dim)
            shape = tuple(shape)

            # Extract raw numpy bytes and reconstruct
            raw_bytes = data[offset:]
            arr = np.frombuffer(raw_bytes, dtype=dtype_str)
            return arr.reshape(shape)
        except (ValueError, IndexError, UnicodeDecodeError) as e:
            raise SerializationError(f"Failed to deserialize NumPy array: {e}") from e

    def _serialize_dataframe(self, df: pd.DataFrame) -> bytes:
        """Serialize DataFrame with column-wise optimization.

        Requires: pandas installed (HAS_PANDAS=True)

        Raises:
            RuntimeError: If pandas not installed"""
        if not HAS_PANDAS:
            raise RuntimeError("Pandas not installed. Install with: pip install cachekit[data]")

        # Column-wise serialization
        serialized = {
            "columns": list(df.columns),
            "index": df.index.tolist() if df.index.name or not df.index.equals(pd.RangeIndex(len(df))) else None,
            "data": {},
        }

        # Serialize each column separately
        for col in df.columns:
            series = df[col]
            if series.dtype.name.startswith("int") or series.dtype.name.startswith("float"):
                # Use NumPy's efficient serialization for numeric data
                serialized["data"][col] = {"type": "numeric", "data": series.values.tobytes(), "dtype": str(series.dtype)}  # type: ignore[union-attr]
            else:
                # Use MessagePack for other types (including datetime columns)
                # tolist() will preserve datetime objects for our custom encoder
                serialized["data"][col] = {"type": "object", "data": series.tolist()}

        msgpack_data = msgpack.packb(serialized, **self._msgpack_pack_opts)

        if self.enable_integrity_checking:
            return self._byte_storage.store(msgpack_data, "dataframe")  # type: ignore[return-value]
        else:
            return msgpack_data  # type: ignore[return-value]

    def _deserialize_dataframe(self, data) -> pd.DataFrame:
        """Deserialize DataFrame from column-wise data.

        Requires: pandas installed (HAS_PANDAS=True)

        Raises:
            RuntimeError: If pandas not installed
        """
        if not HAS_PANDAS:
            raise RuntimeError("Pandas not installed. Install with: pip install cachekit[data]")

        # If data is already unpacked (from Rust layer), use it directly
        if isinstance(data, dict):
            serialized = data
        else:
            # Otherwise unpack msgpack
            serialized = msgpack.unpackb(data, **self._msgpack_unpack_opts)

        # Reconstruct DataFrame column by column
        columns_data = {}
        for col, col_info in serialized["data"].items():
            if col_info["type"] == "numeric":
                # Reconstruct from NumPy bytes
                arr = np.frombuffer(col_info["data"], dtype=col_info["dtype"])
                columns_data[col] = arr
            else:
                # Use object data directly
                columns_data[col] = col_info["data"]

        df = pd.DataFrame(columns_data, columns=serialized["columns"])

        # Restore index if it was serialized
        if serialized["index"] is not None:
            df.index = pd.Index(serialized["index"])

        return df

    def _serialize_series(self, series: pd.Series) -> bytes:
        """Serialize Pandas Series.

        Requires: pandas installed (HAS_PANDAS=True)

        Raises:
            RuntimeError: If pandas not installed"""
        if not HAS_PANDAS:
            raise RuntimeError("Pandas not installed. Install with: pip install cachekit[data]")

        serialized = {
            "name": series.name,
            "index": series.index.tolist() if series.index.name or not series.index.equals(pd.RangeIndex(len(series))) else None,
        }

        if series.dtype.name.startswith("int") or series.dtype.name.startswith("float"):
            serialized.update({"type": "numeric", "data": series.values.tobytes(), "dtype": str(series.dtype)})  # type: ignore[union-attr]
        else:
            # Use tolist() for object types - preserves datetime objects for custom encoder
            serialized.update({"type": "object", "data": series.tolist()})

        msgpack_data = msgpack.packb(serialized, **self._msgpack_pack_opts)

        if self.enable_integrity_checking:
            return self._byte_storage.store(msgpack_data, "series")  # type: ignore[return-value]
        else:
            return msgpack_data  # type: ignore[return-value]

    def _deserialize_series(self, data) -> pd.Series:
        """Deserialize Pandas Series.

        Requires: pandas installed (HAS_PANDAS=True)

        Raises:
            RuntimeError: If pandas not installed
        """
        if not HAS_PANDAS:
            raise RuntimeError("Pandas not installed. Install with: pip install cachekit[data]")

        # If data is already unpacked (from Rust layer), use it directly
        if isinstance(data, dict):
            serialized = data
        else:
            # Otherwise unpack msgpack
            serialized = msgpack.unpackb(data, **self._msgpack_unpack_opts)

        if serialized["type"] == "numeric":
            values = np.frombuffer(serialized["data"], dtype=serialized["dtype"])
        else:
            values = serialized["data"]

        series = pd.Series(values, name=serialized["name"])

        # Restore index if it was serialized
        if serialized["index"] is not None:
            series.index = pd.Index(serialized["index"])

        return series

    def _serialize_msgpack(self, obj: Any) -> bytes:
        """Serialize general object with MessagePack."""
        msgpack_data = msgpack.packb(obj, **self._msgpack_pack_opts)

        if self.enable_integrity_checking:
            return self._byte_storage.store(msgpack_data, self.default_format)  # type: ignore[return-value]
        else:
            return msgpack_data  # type: ignore[return-value]

    def estimate_compression_ratio(self, obj: Any) -> float:
        """Estimate compression ratio for an object.

        Returns:
            float: Compression ratio (original_size / compressed_size)
        """
        if not self.enable_integrity_checking:
            return 1.0  # Python-only mode has no compression

        # Serialize without compression to get original size
        if HAS_NUMPY and isinstance(obj, np.ndarray):  # type: ignore[union-attr]
            temp_data = msgpack.packb(
                {
                    "data": obj.tobytes(),
                    "shape": obj.shape,
                    "dtype": str(obj.dtype),
                },
                **self._msgpack_pack_opts,
            )
        else:
            temp_data = msgpack.packb(obj, **self._msgpack_pack_opts)

        return self._byte_storage.estimate_compression(temp_data)

    def validate_data(self, data: bytes) -> bool:
        """Validate serialized data without deserializing.

        Args:
            data: Serialized bytes to validate

        Returns:
            bool: True if data is valid and can be deserialized
        """
        if self.enable_integrity_checking:
            return self._byte_storage.validate(data)
        else:
            # Python-only mode validation
            try:
                msgpack.unpackb(data, **self._msgpack_unpack_opts)
                return True
            except (msgpack.exceptions.UnpackException, ValueError, TypeError, AttributeError):
                # AttributeError can occur when datetime_object_hook tries to restore invalid data
                return False


# Default instance for convenience
auto_serializer = AutoSerializer()


# Convenience functions
def serialize(obj: Any) -> bytes:
    """Serialize object using auto serializer."""
    data, _metadata = auto_serializer.serialize(obj)
    return data


def deserialize(data: bytes) -> Any:
    """Deserialize data using auto serializer."""
    return auto_serializer.deserialize(data)
