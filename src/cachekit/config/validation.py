"""Configuration validation functions for cachekit."""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar, get_args

from pydantic import ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from pydantic_core.core_schema import ErrorType
from pydantic_settings import BaseSettings, SettingsError

if TYPE_CHECKING:
    from typing_extensions import Self  # typing.Self is 3.11+

logger = logging.getLogger(__name__)

_BUILTIN_ERROR_TYPES = frozenset(get_args(ErrorType))

_T = TypeVar("_T")


class ConfigurationError(Exception):
    """Exception raised for configuration errors.

    Examples:
        Raise with descriptive message:

        >>> raise ConfigurationError("REDIS_URL not configured")  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        cachekit.config.validation.ConfigurationError: REDIS_URL not configured

        Check exception message:

        >>> try:
        ...     raise ConfigurationError("Invalid TTL")
        ... except ConfigurationError as e:
        ...     str(e)
        'Invalid TTL'
    """

    pass


class RedactingSettings(BaseSettings):
    """``BaseSettings`` whose validation errors never carry a raw input (CWE-532).

    ``hide_input_in_errors`` only affects ``str()``/``repr()``: ``errors()`` and ``json()`` still
    snapshot the raw input, which for a settings model is cleartext credentials (a master key, an
    API key, a password in a URL) and is exactly what error trackers serialize. A model-level error
    (``loc == ()``) snapshots the whole input dict. A failed construction is re-raised as a copy
    with every input redacted and each error's type, loc, msg and ctx kept. The copy is still a
    ValidationError (a ValueError), so fail-loud propagation paths are unchanged.
    """

    def __init__(self, **kwargs: Any) -> None:
        sanitized_error: ValidationError | SettingsError | None = None
        try:
            super().__init__(**kwargs)
        except ValidationError as e:
            sanitized_error = _redacted_copy(e)
        except SettingsError as e:
            # An env value that fails to decode (JSON for a list field) chains the decoder's error,
            # which holds the raw value; the message names only the field and the source.
            sanitized_error = SettingsError(str(e))
        # Raised OUTSIDE the except block so __context__/__cause__ stay None —
        # `raise ... from None` only suppresses display; the original (with raw
        # inputs recoverable via .errors()) would still hang off __context__
        # for anything that walks exception chains.
        if sanitized_error is not None:
            raise sanitized_error

    # pydantic calls the overridden __init__ only for mapping input. Malformed JSON, a non-object
    # document, non-mapping input and the strings mode fail in the core validator first, so the
    # model_validate* classmethods redact on their own.
    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> Self:
        return _redacting(functools.partial(super().model_validate, obj, **kwargs))

    @classmethod
    def model_validate_json(cls, json_data: str | bytes | bytearray, **kwargs: Any) -> Self:
        return _redacting(functools.partial(super().model_validate_json, json_data, **kwargs))

    @classmethod
    def model_validate_strings(cls, obj: Any, **kwargs: Any) -> Self:
        return _redacting(functools.partial(super().model_validate_strings, obj, **kwargs))


def _redacting(validate: Callable[[], _T]) -> _T:
    """Run ``validate``, re-raising a ValidationError as its redacted copy with no chain.

    Not a context manager: raising from ``__exit__`` would chain the original, raw inputs and all.
    """
    sanitized_error: ValidationError | None = None
    try:
        return validate()
    except ValidationError as e:
        sanitized_error = _redacted_copy(e)
    raise sanitized_error  # outside the except block, as in RedactingSettings.__init__


def _redacted_copy(error: ValidationError) -> ValidationError:
    """Rebuild ``error`` with every input replaced by ``"[REDACTED]"``.

    Must not raise: it runs inside the ``except`` that caught ``error``, so an exception here would
    chain the original, raw inputs and all. Kept out of ``__init__`` so the raw ``err`` dicts are
    gone from the frame that raises. Side effect: exceptions in a ctx are shared with ``error`` and
    lose their traceback and chain in place.
    """
    errors = error.errors()
    sanitized: list[InitErrorDetails] = []
    for err in errors:
        ctx = err.get("ctx")
        error_type: str | PydanticCustomError = err["type"]
        if "url" not in err or error_type not in _BUILTIN_ERROR_TYPES:
            # Only built-in types rebuild from their name. The url marks a real built-in: a PydanticCustomError
            # has none and may reuse a built-in name ("value_error") without the ctx that name requires. The
            # name check guards against a url on a type this pydantic-core's ErrorType does not list, since a
            # wrong rebuild-by-name raises here and chains the original. Any other (pydantic's own Path fields
            # raise "path_type") rebuilds from its rendered msg, which pydantic produced, hence not a LiteralString.
            error_type = PydanticCustomError(err["type"], err["msg"], ctx)  # pyright: ignore[reportArgumentType]
        detail: InitErrorDetails = {"type": error_type, "loc": err["loc"], "input": "[REDACTED]"}
        if ctx:
            for value in ctx.values():
                if isinstance(value, BaseException):
                    # A validator's exception (ctx["error"]) keeps its traceback, whose frames hold the
                    # validator's locals (the raw value), and its chain can quote the raw value. Drop
                    # both; its type, args and str(), hence msg, are unchanged. Set through
                    # BaseException's own descriptors: a frozen or property-overriding subclass
                    # would raise on plain assignment.
                    for attr in ("__traceback__", "__context__", "__cause__"):
                        getattr(BaseException, attr).__set__(value, None)
            detail["ctx"] = ctx
        sanitized.append(detail)
    # A built-in type re-renders its msg for an input mode, and the error does not say which mode
    # raised it ("Input should be an object" is JSON; "... a valid dictionary ..." is Python). Keep
    # the rebuild whose messages all match, so every error keeps its built-in type and url.
    msgs = [err["msg"] for err in errors]
    python_copy = ValidationError.from_exception_data(error.title, sanitized, hide_input=True)
    rebuilt = python_copy.errors(include_url=False)
    if [err["msg"] for err in rebuilt] == msgs:
        return python_copy
    json_copy = ValidationError.from_exception_data(error.title, sanitized, input_type="json", hide_input=True)
    if [err["msg"] for err in json_copy.errors(include_url=False)] == msgs:
        return json_copy
    # Neither mode matches every error: rebuild the mismatches from their original msg, without a url.
    # Equal lengths by construction; strict=False because raising here would chain the original.
    for err, detail, python_err in zip(errors, sanitized, rebuilt, strict=False):
        if python_err["msg"] != err["msg"]:
            detail["type"] = PydanticCustomError(err["type"], err["msg"], err.get("ctx"))  # pyright: ignore[reportArgumentType]
    return ValidationError.from_exception_data(error.title, sanitized, hide_input=True)


def validate_encryption_config(encryption: bool | None = False, master_key: str | None = None) -> None:
    """Validate encryption configuration when encryption is enabled.

    Checks for a master key: first from the explicit parameter, then from
    CACHEKIT_MASTER_KEY env var via pydantic-settings.

    Args:
        encryption: Tri-state encryption flag. Falsy (None unset / False opt-out) skips
                    validation; only an explicit True requires a resolvable master key.
        master_key: Explicit master key (hex string). Takes precedence over env var.

    Raises:
        ConfigurationError: If encryption config is invalid

    Security Warning:
        Environment variables are NOT secure key storage for production.
        Use secrets management systems (HashiCorp Vault, AWS Secrets Manager, etc.)
        for production deployments.

    Examples:
        No-op when encryption is disabled:

        >>> validate_encryption_config(encryption=False)  # Returns None, no error

        Validation requires CACHEKIT_MASTER_KEY when enabled (requires env var):

        >>> validate_encryption_config(encryption=True)  # doctest: +SKIP
    """
    # Only validate if encryption is explicitly enabled
    if not encryption:
        return

    # Resolve master key: explicit param > env var via settings
    resolved_key = master_key
    if not resolved_key:
        from cachekit.config.singleton import get_settings

        settings = get_settings()
        resolved_key = settings.master_key.get_secret_value() if settings.master_key else None

    if not resolved_key:
        raise ConfigurationError(
            "Master key required when encryption=True. Either pass master_key= "
            "or set CACHEKIT_MASTER_KEY environment variable. "
            "Generate with: python -c 'import secrets; print(secrets.token_hex(32))'"
        )

    # Production environment warning when key came from env var (not inline)
    if not master_key:
        from cachekit.config.singleton import get_settings

        settings = get_settings()
        if not settings.dev_mode:
            logger.warning(
                "Master key loaded from environment variable. "
                "For production, use a secrets management system "
                "(HashiCorp Vault, AWS Secrets Manager, etc.)."
            )

    # Validate key format and length
    try:
        key_bytes = bytes.fromhex(resolved_key)
        if len(key_bytes) < 32:
            raise ConfigurationError(
                f"CACHEKIT_MASTER_KEY must be at least 32 bytes (256 bits). "
                f"Got {len(key_bytes)} bytes. "
                "Generate with: python -c 'import secrets; print(secrets.token_hex(32))'"
            )
    except ValueError as e:
        raise ConfigurationError(f"CACHEKIT_MASTER_KEY must be hex-encoded: {e}") from e
