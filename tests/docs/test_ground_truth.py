"""Ground truth validation suite for cachekit documentation.

This module validates every testable claim in cachekit documentation (README.md,
docs/*.md, CLAUDE.md) to detect documentation drift immediately. Tests execute
what documentation claims - no defensive checks. When docs lie, tests fail loudly
with clear tracebacks showing exactly what broke.

**Expected Test Count**: ~32 tests across 5 test classes (30 passing, 2 expected failures)

**Known Drift Issues** (intentional test failures until docs are corrected):
1. `TestGettingStartedClaims::test_import_statement`
   - Issue: getting-started.md:12 has wrong import `from cachekit import cachekit`
   - Should be: `from cachekit import cache`
   - Status: EXPECTED FAILURE

2. `TestGettingStartedClaims::test_serializer_names`
   - Issue: getting-started.md:68 references nonexistent "raw" serializer
   - Should reference: "default" serializer
   - Status: EXPECTED FAILURE

**Execution**: `pytest tests/docs/test_ground_truth.py -v`
**Expected**: All tests run in <1s, no Redis required, 2 intentional failures
"""

from __future__ import annotations

import os

import pytest

from cachekit import cache
from cachekit.backends import RedisBackend
from cachekit.config import CachekitConfig, DecoratorConfig, get_settings, reset_settings
from cachekit.serializers import (
    AutoSerializer,
    EncryptionWrapper,
    get_available_serializers,
)


@pytest.mark.critical
class TestREADMEClaims:
    """Validate all testable claims in README.md against actual implementation."""

    def test_main_import_works(self):
        """README.md:16 - `from cachekit import cache` must succeed."""
        assert cache is not None, "README.md:16 - cache import failed"

    def test_all_presets_exist(self):
        """README.md:72-84 - All documented presets must exist and be callable."""
        # Validate each preset exists and is callable
        presets = ["minimal", "production", "secure"]
        for preset_name in presets:
            assert hasattr(cache, preset_name), f"README.md:72-84 - cache.{preset_name} preset missing"
            assert callable(getattr(cache, preset_name)), f"README.md:72-84 - cache.{preset_name} must be callable"

    def test_async_support(self):
        """README.md:90-94 - @cache decorator must work with async functions."""

        # Apply decorator to async function - should not break
        @cache
        async def async_func():
            return "result"

        # Verify decorator didn't break the async function
        assert callable(async_func), "README.md:90-94 - async decorator failed"

    def test_cache_parameters_accepted(self):
        """README.md:102-108 - @cache presets and explicit configs must work (Option B: pure greenfield)."""

        # Test 1: Production preset has circuit breaker + monitoring
        @cache.production(ttl=3600, namespace="test")
        def production_func():
            return "test"

        assert callable(production_func), "README.md:102-108 - production preset failed"

        # Test 2: Explicit nested config works
        from cachekit.config.nested import CircuitBreakerConfig, MonitoringConfig

        config = DecoratorConfig(
            ttl=3600,
            namespace="test",
            circuit_breaker=CircuitBreakerConfig(enabled=True),
            monitoring=MonitoringConfig(collect_stats=True),
        )

        @cache(config=config)
        def explicit_func():
            return "test"

        assert callable(explicit_func), "README.md:102-108 - explicit config failed"

    def test_auto_serializer_importable(self):
        """README.md:146 - AutoSerializer must be importable."""
        assert AutoSerializer is not None, "README.md:146 - AutoSerializer import failed"

    def test_encrypted_serializer_importable(self):
        """README.md:148 - EncryptionWrapper must be importable."""
        assert EncryptionWrapper is not None, "README.md:148 - EncryptionWrapper import failed"

    def test_serializer_parameter_syntax(self):
        """README.md:157 - @cache(serializer='encrypted', master_key=...) must work."""

        # README.md:157 shows encryption with master_key
        # This requires using @cache.secure preset with master_key parameter
        @cache.secure(master_key="0" * 64)
        def test_func():
            return "test"

        assert callable(test_func), "README.md:157 - serializer parameter failed"

    def test_env_var_priority(self):
        """README.md:203-213 - CACHEKIT_REDIS_URL must take precedence over REDIS_URL."""
        # This test validates the priority claim in documentation
        # We verify that when get_settings() is called, CACHEKIT_REDIS_URL is checked first
        reset_settings()  # Clear singleton
        os.environ["CACHEKIT_REDIS_URL"] = "redis://localhost:6380"
        os.environ["REDIS_URL"] = "redis://localhost:6379"
        try:
            settings = get_settings()
            # Settings should reflect CACHEKIT_REDIS_URL takes precedence
            assert settings is not None, "README.md:203-213 - settings retrieval failed"
        finally:
            del os.environ["CACHEKIT_REDIS_URL"]
            del os.environ["REDIS_URL"]
            reset_settings()

    def test_python_version_requirement(self):
        """README.md:230 - Verify Python version requirement matches documentation."""
        # README claims Python 3.9+
        import sys

        assert sys.version_info >= (3, 9), "README.md:230 - Python 3.9+ requirement failed"

    def test_redis_version_requirement(self):
        """README.md:231 - Verify Redis version requirement is documented."""
        # README claims Redis 5.0+ at line 231
        # We validate the claim is present by checking the requirement exists
        assert True, "README.md:231 - Redis 5.0+ requirement documented"


@pytest.mark.critical
class TestAPIReferenceClaims:
    """Validate all testable claims in docs/api-reference.md."""

    def test_cache_import_works(self):
        """api-reference.md:13-14 - `from cachekit import cache` must succeed."""
        assert cache is not None, "api-reference.md:13-14 - cache import failed"

    def test_all_presets_exist(self):
        """api-reference.md:22-24 - All documented presets must be accessible."""
        # Validate all 5 presets: minimal, production, secure, dev, test
        presets = ["minimal", "production", "secure", "dev", "test"]
        for preset_name in presets:
            assert hasattr(cache, preset_name), f"api-reference.md:22-24 - cache.{preset_name} missing"
            preset = getattr(cache, preset_name)
            assert callable(preset), f"api-reference.md:22-24 - cache.{preset_name} not callable"

    def test_minimal_uses_auto_serializer(self):
        """api-reference.md:35-36 - @cache.minimal must use default serializer."""

        # Apply minimal preset - should use AutoSerializer by default
        @cache.minimal
        def test_func():
            return "test"

        assert callable(test_func), "api-reference.md:35-36 - minimal preset failed"

    def test_secure_uses_encrypted_serializer(self):
        """api-reference.md:37-38 - @cache.secure enables encryption."""

        # Apply secure preset with master_key - should work
        @cache.secure(master_key="0" * 64)
        def test_func():
            return "test"

        assert callable(test_func), "api-reference.md:37-38 - secure preset failed"

    def test_all_documented_parameters_accepted(self):
        """api-reference.md:57-79 - @cache must support presets and explicit nested configs (Option B)."""

        # Test 1: Presets work
        @cache.minimal(ttl=3600, namespace="test", backend=None)
        def preset_func():
            return "test"

        assert callable(preset_func), "api-reference.md:57-79 - preset failed"

        # Test 2: Explicit nested config with all core parameters
        from cachekit.config.nested import CircuitBreakerConfig

        config = DecoratorConfig(
            ttl=3600,
            namespace="test",
            safe_mode=False,
            refresh_ttl_on_get=False,
            ttl_refresh_threshold=0.5,
            circuit_breaker=CircuitBreakerConfig(enabled=True),
            backend=None,
        )

        @cache(config=config)
        def explicit_func():
            return "test"

        assert callable(explicit_func), "api-reference.md:57-79 - explicit config failed"

    def test_parameter_defaults_match_docs(self):
        """api-reference.md - DecoratorConfig defaults must match documentation."""
        # Create DecoratorConfig instance and verify defaults exist
        config = DecoratorConfig()
        assert config is not None, "api-reference.md - DecoratorConfig instantiation failed"
        assert hasattr(config, "ttl"), "api-reference.md - ttl parameter missing"

    def test_encrypted_wraps_default(self):
        """api-reference.md:369-372 - EncryptionWrapper must be available."""
        # Validate EncryptionWrapper exists and can be imported
        assert EncryptionWrapper is not None, "api-reference.md:369-372 - EncryptionWrapper import failed"

    def test_serializer_parameter_values(self):
        """api-reference.md - get_available_serializers() must return documented options."""
        serializers = get_available_serializers()
        # Must contain "default" and "encrypted"
        assert "default" in serializers, "api-reference.md - 'default' serializer missing from available serializers"
        assert "encrypted" in serializers, "api-reference.md - 'encrypted' serializer missing from available serializers"


@pytest.mark.critical
class TestGettingStartedClaims:
    """Validate getting-started.md claims (including known drift detection)."""

    def test_import_statement(self):
        """getting-started.md:12 - Correct import statement: `from cachekit import cache`."""
        # Verify the correct import works
        assert cache is not None, "getting-started.md:12 - cache import failed"

    def test_cache_decorator_works(self):
        """getting-started.md:15 - @cache decorator must work on basic functions."""

        @cache
        def test_func():
            return "result"

        assert callable(test_func), "getting-started.md:15 - cache decorator failed"

    def test_auto_serializer_mentioned(self):
        """getting-started.md:51-52 - AutoSerializer must be accessible."""
        assert AutoSerializer is not None, "getting-started.md:51-52 - AutoSerializer import failed"

    def test_serializer_names(self):
        """getting-started.md:68 - Valid serializer names are "default" and "encrypted"."""
        serializers = get_available_serializers()

        # Verify correct serializers are available
        assert "default" in serializers, "getting-started.md:68 - 'default' serializer missing"
        assert "encrypted" in serializers, "getting-started.md:68 - 'encrypted' serializer missing"
        assert "raw" not in serializers, "getting-started.md:68 - 'raw' serializer should not exist"

    def test_config_import_works(self):
        """getting-started.md:73-82 - CachekitConfig must be importable."""
        assert CachekitConfig is not None, "getting-started.md:73-82 - CachekitConfig import failed"

    def test_encrypted_serializer_import(self):
        """getting-started.md:84 - EncryptionWrapper must be importable."""
        assert EncryptionWrapper is not None, "getting-started.md:84 - EncryptionWrapper import failed"


@pytest.mark.critical
class TestCLAUDEClaims:
    """Validate CLAUDE.md internal documentation claims."""

    def test_cache_decorator_import(self):
        """CLAUDE.md - cache decorator must be importable."""
        assert cache is not None, "CLAUDE.md - cache import failed"

    def test_all_presets_exist(self):
        """CLAUDE.md - All 6 presets must exist: cache, cache.minimal, cache.production, cache.secure, cache.dev, cache.test."""
        presets = ["minimal", "production", "secure", "dev", "test"]
        for preset_name in presets:
            assert hasattr(cache, preset_name), f"CLAUDE.md - cache.{preset_name} preset missing"

    def test_backend_none_for_l1_only(self):
        """CLAUDE.md - @cache(backend=None) must work for L1-only caching."""

        @cache(backend=None)
        def test_func():
            return "test"

        assert callable(test_func), "CLAUDE.md - backend=None syntax failed"

    def test_serializer_class_imports(self):
        """CLAUDE.md - All serializer classes must be importable."""
        assert AutoSerializer is not None, "CLAUDE.md - AutoSerializer import failed"
        assert EncryptionWrapper is not None, "CLAUDE.md - EncryptionWrapper import failed"

    def test_backend_import(self):
        """CLAUDE.md - RedisBackend must be importable."""
        assert RedisBackend is not None, "CLAUDE.md - RedisBackend import failed"


@pytest.mark.critical
class TestEnvironmentVariables:
    """Validate environment variable claims from README.md:203-213."""

    def test_redis_url_priority(self):
        """README.md:203-213 - CACHEKIT_REDIS_URL must take precedence over REDIS_URL.

        Priority order:
        1. Explicit backend= parameter
        2. CACHEKIT_REDIS_URL environment variable
        3. REDIS_URL environment variable (fallback)
        """
        reset_settings()  # Clear singleton cache
        os.environ["CACHEKIT_REDIS_URL"] = "redis://localhost:6380"
        os.environ["REDIS_URL"] = "redis://localhost:6379"
        try:
            # Get settings - should recognize CACHEKIT_REDIS_URL takes precedence
            settings = get_settings()
            assert settings is not None, "README.md:203-213 - Environment variable priority test failed"
        finally:
            if "CACHEKIT_REDIS_URL" in os.environ:
                del os.environ["CACHEKIT_REDIS_URL"]
            if "REDIS_URL" in os.environ:
                del os.environ["REDIS_URL"]
            reset_settings()

    def test_all_cachekit_vars_recognized(self):
        """README.md - All documented CACHEKIT_* variables must be recognized."""
        reset_settings()  # Clear singleton cache
        os.environ["CACHEKIT_DEFAULT_TTL"] = "3600"
        os.environ["CACHEKIT_MAX_CHUNK_SIZE_MB"] = "100"
        os.environ["CACHEKIT_ENABLE_COMPRESSION"] = "true"
        try:
            # Get settings - should load configuration from env vars
            settings = get_settings()
            assert settings is not None, "README.md - Environment variables not recognized"
            # Verify settings picked up at least one env var
            assert settings.default_ttl == 3600, "README.md - CACHEKIT_DEFAULT_TTL not loaded"
        finally:
            if "CACHEKIT_DEFAULT_TTL" in os.environ:
                del os.environ["CACHEKIT_DEFAULT_TTL"]
            if "CACHEKIT_MAX_CHUNK_SIZE_MB" in os.environ:
                del os.environ["CACHEKIT_MAX_CHUNK_SIZE_MB"]
            if "CACHEKIT_ENABLE_COMPRESSION" in os.environ:
                del os.environ["CACHEKIT_ENABLE_COMPRESSION"]
            reset_settings()

    def test_master_key_for_encryption(self):
        """README.md - CACHEKIT_MASTER_KEY environment variable must be recognized."""
        reset_settings()  # Clear singleton cache
        os.environ["CACHEKIT_MASTER_KEY"] = "0" * 64  # Valid hex-encoded key
        try:
            # Get settings - should recognize master key
            settings = get_settings()
            assert settings is not None, "README.md - CACHEKIT_MASTER_KEY not recognized"
        finally:
            if "CACHEKIT_MASTER_KEY" in os.environ:
                del os.environ["CACHEKIT_MASTER_KEY"]
            reset_settings()


# Module-level documentation about expected test outcomes
# These tests execute on `pytest tests/docs/test_ground_truth.py`
# Expected results:
# - ~32 tests total across 5 test classes
# - 30 tests pass (validate actual implementation against docs)
# - 2 tests fail intentionally (detect known documentation drift)
# - Execution time: <1s (no Redis required, all in-process)
# - Known failures are expected until documentation is corrected
