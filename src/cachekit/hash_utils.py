"""Standardized hashing utilities for cachekit.

Uses BLAKE3 for hashing (approximately 2-3 GB/s throughput).
"""

from typing import Union

import blake3


def fast_hash(data: Union[str, bytes], digest_size: int = 8) -> str:
    """Ultra-fast hash using BLAKE3 - optimized for hot paths.

    Args:
        data: String or bytes to hash
        digest_size: Output size in bytes (default: 8 = 16 hex chars)

    Returns:
        Hex string of specified length

    Performance: ~2-3 GB/s throughput
    """
    if isinstance(data, str):
        data = data.encode("utf-8")

    return blake3.blake3(data).hexdigest()[: digest_size * 2]


def secure_hash(data: Union[str, bytes], digest_size: int = 32) -> str:
    """Secure hash using BLAKE3 - for content integrity.

    Args:
        data: String or bytes to hash
        digest_size: Output size in bytes (default: 32 = 64 hex chars)

    Returns:
        Hex string of specified length

    Use for: Content checksums, data integrity validation
    """
    if isinstance(data, str):
        data = data.encode("utf-8")

    return blake3.blake3(data).hexdigest()[: digest_size * 2]


def function_hash(func_name: str) -> str:
    """Standardized function identifier hash.

    Args:
        func_name: Function identifier (e.g., f"{func.__module__}.{func.__qualname__}")

    Returns:
        8-character hex hash (collision probability: ~1 in 4 billion)
    """
    return fast_hash(func_name, digest_size=4)


def cache_key_hash(args_kwargs_str: str) -> str:
    """Standardized cache key hash for arguments.

    Args:
        args_kwargs_str: String representation of args/kwargs

    Returns:
        32-character hex hash for cache key uniqueness
    """
    return fast_hash(args_kwargs_str, digest_size=16)


def content_checksum(content: Union[str, bytes]) -> str:
    """Content integrity checksum.

    Args:
        content: Content to checksum

    Returns:
        64-character hex hash for integrity validation
    """
    return secure_hash(content, digest_size=32)


def short_fingerprint(data: str) -> str:
    """Short fingerprint for IDs/identifiers.

    Args:
        data: Data to fingerprint

    Returns:
        16-character hex hash for short identifiers
    """
    return fast_hash(data, digest_size=8)
