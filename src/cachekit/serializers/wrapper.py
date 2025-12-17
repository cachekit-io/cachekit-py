"""Standard serialization wrapper for cache storage.

This module provides utilities for wrapping and unwrapping data with metadata
for consistent serialization across all cache backends.
"""

import base64
import json
from typing import Union


class SerializationWrapper:
    """Standard wrapper/unwrapper for cache serialization data.

    Wraps serialized bytes with JSON envelope containing metadata for cache storage.
    The envelope format enables introspection of cached data without deserialization.

    This wrapper is backend-agnostic and works with any cache backend (Redis,
    CachekitIO, Memcached, etc.).

    Examples:
        Wrap and unwrap data:

        >>> data = b"serialized_bytes"
        >>> metadata = {"format": "msgpack", "compressed": True}
        >>> wrapped = SerializationWrapper.wrap(data, metadata, "auto")
        >>> isinstance(wrapped, bytes)
        True

        Unwrap returns original data, metadata, and serializer name:

        >>> unwrapped_data, unwrapped_meta, serializer = SerializationWrapper.unwrap(wrapped)
        >>> unwrapped_data == data
        True
        >>> unwrapped_meta["format"]
        'msgpack'
        >>> serializer
        'auto'

        Works with string input (from cache backend):

        >>> wrapped_str = wrapped.decode("utf-8")
        >>> unwrapped_data, _, _ = SerializationWrapper.unwrap(wrapped_str)
        >>> unwrapped_data == data
        True
    """

    @staticmethod
    def wrap(data: bytes, metadata: dict, serializer_name: str, version: str = "2.0") -> bytes:
        """Wrap serialized data with metadata envelope for cache storage.

        Args:
            data: Serialized bytes to wrap
            metadata: Serialization metadata dict (must include "format" key)
            serializer_name: Name of serializer used (e.g., "default", "auto")
            version: Envelope format version

        Returns:
            JSON-encoded bytes containing base64 data and metadata
        """
        wrapper = {
            "data": base64.b64encode(data).decode("ascii"),
            "metadata": metadata,
            "serializer": serializer_name,
            "version": version,
        }
        return json.dumps(wrapper, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def unwrap(wrapped_data: Union[str, bytes]) -> tuple[bytes, dict, str]:
        """Unwrap data envelope from cache storage.

        Args:
            wrapped_data: JSON envelope (bytes or string) from cache backend

        Returns:
            tuple: (data_bytes, metadata_dict, serializer_name)
        """
        if isinstance(wrapped_data, bytes):
            wrapped_data = wrapped_data.decode("utf-8")

        wrapper = json.loads(wrapped_data)
        data = base64.b64decode(wrapper["data"].encode("ascii"))
        metadata = wrapper.get("metadata", {})
        serializer_name = wrapper.get("serializer", "unknown")
        return data, metadata, serializer_name


__all__ = ["SerializationWrapper"]
