"""Base serialization types and utilities.

This module contains shared types and utilities used by all serializers.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar, Protocol, runtime_checkable


@runtime_checkable
class SerializerProtocol(Protocol):
    """Protocol for pluggable serialization strategies.

    All serializers must implement this protocol to be compatible with
    cachekit's caching system. Serializers are responsible for converting
    Python objects to bytes and vice versa.

    The protocol is runtime-checkable to enable isinstance() validation
    without requiring explicit inheritance.

    Cross-SDK contract (``cross_sdk_compatible``):
        Serializers carry a class-level ``cross_sdk_compatible: bool`` attribute
        that declares whether their wire format is language-agnostic (readable by
        other-language CacheKit SDKs). It governs whether the serializer may be
        used under encryption — see ``EncryptionWrapper`` and the validation in
        ``CacheSerializationHandler.__init__``:

        - ``True``  (StandardSerializer/MessagePack, OrjsonSerializer/JSON,
          ArrowSerializer/Arrow IPC): the user's serializer is threaded into the
          EncryptionWrapper and used as-is under encryption.
        - ``False`` (AutoSerializer, which emits Python-specific type tags; and any
          serializer that does not declare the flag): single-SDK only, so combining
          it with encryption is rejected at decoration time.

        This attribute is intentionally NOT part of the runtime-checkable structural
        set: ``runtime_checkable`` would otherwise force every method-only custom
        serializer to also declare it, breaking ``isinstance(x, SerializerProtocol)``
        on the plaintext path across Python 3.10-3.14. It is enforced for type
        checkers via ``CrossSDKSerializerProtocol`` below, and read at runtime via
        ``getattr(type(s), "cross_sdk_compatible", False)`` (unmarked == single-SDK).

    Examples:
        >>> class MySerializer:
        ...     def serialize(self, obj: Any) -> tuple[bytes, SerializationMetadata]:
        ...         try:
        ...             return msgpack.packb(obj), SerializationMetadata(
        ...                 serialization_format=SerializationFormat.MSGPACK
        ...             )
        ...         except Exception as e:
        ...             raise SerializationError(f"Serialization failed: {e}") from e
        ...     def deserialize(self, data: bytes, metadata: Any = None) -> Any:
        ...         try:
        ...             return msgpack.unpackb(data)
        ...         except Exception as e:
        ...             raise SerializationError(f"Deserialization failed: {e}") from e
        >>> isinstance(MySerializer(), SerializerProtocol)
        True

        >>> # AutoSerializer implements SerializerProtocol
        >>> from cachekit.serializers import AutoSerializer
        >>> serializer = AutoSerializer()
        >>> isinstance(serializer, SerializerProtocol)
        True
        >>> data, metadata = serializer.serialize({"key": "value"})
        >>> isinstance(data, bytes)
        True
        >>> metadata.format
        <SerializationFormat.MSGPACK: 'msgpack'>
    """

    def serialize(self, obj: Any) -> tuple[bytes, SerializationMetadata]:
        """Serialize Python object to bytes.

        Args:
            obj: Python object to serialize (type support varies by implementation)

        Returns:
            Tuple of:
            - bytes: Serialized data (ready for storage or transmission)
            - SerializationMetadata: Format info, compression, encryption flags

        Raises:
            TypeError: If object type not supported by this serializer
            SerializationError: If serialization fails

        Examples:
            >>> serializer = StandardSerializer()  # doctest: +SKIP
            >>> data, metadata = serializer.serialize({"key": "value"})  # doctest: +SKIP
            >>> isinstance(data, bytes)  # doctest: +SKIP
            True
            >>> metadata.format == SerializationFormat.MSGPACK  # doctest: +SKIP
            True
        """
        ...

    def deserialize(self, data: bytes | memoryview, metadata: Any = None) -> Any:
        """Deserialize bytes to Python object.

        Args:
            data: Serialized bytes (from serialize() output)
            metadata: Optional serialization metadata for optimization hints.
                     Implementations may ignore this if format is self-describing.
                     Type is Any to avoid circular import (SerializationMetadata).

        Returns:
            Deserialized Python object (type depends on original object)

        Raises:
            SerializationError: If data is corrupted or invalid format
            TypeError: If data format doesn't match this serializer

        Examples:
            >>> serializer = StandardSerializer()  # doctest: +SKIP
            >>> data, _ = serializer.serialize({"key": "value"})  # doctest: +SKIP
            >>> obj = serializer.deserialize(data)  # doctest: +SKIP
            >>> obj == {"key": "value"}  # doctest: +SKIP
            True
        """
        ...


class CrossSDKSerializerProtocol(SerializerProtocol, Protocol):
    """SerializerProtocol plus the ``cross_sdk_compatible`` class attribute.

    Static-typing-only extension of :class:`SerializerProtocol`. It is deliberately
    NOT ``@runtime_checkable``: the marker is enforced by type checkers (so the core
    serializers and any opt-in custom serializer must declare the flag), while the
    runtime ``isinstance(x, SerializerProtocol)`` check stays method-only and never
    rejects a valid method-only custom serializer just because it omits the flag.

    At runtime the flag is read defensively via
    ``getattr(type(serializer), "cross_sdk_compatible", False)``; an unmarked
    serializer is treated as single-SDK (not safe under encryption).
    """

    cross_sdk_compatible: ClassVar[bool]


class SerializerType(str, Enum):
    """Available serialization strategies (user-facing API).

    USER-FACING: Defines which serializer **implementation** to use.
    Example: serializer=SerializerType.DEFAULT → instantiate StandardSerializer class

    This is separate from encryption (which is a security layer on top).
    Example: serializer="default", encryption=True → StandardSerializer wrapped with EncryptionWrapper

    Examples:
        >>> SerializerType.DEFAULT.value
        'default'
        >>> SerializerType.DEFAULT == "default"
        True
        >>> SerializerType.DEFAULT.name
        'DEFAULT'
    """

    DEFAULT = "default"  # MessagePack + LZ4 compression + xxHash3-64 checksums (production-ready default)
    # Future: PICKLE = "pickle", JSON = "json", etc.


class SerializationFormat(Enum):
    """Wire format of serialized data (internal metadata).

    INTERNAL: Describes what **format** the bytes are actually in (for deserialization hints).
    Example: StandardSerializer produces SerializationFormat.MSGPACK (MessagePack wire format)

    Why separate from SerializerType?
    - SerializerType = which class to instantiate (API choice)
    - SerializationFormat = what wire format was produced (metadata hint)
    - Currently 1:1 mapping, but future serializers may produce multiple formats
      (e.g., PickleSerializer could produce PICKLE_V4 vs PICKLE_V5)

    Examples:
        >>> SerializationFormat.MSGPACK.value
        'msgpack'
        >>> SerializationFormat.ORJSON.value
        'orjson'
        >>> SerializationFormat.ARROW.value
        'arrow'
        >>> SerializationFormat("msgpack") == SerializationFormat.MSGPACK
        True
    """

    MSGPACK = "msgpack"  # MessagePack wire format (produced by StandardSerializer)
    # Note: StandardSerializer = MessagePack + LZ4 compression + xxHash3-64 checksums (via ByteStorage wrapper)
    ORJSON = "orjson"  # Orjson JSON wire format (produced by OrjsonSerializer)
    ARROW = "arrow"  # Apache Arrow IPC wire format (produced by ArrowSerializer)


class SerializationMetadata:
    """Metadata about serialized data.

    Examples:
        Create basic metadata:

        >>> meta = SerializationMetadata(
        ...     serialization_format=SerializationFormat.MSGPACK,
        ...     compressed=True
        ... )
        >>> meta.format
        <SerializationFormat.MSGPACK: 'msgpack'>
        >>> meta.compressed
        True
        >>> meta.encrypted
        False

        Convert to dict and back:

        >>> meta = SerializationMetadata(
        ...     serialization_format=SerializationFormat.ORJSON,
        ...     compressed=False,
        ...     original_type="json"
        ... )
        >>> d = meta.to_dict()
        >>> d["format"]
        'orjson'
        >>> restored = SerializationMetadata.from_dict(d)
        >>> restored.format == meta.format
        True

        Encryption metadata included only when encrypted=True:

        >>> encrypted_meta = SerializationMetadata(
        ...     serialization_format=SerializationFormat.MSGPACK,
        ...     encrypted=True,
        ...     tenant_id="acme-corp",
        ...     encryption_algorithm="AES-256-GCM"
        ... )
        >>> "tenant_id" in encrypted_meta.to_dict()
        True
    """

    def __init__(
        self,
        serialization_format: SerializationFormat,
        encoding: str = "utf-8",
        compressed: bool = False,
        original_type: str | None = None,
        encrypted: bool = False,
        tenant_id: str | None = None,
        encryption_algorithm: str | None = None,
        key_fingerprint: str | None = None,
    ):
        self.format = serialization_format
        self.encoding = encoding
        self.compressed = compressed
        self.original_type = original_type
        self.encrypted = encrypted
        self.tenant_id = tenant_id
        self.encryption_algorithm = encryption_algorithm
        self.key_fingerprint = key_fingerprint

    def to_dict(self) -> dict[str, Any]:
        """Convert metadata to dictionary for storage."""
        data = {
            "format": self.format.value,
            "encoding": self.encoding,
            "compressed": self.compressed,
            "original_type": self.original_type,
        }

        # Add encryption fields if present
        if self.encrypted:
            data.update(
                {
                    "encrypted": self.encrypted,
                    "tenant_id": self.tenant_id,
                    "encryption_algorithm": self.encryption_algorithm,
                    "key_fingerprint": self.key_fingerprint,
                }
            )

        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SerializationMetadata:
        """Create metadata from dictionary."""
        return cls(
            serialization_format=SerializationFormat(data["format"]),
            encoding=data.get("encoding", "utf-8"),
            compressed=data.get("compressed", False),
            original_type=data.get("original_type"),
            encrypted=data.get("encrypted", False),
            tenant_id=data.get("tenant_id"),
            encryption_algorithm=data.get("encryption_algorithm"),
            key_fingerprint=data.get("key_fingerprint"),
        )


class SerializationError(Exception):
    """Exception raised when serialization/deserialization fails.

    Examples:
        >>> try:
        ...     raise SerializationError("Invalid data format")
        ... except SerializationError as e:
        ...     str(e)
        'Invalid data format'

        >>> isinstance(SerializationError("test"), Exception)
        True
    """

    pass


class SuspiciousCacheEntryError(SerializationError):
    """The unauthenticated envelope of a cache entry is inconsistent with the
    handler's configuration in a way tampering would also produce.

    Raised for the CWE-757 downgrade guard (an encryption-enabled handler read
    an entry whose header claims plaintext) and for an encrypted entry missing
    its tenant_id. Both have benign explanations (lazy plaintext→encrypted
    migration, corrupt header), so callers keep treating this as a miss
    (evict → recompute → re-store) even in fail-closed mode — but telemetry
    counts it under its own ``suspicious_envelope`` reason label so a spike
    outside a migration window is visible to operators.
    """

    pass
