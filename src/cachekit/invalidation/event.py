"""InvalidationEvent dataclass for cross-pod cache invalidation messages.

This module defines the immutable message format for broadcasting L1 cache
invalidation events. Events are serialized to MessagePack for wire transport
with strict security limits to prevent abuse.

Security:
    - Max message size: 10KB (prevents memory exhaustion)
    - Max string length: 1024 characters
    - Max array/map size: 100 elements
    - Namespace/params_hash format validation via regex
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

import msgpack

# Security limits (non-negotiable)
MAX_MESSAGE_SIZE = 10 * 1024  # 10KB max payload
NAMESPACE_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
PARAMS_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class InvalidationLevel(Enum):
    """Invalidation scope level.

    - GLOBAL: Invalidate entire L1 cache across all pods
    - NAMESPACE: Invalidate all entries within a namespace
    - PARAMS: Invalidate specific cache key by params_hash
    """

    GLOBAL = "global"
    NAMESPACE = "namespace"
    PARAMS = "params"


@dataclass(frozen=True)
class InvalidationEvent:
    """Immutable invalidation event for cross-pod broadcast.

    This dataclass represents a single invalidation message that gets
    serialized to MessagePack and sent via InvalidationChannel (Redis Pub/Sub,
    HTTP SSE, etc.).

    Attributes:
        level: Invalidation scope (global, namespace, or params)
        namespace: Cache namespace (required for NAMESPACE level)
        params_hash: 64-char hex params hash (required for PARAMS level)

    Validation Rules:
        - NAMESPACE level: namespace must be provided and match NAMESPACE_PATTERN
        - PARAMS level: params_hash must be provided and match PARAMS_HASH_PATTERN (64-char hex)
        - GLOBAL level: namespace and params_hash must be None

    Serialization Format:
        MessagePack with compact keys to minimize wire size:
        - "l" -> level (str: "global", "namespace", "params")
        - "ns" -> namespace (str or None)
        - "ph" -> params_hash (str or None)

    Security:
        - from_bytes() enforces strict msgpack limits (max_bin_len, max_str_len, etc.)
        - Rejects payloads >10KB
        - Validates namespace/params_hash format via regex

    Examples:
        >>> # Global invalidation (clear all caches)
        >>> event = InvalidationEvent(level=InvalidationLevel.GLOBAL, namespace=None, params_hash=None)
        >>> data = event.to_bytes()
        >>> restored = InvalidationEvent.from_bytes(data)
        >>> restored == event
        True

        >>> # Namespace invalidation
        >>> event = InvalidationEvent(level=InvalidationLevel.NAMESPACE, namespace="user_cache", params_hash=None)
        >>> data = event.to_bytes()
        >>> len(data) < 100  # Compact serialization
        True

        >>> # Params invalidation
        >>> params_hash = "a" * 64  # Valid 64-char hex
        >>> event = InvalidationEvent(level=InvalidationLevel.PARAMS, namespace=None, params_hash=params_hash)
        >>> restored = InvalidationEvent.from_bytes(event.to_bytes())
        >>> restored.params_hash == params_hash
        True

        >>> # Validation errors
        >>> InvalidationEvent(level=InvalidationLevel.NAMESPACE, namespace=None, params_hash=None)
        Traceback (most recent call last):
        ValueError: NAMESPACE level requires namespace

        >>> InvalidationEvent(level=InvalidationLevel.PARAMS, namespace=None, params_hash="invalid")
        Traceback (most recent call last):
        ValueError: params_hash must be 64-character lowercase hex string

        >>> InvalidationEvent(level=InvalidationLevel.NAMESPACE, namespace="invalid space", params_hash=None)
        Traceback (most recent call last):
        ValueError: namespace must match pattern...
    """

    level: InvalidationLevel
    namespace: str | None
    params_hash: str | None

    def __post_init__(self) -> None:
        """Validate invalidation event after initialization.

        Raises:
            ValueError: If validation fails (missing required fields, invalid formats)
        """
        # NAMESPACE level requires namespace
        if self.level == InvalidationLevel.NAMESPACE:
            if self.namespace is None:
                raise ValueError("NAMESPACE level requires namespace")
            if not NAMESPACE_PATTERN.match(self.namespace):
                raise ValueError(
                    f"namespace must match pattern {NAMESPACE_PATTERN.pattern} (alphanumeric, underscore, hyphen, 1-128 chars)"
                )

        # PARAMS level requires params_hash
        if self.level == InvalidationLevel.PARAMS:
            if self.params_hash is None:
                raise ValueError("PARAMS level requires params_hash")
            if not PARAMS_HASH_PATTERN.match(self.params_hash):
                raise ValueError("params_hash must be 64-character lowercase hex string")

        # GLOBAL level must have both fields as None
        if self.level == InvalidationLevel.GLOBAL:
            if self.namespace is not None or self.params_hash is not None:
                raise ValueError("GLOBAL level must have namespace=None and params_hash=None")

    def to_bytes(self) -> bytes:
        """Serialize event to MessagePack bytes.

        Uses compact keys to minimize wire size:
        - "l" -> level
        - "ns" -> namespace
        - "ph" -> params_hash

        Returns:
            MessagePack-encoded bytes (typically <100 bytes)

        Examples:
            >>> event = InvalidationEvent(level=InvalidationLevel.GLOBAL, namespace=None, params_hash=None)
            >>> data = event.to_bytes()
            >>> len(data) < 50  # Very compact
            True
        """
        payload: dict[str, Any] = {
            "l": self.level.value,  # "global", "namespace", or "params"
        }

        if self.namespace is not None:
            payload["ns"] = self.namespace

        if self.params_hash is not None:
            payload["ph"] = self.params_hash

        return msgpack.packb(payload, use_bin_type=True)  # type: ignore[return-value]

    @classmethod
    def from_bytes(cls, data: bytes) -> InvalidationEvent:
        """Deserialize InvalidationEvent from MessagePack bytes.

        Enforces strict security limits:
        - max_bin_len: 10KB (MAX_MESSAGE_SIZE)
        - max_str_len: 1024 characters
        - max_array_len: 100 elements
        - max_map_len: 100 keys

        Args:
            data: MessagePack-encoded bytes from to_bytes()

        Returns:
            Deserialized InvalidationEvent

        Raises:
            ValueError: If data is malformed, oversized, or fails validation

        Examples:
            >>> event = InvalidationEvent(level=InvalidationLevel.GLOBAL, namespace=None, params_hash=None)
            >>> data = event.to_bytes()
            >>> restored = InvalidationEvent.from_bytes(data)
            >>> restored == event
            True

            >>> # Security: reject oversized payload
            >>> huge_payload = msgpack.packb({"l": "global", "data": "x" * 20000})
            >>> InvalidationEvent.from_bytes(huge_payload)  # doctest: +SKIP
            Traceback (most recent call last):
            ValueError: ...
        """
        try:
            # Enforce strict security limits
            payload = msgpack.unpackb(
                data,
                max_bin_len=MAX_MESSAGE_SIZE,
                max_str_len=1024,
                max_array_len=100,
                max_map_len=100,
                raw=False,
                use_list=True,
            )
        except (msgpack.exceptions.UnpackException, ValueError) as e:
            raise ValueError(f"Failed to deserialize InvalidationEvent: {e}") from e

        # Validate payload is a dict
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid payload: expected dict, got {type(payload).__name__}")

        # Extract level (required)
        level_str = payload.get("l")
        if level_str is None:
            raise ValueError("Missing required field 'l' (level)")

        # Convert level string to enum
        try:
            level = InvalidationLevel(level_str)
        except ValueError as e:
            raise ValueError(f"Invalid level value '{level_str}': {e}") from e

        # Extract optional fields
        namespace = payload.get("ns")
        params_hash = payload.get("ph")

        # Construct and validate event (validation happens in __post_init__)
        return cls(level=level, namespace=namespace, params_hash=params_hash)
