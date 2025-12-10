"""OrjsonSerializer - High-performance JSON serialization with orjson.

This module provides fast JSON serialization using the orjson library (Rust-powered).
Optimized for JSON-friendly data (dicts, lists, strings, numbers, booleans, None).

Integrity Protection:
- xxHash3-64 checksums protect against silent data corruption
- Checksum is computed on original data before storage
- Validation occurs during deserialization (detects bit flips, truncation, corruption)
- 8-byte overhead per cached item (faster than cryptographic hashes)
"""

from __future__ import annotations

from typing import Any

import orjson
import xxhash

from .base import SerializationError, SerializationFormat, SerializationMetadata


class OrjsonSerializer:
    """High-performance JSON serialization using orjson with xxHash3-64 integrity protection.

    Implements SerializerProtocol via structural subtyping (PEP 544).
    No inheritance required - protocol compliance validated at runtime.

    Features:
    - 2-3x faster than stdlib json (Rust-powered)
    - Compact output (no whitespace)
    - Automatic datetime/UUID serialization
    - Streaming support for large objects
    - Predictable error messages for unsupported types
    - xxHash3-64 checksums for corruption detection

    Integrity Protection:
    - Format: [8-byte xxHash3-64 checksum][serialized data]
    - Checksum computed on original JSON bytes
    - Validation on deserialize detects bit flips, truncation, corruption
    - 8-byte overhead per cached item (faster than cryptographic hashes)

    Use Cases:
    - REST API response caching (JSON-native data)
    - Configuration caching (dict/list structures)
    - Simple data structures (no NumPy/DataFrames)
    - Production caching requiring integrity guarantees

    Limitations:
    - JSON-only: No binary data, custom objects, or complex types
    - No DataFrame optimization (use ArrowSerializer)
    - No NumPy optimization (use DefaultSerializer)

    Protocol Compliance:
        serialize(obj) -> tuple[bytes, SerializationMetadata]
        deserialize(data, metadata=None) -> Any

    Examples:
        >>> serializer = OrjsonSerializer()
        >>> data, meta = serializer.serialize({"key": "value"})
        >>> isinstance(data, bytes)
        True
        >>> meta.format
        <SerializationFormat.ORJSON: 'orjson'>
        >>> result = serializer.deserialize(data)
        >>> result == {"key": "value"}
        True

        >>> # Corruption detection
        >>> data, _ = serializer.serialize({"key": "value"})
        >>> corrupted = data[:12] + b'X' + data[13:]  # Corrupt one byte
        >>> serializer.deserialize(corrupted)  # doctest: +SKIP
        Traceback (most recent call last):
        SerializationError: Checksum validation failed - data corruption detected
    """

    def __init__(self, option: int = orjson.OPT_SORT_KEYS, enable_integrity_checking: bool = True):
        """Initialize OrjsonSerializer.

        Args:
            option: orjson serialization options (bitwise OR of orjson.OPT_* flags)
                Default: OPT_SORT_KEYS for deterministic output
                Common options:
                - OPT_SORT_KEYS: Sort dictionary keys (reproducible output)
                - OPT_INDENT_2: Pretty-print with 2-space indentation (debugging)
                - OPT_NAIVE_UTC: Treat naive datetime as UTC
                - Combine with bitwise OR: OPT_SORT_KEYS | OPT_NAIVE_UTC
            enable_integrity_checking: Enable xxHash3-64 checksum validation (default: True)
                When True: 8-byte checksum overhead + validation cost (integrity guarantee)
                When False: No checksum (faster, use for @cache.minimal speed-first scenarios)

        Examples:
            >>> serializer = OrjsonSerializer()  # Default: integrity ON
            >>> serializer_fast = OrjsonSerializer(enable_integrity_checking=False)  # Speed-first
            >>> serializer_multi = OrjsonSerializer(option=orjson.OPT_SORT_KEYS | orjson.OPT_NAIVE_UTC)
        """
        self.option = option
        self.enable_integrity_checking = enable_integrity_checking

    def serialize(self, obj: Any) -> tuple[bytes, SerializationMetadata]:
        """Serialize object to JSON bytes with optional xxHash3-64 integrity protection.

        Args:
            obj: Python object to serialize (must be JSON-serializable)

        Returns:
            Tuple of (serialized bytes, metadata)
            Format (integrity ON): [8-byte xxHash3-64 checksum][JSON bytes]
            Format (integrity OFF): [JSON bytes]

        Raises:
            TypeError: If object type is not JSON-serializable (bytes, custom objects, etc.)
            SerializationError: If serialization fails (data encoding error)

        Examples:
            >>> serializer = OrjsonSerializer()
            >>> data, meta = serializer.serialize({"test": 123})
            >>> isinstance(data, bytes)
            True
            >>> len(data) > 8  # Has 8-byte checksum prefix
            True
            >>> meta.format.value
            'orjson'
        """
        try:
            # Serialize to JSON bytes
            json_data = orjson.dumps(obj, option=self.option)

            # Conditionally add integrity protection
            if self.enable_integrity_checking:
                # Compute xxHash3-64 checksum of original data (8 bytes)
                checksum = xxhash.xxh3_64_digest(json_data)
                # Envelope format: [checksum][data]
                envelope = checksum + json_data
            else:
                # No integrity checking - return raw JSON data
                envelope = json_data

            metadata = SerializationMetadata(
                serialization_format=SerializationFormat.ORJSON,
                compressed=False,  # No compression (handled by Rust layer if enabled)
                encrypted=False,  # Encryption is EncryptionWrapper's responsibility
                original_type="orjson",
            )
            return envelope, metadata
        except TypeError as e:
            # TypeError = unsupported type (bytes, custom objects, etc.)
            raise TypeError(
                f"Object of type {type(obj).__name__} is not JSON-serializable. "
                f"OrjsonSerializer supports: dict, list, str, int, float, bool, None, datetime, UUID"
            ) from e
        except ValueError as e:
            # ValueError = data encoding error
            raise SerializationError(f"Failed to serialize object to JSON: {e}") from e

    def deserialize(self, data: bytes, metadata: SerializationMetadata | None = None) -> Any:
        """Deserialize JSON bytes with optional xxHash3-64 integrity validation.

        Args:
            data: Bytes from serialize() (with or without checksum envelope)
            metadata: Optional metadata (ignored - JSON is self-describing)

        Returns:
            Deserialized Python object

        Raises:
            SerializationError: If data is malformed, not valid JSON, or checksum validation fails

        Examples:
            >>> serializer = OrjsonSerializer()
            >>> data, _ = serializer.serialize({"test": 123})
            >>> result = serializer.deserialize(data)
            >>> result == {"test": 123}
            True
        """
        try:
            if self.enable_integrity_checking:
                # Guard clause: Minimum size check (8 bytes checksum + at least 2 bytes JSON: {})
                if len(data) < 10:
                    raise SerializationError(
                        f"Invalid data: Expected at least 10 bytes (8-byte checksum + 2-byte JSON), got {len(data)} bytes"
                    )

                # Extract checksum and JSON data
                expected_checksum = data[:8]
                json_data = data[8:]

                # Validate checksum
                computed_checksum = xxhash.xxh3_64_digest(json_data)
                if computed_checksum != expected_checksum:
                    raise SerializationError("Checksum validation failed - data corruption detected")

                # Deserialize JSON
                return orjson.loads(json_data)
            else:
                # No integrity checking - deserialize directly
                # This handles both: data written with integrity=False AND backward compatible reads
                # If data has checksum but we're not validating, just try to parse as JSON
                return orjson.loads(data)
        except orjson.JSONDecodeError as e:
            raise SerializationError(f"Failed to deserialize JSON data: {e}") from e
        except ValueError as e:
            raise SerializationError(f"Failed to deserialize JSON data: {e}") from e
