"""Thread-safe singleton pattern for global cachekit settings."""

import threading
from typing import Optional

from .settings import CachekitConfig

# Thread-safe settings singleton
_settings_instance: Optional[CachekitConfig] = None
_settings_lock = threading.RLock()


def get_settings() -> CachekitConfig:
    """Get or create the global settings singleton instance.

    Thread-safe lazy initialization of CachekitConfig from environment variables.
    The instance is cached for performance - subsequent calls return the same instance.

    Returns:
        CachekitConfig: Global settings instance

    Examples:
        Get singleton instance (same object on repeated calls):

        >>> reset_settings()  # Start fresh
        >>> s1 = get_settings()
        >>> s2 = get_settings()
        >>> s1 is s2
        True

        Access configuration values:

        >>> reset_settings()
        >>> settings = get_settings()
        >>> settings.default_ttl
        3600
        >>> settings.enable_compression
        True

    Note:
        This function is thread-safe and uses double-checked locking for performance.
        The settings instance is created from environment variables on first call.
    """
    global _settings_instance

    # Fast path - return cached instance without lock
    if _settings_instance is not None:
        return _settings_instance

    # Slow path - create instance with lock
    with _settings_lock:
        # Double-check pattern - another thread might have created it
        if _settings_instance is None:
            _settings_instance = CachekitConfig.from_env()

        return _settings_instance


def reset_settings() -> None:
    """Reset the global settings singleton instance.

    This clears the cached settings instance, forcing get_settings() to
    re-read from environment variables on next call.

    Useful for testing scenarios where environment variables change.

    Examples:
        Reset forces new instance creation:

        >>> s1 = get_settings()
        >>> reset_settings()
        >>> s2 = get_settings()
        >>> s1 is s2
        False

        Safe to call even when no instance exists:

        >>> reset_settings()  # No error even if nothing to reset
    """
    global _settings_instance

    with _settings_lock:
        _settings_instance = None
