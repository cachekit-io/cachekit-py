"""Interop-mode value serializer (interop/v1, spec/interop-mode.md).

Emits one canonical plain-MessagePack document per value — no ByteStorage
envelope, no LZ4, no xxHash3-64 checksum, never the CK v3 frame. Any language
with a MessagePack library can read the bytes; corruption/tamper protection
comes from AES-GCM when the entry is encrypted (EncryptionWrapper composes
over this serializer exactly like any other).

Metadata is fixed by the spec: format=msgpack, compressed=False and NO
original_type — which makes EncryptionWrapper build the interop AAD with
exactly four components (v0x03: tenant_id, cache_key, "msgpack", "False").
"""

from __future__ import annotations

from typing import Any, ClassVar

from ..interop import decode_interop_value, encode_interop_value
from .base import SerializationError, SerializationFormat, SerializationMetadata


class InteropSerializer:
    """SerializerProtocol implementation for interop-mode values.

    Examples:
        Round-trip a cross-SDK-readable value:

        >>> s = InteropSerializer()
        >>> data, meta = s.serialize({"name": "alice", "age": 30})
        >>> data.hex()  # canonical: sorted keys, shortest forms
        '82a36167651ea46e616d65a5616c696365'
        >>> meta.compressed
        False
        >>> meta.original_type is None
        True
        >>> s.deserialize(data)
        {'age': 30, 'name': 'alice'}
    """

    # Plain MessagePack is the cross-SDK format by definition; this flag lets
    # CacheSerializationHandler's encryption validation accept the serializer.
    cross_sdk_compatible: ClassVar[bool] = True

    def serialize(self, obj: Any) -> tuple[bytes, SerializationMetadata]:
        try:
            data = encode_interop_value(obj)
        except ValueError:
            # InteropError (a ValueError): spec-mandated model rejection —
            # propagate untouched so it fails loud at the caller.
            raise
        except Exception as e:
            raise SerializationError(f"interop value serialization failed: {e}") from e
        metadata = SerializationMetadata(
            serialization_format=SerializationFormat.MSGPACK,
            compressed=False,
            original_type=None,
        )
        return data, metadata

    def deserialize(self, data: bytes | memoryview, metadata: Any = None) -> Any:
        return decode_interop_value(data)


__all__ = ["InteropSerializer"]
