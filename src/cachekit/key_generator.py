"""Cache key generation functionality."""

from __future__ import annotations

import hashlib
from typing import Any, Callable, cast

import msgpack


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
        # Step 1: Normalize recursively
        normalized_args = [self._normalize(arg) for arg in args]
        normalized_kwargs = {k: self._normalize(v) for k, v in sorted(kwargs.items())}

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

    def _normalize(self, obj: Any) -> Any:
        """Normalize object for deterministic MessagePack encoding.

        CRITICAL: Ensures identical serialization across Python, TypeScript, Go, PHP.
        """
        if isinstance(obj, dict):
            # Recursively normalize dict with sorted keys
            return {k: self._normalize(v) for k, v in sorted(obj.items())}

        elif isinstance(obj, (list, tuple)):
            # Recursively normalize collections (tuple→list)
            return [self._normalize(x) for x in obj]

        elif isinstance(obj, float):
            # CRITICAL: Normalize -0.0 → 0.0 for cross-language compatibility
            return 0.0 if obj == 0.0 else obj

        else:
            # Primitives (int, str, bytes, bool, None) pass through unchanged
            return obj

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
