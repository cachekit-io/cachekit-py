"""
CRITICAL PATH TEST: Reliability Profiles

This test MUST pass for profile-based configuration to work correctly.
Tests reliability profile selection, configuration generation, and recommendations.
"""

import pytest

from cachekit.health import HealthLevel
from cachekit.reliability.profiles import (
    PROFILE_CONFIGS,
    ReliabilityProfile,
    balanced_reliability_decorator,
    create_optimized_decorator_config,
    full_reliability_decorator,
    get_decorator_kwargs,
    get_profile_config,
    get_profile_description,
    minimal_reliability_decorator,
    recommend_profile,
)

pytestmark = pytest.mark.critical


class TestReliabilityProfiles:
    """Critical tests for reliability profile system."""

    def test_all_profiles_have_configs(self):
        """CRITICAL: All profile enums have corresponding configurations."""
        for profile in ReliabilityProfile:
            assert profile in PROFILE_CONFIGS, f"Missing config for {profile}"
            config = PROFILE_CONFIGS[profile]
            assert config is not None

    def test_get_profile_config_minimal(self):
        """CRITICAL: MINIMAL profile returns correct configuration."""
        config = get_profile_config(ReliabilityProfile.MINIMAL)

        # Core features
        assert config.circuit_breaker is True, "Circuit breaker should be enabled"
        assert config.adaptive_timeout is False, "Adaptive timeout should be disabled for performance"
        assert config.backpressure is False, "Backpressure should be disabled for performance"

        # Monitoring
        assert config.collect_stats is False, "Stats collection disabled for performance"
        assert config.async_metrics is False, "Async metrics disabled for minimal profile"

        # Performance
        assert config.max_concurrent_requests == 1000, "Higher concurrency limit for minimal profile"
        assert config.log_level_threshold == "WARNING", "Only warnings/errors logged"

    def test_get_profile_config_balanced(self):
        """CRITICAL: BALANCED profile returns correct configuration."""
        config = get_profile_config(ReliabilityProfile.BALANCED)

        # Core features - all enabled
        assert config.circuit_breaker is True
        assert config.adaptive_timeout is True
        assert config.backpressure is True

        # Monitoring - async for performance
        assert config.collect_stats is True
        assert config.async_metrics is True

        # Reasonable defaults
        assert config.max_concurrent_requests == 100
        assert config.health_check_level == HealthLevel.BASIC
        assert config.log_level_threshold == "INFO"

    def test_get_profile_config_full(self):
        """CRITICAL: FULL profile returns correct configuration."""
        config = get_profile_config(ReliabilityProfile.FULL)

        # All features enabled
        assert config.circuit_breaker is True
        assert config.adaptive_timeout is True
        assert config.backpressure is True

        # Full monitoring
        assert config.collect_stats is True
        assert config.async_metrics is True
        assert config.enable_structured_logging is True

        # Comprehensive health checks
        assert config.health_check_level == HealthLevel.FULL
        assert config.backpressure_read_operations is True, "Backpressure on reads for full reliability"
        assert config.log_level_threshold == "DEBUG", "Full logging"

    def test_get_decorator_kwargs_basic(self):
        """CRITICAL: get_decorator_kwargs converts profile to decorator parameters."""
        kwargs = get_decorator_kwargs(ReliabilityProfile.BALANCED)

        assert "circuit_breaker" in kwargs
        assert "adaptive_timeout" in kwargs
        assert "backpressure" in kwargs
        assert "max_concurrent_requests" in kwargs
        assert kwargs["circuit_breaker"] is True
        assert kwargs["adaptive_timeout"] is True

    def test_get_decorator_kwargs_with_overrides(self):
        """CRITICAL: get_decorator_kwargs applies overrides correctly."""
        overrides = {
            "circuit_breaker": False,
            "max_concurrent_requests": 500,
            "custom_param": "test_value",
        }

        kwargs = get_decorator_kwargs(ReliabilityProfile.BALANCED, overrides=overrides)

        assert kwargs["circuit_breaker"] is False, "Override should apply"
        assert kwargs["max_concurrent_requests"] == 500, "Override should apply"
        assert kwargs["custom_param"] == "test_value", "Custom params should pass through"

    def test_create_optimized_decorator_config_default(self):
        """CRITICAL: create_optimized_decorator_config creates balanced config by default."""
        config = create_optimized_decorator_config()

        # Should use balanced profile by default
        assert config["circuit_breaker"] is True
        assert config["adaptive_timeout"] is True
        assert config["backpressure"] is True

        # Optimized components
        assert "_use_async_metrics" in config
        assert "_use_lightweight_health" in config
        assert config["_use_lightweight_health"] is True

    def test_create_optimized_decorator_config_with_profile(self):
        """CRITICAL: create_optimized_decorator_config respects profile selection."""
        config = create_optimized_decorator_config(profile=ReliabilityProfile.MINIMAL)

        assert config["circuit_breaker"] is True
        assert config["adaptive_timeout"] is False, "Minimal profile disables adaptive timeout"
        assert config["backpressure"] is False, "Minimal profile disables backpressure"

    def test_create_optimized_decorator_config_with_overrides(self):
        """CRITICAL: create_optimized_decorator_config applies overrides."""
        config = create_optimized_decorator_config(
            profile=ReliabilityProfile.BALANCED,
            circuit_breaker=False,
            custom_setting="test",
        )

        assert config["circuit_breaker"] is False, "Override should apply"
        assert config["custom_setting"] == "test", "Custom override should apply"

    def test_get_profile_description_all_profiles(self):
        """CRITICAL: All profiles have descriptions."""
        for profile in ReliabilityProfile:
            description = get_profile_description(profile)
            assert isinstance(description, str)
            assert len(description) > 50, f"Description for {profile} should be comprehensive"

    def test_recommend_profile_high_throughput(self):
        """CRITICAL: High throughput (>1000 RPS) recommends MINIMAL or BALANCED."""
        # High throughput, low criticality -> MINIMAL
        profile = recommend_profile(throughput_rps=2000, criticality="low", latency_sensitive=False)
        assert profile == ReliabilityProfile.MINIMAL

        # High throughput, medium/high criticality -> BALANCED
        profile = recommend_profile(throughput_rps=2000, criticality="medium", latency_sensitive=False)
        assert profile == ReliabilityProfile.BALANCED

        profile = recommend_profile(throughput_rps=2000, criticality="high", latency_sensitive=False)
        assert profile == ReliabilityProfile.BALANCED

    def test_recommend_profile_low_throughput_high_criticality(self):
        """CRITICAL: Low throughput + high criticality recommends FULL."""
        profile = recommend_profile(throughput_rps=50, criticality="high", latency_sensitive=False)
        assert profile == ReliabilityProfile.FULL

    def test_recommend_profile_latency_sensitive(self):
        """CRITICAL: Latency-sensitive applications get performance profiles."""
        # Latency sensitive, low criticality -> MINIMAL
        profile = recommend_profile(throughput_rps=500, criticality="low", latency_sensitive=True)
        assert profile == ReliabilityProfile.MINIMAL

        # Latency sensitive, high criticality -> BALANCED (compromise)
        profile = recommend_profile(throughput_rps=500, criticality="high", latency_sensitive=True)
        assert profile == ReliabilityProfile.BALANCED

    def test_recommend_profile_default_cases(self):
        """CRITICAL: Default cases recommend BALANCED."""
        # Medium throughput, medium criticality -> BALANCED
        profile = recommend_profile(throughput_rps=500, criticality="medium", latency_sensitive=False)
        assert profile == ReliabilityProfile.BALANCED

        # No specific conditions -> BALANCED
        profile = recommend_profile(throughput_rps=200, criticality="low", latency_sensitive=False)
        assert profile == ReliabilityProfile.BALANCED

    def test_convenience_functions_minimal(self):
        """CRITICAL: minimal_reliability_decorator returns correct config."""
        config = minimal_reliability_decorator()

        assert config["circuit_breaker"] is True
        assert config["adaptive_timeout"] is False
        assert config["backpressure"] is False

    def test_convenience_functions_balanced(self):
        """CRITICAL: balanced_reliability_decorator returns correct config."""
        config = balanced_reliability_decorator()

        assert config["circuit_breaker"] is True
        assert config["adaptive_timeout"] is True
        assert config["backpressure"] is True

    def test_convenience_functions_full(self):
        """CRITICAL: full_reliability_decorator returns correct config."""
        config = full_reliability_decorator()

        assert config["circuit_breaker"] is True
        assert config["adaptive_timeout"] is True
        assert config["backpressure"] is True
        assert config["_health_check_level"] == HealthLevel.FULL.value

    def test_convenience_functions_with_overrides(self):
        """CRITICAL: Convenience functions accept overrides."""
        config = minimal_reliability_decorator(circuit_breaker=False, custom="value")

        assert config["circuit_breaker"] is False, "Override should apply"
        assert config["custom"] == "value", "Custom param should apply"
