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
    (``loc == ()``) snapshots the whole input dict. A failure in the constructor (``from_env()``
    included) or a ``model_validate*`` classmethod is re-raised as a copy with every input redacted
    and each error's type and loc kept, and its msg and ctx too, except where a ctx exception's msg
    came from its dropped traceback or chain, or a custom error's msg would change when formatted
    again with its own ctx (that ctx is dropped). An error that cannot be rebuilt at all comes back
    as one ctx-less error that withholds the details. The copy is still a ValidationError (a
    ValueError), so fail-loud propagation paths are unchanged.
    """

    def __init__(self, **kwargs: Any) -> None:
        _redacting(functools.partial(super().__init__, **kwargs))

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
    """Run ``validate``, re-raising a failure with no raw input and no chain.

    A ValidationError becomes its redacted copy. A SettingsError (an env value that fails to decode
    chains the decoder's error, which holds the raw value) or a UnicodeError (an env file that is
    not valid UTF-8 carries the file's bytes) becomes a SettingsError with only its message, which
    names what failed without quoting it. Not a context manager: raising from ``__exit__`` would
    chain the original, raw inputs and all.
    """
    try:
        return validate()
    except ValidationError as e:
        failure: ValidationError | SettingsError = e
    except (SettingsError, UnicodeError) as e:
        failure = SettingsError(str(e))
    # Redacted and raised OUTSIDE the except block, so nothing raised or reported meanwhile has the
    # original as its __context__: `raise ... from None` only suppresses display, and the exception a
    # ctx exception's str() raises once its chain is dropped is reported to sys.unraisablehook as the
    # msgs are read.
    if isinstance(failure, ValidationError):
        try:
            failure = _redacted_copy(failure)  # also drops this frame's last reference to the original
        except Exception:
            # A validator's ctx can defeat the rebuild (an object posing as an exception, a non-str ctx
            # key); withhold the details rather than raise a traceback whose frames hold the original.
            withheld = PydanticCustomError("redaction_failed", "Validation failed; details withheld")
            failure = ValidationError.from_exception_data(
                failure.title, [{"type": withheld, "loc": (), "input": "[REDACTED]"}], hide_input=True
            )
    raise failure


def _redacted_copy(error: ValidationError) -> ValidationError:
    """Rebuild ``error`` with every input replaced by ``"[REDACTED]"``.

    Should not raise: a failure here would carry this frame and ``_redacting``'s, both holding the
    original, raw inputs and all, so ``_redacting`` withholds every detail instead. Kept out of
    ``_redacting`` so the raw ``err`` dicts are gone from the frame that raises. Side effect: exceptions
    in a ctx are shared with ``error`` and lose their traceback and chain in place, first, so that no
    msg read afterwards quotes them.
    """
    for err in error.errors():
        for value in (err.get("ctx") or {}).values():
            if isinstance(value, BaseException):
                # A validator's exception (ctx["error"]) keeps its traceback, whose frames hold the
                # validator's locals (the raw value), and its chain can quote the raw value. Drop both,
                # through BaseException's own descriptors: a frozen or property-overriding subclass would
                # raise on plain assignment. Its type and args are unchanged.
                for attr in ("__traceback__", "__context__", "__cause__"):
                    getattr(BaseException, attr).__set__(value, None)
    # Read the msgs only now: one rendered from a ctx exception's str() that read its chain no longer
    # quotes it. Every msg below (a custom error's template, the mode check) is the post-drop one.
    errors = error.errors()
    sanitized: list[InitErrorDetails] = []
    for err in errors:
        ctx = err.get("ctx")
        error_type: str | PydanticCustomError = err["type"]
        if "url" not in err or error_type not in _BUILTIN_ERROR_TYPES:
            # Only built-in types rebuild from their name. The url marks a real built-in: a PydanticCustomError
            # has none and may reuse a built-in name ("value_error") without the ctx that name requires. The
            # name check guards against a url on a type this pydantic-core's ErrorType does not list, since a
            # wrong rebuild-by-name raises here. Any other (pydantic's own Path fields raise "path_type")
            # rebuilds from its rendered msg, which pydantic produced, hence not a LiteralString.
            error_type = PydanticCustomError(err["type"], err["msg"], ctx)  # pyright: ignore[reportArgumentType]
            # A custom error formats its template with its own ctx on every render. When that changes the
            # rendered msg (a ctx value that quotes a placeholder), formatting it again would put a ctx value
            # into the msg, so keep the msg verbatim and drop the ctx.
            if error_type.message() != err["msg"]:
                error_type = PydanticCustomError(err["type"], err["msg"])  # pyright: ignore[reportArgumentType]
        detail: InitErrorDetails = {"type": error_type, "loc": err["loc"], "input": "[REDACTED]"}
        if ctx and isinstance(error_type, str):  # a custom error carries its own ctx, or none
            detail["ctx"] = ctx
        sanitized.append(detail)
    # A ValidationError renders every msg in one input mode and does not say which ("Input should be
    # an object" is JSON; "... a valid dictionary ..." is Python). Keep the Python rebuild when it
    # reproduces the msgs, else the JSON one, so every built-in keeps its type and url.
    python_copy = ValidationError.from_exception_data(error.title, sanitized, hide_input=True)
    if [err["msg"] for err in python_copy.errors(include_url=False)] == [err["msg"] for err in errors]:
        return python_copy
    return ValidationError.from_exception_data(error.title, sanitized, input_type="json", hide_input=True)


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
