"""Test helpers for driving :class:`CircuitBreaker` through its public API.

``CircuitBreaker.call()`` / ``.call_async()`` were removed as dead production
code (LAB-522): the live path — and ``FeatureOrchestrator`` — guards operations
with ``should_attempt_call()`` and then records the outcome via
``record_success()`` / ``record_failure()``. These helpers reproduce the old
check -> run -> record convenience flow on top of that public API so the
state-machine tests keep exercising real behavior without resurrecting the
wrapper in ``src``.
"""

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.reliability.circuit_breaker import CircuitBreaker

T = TypeVar("T")


def guarded_call(breaker: CircuitBreaker, operation: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a sync operation through the breaker using its public API.

    Raises ``BackendError`` (TRANSIENT) when the breaker would reject the
    request, mirroring the fail-fast behavior of the removed ``call()``.
    """
    if not breaker.should_attempt_call():
        raise BackendError("Circuit breaker is OPEN", error_type=BackendErrorType.TRANSIENT)
    try:
        result = operation(*args, **kwargs)
    except Exception as exc:
        breaker.record_failure(exc)
        raise
    breaker.record_success()
    return result


async def guarded_call_async(breaker: CircuitBreaker, operation: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
    """Async counterpart of :func:`guarded_call` (awaits the operation)."""
    if not breaker.should_attempt_call():
        raise BackendError("Circuit breaker is OPEN", error_type=BackendErrorType.TRANSIENT)
    try:
        result = await operation(*args, **kwargs)
    except Exception as exc:
        breaker.record_failure(exc)
        raise
    breaker.record_success()
    return result
