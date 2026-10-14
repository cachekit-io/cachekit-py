"""Structured JSON logging with sampling.

Each entry is serialised and written on the caller's thread. There is no background writer: a thread
started at import is not running in a forked child (Gunicorn ``--preload``, Celery prefork,
``multiprocessing``'s fork start method), so everything a child buffered for it was lost.

In a child forked from C without at-fork hooks (uWSGI without ``--py-call-uwsgi-fork-hooks``),
no ``StructuredLogger`` method logs: ``logging``'s own after-fork lock reset did not run there, so a
level check or a handler lock a parent thread held at fork would hang the caller.
"""

import json
import logging
import os
import platform
import random
import threading
import time
from typing import Any

from cachekit import l1_cache
from cachekit.config import get_settings
from cachekit.hash_utils import redact_error_for_log, redact_key_for_log

# Configure base logger
logger = logging.getLogger(__name__)


# Loaded once at module import from the settings singleton
SAMPLING_RATE = get_settings().log_sampling_rate

# Performance and health thresholds
HIGH_UTILIZATION_THRESHOLD = 0.9  # When to warn about high utilization

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
    "FATAL": logging.CRITICAL,
}

# What json.dumps raises on a value its default= never sees: a non-str dict key, a cycle, a too-deep nest
_UNENCODABLE = (TypeError, ValueError, RecursionError)


def _type_name(value: object) -> str:
    """Stand in for a value JSON cannot encode: its type only, as its str() may embed a cache key (CWE-532)."""
    return f"<{type(value).__name__}>"


def _encodable(value: object) -> object:
    """The value if json can encode it, else its type name."""
    try:
        json.dumps(value, default=_type_name)
    except _UNENCODABLE:
        return _type_name(value)
    return value


def _enabled(levelno: int) -> bool:
    """Whether to write an entry at levelno: never in a child forked without at-fork hooks (module docstring)."""
    return not l1_cache._forked_without_hooks() and logger.isEnabledFor(levelno)


class StructuredLogger:
    """Structured logger.

    Each entry is one JSON line on the ``cachekit.logging`` logger, written synchronously.

    Features:
    - Nothing is sampled or serialised below that logger's level
    - Sampling (10% default) of DEBUG/INFO/WARNING; ERROR and CRITICAL are never sampled
    - PII key-name masking (password/token/secret/key/auth kwargs)
    """

    def __init__(self, name: str):
        self.name = name

        self._hostname = platform.node()

        # PII patterns to mask (pre-compiled for speed)
        self._pii_keys = {"password", "token", "secret", "key", "auth"}

        # Sink for cache_operation and connection_pool_utilization: plain-text messages, fields in record.structured
        self.logger = logging.getLogger(name)

    def _should_sample(self) -> bool:
        """Fast sampling decision."""
        # Using random for non-cryptographic sampling - performance critical
        return random.random() < SAMPLING_RATE  # noqa: S311

    def _mask_pii(self, data: dict[str, Any]) -> dict[str, Any]:
        """Fast PII masking - only on sampled entries."""
        for key in data:
            if any(pii in key.lower() for pii in self._pii_keys):
                data[key] = "****"
        return data

    def _write(self, level: str, levelno: int, message: str, fields: dict[str, Any]) -> None:
        """Serialise one entry and log it now."""
        entry: dict[str, Any] = {
            **self._mask_pii(fields),  # first, so a caller's pid= cannot replace the real one
            "timestamp": time.time(),
            "level": level,
            "message": message,
            "logger": self.name,
            "hostname": self._hostname,
            "pid": os.getpid(),  # read per entry: a forked child reports its own
            "thread_id": threading.get_ident(),
        }
        try:
            line = json.dumps(entry, separators=(",", ":"), default=_type_name)
        except _UNENCODABLE:  # raising would replace the exception a caller may be logging
            line = json.dumps({k: _encodable(v) for k, v in entry.items()}, separators=(",", ":"), default=_type_name)
        logger.log(levelno, line)

    def log(self, level: str, message: str, **kwargs):
        """Main logging method with sampling."""
        level = level.upper()
        levelno = _LEVELS.get(level, logging.INFO)
        if _enabled(levelno) and (levelno >= logging.ERROR or self._should_sample()):
            self._write(level, levelno, message, kwargs)

    def debug(self, message: str, **kwargs):
        """Debug level logging."""
        self.log("DEBUG", message, **kwargs)

    def info(self, message: str, **kwargs):
        """Info level logging."""
        self.log("INFO", message, **kwargs)

    def warning(self, message: str, **kwargs):
        """Warning level logging."""
        self.log("WARNING", message, **kwargs)

    def error(self, message: str, **kwargs):
        """Error level logging - never sampled. Not via log(): a caller field named level or message would collide."""
        if _enabled(logging.ERROR):
            self._write("ERROR", logging.ERROR, message, kwargs)

    def cache_operation(self, operation: str, cache_key: str, **kwargs):
        """Log cache operation with standard fields."""
        # Always redact: cache keys embed caller-supplied tenant/user identifiers
        # (CWE-532, LAB-304). PII-pattern masking (SSN/email/...) does not catch
        # them, and a raw [:50] prefix is exactly the leak — so neither is an
        # alternative to the digest.
        #
        # Same guard the orchestrator sink uses, not a bare redact_cache_key():
        # callers reach this method with values already redacted upstream, and
        # re-hashing would emit a second, different digest for one key and break
        # correlation between the two sinks. Sentinels stay readable too.
        display_key = redact_key_for_log(cache_key) if cache_key else ""

        # CWE-532 at the sink: render an exception key-free; a str is already rendered (re-sanitising one emits "str").
        if isinstance(kwargs.get("error"), BaseException):
            kwargs["error"] = redact_error_for_log(kwargs["error"])

        levelno = logging.ERROR if "error" in kwargs else logging.INFO

        # Build context
        context = self._get_context()
        context.update(
            {
                "operation": operation,
                "cache_key": display_key,
                **kwargs,
            }
        )

        # Update Prometheus metrics if available
        try:
            from cachekit.reliability.metrics_collection import cache_latency, cache_operations

            # Determine status
            if "error" in kwargs:
                status = "error"
            elif operation == "get" and "hit" in kwargs:
                status = "hit" if kwargs.get("hit") else "miss"
            else:
                status = "success"

            # Update operation counter
            cache_operations.labels(
                operation=operation,
                status=status,
                serializer=kwargs.get("serializer", ""),
                namespace=kwargs.get("namespace", "default"),
            ).inc()

            # Update latency histogram if duration provided
            if "duration_ms" in kwargs:
                cache_latency.labels(operation=operation, serializer=kwargs.get("serializer", "")).observe(
                    kwargs["duration_ms"] / 1000.0
                )  # Convert to seconds

        except (ImportError, Exception):
            # Silently ignore if Prometheus metrics not available
            pass

        if not l1_cache._forked_without_hooks():  # module docstring; the metrics above still count
            self.logger.log(levelno, "cache_operation", extra={"structured": context})

    def connection_pool_utilization(self, utilization: float, **kwargs):
        """Log connection pool metrics."""
        if l1_cache._forked_without_hooks():  # module docstring
            return

        # Determine log level based on utilization
        if utilization > HIGH_UTILIZATION_THRESHOLD:
            self.logger.log(
                logging.WARNING,
                f"High connection pool utilization: {utilization:.1%}",
                extra={"structured": {"utilization": utilization, **kwargs}},
            )
        else:
            self.logger.log(
                logging.INFO,
                f"Connection pool utilization: {utilization:.1%}",
                extra={"structured": {"utilization": utilization, **kwargs}},
            )

        self.log("INFO", "pool_utilization", utilization=round(utilization, 3), **kwargs)

    def _get_context(self) -> dict[str, Any]:
        """Get current logging context."""
        return {
            "timestamp": time.time(),
            "thread_id": threading.get_ident(),
        }


# Global logger instances cache
_logger_instances: dict[str, StructuredLogger] = {}
_logger_lock = threading.Lock()


def get_structured_logger(name: str) -> StructuredLogger:
    """Get or create a structured logger instance.

    Args:
        name: Logger name (usually __name__)

    Returns:
        Structured logger instance
    """
    # Fast path - check if already exists
    if name in _logger_instances:
        return _logger_instances[name]

    # Slow path - create new instance
    with _logger_lock:
        # Double-check pattern
        if name not in _logger_instances:
            _logger_instances[name] = StructuredLogger(name)
        return _logger_instances[name]
