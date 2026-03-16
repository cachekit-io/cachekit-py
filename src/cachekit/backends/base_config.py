"""Base configuration for all backend implementations.

All backend configs MUST inherit from BaseBackendConfig to ensure
consistent behavior, validation, and environment variable handling.

Design rationale:
    - Pydantic v2 model_config does NOT merge on inheritance (replaces)
    - Child classes must explicitly spread parent config via inherit_config()
    - This enables isinstance() checks and enforces patterns

Usage:
    >>> from cachekit.backends.base_config import BaseBackendConfig, inherit_config
    >>> from pydantic_settings import SettingsConfigDict
    >>>
    >>> class MyBackendConfig(BaseBackendConfig):
    ...     model_config = SettingsConfigDict(
    ...         **inherit_config(BaseBackendConfig),
    ...         env_prefix="CACHEKIT_MY_",
    ...     )
    ...     my_setting: str = "default"
"""

from __future__ import annotations

from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

# Keys that child classes MUST override (not inherited from base)
_CHILD_OVERRIDE_KEYS = frozenset({"env_prefix"})


def inherit_config(base_cls: type[BaseSettings]) -> dict[str, Any]:
    """Extract inheritable model_config settings from a base class.

    Filters out keys that child classes must explicitly set (like env_prefix).

    Args:
        base_cls: The base class to inherit config from

    Returns:
        Dict of model_config settings safe to spread into child class

    Example:
        >>> class ChildConfig(BaseBackendConfig):
        ...     model_config = SettingsConfigDict(
        ...         **inherit_config(BaseBackendConfig),
        ...         env_prefix="MY_PREFIX_",
        ...     )
    """
    return {k: v for k, v in base_cls.model_config.items() if k not in _CHILD_OVERRIDE_KEYS}


class BaseBackendConfig(BaseSettings):
    """Base class for all backend configurations.

    Provides consistent settings for environment variable parsing,
    validation strictness, and configuration patterns.

    All backend configs MUST:
        1. Inherit from this class
        2. Spread model_config via inherit_config(): `**inherit_config(BaseBackendConfig)`
        3. Add their own env_prefix
        4. Implement from_env() classmethod

    Attributes:
        model_config: Standard pydantic-settings configuration with:
            - env_nested_delimiter="__" for nested config support
            - case_sensitive=False for env var flexibility
            - extra="forbid" for strict validation (catch typos)
            - populate_by_name=True for alias support

    Example:
        >>> class MyBackendConfig(BaseBackendConfig):
        ...     model_config = SettingsConfigDict(
        ...         **inherit_config(BaseBackendConfig),
        ...         env_prefix="CACHEKIT_MY_",
        ...     )
        ...     my_url: str = "http://localhost:8080"
    """

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="forbid",
        populate_by_name=True,
    )

    @classmethod
    def from_env(cls) -> BaseBackendConfig:
        """Create configuration from environment variables.

        Subclasses should override type hint but can reuse implementation.

        Returns:
            Configuration instance loaded from environment
        """
        return cls()
