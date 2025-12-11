"""Intent-based cache decorator interface.

Provides the @cache decorator with intent-based variants (@cache.minimal, @cache.production, @cache.secure, @cache.dev, @cache.test).
"""

from __future__ import annotations

import functools
from dataclasses import replace
from typing import Any, Callable, TypeVar

from ..config import DecoratorConfig
from .wrapper import create_cache_wrapper

F = TypeVar("F", bound=Callable[..., Any])


def _apply_cache_logic(
    func: Callable[..., Any], decorator_config: DecoratorConfig, _l1_only_mode: bool = False
) -> Callable[..., Any]:
    """Apply resolved configuration using the wrapper factory.

    Args:
        func: Function to wrap
        decorator_config: DecoratorConfig instance with all settings
        _l1_only_mode: If True, backend=None was explicitly passed (L1-only mode).
                       This prevents the wrapper from trying to get a backend from the provider.

    Returns:
        Wrapped function
    """
    # Use the wrapper factory with DecoratorConfig
    return create_cache_wrapper(func, config=decorator_config, _l1_only_mode=_l1_only_mode)


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
            >>> config.timeout.enabled
            True

        Intent-based secure (security-critical with encryption):
            >>> config = DecoratorConfig.secure(
            ...     master_key="a" * 64,
            ...     ttl=600,
            ...     backend=None
            ... )
            >>> config.encryption.enabled
            True
            >>> config.l1.enabled
            True

        RORO configuration (clean and type-safe):
            >>> @cache(config=DecoratorConfig.minimal(ttl=300, backend=None))
            ... def optimized() -> str:
            ...     return "cached"
            >>> optimized()
            'cached'

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
        **manual_overrides: Any manual parameter overrides (including serializer)

    Returns:
        Decorated function with intelligent caching
    """

    def decorator(f: F) -> F:
        # Resolve backend at decorator application time
        # Track if backend=None was explicitly passed (L1-only mode)
        # This is a sentinel problem: we need to distinguish between:
        # 1. User passed @cache(backend=None) explicitly -> L1-only mode
        # 2. User didn't pass backend at all -> should try provider
        _explicit_l1_only = "backend" in manual_overrides and manual_overrides.get("backend") is None
        backend = manual_overrides.pop("backend", None)

        # Backward compatibility: map flattened l1_enabled to nested l1.enabled
        if "l1_enabled" in manual_overrides:
            from cachekit.config.nested import L1CacheConfig

            l1_enabled = manual_overrides.pop("l1_enabled")
            # Merge with existing l1 config if provided
            existing_l1 = manual_overrides.pop("l1", L1CacheConfig())
            manual_overrides["l1"] = replace(existing_l1, enabled=l1_enabled)

        # RORO config takes highest precedence
        if config is not None:
            # DecoratorConfig instance provided - use it directly with overrides
            if not isinstance(config, DecoratorConfig):
                raise TypeError(
                    f"config parameter must be DecoratorConfig instance, got {type(config).__name__}. "
                    f"Use DecoratorConfig.minimal(), .production(), .secure(), .dev(), or .test()"
                )
            resolved_config = config
            if manual_overrides or backend is not None:
                # Apply overrides by creating new DecoratorConfig with merged settings
                override_dict = manual_overrides.copy()
                if backend is not None:
                    override_dict["backend"] = backend
                resolved_config = replace(config, **override_dict)
        # Intent-based presets (renamed per Task 6)
        elif _intent == "minimal":  # Renamed from "fast"
            resolved_config = DecoratorConfig.minimal(backend=backend, **manual_overrides)
        elif _intent == "production":  # Renamed from "safe"
            resolved_config = DecoratorConfig.production(backend=backend, **manual_overrides)
        elif _intent == "secure":
            # Extract master_key from manual_overrides (required for secure preset)
            master_key = manual_overrides.pop("master_key", None)
            tenant_extractor = manual_overrides.pop("tenant_extractor", None) or None
            if not master_key:
                raise ValueError("cache.secure requires master_key parameter")
            resolved_config = DecoratorConfig.secure(
                master_key=master_key, tenant_extractor=tenant_extractor, backend=backend, **manual_overrides
            )
        elif _intent == "dev":
            resolved_config = DecoratorConfig.dev(backend=backend, **manual_overrides)
        elif _intent == "test":
            resolved_config = DecoratorConfig.test(backend=backend, **manual_overrides)
        else:
            # No intent specified - use default DecoratorConfig with overrides
            resolved_config = DecoratorConfig(backend=backend, **manual_overrides)

        # Delegate to wrapper factory with L1-only mode flag
        # Note: _explicit_l1_only is ONLY set when backend=None was explicitly passed
        # via manual_overrides. DecoratorConfig.backend defaults to None, but that
        # should NOT trigger L1-only mode - it should fall back to the provider.
        return _apply_cache_logic(f, resolved_config, _l1_only_mode=_explicit_l1_only)  # type: ignore[return-value]

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
# Note: L1-only mode requires explicit backend=None parameter (no preset decorator)
