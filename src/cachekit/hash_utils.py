"""Standardized hashing utilities for cachekit.

Uses BLAKE3 for hashing (approximately 2-3 GB/s throughput).
"""

import hashlib
import re
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
    ``_REDACTED_KEY_RE`` below and by ``test_pass_through_is_strict_allow_list``
    — change them together.
    """
    return f"<redacted:{hashlib.blake2b(str(cache_key).encode('utf-8'), digest_size=8).hexdigest()}>"


#: Placeholders that occupy the cache_key field but are not keys and carry no
#: caller data, so they stay readable. ``system`` is the label health.py logs its
#: checks under; hashing it turned a readable operator-facing field into an
#: opaque digest and silently broke any dashboard filtering on it. None of these
#: is a well-formed cache key (real keys are ``ns:...``), so nothing caller-supplied
#: can impersonate one.
_SENTINEL_KEYS = frozenset({"unknown", "<generation_failed>", "system"})

#: Matches exactly what redact_cache_key() emits — keep the two in step.
_REDACTED_KEY_RE = re.compile(r"<redacted:[0-9a-f]{16}>\Z")


def redact_key_for_log(cache_key: object) -> str:
    """Redact a cache key for logging unless it is a known sentinel or already redacted.

    Cache keys embed caller-supplied tenant/user identifiers and must never reach
    logs verbatim (CWE-532, issue #163). Real keys are canonical ``ns:...`` strings;
    sentinels (``unknown``, ``<generation_failed>``) and redact_cache_key() output
    (``<redacted:{16 hex}>``) carry no caller data and stay readable as-is.

    Matching the strict generated format makes redaction idempotent, so one key can
    cross several sinks — ``handle_cache_error`` into ``log_cache_operation``, or a
    caller handing an already-redacted value to ``SimpleLogger`` — and still emit a
    single digest that correlates across all of them. Re-hashing would mint a fresh
    digest per hop and break that correlation, without opening a pass-through for
    arbitrary angle-bracketed strings.

    Prefer this over :func:`redact_cache_key` at any *sink*. Reach for the bare
    function only where the input is known-raw and cannot already be redacted.

    Lives beside redact_cache_key() in this leaf module so the decorator
    orchestrator, ``cachekit.logging`` and the backend loggers share one policy
    without importing each other.
    """
    key_str = str(cache_key)
    if key_str in _SENTINEL_KEYS or _REDACTED_KEY_RE.fullmatch(key_str):
        return key_str
    return redact_cache_key(key_str)


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
