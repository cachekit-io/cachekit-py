"""Cache key generation functionality."""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any, Callable, NoReturn, cast
from uuid import UUID

import msgpack

if TYPE_CHECKING:
    pass

# Constants for constrained array support (per round-table review 2025-12-18)
ARRAY_MAX_BYTES = 100_000  # 100KB per array
ARRAY_AGGREGATE_MAX = 5_000_000  # 5MB total across all args
SUPPORTED_ARRAY_DTYPES = {"int32", "int64", "float32", "float64"}
DTYPE_MAP = {"int32": "i32", "int64": "i64", "float32": "f32", "float64": "f64"}


class CacheKeyGenerator:
    """Generates consistent cache keys from function calls.

    Uses MessagePack + Blake2b-256 for cross-language compatibility.
    Implements protocol-v1.0.md Section 3.3 (MessagePack-based approach).
    """

    # Key length constants
    MAX_KEY_LENGTH = 250  # Practical cache key length limit (Redis, Memcached, etc.)
    KEY_PREFIX_LENGTH = 50  # Length of prefix to keep when shortening keys

    # Serializer codes for compact metadata encoding (1 char each)
    SERIALIZER_CODES = {
        "std": "s",  # StandardSerializer (multi-language MessagePack)
        "auto": "a",  # AutoSerializer (Python-specific, NumPy/pandas)
        "orjson": "o",  # OrjsonSerializer (JSON-based)
        "arrow": "w",  # ArrowSerializer (columnar format, w=arroW)
    }

    def __init__(self):
        """Initialize the key generator.

        Uses MessagePack + Blake2b-256 per protocol-v1.0.md Section 3.3.
        """
        pass

    def generate_key(
        self,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        namespace: str | None = None,
        integrity_checking: bool = True,
        serializer_type: str = "std",
    ) -> str:
        """Generate a cache key from function and arguments.

        Args:
            func: The function being cached
            args: Positional arguments passed to the function
            kwargs: Keyword arguments passed to the function
            namespace: Optional namespace prefix for the key
            integrity_checking: Whether integrity checking is enabled (ByteStorage vs plain MessagePack)
            serializer_type: Serializer type code ("std", "auto", "orjson", "arrow")

        Returns:
            A consistent string key for caching

        Note:
            Uses compact metadata suffix format: :<ic><serializer_code>
            Example: ":1s" = integrity_checking=True, serializer=StandardSerializer
        """
        # Build key components efficiently (avoid f-strings in hot path)
        key_parts = []

        # Add namespace if provided
        if namespace:
            key_parts.extend(["ns:", namespace, ":"])

        # Add function identifier (module + name) - single string operation
        key_parts.extend(["func:", func.__module__, ".", func.__qualname__, ":"])

        # Generate args hash using Blake2b-256
        args_hash = self._blake2b_hash(args, kwargs)

        key_parts.extend(["args:", args_hash, ":"])

        # Add compact metadata suffix: :<ic><serializer_code>
        # Example: ":1s" = integrity_checking=True, serializer=std
        ic_flag = "1" if integrity_checking else "0"
        serializer_code = self.SERIALIZER_CODES.get(serializer_type, "s")  # Default to "s" if unknown
        key_parts.extend([ic_flag, serializer_code])

        # Single join operation reduces string allocations
        key = "".join(key_parts)

        # Ensure key is within practical limits and contains no problematic characters
        return self._normalize_key(key)

    def _blake2b_hash(self, args: tuple, kwargs: dict) -> str:
        """Generate hash using MessagePack + Blake2b-256.

        Blake2b-256 (32 bytes = 64 hex chars) for collision resistance.
        MessagePack ensures cross-language compatibility.

        Raises:
            TypeError: If args/kwargs contain unsupported types (custom objects, numpy arrays, etc.)
        """
        # Track aggregate array bytes for DoS prevention
        array_bytes_seen: list[int] = [0]

        # Step 1: Normalize recursively
        normalized_args = [self._normalize(arg, array_bytes_seen) for arg in args]
        normalized_kwargs = {k: self._normalize(v, array_bytes_seen) for k, v in sorted(kwargs.items())}

        # Step 2: Serialize with MessagePack
        try:
            msgpack_bytes = cast(
                bytes, msgpack.packb([normalized_args, normalized_kwargs], use_bin_type=True, strict_types=True)
            )
        except TypeError as e:
            # Wrap msgpack's TypeError with a more descriptive message
            raise TypeError(f"Unsupported type for cache key generation: {e}") from e

        # Step 3: Hash with Blake2b-256
        return hashlib.blake2b(msgpack_bytes, digest_size=32).hexdigest()

    def _normalize(self, obj: Any, _array_bytes_seen: list[int] | None = None) -> Any:
        """Normalize object for deterministic MessagePack encoding.

        CRITICAL: Cross-language compatible types ONLY per Protocol v1.1.

        Supported types (per round-table review 2025-12-18):
        - Primitives: int, str, bytes, bool, None, float
        - Collections: dict (sorted keys), list, tuple
        - Extended: Path, UUID, Decimal, Enum, datetime (UTC only)
        - Arrays: numpy.ndarray (1D, ≤100KB, i32/i64/f32/f64)

        Args:
            obj: Object to normalize
            _array_bytes_seen: Internal tracker for aggregate array size (DoS prevention)

        Returns:
            Normalized object safe for MessagePack serialization

        Raises:
            TypeError: For unsupported types with helpful guidance
        """
        # Initialize aggregate tracker if not provided
        if _array_bytes_seen is None:
            _array_bytes_seen = [0]

        # === COLLECTIONS (recursive) ===
        if isinstance(obj, dict):
            return {k: self._normalize(v, _array_bytes_seen) for k, v in sorted(obj.items())}

        if isinstance(obj, (list, tuple)):
            return [self._normalize(x, _array_bytes_seen) for x in obj]

        # === FLOAT (cross-language compat) ===
        if isinstance(obj, float):
            # CRITICAL: Normalize -0.0 → 0.0 for cross-language compatibility
            return 0.0 if obj == 0.0 else obj

        # === EXTENDED TYPES ===

        # Path: normalize to POSIX format for cross-platform consistency
        if isinstance(obj, (Path, PurePath)):
            return obj.as_posix()

        # UUID: standard string format
        if isinstance(obj, UUID):
            return str(obj)

        # Decimal: exact string representation
        if isinstance(obj, Decimal):
            return str(obj)

        # Enum: use value (recursively normalize in case value is complex)
        if isinstance(obj, Enum):
            return self._normalize(obj.value, _array_bytes_seen)

        # datetime: UTC only, reject naive datetimes
        if isinstance(obj, datetime):
            if obj.tzinfo is None:
                raise TypeError(
                    "Naive datetime not allowed in cache keys (timezone ambiguity). "
                    "Use timezone-aware datetime: datetime(..., tzinfo=timezone.utc)"
                )
            return obj.isoformat()

        # === NUMPY ARRAY (constrained support) ===
        if self._is_numpy_array(obj):
            return self._normalize_array(obj, _array_bytes_seen)

        # === PRIMITIVES (pass through) ===
        if isinstance(obj, (int, str, bytes, bool, type(None))):
            return obj

        # === UNSUPPORTED: Fail fast with helpful message ===
        return self._raise_unsupported_type(obj)

    def _is_numpy_array(self, obj: Any) -> bool:
        """Check if object is numpy array without importing numpy."""
        return type(obj).__module__ == "numpy" and type(obj).__name__ == "ndarray"

    def _normalize_array(self, arr: Any, _array_bytes_seen: list[int]) -> list[Any]:
        """Normalize numpy array with strict constraints.

        Constraints (per round-table review 2025-12-18):
        - 1D only (cross-language simplicity)
        - ≤100KB (memory safety)
        - 4 dtypes: i32, i64, f32, f64 (cross-language compatibility)
        - Little-endian byte order (platform determinism)
        - 256-bit Blake2b hash (collision resistance)
        - Version prefix for future protocol changes

        Args:
            arr: numpy.ndarray to normalize
            _array_bytes_seen: Aggregate byte counter for DoS prevention

        Returns:
            List of ["__array_v1__", shape_list, dtype_str, content_hash]
            (list format for MessagePack compatibility with strict_types=True)

        Raises:
            TypeError: If array doesn't meet constraints
        """
        import numpy as np

        # Constraint 1: Size limit per array
        if arr.nbytes > ARRAY_MAX_BYTES:
            raise TypeError(
                f"Array too large ({arr.nbytes:,} bytes, max {ARRAY_MAX_BYTES:,}). Use key= parameter for large arrays."
            )

        # Constraint 2: Aggregate size limit (DoS prevention)
        _array_bytes_seen[0] += arr.nbytes
        if _array_bytes_seen[0] > ARRAY_AGGREGATE_MAX:
            raise TypeError(
                f"Total array size exceeds {ARRAY_AGGREGATE_MAX:,} bytes. Use key= parameter for batch array operations."
            )

        # Constraint 3: 1D only
        if arr.ndim != 1:
            raise TypeError(
                f"Only 1D arrays supported in cache keys (got {arr.ndim}D). "
                f"Use key= parameter for multidimensional arrays, or flatten with arr.ravel()."
            )

        # Constraint 4: Supported dtypes only
        dtype_name = arr.dtype.name
        if dtype_name not in SUPPORTED_ARRAY_DTYPES:
            raise TypeError(
                f"Unsupported array dtype '{dtype_name}'. "
                f"Supported: {', '.join(sorted(SUPPORTED_ARRAY_DTYPES))}. "
                f"Cast with arr.astype(np.float64) or use key= parameter."
            )

        # Ensure C-contiguous memory layout
        arr = np.ascontiguousarray(arr)

        # Force little-endian byte order for cross-platform determinism
        if arr.dtype.byteorder not in ("=", "<", "|"):
            arr = arr.astype(arr.dtype.newbyteorder("<"))
        elif arr.dtype.byteorder == "=" and sys.byteorder == "big":
            arr = arr.byteswap().newbyteorder("<")

        # 256-bit Blake2b hash (per security review)
        content_hash = hashlib.blake2b(arr.tobytes(), digest_size=32).hexdigest()

        # Standardized dtype string for cross-language compatibility
        dtype_str = DTYPE_MAP[dtype_name]

        # Version prefix for protocol evolution
        # Return as list (not tuple) for MessagePack compatibility with strict_types=True
        # Shape converted to list as well
        return ["__array_v1__", list(arr.shape), dtype_str, content_hash]

    def _raise_unsupported_type(self, obj: Any) -> NoReturn:
        """Raise helpful TypeError for unsupported types.

        Args:
            obj: The unsupported object

        Raises:
            TypeError: Always, with guidance on how to handle the type
        """
        type_name = type(obj).__module__ + "." + type(obj).__qualname__

        # Specific guidance for numpy arrays that don't meet constraints
        if "numpy" in type_name and "ndarray" in type_name:
            raise TypeError(
                "numpy array doesn't meet cache key constraints. "
                "Requirements: 1D, ≤100KB, dtype in (i32, i64, f32, f64). "
                "Use key= parameter for other arrays."
            )

        if "pandas" in type_name:
            raise TypeError(
                "pandas objects not supported as cache key arguments "
                "(Parquet serialization is non-deterministic). "
                "Recommended patterns:\n"
                "  1. Pass identifier, return DataFrame: @cache def load(id: int) -> pd.DataFrame\n"
                "  2. Use explicit key: @cache(key=lambda df: hashlib.blake2b(df.to_parquet()).hexdigest())"
            )

        if isinstance(obj, (set, frozenset)):
            raise TypeError(
                "set/frozenset not supported in cache keys (mixed-type sorting crashes). "
                "Convert to sorted list: sorted(list(your_set))"
            )

        raise TypeError(
            f"Unsupported type '{type_name}' for cache key. "
            f"Supported: dict, list, tuple, int, float, str, bytes, bool, None, "
            f"Path, UUID, Decimal, Enum, datetime (UTC), 1D numpy arrays (≤100KB, i32/i64/f32/f64). "
            f"For custom types, use key= parameter."
        )

    def _normalize_key(self, key: str) -> str:
        """Normalize key to ensure it's valid for cache backends.

        Args:
            key: Raw cache key

        Returns:
            Normalized key safe for cache backends (Redis, Memcached, etc.)
        """
        # Replace problematic characters
        normalized = key.replace(" ", "_").replace("\n", "_").replace("\r", "_")

        # Ensure key length is within practical limits for cache backends
        if len(normalized) > self.MAX_KEY_LENGTH:
            # If too long, hash the key to get consistent shorter version
            # Use Blake2b-256 (32 bytes) for consistency
            key_hash = hashlib.blake2b(normalized.encode("utf-8"), digest_size=32).hexdigest()

            # Keep first part of original key for readability + hash
            prefix = normalized[: self.KEY_PREFIX_LENGTH] if len(normalized) > self.KEY_PREFIX_LENGTH else normalized
            normalized = f"{prefix}:{key_hash[:32]}"

        return normalized
