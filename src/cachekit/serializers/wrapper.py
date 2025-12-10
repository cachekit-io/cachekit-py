"""Standard serialization wrapper for Redis storage.

This module provides utilities for wrapping and unwrapping data with metadata
for consistent Redis serialization across the codebase.
"""

import base64
import json
from typing import Union


class SerializationWrapper:
    """Standard wrapper/unwrapper for Redis serialization data.

    Wraps serialized bytes with JSON envelope containing metadata for Redis storage.
    The envelope format enables introspection of cached data without deserialization.

    Examples:
        Wrap and unwrap data:

        >>> data = b"serialized_bytes"
        >>> metadata = {"format": "msgpack", "compressed": True}
        >>> wrapped = SerializationWrapper.wrap_for_redis(data, metadata, "auto")
        >>> isinstance(wrapped, bytes)
        True

        Unwrap returns original data, metadata, and serializer name:

        >>> unwrapped_data, unwrapped_meta, serializer = SerializationWrapper.unwrap_from_redis(wrapped)
        >>> unwrapped_data == data
        True
        >>> unwrapped_meta["format"]
        'msgpack'
        >>> serializer
        'auto'

        Works with string input (from Redis):

        >>> wrapped_str = wrapped.decode("utf-8")
        >>> unwrapped_data, _, _ = SerializationWrapper.unwrap_from_redis(wrapped_str)
        >>> unwrapped_data == data
        True
    """

    @staticmethod
    def wrap_for_redis(data: bytes, metadata: dict, serializer_name: str, version: str = "2.0") -> bytes:
        """Standard wrapper for Redis storage."""
        wrapper = {
            "data": base64.b64encode(data).decode("ascii"),
            "metadata": metadata,
            "serializer": serializer_name,
            "version": version,
        }
        return json.dumps(wrapper, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def unwrap_from_redis(redis_data: Union[str, bytes]) -> tuple[bytes, dict, str]:
        """Standard unwrapper for Redis data.

        Returns:
            tuple: (data_bytes, metadata_dict, serializer_name)
        """
        if isinstance(redis_data, bytes):
            redis_data = redis_data.decode("utf-8")

        wrapper = json.loads(redis_data)
        data = base64.b64decode(wrapper["data"].encode("ascii"))
        metadata = wrapper.get("metadata", {})
        serializer_name = wrapper.get("serializer", "unknown")
        return data, metadata, serializer_name


__all__ = ["SerializationWrapper"]
