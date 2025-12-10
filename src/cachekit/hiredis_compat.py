"""Hiredis compatibility module for Python 3.13+ free-threading mode.

This module must be imported before any redis imports to properly
configure hiredis behavior in Python 3.13+ free-threading environments.
"""

import logging
import sys

logger = logging.getLogger(__name__)


def _get_disable_hiredis_setting() -> bool:
    """Get disable_hiredis setting from RedisBackendConfig.

    Returns:
        bool: True if hiredis should be disabled
    """
    try:
        from cachekit.backends.redis.config import RedisBackendConfig

        redis_config = RedisBackendConfig.from_env()
        return redis_config.disable_hiredis
    except Exception as e:
        logger.debug(f"Could not load Redis config for hiredis setting: {e}")
        return False


def configure_hiredis_for_free_threading():
    """Configure hiredis behavior for Python 3.13 free-threading mode.

    In Python 3.13+, the GIL can be disabled for free-threading mode.
    However, hiredis (a C extension) is not yet GIL-safe and will force
    the GIL to be re-enabled, causing RuntimeWarnings.

    This function provides control over hiredis usage based on:
    1. CACHEKIT_DISABLE_HIREDIS configuration setting
    2. Python version and GIL status detection
    3. Enterprise preference for warning-free operation

    Returns:
        bool: True if hiredis was disabled
    """
    # Check explicit user preference first
    if _get_disable_hiredis_setting():
        logger.info("Hiredis disabled via CACHEKIT_DISABLE_HIREDIS configuration")
        return _disable_hiredis()

    # Note: We can't differentiate between explicit false vs default false
    # This is acceptable - if user sets CACHEKIT_DISABLE_HIREDIS=false, we continue
    # to auto-detection below

    # Check if we're in Python 3.13+ with GIL disabled (free-threading mode)
    if sys.version_info >= (3, 13):
        try:
            # Check if GIL is disabled (free-threading mode)
            gil_enabled = getattr(sys, "_is_gil_enabled", lambda: True)()
            if not gil_enabled:
                logger.info(
                    "Python 3.13+ free-threading detected (GIL disabled). "
                    "Disabling hiredis to prevent GIL re-enabling and RuntimeWarnings. "
                    "Set CACHEKIT_DISABLE_HIREDIS=false to override."
                )
                return _disable_hiredis()
        except Exception as e:
            logger.debug(f"Could not determine GIL status: {e}")
            # If we can't determine GIL status, continue with default behavior

    return False


def _disable_hiredis():
    """Disable hiredis parser to prevent GIL warnings."""
    try:
        # Import redis.connection to access HIREDIS_AVAILABLE
        import redis.connection

        # Store original state for potential restoration
        getattr(redis.connection, "HIREDIS_AVAILABLE", None)

        # Disable hiredis
        redis.connection.HIREDIS_AVAILABLE = False  # type: ignore[attr-defined]

        logger.debug("Hiredis parser disabled - using pure Python parser")
        return True

    except Exception as e:
        logger.warning(f"Failed to disable hiredis: {e}. GIL warnings may appear.")
        return False


# Apply configuration when module is imported
_hiredis_disabled = configure_hiredis_for_free_threading()

# Export status for other modules
HIREDIS_DISABLED = _hiredis_disabled
