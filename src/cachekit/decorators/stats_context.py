"""Context variable for exposing function stats to backends."""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .wrapper import _FunctionStats

# ContextVar for exposing current function stats to backends
_current_function_stats: contextvars.ContextVar[_FunctionStats | None] = contextvars.ContextVar(
    "cachekit_function_stats", default=None
)


def get_current_function_stats() -> _FunctionStats | None:
    """Get current function stats from context.

    Returns current function statistics if called within a @cache decorated function,
    None otherwise. This enables backends to inject observability headers.

    Returns:
        Current function stats if within a cache operation, None otherwise.

    Note:
        Returns None when called outside a @cache decorated function context.
        Used internally by backends to inject metrics headers.

    Examples:
        Returns None when no stats context is set:

        >>> get_current_function_stats() is None
        True
    """
    return _current_function_stats.get()


def set_current_function_stats(stats: _FunctionStats | None) -> contextvars.Token:
    """Set current function stats in context.

    Args:
        stats: Function stats tracker to expose.

    Returns:
        Token for resetting the context variable.
    """
    return _current_function_stats.set(stats)


def reset_current_function_stats(token: contextvars.Token) -> None:
    """Reset current function stats in context.

    Args:
        token: Token from set_current_function_stats().
    """
    _current_function_stats.reset(token)


__all__ = [
    "get_current_function_stats",
    "set_current_function_stats",
    "reset_current_function_stats",
]
