"""Intent-based cache decorator interface.

Provides the @cache decorator with intent-based variants (@cache.minimal, @cache.production,
@cache.secure, @cache.dev, @cache.test, @cache.local).
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import replace
from typing import Any, TypeVar

from ..config import ConfigurationError, DecoratorConfig
from ..config.decorator import (
    _FIELD_NAMES,
    _PRESET_EXTRA_KWARGS,
    _SECRET_KWARGS,
    UNSET,
    _reject_unsupported,
)
from ..config.validation import hide_any_secret, hide_secret, reveal_secret
from .local_wrapper import _ALLOWED_PARAMS as _LOCAL_KWARGS
from .wrapper import _ENCRYPTING_SERIALIZER_REFUSAL, _is_encrypting_serializer, create_cache_wrapper

F = TypeVar("F", bound=Callable[..., Any])

# The encryption keywords bare @cache folds into its EncryptionConfig.
_ENCRYPTION_KWARGS = frozenset(
    {"encryption", "master_key", "tenant_extractor", "single_tenant_mode", "deployment_uuid", "fail_closed"}
)
# Every keyword some form of @cache takes. Each form refuses those it does not take, and every form refuses any other.
_DECORATOR_KWARGS = _FIELD_NAMES.union(_ENCRYPTION_KWARGS, _LOCAL_KWARGS, {"l1_enabled"}, *_PRESET_EXTRA_KWARGS.values())


def cache(
    func: F | None = None, *, config: DecoratorConfig | None = None, _intent: str | None = None, **manual_overrides
) -> F | Callable[[F], F]:
    """Intelligent cache decorator with intent-base presets.

    This is the primary caching interface that provides:
    - Zero-config intelligence: @cache automatically detects settings
    - Intent-based optimization: @cache.minimal, @cache.production, @cache.secure, @cache.dev, @cache.test
    - Manual control when needed: @cache(ttl=3600, namespace="custom")

    Examples:
        Configuration patterns (verified with pytest --doctest-modules):

        Zero-config with L1-only backend:
            >>> @cache(backend=None)
            ... def compute_result() -> int:
            ...     return 42
            >>> compute_result()
            42

        Intent-based minimal (speed-critical):
            >>> config = DecoratorConfig.minimal(ttl=300, backend=None)
            >>> config.ttl
            300
            >>> config.circuit_breaker.enabled
            False

        Intent-based production (reliability-critical):
            >>> config = DecoratorConfig.production(ttl=600, backend=None)
            >>> config.circuit_breaker.enabled
            True

        Intent-based secure (security-critical with encryption; L1 holds ciphertext):
            >>> config = DecoratorConfig.secure(master_key="a" * 64, ttl=600)
            >>> config.encryption.enabled
            True
            >>> config.l1.enabled
            True

        Secure refuses L1-only mode — raw objects cannot be ciphertext (LAB-4665):
            >>> @cache.secure(master_key="a" * 64, backend=None)  # doctest: +IGNORE_EXCEPTION_DETAIL
            ... def leaks_plaintext() -> str:
            ...     return "pii"
            Traceback (most recent call last):
                ...
            cachekit.config.validation.ConfigurationError: encryption requires a backend

        RORO configuration (clean and type-safe); backend=None in a config is L1-only too:
            >>> runs = []
            >>> @cache(config=DecoratorConfig.minimal(ttl=300, backend=None))
            ... def optimized() -> str:
            ...     runs.append(1)
            ...     return "cached"
            >>> optimized(), optimized()
            ('cached', 'cached')
            >>> len(runs)  # the second call is an L1 hit
            1

        Manual override with namespace:
            >>> @cache(ttl=1800, namespace="custom", backend=None)
            ... def custom_function() -> dict:
            ...     return {"result": "value"}
            >>> custom_function()
            {'result': 'value'}

    Args:
        func: The function to decorate (when used without parentheses)
        config: DecoratorConfig object for RORO-style configuration
        _intent: Internal parameter for intent variants (fast/safe/secure)
        **manual_overrides: Any manual parameter overrides (including serializer).
            Notable: ``stale_ttl`` (LAB-381 stale-while-revalidate) — a stale-grace
            window in seconds past the fresh ``ttl``. During the window an expired
            entry is served immediately and the function re-runs in the background,
            so no request pays the recompute at a TTL boundary. Requires a positive
            ``ttl`` and an SWR-capable backend (CachekitIO); ``ttl + stale_ttl``
            is capped at 2,592,000 s (30 days). ``@cache.io`` defaults it to
            ``ttl`` — pass ``stale_ttl=0`` to opt out. The background recompute
            sees a snapshot of the caller's ``contextvars`` (so contextvar-based
            tenant extraction works), but no other request-scoped resources —
            open sessions/connections from the request must not be relied on.
            ``api_key`` (``@cache.io`` only) — the cachekit.io API key; falls back
            to ``CACHEKIT_API_KEY`` when omitted. ``@cache.io`` always builds its
            own CachekitIOBackend and rejects ``backend=`` and ``config=`` with
            ConfigurationError. ``@cache.secure`` rejects ``config=`` too; its RORO
            form is ``@cache(config=DecoratorConfig.secure(...))``. Beside ``config=``
            an override may name only a DecoratorConfig field or ``l1_enabled``, and
            may not set ``encryption=`` on an encrypted config or ``backend=`` on an io
            config. A keyword a form does not accept raises ConfigurationError
            (``@cache.local``: TypeError).

    Returns:
        Decorated function with intelligent caching
    """

    # Secrets stay wrapped from here down, so no frame on an error's traceback holds them raw in a local or
    # in this dict (CWE-532); each is unwrapped only where it is used. So does a value under a keyword no form takes:
    # it may be a key under a misspelt name (master_keey=), and a guard below can raise before the check that refuses it.
    # A bytes master_key is wrapped too, as every form refuses it; a bytes api_key is taken, and stays as passed.
    for _name in manual_overrides.keys() & _SECRET_KWARGS:
        manual_overrides[_name] = hide_secret(manual_overrides[_name])
    for _name in (manual_overrides.keys() - _DECORATOR_KWARGS) | (manual_overrides.keys() & {"master_key"}):
        manual_overrides[_name] = hide_any_secret(manual_overrides[_name])

    def decorator(f: F) -> F:
        # Every application works on its own copy: the pops and rewrites below would otherwise empty the dict
        # this decorator object shares with every function it wraps, and the next one would silently lose
        # backend=None, l1_enabled, or cache.secure's master_key and tenant_extractor.
        overrides = dict(manual_overrides)

        # LOCAL INTENT: short-circuit before any DecoratorConfig resolution.
        # Must be first — backend pop and l1_enabled mapping below would
        # silently consume kwargs that create_local_wrapper must reject.
        if _intent == "local":
            if config is not None:
                raise TypeError(
                    "@cache.local() does not accept config=. DecoratorConfig configures "
                    "backends, serialization, and encryption — none of which apply to "
                    "in-process reference caching. Pass parameters directly: "
                    "@cache.local(ttl=300, max_entries=256)"
                )
            from .local_wrapper import create_local_wrapper

            return create_local_wrapper(f, **overrides)  # type: ignore[return-value]

        # config= would replace the io/secure preset wholesale, silently: io would take any backend,
        # secure would take an unencrypted config and cache plaintext. The factory already IS the config.
        if _intent in ("io", "secure") and config is not None:
            raise ConfigurationError(
                f"@cache.{_intent}() does not accept config= — DecoratorConfig.{_intent}() already is the "
                f"{_intent} config. For the RORO form use @cache(config=DecoratorConfig.{_intent}(...))."
            )

        if config is not None and not isinstance(config, DecoratorConfig):
            raise TypeError(
                f"config parameter must be DecoratorConfig instance, got {type(config).__name__}. "
                f"Use DecoratorConfig.minimal(), .production(), .secure(), .dev(), or .test()"
            )

        # backend=None is L1-only; an omitted backend is UNSET and resolves per "Backend Resolution Priority".
        _explicit_backend = "backend" in overrides
        backend = overrides.pop("backend", UNSET)
        _explicit_l1_only = backend is None or (backend is UNSET and config is not None and config.backend is None)

        # Refuse an encrypting serializer= before the preset resolves, ahead of the presets' own key checks: a
        # caller who put the key inside the EncryptionWrapper would otherwise be told the decorator has no key.
        # backend=None, as keyword or in config=, skips this check, so create_cache_wrapper's L1-only refusal keeps
        # its message. That refusal runs after the preset's own checks, so one of those (a missing key or tenant
        # mode, say) can still fire first.
        if not _explicit_l1_only and _is_encrypting_serializer(overrides.get("serializer")):
            raise ConfigurationError(_ENCRYPTING_SERIALIZER_REFUSAL)

        # Tier 2 resolution: if no explicit backend and not L1-only mode,
        # check module-level default set via set_default_backend(). Kept here
        # (not only lazily) because decoration-time validation — the interop
        # backend guard and stale_ttl/SWR capability (LAB-557) — needs the
        # backend when it is already known. If the default is set LATER, the
        # wrapper re-consults it at first call (_resolve_lazy_backend, LAB-4457).
        # A backend already in config= is explicit and beats the default: fetched here, the
        # default would replace it below — DecoratorConfig.io(api_key=B) under a key-A default
        # would send tenant B's traffic under key A.
        # A config's backend=None is L1-only and stays so: the default fills only an UNSET backend.
        if backend is UNSET and (config is None or config.backend is UNSET):
            from ..config.decorator import get_default_backend

            default_backend = get_default_backend()
            if default_backend is not None:
                backend = default_backend

        # Flattened l1_enabled flips only l1.enabled, applied AFTER resolution (LAB-4828): every
        # preset factory already passes its own l1=, so forwarding it collides, and building it here
        # from L1CacheConfig() would drop the preset's / config='s L1 tuning (minimal swr_enabled=False).
        _has_l1_enabled = "l1_enabled" in overrides
        l1_enabled = overrides.pop("l1_enabled", None)

        # Map flattened tri-state encryption flag + related kwargs to nested EncryptionConfig.
        # Tri-state (issue #128): @cache(encryption=False) is a DELIBERATE opt-out that must
        # survive a present CACHEKIT_MASTER_KEY. None=unset, True=force, False=off.
        #
        # Scope: ONLY the bare/default decorator path (no config=, no _intent). Intent presets
        # (.secure, .io, ...) own their encryption-param handling, and config= is the RORO form.
        # An already-constructed EncryptionConfig passes through untouched — wrapping it again
        # would nest EncryptionConfig inside EncryptionConfig.enabled.
        if config is None and _intent is None:
            from cachekit.config.nested import EncryptionConfig

            _enc_passthrough = isinstance(overrides.get("encryption"), EncryptionConfig)
            if not _enc_passthrough and (_ENCRYPTION_KWARGS & overrides.keys()):
                enc_overrides: dict[str, Any] = {}
                if "encryption" in overrides:
                    enc_overrides["enabled"] = overrides.pop("encryption")
                for _k in ("master_key", "tenant_extractor", "single_tenant_mode", "deployment_uuid", "fail_closed"):
                    if _k in overrides:
                        enc_overrides[_k] = overrides.pop(_k)
                overrides["encryption"] = replace(EncryptionConfig(), **{k: reveal_secret(v) for k, v in enc_overrides.items()})

        # Checked here, not only in the preset's classmethod, so a refused value is wrapped in this frame's dicts too
        # (CWE-532), and before @cache.secure looks up its key, so a misspelt keyword is not reported as a missing key.
        if config is None:
            _reject_unsupported(
                f"The {_intent} preset" if _intent else "@cache",
                overrides,
                _FIELD_NAMES | _PRESET_EXTRA_KWARGS.get(_intent or "", frozenset()),
                held_by=(manual_overrides,),
            )

        # RORO config takes highest precedence
        if config is not None:
            # DecoratorConfig instance provided (type checked above) - use it with overrides
            resolved_config = config
            # An override may not disable integrity on an encrypted config= — the rule
            # DecoratorConfig.secure() enforces on its own kwargs.
            integrity_override = overrides.get("integrity_checking", True)
            if config.encryption.enabled is True and not integrity_override:
                raise ConfigurationError(
                    f"integrity_checking={integrity_override!r} cannot override an encrypted config= "
                    "(e.g. DecoratorConfig.secure()). Omit integrity_checking."
                )
            # Nor may an override replace an encrypted config's EncryptionConfig, and with it the key and tenant
            # mode, as DecoratorConfig.secure() refuses encryption= among its own kwargs.
            if config.encryption.enabled is True and "encryption" in overrides:
                raise ConfigurationError(
                    "encryption= cannot override an encrypted config= (DecoratorConfig.secure(), or one built with "
                    "encryption=EncryptionConfig(enabled=True, ...)). Set encryption options where the config is "
                    "built, e.g. DecoratorConfig.secure(master_key=..., fail_closed=True)."
                )
            # io's CachekitIOBackend is the preset, as on @cache.io(backend=...).
            if config._from_io and _explicit_backend:
                raise ConfigurationError(
                    "@cache(config=DecoratorConfig.io(...)) does not accept backend= — the io config always caches "
                    "through its own CachekitIOBackend.\n\n"
                    "To cache through another backend, use a different preset:\n"
                    "  @cache(config=DecoratorConfig.production(backend=my_backend))"
                )
            if overrides or backend is not UNSET:
                # Apply overrides by creating new DecoratorConfig with merged settings
                override_dict = overrides.copy()
                if backend is not UNSET:
                    override_dict["backend"] = backend
                _reject_unsupported("@cache(config=...)", override_dict, held_by=(overrides, manual_overrides))
                resolved_config = replace(config, **override_dict)
        # Intent-based presets (renamed per Task 6)
        elif _intent == "minimal":  # Renamed from "fast"
            resolved_config = DecoratorConfig.minimal(backend=backend, **overrides)
        elif _intent == "production":  # Renamed from "safe"
            resolved_config = DecoratorConfig.production(backend=backend, **overrides)
        elif _intent == "secure":
            # Extract master_key from overrides, fall back to env var via settings
            master_key = overrides.pop("master_key", None)
            tenant_extractor = overrides.pop("tenant_extractor", None) or None
            if not master_key:
                from cachekit.config.singleton import get_settings

                master_key = get_settings().master_key
            if not master_key:
                raise ValueError("cache.secure requires master_key parameter or CACHEKIT_MASTER_KEY environment variable")
            resolved_config = DecoratorConfig.secure(
                master_key=master_key, tenant_extractor=tenant_extractor, backend=backend, **overrides
            )
        elif _intent == "dev":
            resolved_config = DecoratorConfig.dev(backend=backend, **overrides)
        elif _intent == "test":
            resolved_config = DecoratorConfig.test(backend=backend, **overrides)
        elif _intent == "io":
            # io owns its backend. Hand an explicit backend= back so DecoratorConfig.io
            # rejects it — one error site for both the decorator and the classmethod.
            # `backend` is still the caller's value here: the default lookup above only
            # runs when no backend= was passed.
            if _explicit_backend:
                overrides["backend"] = backend
            resolved_config = DecoratorConfig.io(**overrides)
        else:
            # No intent specified - use default DecoratorConfig with overrides
            resolved_config = DecoratorConfig(backend=backend, **overrides)

        if _has_l1_enabled:
            resolved_config = replace(resolved_config, l1=replace(resolved_config.l1, enabled=l1_enabled))

        # resolved_config.backend is None exactly when the caller asked for L1-only, by keyword or in config=;
        # create_cache_wrapper reads it from there.
        return create_cache_wrapper(f, config=resolved_config)  # type: ignore[return-value]

    # Handle both @cache and @cache() syntax
    if func is None:
        return decorator
    else:
        return decorator(func)


# Intent-based decorator variants (Task 6: renamed per config-simplification spec)
cache.minimal = functools.partial(cache, _intent="minimal")  # type: ignore[attr-defined]  # Renamed from .fast
cache.production = functools.partial(cache, _intent="production")  # type: ignore[attr-defined]  # Renamed from .safe
cache.secure = functools.partial(cache, _intent="secure")  # type: ignore[attr-defined]
cache.dev = functools.partial(cache, _intent="dev")  # type: ignore[attr-defined]
cache.test = functools.partial(cache, _intent="test")  # type: ignore[attr-defined]
cache.io = functools.partial(cache, _intent="io")  # type: ignore[attr-defined]  # SaaS backend
cache.local = functools.partial(cache, _intent="local")  # type: ignore[attr-defined]
# Note: L1-only mode is backend=None, as a keyword or inside config=
