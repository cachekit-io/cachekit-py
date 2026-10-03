"""Standardized hashing and log-redaction utilities for cachekit.

Runs two digests for two jobs: BLAKE3 for cache-key hashing, blake2b for the
log-redaction correlation id below.

This is also the leaf home for the log-redaction policy — ``redact_cache_key``,
``redact_key_for_log`` and ``redact_error_for_log`` (CWE-532) — and for ``_WarnThrottle``,
which bounds how often a background failure is logged. They live here, not in
``cache_handler`` or ``backends.errors``, so backend/L1 modules, the decorator and the
invalidation channel share one policy without an import cycle (``backends.errors`` imports
this module).
"""

import hashlib
import os
import re
import threading
import time
from typing import Union

import blake3

# At most one WARNING per window for each kind of background failure the caller never sees
# (key tracking, a failed refresh, a refresh that could not run, an invalidation that could not
# be announced); failures in between log at DEBUG and are counted into the next WARNING. A
# registry outage fails every L2 write and a failing upstream every refresh, so one WARNING
# each would be a log flood.
_WARN_INTERVAL_SECONDS = 60.0


class _WarnThrottle:
    """One WARNING per _WARN_INTERVAL_SECONDS for one kind of failure; claim() counts the rest.

    Fork-safe by the owner-PID idiom: a forked child's first claim replaces the lock, which a
    parent thread that did not survive the fork may hold, and drops the parent's count. Sibling
    threads racing that swap cost at worst one extra WARNING, once per fork.

    Examples:
        >>> throttle = _WarnThrottle()
        >>> throttle.claim(), throttle.claim(), throttle.claim()  # WARNING, DEBUG, DEBUG
        (1, 0, 0)
    """

    __slots__ = ("_count", "_lock", "_pid", "_warned_at")

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self._lock = threading.Lock()
        self._warned_at, self._count = float("-inf"), 0
        self._pid = os.getpid()

    def claim(self) -> int:
        """Count one failure. Returns 0 if it should log at DEBUG, else the failures since the
        last WARNING, this one included, for the WARNING it should log.

        The window is claimed under the lock and the caller logs outside it: concurrent
        failures then emit one WARNING, and a slow log sink never serializes the failing callers.
        """
        if self._pid != os.getpid():
            self._reset()
        with self._lock:
            self._count += 1
            now = time.monotonic()
            if now - self._warned_at < _WARN_INTERVAL_SECONDS:
                return 0
            count, self._count, self._warned_at = self._count, 0, now
            return count


def redact_cache_key(cache_key: object) -> str:
    """Redact a cache key for log/error messages.

    Cache keys can embed caller-supplied tenant/user identifiers, so they must never reach
    logs verbatim (issue #163). A fixed-length blake2b digest keeps messages correlatable
    across the sync and async cache-set failure paths without leaking the key itself.

    Unkeyed by design — cross-process correlation is the point. The digest is as guessable
    as the key material (function args or a custom key), so it is a correlation id, not a
    secret (see SECURITY.md, "Digest strength").

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


def redact_error_for_log(error: object) -> str:
    """Render an exception for a log/error message without leaking cache keys (CWE-532).

    An exception's ``str()`` reaches log interpolation at every cache-error sink and has
    unknown provenance: it can echo the raw cache key directly (a redis ResponseError
    naming the key) or transitively (a ``BackendError`` whose free-form ``.message`` was
    built with the key). So this helper logs **no free-form exception text at all** — it
    does not trust that ``.message`` is key-free, it structurally cannot include it:

    - ``BackendError`` is rendered from its allow-listed, non-key fields only — the Python
      type plus the ``BackendErrorType`` classification (``.error_type``, an enum of fixed
      verbs). Its ``.message`` and raw ``.key`` are never read here; the redacted key digest
      is already emitted in the separate ``key`` log field, and full detail stays on the
      exception object for programmatic access.
    - Every other exception collapses to its bare type name.

    Sits beside redact_key_for_log() so both log sinks share one error policy.
    ``BackendError`` is imported lazily to keep this leaf module free of a back-edge to
    ``backends.errors`` (which imports this module).
    """
    from cachekit.backends.errors import BackendError

    if isinstance(error, BackendError):
        error_type = getattr(error.error_type, "value", error.error_type)
        return f"{type(error).__name__}({error_type})"
    return type(error).__name__


def blake3_hash(data: Union[str, bytes], digest_size: int = 8) -> str:
    """BLAKE3 digest truncated to ``digest_size`` bytes.

    Args:
        data: String or bytes to hash
        digest_size: Output size in bytes (default: 8 = 16 hex chars)

    Returns:
        Hex string of specified length
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
    return blake3_hash(func_name, digest_size=4)


def cache_key_hash(args_kwargs_str: str) -> str:
    """Standardized cache key hash for arguments.

    Args:
        args_kwargs_str: String representation of args/kwargs

    Returns:
        32-character hex hash for cache key uniqueness
    """
    return blake3_hash(args_kwargs_str, digest_size=16)
