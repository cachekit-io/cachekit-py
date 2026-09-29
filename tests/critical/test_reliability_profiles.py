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
    get_profile_config,
    get_profile_description,
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
        assert config.backpressure is True

        # Full monitoring
        assert config.collect_stats is True
        assert config.async_metrics is True
        assert config.enable_structured_logging is True

        # Comprehensive health checks
        assert config.health_check_level == HealthLevel.FULL
        assert config.backpressure_read_operations is True, "Backpressure on reads for full reliability"
        assert config.log_level_threshold == "DEBUG", "Full logging"

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
