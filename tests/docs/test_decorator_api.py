"""Test decorator API matches documentation.

This module validates that all documented decorator presets exist in the codebase
and that deprecated/removed presets are truly gone. Prevents documentation drift.
"""

from __future__ import annotations

import pytest

from cachekit import cache


@pytest.mark.critical
class TestDecoratorPresetsExist:
    """Verify all documented decorator presets are available."""

    def test_minimal_preset_exists(self):
        """@cache.minimal preset must exist (documented in README, api-reference)."""
        assert hasattr(cache, "minimal"), "cache.minimal preset missing"
        assert callable(cache.minimal), "cache.minimal must be callable"

    def test_production_preset_exists(self):
        """@cache.production preset must exist (documented in README, api-reference)."""
        assert hasattr(cache, "production"), "cache.production preset missing"
        assert callable(cache.production), "cache.production must be callable"

    def test_secure_preset_exists(self):
        """@cache.secure preset must exist (documented in README, api-reference)."""
        assert hasattr(cache, "secure"), "cache.secure preset missing"
        assert callable(cache.secure), "cache.secure must be callable"

    def test_dev_preset_exists(self):
        """@cache.dev preset must exist (documented in api-reference)."""
        assert hasattr(cache, "dev"), "cache.dev preset missing"
        assert callable(cache.dev), "cache.dev must be callable"

    def test_test_preset_exists(self):
        """@cache.test preset must exist (documented in api-reference)."""
        assert hasattr(cache, "test"), "cache.test preset missing"
        assert callable(cache.test), "cache.test must be callable"


@pytest.mark.critical
class TestDeprecatedPresetsRemoved:
    """Verify old/deprecated decorator presets are removed."""

    def test_fast_preset_removed(self):
        """@cache.fast was renamed to @cache.minimal - old name must not exist."""
        assert not hasattr(cache, "fast"), "cache.fast still exists - should be cache.minimal"

    def test_safe_preset_removed(self):
        """@cache.safe was renamed to @cache.production - old name must not exist."""
        assert not hasattr(cache, "safe"), "cache.safe still exists - should be cache.production"


@pytest.mark.critical
class TestAllDocumentedPresetsWork:
    """Smoke test that all presets can actually be used as decorators."""

    def test_all_presets_can_decorate_sync_function(self):
        """All documented presets should work on sync functions."""
        # Test presets that don't require parameters
        presets = [cache, cache.minimal, cache.production, cache.dev, cache.test]

        for preset in presets:

            @preset
            def test_func():
                return "test"

            # Verify decorator didn't break the function
            result = test_func()
            assert result == "test", f"Preset {preset} broke function execution"

    def test_secure_preset_requires_master_key(self):
        """@cache.secure requires master_key parameter."""
        # Verify secure preset exists but requires master_key
        with pytest.raises(ValueError, match="master_key"):

            @cache.secure
            def test_func():
                return "test"
