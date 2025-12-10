"""Cachekit configuration module.

This module provides a clean, modular configuration system with:
- CachekitConfig: Pydantic-based settings with environment variable support
- DecoratorConfig: Unified config for cache decorators with nested config groups
- Validation utilities
- Thread-safe singleton pattern
"""

# Core configuration classes
from .decorator import DecoratorConfig, get_default_backend, set_default_backend

# Nested configuration groups (for customizing DecoratorConfig components)
from .nested import (
    BackpressureConfig,
    CircuitBreakerConfig,
    EncryptionConfig,
    L1CacheConfig,
    MonitoringConfig,
    TimeoutConfig,
)
from .settings import CachekitConfig

# Singleton pattern
from .singleton import get_settings, reset_settings

# Validation
from .validation import ConfigurationError, validate_encryption_config

__all__ = [
    # Configuration classes
    "CachekitConfig",
    "DecoratorConfig",
    # Nested config groups
    "BackpressureConfig",
    "CircuitBreakerConfig",
    "EncryptionConfig",
    "L1CacheConfig",
    "MonitoringConfig",
    "TimeoutConfig",
    # Backend management
    "set_default_backend",
    "get_default_backend",
    # Singleton
    "get_settings",
    "reset_settings",
    # Validation
    "ConfigurationError",
    "validate_encryption_config",
]
