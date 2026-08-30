"""Standardized hashing utilities for cachekit.

Uses BLAKE3 for hashing (approximately 2-3 GB/s throughput).
"""

import hashlib
from typing import Union

import blake3


def redact_cache_key(cache_key: object) -> str:
    """Redact a cache key for log/error messages.

    Cache keys can embed caller-supplied tenant/user identifiers, so they must never reach
    logs verbatim (issue #163). A fixed-length blake2b digest keeps messages correlatable
    across the sync and async cache-set failure paths without leaking the key itself.

    Lives in this leaf module so backend/L1 modules can use it without importing
    cache_handler (which imports them).

    The exact output format (``<redacted:{16 hex}>``) is pinned by
    ``decorators.orchestrator._REDACTED_KEY_RE`` and
    ``test_pass_through_is_strict_allow_list`` — change them together.
    """
    return f"<redacted:{hashlib.blake2b(str(cache_key).encode('utf-8'), digest_size=8).hexdigest()}>"


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
