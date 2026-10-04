"""cachekit.io backend implementation for cachekit."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import math
import os
import random
import statistics
import threading
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import quote

from pydantic import SecretStr, ValidationError

from cachekit.backends._uninterrupted import _await_uninterrupted
from cachekit.backends.cachekitio.client import ClientLease, lease_http_client
from cachekit.backends.cachekitio.config import CachekitIOBackendConfig
from cachekit.backends.cachekitio.error_handler import HTTPStatusError, classify_http_error
from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.config.validation import ConfigurationError, hide_secret
from cachekit.decorators.stats_context import get_current_function_stats
from cachekit.hash_utils import redact_cache_key, redact_error_for_log
from cachekit.logging import get_structured_logger

if TYPE_CHECKING:
    from urllib3 import BaseHTTPResponse

    from cachekit.decorators.wrapper import _FunctionStats

# Module-level logger
_logger = get_structured_logger(__name__)
# Unsampled stdlib logger for one-off lock warnings that _logger's sampling could drop.
logger = logging.getLogger(__name__)

# Lock capability token travels in this request header, never the query string:
# a ?lock_id= query leaks the token into access/proxy logs and OpenTelemetry
# http.url spans (CWE-532), letting anyone with log access replay it. The SaaS
# handler dual-reads this header + the legacy ?lock_id= query during rollout,
# preferring the header. See protocol spec/saas-api.md (DELETE .../lock).
LOCK_ID_HEADER = "X-CacheKit-Lock-Id"

# Protocol-canonical TTL header (spec/saas-api.md). The legacy X-TTL is sent
# alongside it until the dual-reading server (saas#245) is deployed everywhere;
# sending both is value-identical and safe against either server generation.
# TODO(LAB-381 follow-up): drop X-TTL once saas#245 is live in prod.
TTL_HEADER = "X-CacheKit-TTL"
LEGACY_TTL_HEADER = "X-TTL"

# Stale-while-revalidate (LAB-381, spec/saas-api.md#stale-while-revalidate).
# STALE_TTL_HEADER rides PUTs to open a stale-grace window past the fresh TTL;
# FRESHNESS_HEADER labels every GET/HEAD 200 as fresh|stale. Pre-SWR servers
# ignore the former and never emit the latter. FRESH_FOR_HEADER carries the
# remaining freshness in whole seconds on GET 200s (LAB-557,
# spec/saas-api.md#remaining-freshness); pre-signal servers omit it.
STALE_TTL_HEADER = "X-CacheKit-Stale-TTL"
FRESHNESS_HEADER = "X-CacheKit-Freshness"
FRESH_FOR_HEADER = "X-CacheKit-Fresh-For"
# A Fresh-For value never exceeds the 30-day TTL cap, which is seven digits.
_FRESH_FOR_MAX_S = 2_592_000
_FRESH_FOR_MAX_DIGITS = 7

# A write the server sheds with 503 + a short Retry-After (request deadline, a retryable store
# fault) is sent once more, inline, after exactly that delay (LAB-7686). PUT and DELETE are
# idempotent, so a duplicate is harmless. A longer hint (the rate-limiter fault sends 10 s) is
# honoured by not retrying, rather than by retrying early.
_RETRY_METHODS = frozenset({"PUT", "DELETE"})
_MAX_RETRY_AFTER_S = 2

# The SaaS has no bulk delete, so whole-function invalidation sends this many DELETEs at once over
# the client's pool, one HTTP/1.1 connection each (LAB-7070). Each still takes its own limiter verdict.
_DELETE_FANOUT = 16
# A longer rate-limit hint than this is treated as a deny, not waited out. The tenant limiter's
# window is 60 s, so a real hint is far below it; the cap only bounds the parse.
_MAX_RATE_LIMIT_WAIT_S = 3600


def _write_retry_delay(method: str, response: BaseHTTPResponse) -> int | None:
    """Seconds to wait before the one retry of a shed write, or None for no retry.

    Only the delta-seconds form of ``Retry-After`` is read; an HTTP-date, a fraction or a
    missing header means no retry.

    Examples:
        >>> from urllib3 import HTTPResponse
        >>> _write_retry_delay("PUT", HTTPResponse(status=503, headers={"Retry-After": "1"}))
        1
        >>> _write_retry_delay("PUT", HTTPResponse(status=503, headers={"Retry-After": "10"})) is None
        True
        >>> _write_retry_delay("PATCH", HTTPResponse(status=503, headers={"Retry-After": "1"})) is None
        True
    """
    if method not in _RETRY_METHODS or response.status != 503:
        return None
    return _retry_after_seconds(response, _MAX_RETRY_AFTER_S)


def _retry_after_seconds(response: BaseHTTPResponse, max_seconds: int) -> int | None:
    """The delta-seconds ``Retry-After`` of ``response`` if it is at most ``max_seconds``, else None.

    An HTTP-date, a fraction, a missing header or a larger value is None.

    Examples:
        >>> from urllib3 import HTTPResponse
        >>> _retry_after_seconds(HTTPResponse(status=429, headers={"Retry-After": "007"}), 60)
        7
        >>> _retry_after_seconds(HTTPResponse(status=429, headers={"Retry-After": "61"}), 60) is None
        True
    """
    value = response.headers.get("Retry-After", "").strip()
    if not (value.isascii() and value.isdigit()):
        return None
    # Bound the digits before int(): past 4,300 digits it raises ValueError, which would
    # turn the response into an UNKNOWN error. Zero padding is valid delta-seconds, so strip it.
    value = value.lstrip("0") or "0"
    if len(value) > len(str(max_seconds)):
        return None
    delay = int(value)
    return delay if delay <= max_seconds else None


def _rate_limit_delay(error: BackendError) -> int | None:
    """Seconds to wait out a rate-limited DELETE, or None when ``error`` is not one.

    Rate limited means a 429 with a delta-seconds ``Retry-After``. A 429 without one is the
    quota or balance deny (``X-CacheKit-Deny-Reason``): waiting does not clear it.
    """
    cause = error.original_exception
    if isinstance(cause, HTTPStatusError) and cause.response.status == 429:
        return _retry_after_seconds(cause.response, _MAX_RATE_LIMIT_WAIT_S)
    return None


def _parse_fresh_for(value: str | None) -> int | None:
    """Map an ``X-CacheKit-Fresh-For`` value to seconds (LAB-557, spec/saas-api.md#remaining-freshness).

    Absent = pre-signal server → None (legacy behavior: no bound). Anything but 1-7 ASCII
    digits at most the 30-day TTL cap = drift → 0 (do not extend local service — the
    conservative action, mirroring the unrecognized-freshness → stale rule). The shape check
    runs before ``int()``, which accepts ``+5``, ``1_0``, whitespace and non-ASCII digits
    (LAB-7838). Takes the header string, not a response, so it outlives the HTTP client.

    Examples:
        >>> _parse_fresh_for("30"), _parse_fresh_for(None), _parse_fresh_for("+5"), _parse_fresh_for("3000000")
        (30, None, 0, 0)
    """
    if value is None:
        return None
    # Length first, per spec: a valid value never needs more than seven digits.
    if len(value) <= _FRESH_FOR_MAX_DIGITS and value.isascii() and value.isdigit():
        parsed = int(value)
        if parsed <= _FRESH_FOR_MAX_S:
            return parsed
    # Drift signal, not a crash: a server/proxy emitting garbage here disables L1 backfill
    # for affected reads — log so a fleet-wide latency regression is diagnosable.
    # Truncated, because the value is unbounded.
    _logger.debug(f"Invalid {FRESH_FOR_HEADER} header {value[:32]!r}; treating as 0 (no L1 backfill)")
    return 0


# Protocol spec/saas-api.md § Cache-Key Path Encoding, rule 2; rationale in _encode_key.
_RESERVED_KEY_SEGMENTS = frozenset({"", ".", "..", "health", "ttl", "lock"})

_API_KEY_HINT = (
    "\n\ncachekit.io requires an API key: pass api_key=... or set CACHEKIT_API_KEY\nGet an API key at: https://cachekit.io"
)


def _inject_metrics_headers(stats: _FunctionStats | None) -> dict[str, str]:
    """Extract cache metrics and format as HTTP headers.

    Extracts L1 hits, L2 hits, and misses from the provided function statistics,
    calculates the L1 hit rate with zero-division protection, and merges session
    headers for complete request tracing. Also injects L1 status for rate limit
    classification.

    Args:
        stats: Function statistics tracker, or None for graceful degradation.

    Returns:
        dict[str, str]: Headers dictionary containing:
            - X-CacheKit-Session-ID: Process-scoped session identifier
            - X-CacheKit-L1-Hits: Count of L1 cache hits
            - X-CacheKit-L2-Hits: Count of L2 cache hits
            - X-CacheKit-Misses: Count of cache misses
            - X-CacheKit-L1-Hit-Rate: L1 hit rate (0.000 to 1.000)
            - X-CacheKit-L1-Status: Rate limit classification ("hit", "miss", or "disabled")
            - X-CacheKit-Session-Start: Process start timestamp (ms)

    Behavior on None stats:
        Returns empty dict to prevent downstream errors. This allows graceful
        degradation when statistics are not available.

    Examples:
        >>> from cachekit.decorators.wrapper import _FunctionStats
        >>> stats = _FunctionStats(function_identifier="test.module.func", l1_enabled=True)
        >>> stats.record_l1_hit()
        >>> stats.record_l1_hit()
        >>> stats.record_l2_hit(2.5)
        >>> headers = _inject_metrics_headers(stats)
        >>> headers["X-CacheKit-L1-Hits"]
        '2'
        >>> headers["X-CacheKit-L2-Hits"]
        '1'
        >>> float(headers["X-CacheKit-L1-Hit-Rate"])
        0.667
        >>> headers["X-CacheKit-L1-Status"]
        'miss'

        >>> # Zero-division protection: 0 total hits = 0.000 rate
        >>> empty_stats = _FunctionStats(function_identifier="test.module.empty", l1_enabled=True)
        >>> headers = _inject_metrics_headers(empty_stats)
        >>> headers["X-CacheKit-L1-Hit-Rate"]
        '0.000'
        >>> headers["X-CacheKit-L1-Status"]
        'miss'

        >>> # L1 disabled
        >>> disabled_stats = _FunctionStats(function_identifier="test.module.disabled", l1_enabled=False)
        >>> headers = _inject_metrics_headers(disabled_stats)
        >>> headers["X-CacheKit-L1-Status"]
        'disabled'

        >>> # Graceful degradation with None (standalone usage)
        >>> headers = _inject_metrics_headers(None)
        >>> headers
        {'X-CacheKit-L1-Status': 'disabled'}
    """
    # Graceful degradation: default to L1-Status: disabled if stats is None
    # This ensures standalone usage with ck_sdk_* keys doesn't trigger 400 errors
    if stats is None:
        return {"X-CacheKit-L1-Status": "disabled"}

    # Extract metrics from stats
    info = stats.get_info()
    l1_hits = info.l1_hits
    l2_hits = info.l2_hits
    misses = info.misses

    # Calculate L1 hit rate with zero-division guard
    total_hits = l1_hits + l2_hits
    if total_hits > 0:
        l1_hit_rate = l1_hits / total_hits
    else:
        l1_hit_rate = 0.0

    # Format L1 hit rate to 3 decimal places
    l1_hit_rate_str = f"{l1_hit_rate:.3f}"

    # Determine L1 status for rate limit classification
    # Conservative approach: report "miss" for enabled (counts as backend op)
    # or "disabled" if L1 is not enabled
    if stats.l1_enabled:
        l1_status = "miss"  # Conservative: treat all as backend ops
    else:
        l1_status = "disabled"

    # Get session headers with exception safety
    try:
        from cachekit.backends.cachekitio.session import get_session_start_ms

        if info.session_id:
            # Use function-specific session ID from info (regenerated after cache_clear)
            session_headers = {
                "X-CacheKit-Session-ID": info.session_id,
                "X-CacheKit-Session-Start": str(get_session_start_ms()),
            }
        else:
            # Fallback to process-level session (backward compatibility)
            from cachekit.backends.cachekitio.session import get_session_headers

            session_headers = get_session_headers()
    except Exception as e:
        # Session header generation failed - continue without session headers
        # This ensures backend requests never fail due to session tracking issues
        _logger.debug(f"Session header generation failed: {redact_error_for_log(e)}")
        session_headers = {}

    # Build metrics headers
    metrics_headers = {
        "X-CacheKit-L1-Hits": str(l1_hits),
        "X-CacheKit-L2-Hits": str(l2_hits),
        "X-CacheKit-Misses": str(misses),
        "X-CacheKit-L1-Hit-Rate": l1_hit_rate_str,
        "X-CacheKit-L1-Status": l1_status,
    }

    # Merge and return
    return {**session_headers, **metrics_headers}


class CachekitIOBackend:
    """Distributed cache backend via Cloudflare Workers.

    Implements BaseBackend protocol with proper error handling,
    connection pooling, and circuit breaker integration.

    Example:
        >>> from cachekit import cache
        >>> from cachekit.backends.cachekitio import CachekitIOBackend
        >>> # Load from env: CACHEKIT_API_KEY=ck_live_...
        >>> # Usage:
        >>> # @cache(backend=CachekitIOBackend())
        >>> # def expensive_function(x):
        >>> #     return x * 2
        >>> CachekitIOBackend.__name__
        'CachekitIOBackend'
    """

    def __init__(
        self,
        api_url: str | None = None,
        api_key: str | SecretStr | None = None,
        timeout: float | None = None,
    ) -> None:
        """Initialize cachekit.io backend.

        Args:
            api_url: API endpoint URL. Default: ``CACHEKIT_API_URL``, then ``https://api.cachekit.io``.
            api_key: API key (``ck_live_...``). Default: ``CACHEKIT_API_KEY``.
            timeout: Request timeout in seconds. Default: ``CACHEKIT_TIMEOUT``, then 5.0.

        Each argument left as None is loaded from the environment via pydantic-settings,
        so ``CachekitIOBackend(api_key=...)`` alone is valid — an explicit argument wins,
        everything else still comes from the environment.

        Raises:
            ConfigurationError: missing or empty API key, one that is not an RFC 6750 bearer token, or an API URL that fails
                validation (credentials in the URL, non-HTTPS, private address, host not in the allowlist).
        """
        api_key = hide_secret(api_key)  # the config takes it wrapped; no local here holds it raw (CWE-532)
        overrides: dict[str, Any] = {"api_url": api_url, "api_key": api_key, "timeout": timeout}
        errors = None
        try:
            self._config = CachekitIOBackendConfig(**{k: v for k, v in overrides.items() if v is not None})
        except ValidationError as exc:
            errors = exc.errors(include_input=False)
        # Raised OUTSIDE the except block (CWE-532): the ValidationError's traceback holds the config's
        # raw kwargs, and `raise ... from None` only hides it — it would still hang off __context__.
        if errors is not None:
            problems = "; ".join(f"{'.'.join(str(part) for part in err['loc']) or 'config'}: {err['msg']}" for err in errors)
            key_absent = any(err["loc"] == ("api_key",) and err["type"] in ("missing", "too_short") for err in errors)
            hint = _API_KEY_HINT if key_absent else ""
            raise ConfigurationError(f"Invalid cachekit.io backend configuration — {problems}{hint}")

        # One thread-safe client per config and process, for sync and async methods alike: an async method sends
        # on it through asyncio.to_thread. Holding the lease keeps the client open; dropping it closes the client.
        # Fork: each request re-leases when the lease's PID is not this process's (see _own_lease).
        self._lease = lease_http_client(self._config)

    def _own_lease(self) -> ClientLease:
        """This process's lease: a forked child re-leases, so it never sends on its parent's connections.

        Those connections share the parent's TLS sessions: whichever process writes second on one breaks
        it, and a raced read can return the other process's response. Checked per request rather than by
        an at-fork hook, because uWSGI forks without running Python's at-fork hooks; os.getpid() is
        negligible next to the request. The lease is published through one reference and carries its own
        PID, so a thread never pairs a new PID with an inherited client.
        """
        lease = self._lease
        if lease.pid != os.getpid():
            lease = self._lease = lease_http_client(self._config)
        return lease

    @staticmethod
    def _encode_key(key: str) -> str:
        """Percent-encode a cache key for safe interpolation into the request path.

        ``safe=""`` encodes *every* reserved character — ``/`` ``?`` ``#`` ``%`` and the
        rest — so a caller-controlled key (the ``@cache(key=...)`` escape hatch) can never
        escape ``/v1/cache/{key}`` via an injected delimiter, query, or fragment
        (CWE-22 / CWE-20). Encode-once matches the SaaS validator's single decode, so a
        canonical key round-trips byte-for-byte. See ``SECURITY.md`` for the cross-SDK
        wire-parity contract (cachekit-rs / cachekit-ts).

        Reserved segments: the empty key, ``.``, ``..``, ``health``, ``ttl`` and ``lock``
        encode to themselves and cannot be sent at all (protocol ``spec/saas-api.md``
        § Cache-Key Path Encoding, rule 2). The dots are dot-segments that a URL parser
        removes before routing (``..`` -> ``/v1``, ``../ttl`` -> ``/v1/ttl``), and
        percent-encoding them does not help: the SaaS parses the URL under WHATWG, which
        collapses ``%2E`` / ``%2E%2E`` too. The words are route tokens (``/v1/cache/health``
        is the health endpoint). The empty key is an empty segment: ``/v1/cache/`` and
        ``/v1/cache//ttl`` address no stored entry. Each of these would send the bearer token
        to a path that is not the caller's entry, so they are rejected before any request
        is made. Only an exact match is reserved: ``a:..`` and ``..a`` are sent as-is, and
        canonical keys (which always contain ``:``) never match.

        Raises:
            BackendError: ``PERMANENT`` (never retried) — the key is reserved. Raised from every
                public method, including ``get_ttl`` / ``refresh_ttl``, which otherwise swallow a
                SaaS 400 as ``None`` / ``False``.

        Examples:
            >>> CachekitIOBackend._encode_key("ns:app:func:mod.fn:args:ab:1s")
            'ns%3Aapp%3Afunc%3Amod.fn%3Aargs%3Aab%3A1s'
            >>> CachekitIOBackend._encode_key("..")
            Traceback (most recent call last):
            ...
            cachekit.backends.errors.BackendError: ...
        """
        encoded = quote(key, safe="")
        if encoded in _RESERVED_KEY_SEGMENTS:
            raise BackendError(
                f"Cache key {encoded!r} is a reserved URL path segment and cannot be stored in "
                "cachekit.io; choose a different key",
                error_type=BackendErrorType.PERMANENT,
            )
        return encoded

    def _send(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> BaseHTTPResponse:
        """One attempt on this process's client, any status. Raises BackendError for a transport failure."""
        # Held for the whole request: a concurrent re-lease after a fork must not close this client under it.
        lease = self._own_lease()
        try:
            return lease.client.request(method, url, body=body, headers=headers)
        except Exception as exc:
            raise classify_http_error(exc, operation=method.lower()) from exc

    @staticmethod
    def _checked(method: str, response: BaseHTTPResponse, miss_on_404: bool) -> BaseHTTPResponse:
        """``response`` if it is a 2xx, or a 404 that ``miss_on_404`` accepts; otherwise the classified BackendError."""
        if 200 <= response.status < 300 or (miss_on_404 and response.status == 404):
            return response
        exc = HTTPStatusError(response)
        raise classify_http_error(exc, response=response, operation=method.lower()) from exc

    def _request_sync(
        self,
        method: str,
        endpoint: str,
        *,
        miss_on_404: bool = False,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> BaseHTTPResponse:
        """Make sync HTTP request with error handling and metrics injection.

        Args:
            method: HTTP method (GET, HEAD, PUT, DELETE, POST, PATCH)
            endpoint: API endpoint (relative to base_url/v1/cache/)
            miss_on_404: Return a 404 response instead of raising. Key reads
                and exists treat 404 as a miss; skipping the error path
                saves the HTTPStatusError + BackendError round-trip on every miss.
            body: Request body
            headers: Request headers, over the client's own

        Returns:
            The response, its body already read

        Raises:
            BackendError: Classified error for circuit breaker, for a transport failure or any
                status other than 2xx (and 404 under ``miss_on_404``)

        Notes:
            A PUT or DELETE answered 503 with ``Retry-After`` of at most 2 s is sent once
            more after exactly that delay (see ``_write_retry_delay``); nothing else retries.

            Automatically injects cache metrics headers (L1/L2 hits, session ID) when
            called from within a @cache decorated function; with no stats in context,
            only ``X-CacheKit-L1-Status: disabled``.
        """
        url = f"/v1/cache/{endpoint}"
        headers = self._request_headers(headers)
        response = self._send(method, url, body, headers)
        if (delay := _write_retry_delay(method, response)) is not None:
            _logger.debug(f"CachekitIO {method} got HTTP 503; retrying once after Retry-After {delay}s")
            time.sleep(delay)
            response = self._send(method, url, body, headers)
        return self._checked(method, response, miss_on_404)

    async def _request_async(
        self,
        method: str,
        endpoint: str,
        *,
        miss_on_404: bool = False,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> BaseHTTPResponse:
        """``_request_sync`` for a coroutine: each attempt runs on the same client in ``asyncio.to_thread``.

        The retry wait is ``asyncio.sleep``, so it holds no thread. A cancel stops the wait for the
        response, not the request: the attempt in flight still completes on its worker thread.
        """
        url = f"/v1/cache/{endpoint}"
        headers = self._request_headers(headers)
        response = await asyncio.to_thread(self._send, method, url, body, headers)
        if (delay := _write_retry_delay(method, response)) is not None:
            _logger.debug(f"CachekitIO {method} got HTTP 503; retrying once after Retry-After {delay}s")
            await asyncio.sleep(delay)
            response = await asyncio.to_thread(self._send, method, url, body, headers)
        return self._checked(method, response, miss_on_404)

    @staticmethod
    def _request_headers(headers: dict[str, str] | None) -> dict[str, str]:
        # Always — defaults to L1-Status: disabled when no stats. Built on the caller's thread: the stats
        # come from its context.
        metrics_headers = _inject_metrics_headers(get_current_function_stats())
        return {**headers, **metrics_headers} if headers else metrics_headers

    # ==================== BaseBackend Protocol (Sync) ====================
    # These sync methods send on the shared client on the calling thread (no event loop required)

    def get(self, key: str) -> bytes | None:
        """Retrieve value from cache (sync).

        Args:
            key: Cache key

        Returns:
            Cached bytes value or None if not found

        Raises:
            BackendError: If operation fails (network, auth, etc.)
        """
        response = self._request_sync("GET", self._encode_key(key), miss_on_404=True)
        if response.status == 404:
            return None
        return response.data

    @staticmethod
    def _is_stale(response: BaseHTTPResponse) -> bool:
        """Map the X-CacheKit-Freshness header to staleness (spec/saas-api.md).

        Absent header = fresh (pre-SWR server); unrecognized value = stale
        (revalidation is the conservative action). Tokens are lowercase and
        case-sensitive per spec.
        """
        value = response.headers.get(FRESHNESS_HEADER)
        return value is not None and value != "fresh"

    @staticmethod
    def _fresh_for(response: BaseHTTPResponse) -> int | None:
        """Read X-CacheKit-Fresh-For off ``response``; the grammar lives in ``_parse_fresh_for``."""
        return _parse_fresh_for(response.headers.get(FRESH_FOR_HEADER))

    def get_with_freshness(self, key: str) -> tuple[bytes, bool, int | None] | None:
        """Retrieve value plus its SWR freshness and remaining-freshness bound (sync).

        Returns:
            ``(value, is_stale, fresh_for)`` on a hit — ``is_stale`` is True only
            for an entry in its stale-grace window (LAB-381); ``fresh_for`` is the
            server's remaining freshness in seconds, or None from a pre-signal
            server (LAB-557) — or None on a miss.

        Raises:
            BackendError: If operation fails (network, auth, etc.)
        """
        response = self._request_sync("GET", self._encode_key(key), miss_on_404=True)
        if response.status == 404:
            return None
        return response.data, self._is_stale(response), self._fresh_for(response)

    def set(self, key: str, value: bytes, ttl: int | None = None, stale_ttl: int | None = None) -> None:
        """Store value in cache (sync).

        Args:
            key: Cache key
            value: Bytes to cache
            ttl: Time-to-live in seconds (optional)
            stale_ttl: Stale-grace window in seconds past the fresh TTL
                (LAB-381 SWR). Only honoured alongside an explicit ``ttl``;
                pre-SWR servers ignore it.

        Raises:
            BackendError: If operation fails
        """
        self._request_sync("PUT", self._encode_key(key), body=value, headers=self._set_headers(ttl, stale_ttl))

    @staticmethod
    def _set_headers(ttl: int | None, stale_ttl: int | None) -> dict[str, str]:
        """PUT timing headers: canonical + legacy TTL (dual-send until saas#245 deploys), stale window."""
        headers: dict[str, str] = {}
        if ttl is not None:
            headers[TTL_HEADER] = str(ttl)
            headers[LEGACY_TTL_HEADER] = str(ttl)
        if stale_ttl is not None and stale_ttl > 0 and ttl is not None:
            headers[STALE_TTL_HEADER] = str(stale_ttl)
        return headers

    def delete(self, key: str) -> bool:
        """Delete key from cache (sync).

        Args:
            key: Cache key

        Returns:
            True on every successful delete, whether or not the key existed: the
            server does not report existence on DELETE.

        Raises:
            BackendError: If operation fails
        """
        self._request_sync("DELETE", self._encode_key(key))
        return True

    def _delete_many(self, keys: list[str]) -> set[str]:
        """Delete many keys, up to ``_DELETE_FANOUT`` at a time (internal: whole-function invalidation).

        A concurrency cap does not cap rate: the tenant limiter counts admissions over time, so
        16 workers spend its budget far faster than one key at a time. So the fan-out runs only
        until a DELETE is rate limited (a 429 with ``Retry-After``). From then on no new
        concurrent DELETE starts, and the rate-limited and unsent keys go to ``_delete_paced``.

        A key fails when its ``delete`` raises any other ``BackendError`` (a 429 without
        ``Retry-After`` included), or when pacing runs out of time: it is returned, and the
        caller keeps it tracked for the next sweep. Any other exception propagates once every
        fan-out delete has finished, and the caller then deletes the keys one by one.
        """
        if not keys:
            return set()
        rate_limited = threading.Event()
        round_trips: list[float] = []

        def delete_one(key: str) -> bool | None:
            """True deleted, False failed, None left for the paced phase."""
            if rate_limited.is_set():
                return None
            start = time.monotonic()
            try:
                self.delete(key)
                return True
            except BackendError as e:
                if _rate_limit_delay(e) is not None:
                    rate_limited.set()
                    return None
                _logger.debug(f"Failed to delete L2 key {redact_cache_key(key)}: {redact_error_for_log(e)}")
                return False
            finally:
                round_trips.append(time.monotonic() - start)

        started = time.monotonic()
        # No more workers than pooled connections: a full pool opens, then discards, a connection per extra request.
        with ThreadPoolExecutor(max_workers=min(_DELETE_FANOUT, self._config.connection_pool_size, len(keys))) as pool:
            # One context copy per key: the metrics headers read the caller's contextvars, and a
            # Context cannot be entered by two threads at once.
            futures = [pool.submit(contextvars.copy_context().run, delete_one, key) for key in keys]
        outcomes = [future.result() for future in futures]
        failed = {key for key, outcome in zip(keys, outcomes, strict=True) if outcome is False}
        paced = [key for key, outcome in zip(keys, outcomes, strict=True) if outcome is None]
        if paced:
            # About what the serial loop would have taken, so no caller blocks much longer than it did.
            deadline = started + len(keys) * statistics.fmean(round_trips)
            failed |= self._delete_paced(paced, deadline)
        return failed

    def _delete_paced(self, keys: list[str], deadline: float) -> set[str]:
        """Delete ``keys`` one at a time, in order, waiting out each rate-limited DELETE.

        A rate-limited key is retried after its ``Retry-After``. If that wait would end past
        ``deadline``, that key and every key after it fail without being sent, and one WARNING
        names the rate limit. Any other ``BackendError`` fails its key at once. Returns the
        keys not deleted.
        """
        failed: set[str] = set()
        for i, key in enumerate(keys):
            while True:
                try:
                    self.delete(key)
                    break
                except BackendError as e:
                    delay = _rate_limit_delay(e)
                    if delay is None:
                        _logger.debug(f"Failed to delete L2 key {redact_cache_key(key)}: {redact_error_for_log(e)}")
                        failed.add(key)
                        break
                    if time.monotonic() + delay > deadline:
                        logger.warning(
                            "CachekitIO rate limit: stopped pacing invalidation deletes at the deadline; %d key(s) not deleted",
                            len(keys) - i,
                        )
                        return failed | set(keys[i:])
                    time.sleep(delay)
        return failed

    def exists(self, key: str) -> bool:
        """Check if key exists in cache (sync).

        Args:
            key: Cache key

        Returns:
            True if key exists, False otherwise

        Raises:
            BackendError: If operation fails
        """
        # Use HEAD request (idiomatic HTTP for existence checks)
        response = self._request_sync("HEAD", self._encode_key(key), miss_on_404=True)
        return response.status != 404

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        """Check cachekit.io backend health (sync).

        Pings backend to verify connectivity and measures latency.

        Returns:
            Tuple of (is_healthy, details_dict)
            is_healthy: True if backend is responsive
            details_dict: Contains latency_ms, backend_type, api_url
        """
        try:
            start = time.time()
            response = self._request_sync("GET", "health")
            latency_ms = (time.time() - start) * 1000

            data = response.json()
            return (
                True,
                {
                    "backend_type": "saas",
                    "latency_ms": round(latency_ms, 2),
                    "api_url": self._config.api_url,
                    "version": data.get("version", "unknown"),
                },
            )
        except Exception as exc:
            return (
                False,
                {
                    "backend_type": "saas",
                    "latency_ms": -1,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )

    # ==================== Async Backend Methods (Primary Implementation) ====================

    async def get_async(self, key: str) -> bytes | None:
        """Retrieve value from cache (async).

        Args:
            key: Cache key

        Returns:
            Cached bytes value or None if not found

        Raises:
            BackendError: If operation fails (network, auth, etc.)
        """
        response = await self._request_async("GET", self._encode_key(key), miss_on_404=True)
        if response.status == 404:
            return None
        return response.data

    async def set_async(self, key: str, value: bytes, ttl: int | None = None, stale_ttl: int | None = None) -> None:
        """Store value in cache (async).

        Args:
            key: Cache key
            value: Bytes to cache
            ttl: Time-to-live in seconds (optional)
            stale_ttl: Stale-grace window in seconds past the fresh TTL
                (LAB-381 SWR). Only honoured alongside an explicit ``ttl``;
                pre-SWR servers ignore it.

        Raises:
            BackendError: If operation fails
        """
        await self._request_async("PUT", self._encode_key(key), body=value, headers=self._set_headers(ttl, stale_ttl))

    async def delete_async(self, key: str) -> bool:
        """Delete key from cache (async).

        Args:
            key: Cache key

        Returns:
            True on every successful delete, whether or not the key existed: the
            server does not report existence on DELETE.

        Raises:
            BackendError: If operation fails
        """
        await self._request_async("DELETE", self._encode_key(key))
        return True

    async def exists_async(self, key: str) -> bool:
        """Check if key exists in cache (async).

        Args:
            key: Cache key

        Returns:
            True if key exists, False otherwise

        Raises:
            BackendError: If operation fails
        """
        # Use HEAD request (idiomatic HTTP for existence checks)
        response = await self._request_async("HEAD", self._encode_key(key), miss_on_404=True)
        return response.status != 404

    async def health_check_async(self) -> tuple[bool, dict[str, Any]]:
        """Check cachekit.io backend health (async).

        Pings backend to verify connectivity and measures latency.

        Returns:
            Tuple of (is_healthy, details_dict)
            is_healthy: True if backend is responsive
            details_dict: Contains latency_ms, backend_type, api_url
        """
        try:
            start = time.time()
            response = await self._request_async("GET", "health")
            latency_ms = (time.time() - start) * 1000

            data = response.json()
            return (
                True,
                {
                    "backend_type": "saas",
                    "latency_ms": round(latency_ms, 2),
                    "api_url": self._config.api_url,
                    "version": data.get("version", "unknown"),
                },
            )
        except Exception as exc:
            return (
                False,
                {
                    "backend_type": "saas",
                    "latency_ms": -1,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )

    # ==================== LockableBackend Protocol ====================

    async def _try_acquire_lock(self, lock_key: str, timeout: float) -> str | None:
        """Single attempt at the SaaS lock endpoint. Returns lock_id, or None if held.

        ``lock_key`` is the bare cache key (LockableBackend contract). The SaaS lock
        endpoint is ``POST /v1/cache/{key}/lock`` — no internal derivation is needed
        because lock semantics live in the URL path, not the key namespace. Appending
        ``:lock`` here would push the SaaS validator past its 7-segment canonical key
        budget and trigger a 400 at the edge.

        Non-finite (NaN / ±inf) or non-positive ``timeout`` is clamped to 1ms — without
        the guard, ``int(NaN)`` / ``int(inf)`` would raise outside ``BackendError`` and
        escape the wrapper's degrade-to-no-lock branch.

        Raises:
            BackendError: For any failed request, whatever its type. Only ``200`` with a null
                ``lock_id`` means contested (protocol ``POST /v1/cache/{key}/lock``); an error
                status or a network failure ends the wait, and the wrapper degrades to no-lock
                execution instead of polling a failing endpoint for ``blocking_timeout``.
        """
        # Clamp non-positive / non-finite timeouts before the int conversion.
        # int(NaN) / int(inf) raise ValueError/OverflowError that aren't BackendError, so
        # they'd escape the wrapper's degrade-to-no-lock branch and crash the @cache.io call.
        timeout_ms = max(1, int(timeout * 1000)) if math.isfinite(timeout) else 1
        encoded_key = self._encode_key(lock_key)
        response = await self._request_async(
            "POST",
            f"{encoded_key}/lock",
            body=json.dumps({"timeout_ms": timeout_ms}).encode(),
            headers={"Content-Type": "application/json"},
        )

        try:
            data = response.json()
            lock_id = data.get("lock_id")
        except (ValueError, AttributeError):
            # Malformed body from the SaaS — treat as held (retry) rather than crashing
            # the wrapper. Same failure class as the original issue #129 if we re-raised.
            return None

        return lock_id if isinstance(lock_id, str) else None

    async def _try_acquire_lock_drained(self, lock_key: str, timeout: float) -> str | None:
        """``_try_acquire_lock``, run to completion even if the caller is cancelled meanwhile.

        A cancel thrown into the in-flight POST loses the server's answer, not the grant, so the
        attempt runs as its own Task and is drained. If the caller was cancelled, a won lock is
        released before the cancel propagates, and a failed attempt is logged: the cancel always
        wins, otherwise the wrapper would carry on (or retry) in a task that was cancelled.
        """
        attempt = asyncio.ensure_future(self._try_acquire_lock(lock_key, timeout))
        try:
            return await _await_uninterrupted(attempt)
        except asyncio.CancelledError:
            if attempt.cancelled():
                raise  # the attempt itself was cancelled (e.g. asyncio.run teardown): no outcome to read
            if (err := attempt.exception()) is not None:
                logger.warning(
                    "CachekitIO lock attempt for %s failed (%s) while acquire_lock was being cancelled",
                    redact_cache_key(lock_key),
                    redact_error_for_log(err),
                )
            elif (won := attempt.result()) is not None:
                await self._release_lock(lock_key, won)
            raise

    @asynccontextmanager
    async def acquire_lock(
        self,
        key: str,
        timeout: float,
        blocking_timeout: Optional[float] = None,
    ) -> AsyncIterator[bool]:
        """Acquire distributed lock (LockableBackend protocol).

        The SaaS endpoint returns immediately; client-side polling implements
        ``blocking_timeout`` with proportional jitter (0.5×–1× the capped delay) to
        avoid lockstep retries on concurrent waiters. Each retry is a billable SaaS
        request — keep the cap tight.

        Args:
            key: Lock key
            timeout: Server-side hold duration before auto-release (seconds)
            blocking_timeout: Max client-side wait to acquire (None = single attempt)

        Yields:
            True if acquired, False if ``blocking_timeout`` elapsed without acquisition
        """
        lock_id: str | None = None
        try:
            lock_id = await self._try_acquire_lock_drained(key, timeout)

            if lock_id is None and blocking_timeout is not None:
                deadline = time.monotonic() + blocking_timeout
                delay = 0.05
                while lock_id is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    # Proportional jitter (not crypto): spread concurrent waiters, 0.5×–1× the capped delay.
                    jitter = 0.5 + random.random() * 0.5  # noqa: S311 — backoff jitter, not security
                    await asyncio.sleep(min(delay, remaining) * jitter)
                    lock_id = await self._try_acquire_lock_drained(key, timeout)
                    delay = min(delay * 2, 0.5)

            yield lock_id is not None
        finally:
            if lock_id is not None:
                await self._release_lock(key, lock_id)

    async def _release_lock(self, lock_key: str, lock_id: str) -> bool:
        """Release distributed lock. Internal helper for ``acquire_lock``'s cleanup.

        Best-effort: swallows ``BackendError`` and returns False so a release failure
        inside ``__aexit__`` cannot mask the user's exception. The server-side ``timeout``
        on the lock is the safety net if the DELETE never lands.

        Drained: the DELETE runs as its own Task to completion however many cancels land
        meanwhile; a cancel thrown into it would otherwise leave the lock held.
        """
        return await _await_uninterrupted(asyncio.ensure_future(self._delete_lock(lock_key, lock_id)))

    async def _delete_lock(self, lock_key: str, lock_id: str) -> bool:
        # lock_key is caller-controlled → percent-encode it into the path. lock_id is a
        # capability token and travels in the X-CacheKit-Lock-Id header, NOT the query
        # string (CWE-532): a ?lock_id= query leaks it into access/proxy logs and OTel
        # http.url spans. It is server-issued, so it needs no URL-encoding.
        encoded_key = self._encode_key(lock_key)
        try:
            await self._request_async("DELETE", f"{encoded_key}/lock", headers={LOCK_ID_HEADER: lock_id})
            return True
        except BackendError:
            # Swallowed inside the drained Task, not around the drain: once a cancel has landed the
            # drain re-raises it, and an error left on the Task surfaces only at GC as "never retrieved".
            return False

    # ==================== TTLInspectableBackend Protocol ====================

    async def get_ttl(self, key: str) -> int | None:
        """Get remaining TTL for key in seconds.

        Args:
            key: Cache key

        Returns:
            TTL in seconds, None if key doesn't exist or has no expiry

        Raises:
            BackendError: If the key is reserved (see ``_encode_key``); encoded outside the
                ``try`` so the rejection is not mistaken for a missing key.
        """
        encoded_key = self._encode_key(key)
        try:
            response = await self._request_async("GET", f"{encoded_key}/ttl")
            data = response.json()
            return data.get("ttl")
        except BackendError:
            return None

    async def refresh_ttl(self, key: str, ttl: int) -> bool:
        """Refresh/update TTL for existing key.

        Args:
            key: Cache key
            ttl: New TTL in seconds

        Returns:
            True if updated, False otherwise

        Raises:
            BackendError: If the key is reserved (see ``_encode_key``).
        """
        encoded_key = self._encode_key(key)
        try:
            payload = json.dumps({"ttl": ttl})
            await self._request_async(
                "PATCH",
                f"{encoded_key}/ttl",
                body=payload.encode(),
                headers={"Content-Type": "application/json"},
            )
            return True
        except BackendError:
            return False

    # ==================== TimeoutConfigurableBackend Protocol ====================

    def with_timeout(self, timeout: float) -> CachekitIOBackend:
        """Create new backend instance with different timeout.

        Args:
            timeout: New timeout in seconds

        Returns:
            New CachekitIOBackend instance with updated timeout
        """
        return CachekitIOBackend(
            api_url=self._config.api_url,
            api_key=self._config.api_key,
            timeout=timeout,
        )
