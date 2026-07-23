"""Thread-safe singleton pattern for global cachekit settings."""

import os
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
        >>> settings.l1_max_size_mb
        100

    Note:
        This function is thread-safe and uses double-checked locking for performance.
        The settings instance is created from environment variables on first call.
    """
    global _settings_instance

    # Fast path - snapshot the global once. A concurrent reset_settings() can null _settings_instance
    # at any time, so the unlocked self-heal check below reads the snapshot, never the global, or it
    # could dereference None between the not-None check and the .master_key access.
    instance = _settings_instance
    if instance is not None:
        # Self-heal the keyless-then-key-set ordering trap (#195): if the config was first built
        # before CACHEKIT_MASTER_KEY entered the environment (e.g. an import-time cache decorator
        # evaluated before the app loaded its secrets), it froze master_key=None — encryption would
        # then silently never activate. Re-read once the key appears, so it turns on without an
        # explicit reset_settings(). Idempotent: after the rebuild master_key is set, so this never
        # fires again (no per-call churn once a key is present).
        if instance.master_key is None and os.environ.get("CACHEKIT_MASTER_KEY"):
            with _settings_lock:
                # Re-read the global under the lock: a peer may have rebuilt it (key now set) or
                # reset_settings() may have cleared it (back to None). Rebuild only if still keyless.
                if _settings_instance is None or _settings_instance.master_key is None:
                    _settings_instance = CachekitConfig.from_env()
                return _settings_instance
        return instance

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
