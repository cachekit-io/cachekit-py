from __future__ import annotations

import contextvars
import uuid
from typing import Any, Callable, Protocol, runtime_checkable


@runtime_checkable
class TenantContextExtractor(Protocol):
    """Protocol for tenant ID extraction strategies.

    Implementations must extract tenant_id from function arguments and validate
    UUID format. FAIL CLOSED: raise ValueError if extraction fails.
    """

    def extract(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        """Extract tenant_id from function arguments.

        Args:
            args: Positional arguments from cached function call
            kwargs: Keyword arguments from cached function call

        Returns:
            tenant_id: UUID string (format: "550e8400-e29b-41d4-a716-446655440000")

        Raises:
            ValueError: If extraction fails or value is not valid UUID format
                       (FAIL CLOSED - no fallback to shared key)
        """
        ...


class ArgumentNameExtractor:
    """Extract tenant_id from function kwargs by argument name.

    Security: FAIL CLOSED - raises ValueError if argument not found.

    Examples:
        Extract tenant_id from kwargs:

        >>> extractor = ArgumentNameExtractor("org_id")
        >>> extractor.extract((), {"user_id": 123, "org_id": "550e8400-e29b-41d4-a716-446655440000"})
        '550e8400-e29b-41d4-a716-446655440000'

        Missing argument raises ValueError (FAIL CLOSED):

        >>> extractor.extract((), {"user_id": 123})  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        ValueError: Tenant ID argument 'org_id' not found in function kwargs...

        Invalid UUID format raises ValueError:

        >>> extractor.extract((), {"org_id": "not-a-uuid"})  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        ValueError: tenant_id must be valid UUID format...
    """

    def __init__(self, arg_name: str = "tenant_id"):
        """Initialize extractor with argument name to search for.

        Args:
            arg_name: Name of kwarg containing tenant_id (default: "tenant_id")
        """
        self.arg_name = arg_name

    def extract(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        """Extract tenant_id from kwargs by name.

        Args:
            args: Positional arguments (unused)
            kwargs: Keyword arguments to search

        Returns:
            tenant_id: Validated UUID string

        Raises:
            ValueError: If argument not found or not valid UUID format
        """
        # Look in kwargs first
        if self.arg_name in kwargs:
            tenant_id = str(kwargs[self.arg_name])
            _validate_tenant_id_format(tenant_id)
            return tenant_id

        # FAIL CLOSED - no fallback to default (security violation)
        raise ValueError(
            f"Tenant ID argument '{self.arg_name}' not found in function kwargs. "
            f"Multi-tenant encryption requires explicit tenant_id. "
            f"Cannot fall back to shared key (security violation)."
        )


class CallableExtractor:
    """Extract tenant_id using custom callable function.

    Allows flexible extraction logic for complex tenant identification scenarios.

    Examples:
        Custom extraction function:

        >>> def get_org_id(args, kwargs):
        ...     return kwargs.get("org_id")
        >>> extractor = CallableExtractor(get_org_id)
        >>> extractor.extract((), {"org_id": "550e8400-e29b-41d4-a716-446655440000"})
        '550e8400-e29b-41d4-a716-446655440000'

        Lambda-based extraction:

        >>> extractor = CallableExtractor(lambda args, kwargs: kwargs["tenant"])
        >>> extractor.extract((), {"tenant": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"})
        'a1b2c3d4-e5f6-7890-abcd-ef1234567890'

        Invalid UUID from extractor raises ValueError:

        >>> bad_extractor = CallableExtractor(lambda a, k: "invalid")
        >>> bad_extractor.extract((), {})  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        ValueError: tenant_id must be valid UUID format...
    """

    def __init__(self, extractor_fn: Callable[[tuple[Any, ...], dict[str, Any]], str]):
        """Initialize extractor with custom function.

        Args:
            extractor_fn: Function that extracts tenant_id from (args, kwargs)
                         Must return UUID string or raise ValueError
        """
        self.extractor_fn = extractor_fn

    def extract(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        """Extract tenant_id using custom function.

        Args:
            args: Positional arguments
            kwargs: Keyword arguments

        Returns:
            tenant_id: Validated UUID string

        Raises:
            ValueError: If extractor_fn fails or returns invalid UUID
        """
        tenant_id = self.extractor_fn(args, kwargs)
        _validate_tenant_id_format(tenant_id)
        return tenant_id


class ContextVarExtractor:
    """Extract tenant_id from contextvars (async-safe).

    RECOMMENDED: Use this for async frameworks (FastAPI, Starlette, asyncio).
    Contextvars prevent tenant context leakage in coroutine-based concurrency
    where threading.local() is unsafe.

    Security: FAIL CLOSED - raises ValueError if tenant_id not set in context.

    Examples:
        Set and extract tenant_id from context:

        >>> ContextVarExtractor.set_tenant_id("550e8400-e29b-41d4-a716-446655440000")
        >>> extractor = ContextVarExtractor()
        >>> extractor.extract((), {})
        '550e8400-e29b-41d4-a716-446655440000'

        Invalid UUID format raises ValueError on set:

        >>> ContextVarExtractor.set_tenant_id("not-a-uuid")  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        ValueError: tenant_id must be valid UUID format...
    """

    _tenant_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("tenant_id")

    @classmethod
    def set_tenant_id(cls, tenant_id: str) -> None:
        """Set tenant_id for current async context (async-safe).

        Must be called before cached function execution (typically in middleware).

        Args:
            tenant_id: UUID string format

        Raises:
            ValueError: If tenant_id is not valid UUID format
        """
        _validate_tenant_id_format(tenant_id)
        cls._tenant_id_var.set(tenant_id)

    def extract(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        """Extract tenant_id from contextvars.

        Args:
            args: Positional arguments (unused)
            kwargs: Keyword arguments (unused)

        Returns:
            tenant_id: UUID string from context

        Raises:
            ValueError: If tenant_id not set in context (FAIL CLOSED)
        """
        try:
            tenant_id = self._tenant_id_var.get()
        except LookupError:
            # FAIL CLOSED - no fallback to default (security violation)
            raise ValueError(
                "Tenant ID not set in context. "
                "Multi-tenant encryption requires ContextVarExtractor.set_tenant_id() to be called. "
                "Cannot fall back to shared key (security violation)."
            ) from None
        return tenant_id


def _validate_tenant_id_format(tenant_id: str) -> None:
    """Validate tenant_id is valid UUID format (RFC 4122).

    Args:
        tenant_id: String to validate

    Raises:
        ValueError: If not valid UUID format

    Examples:
        Valid UUID passes validation:

        >>> _validate_tenant_id_format("550e8400-e29b-41d4-a716-446655440000")  # No error

        Invalid UUID raises ValueError:

        >>> _validate_tenant_id_format("not-a-uuid")  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        ValueError: tenant_id must be valid UUID format...

        Empty string raises ValueError:

        >>> _validate_tenant_id_format("")  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        ValueError: tenant_id must be valid UUID format...
    """
    try:
        uuid.UUID(tenant_id)
    except (ValueError, AttributeError, TypeError) as e:
        raise ValueError(
            f"tenant_id must be valid UUID format (e.g., '550e8400-e29b-41d4-a716-446655440000'). Got: {tenant_id!r}"
        ) from e


# Default tenant extractor for simple use cases
DEFAULT_TENANT_EXTRACTOR = ArgumentNameExtractor("tenant_id")
