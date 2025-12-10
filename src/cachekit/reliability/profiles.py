"""Reliability profiles for different performance/feature trade-offs.

This module provides pre-configured reliability profiles that balance
performance with different levels of reliability features.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from cachekit.health import HealthLevel

# Profile selection thresholds
HIGH_THROUGHPUT_THRESHOLD_RPS = 1000  # Requests per second threshold for high throughput
LOW_THROUGHPUT_THRESHOLD_RPS = 100  # Requests per second threshold for low throughput


class ReliabilityProfile(Enum):
    """Pre-defined reliability profiles.

    Examples:
        >>> ReliabilityProfile.MINIMAL.value
        'minimal'
        >>> ReliabilityProfile.BALANCED.value
        'balanced'
        >>> ReliabilityProfile.FULL.value
        'full'
    """

    MINIMAL = "minimal"  # Maximum performance, minimal features
    BALANCED = "balanced"  # Good balance of performance and reliability
    FULL = "full"  # All features enabled, maximum reliability


@dataclass
class ProfileConfig:
    """Configuration for a reliability profile.

    Examples:
        Create with defaults (balanced):

        >>> config = ProfileConfig()
        >>> config.circuit_breaker
        True
        >>> config.adaptive_timeout
        True

        Access health check settings:

        >>> config.health_cache_duration
        5.0
    """

    # Core reliability features
    circuit_breaker: bool = True
    adaptive_timeout: bool = True
    backpressure: bool = True

    # Monitoring and observability
    collect_stats: bool = True
    async_metrics: bool = True
    enable_structured_logging: bool = True

    # Health monitoring
    health_check_level: HealthLevel = HealthLevel.PING
    health_cache_duration: float = 5.0

    # Performance tuning
    max_concurrent_requests: int = 100
    metrics_batch_size: int = 100
    metrics_flush_interval: float = 0.1

    # Security features
    mask_sensitive_data: bool = True

    # Backpressure settings
    backpressure_read_operations: bool = False  # Skip backpressure for reads
    backpressure_timeout: float = 0.1  # Shorter timeout for cache ops

    # Logging optimization
    lazy_pii_masking: bool = True
    log_level_threshold: str = "INFO"


# Pre-defined profile configurations
PROFILE_CONFIGS: dict[ReliabilityProfile, ProfileConfig] = {
    ReliabilityProfile.MINIMAL: ProfileConfig(
        # Core features - only circuit breaker enabled
        circuit_breaker=True,
        adaptive_timeout=False,  # Disabled for maximum performance
        backpressure=False,  # Disabled for maximum performance
        # Minimal monitoring
        collect_stats=False,
        async_metrics=False,
        enable_structured_logging=False,
        # Basic health checks only
        health_check_level=HealthLevel.PING,
        health_cache_duration=1.0,  # Cache longer for performance
        # Performance optimized
        max_concurrent_requests=1000,  # Higher limit
        # Security still enabled
        mask_sensitive_data=True,
        # Minimal logging
        lazy_pii_masking=True,
        log_level_threshold="WARNING",  # Only log warnings and errors
    ),
    ReliabilityProfile.BALANCED: ProfileConfig(
        # Core features enabled
        circuit_breaker=True,
        adaptive_timeout=True,
        backpressure=True,
        # Async monitoring for performance
        collect_stats=True,
        async_metrics=True,  # Async for performance
        enable_structured_logging=True,
        # Basic health checks
        health_check_level=HealthLevel.BASIC,
        health_cache_duration=5.0,
        # Reasonable limits
        max_concurrent_requests=100,
        metrics_batch_size=100,
        metrics_flush_interval=0.1,
        # Security enabled
        mask_sensitive_data=True,
        # Optimized backpressure
        backpressure_read_operations=False,  # Skip reads
        backpressure_timeout=0.1,
        # Optimized logging
        lazy_pii_masking=True,
        log_level_threshold="INFO",
    ),
    ReliabilityProfile.FULL: ProfileConfig(
        # All features enabled
        circuit_breaker=True,
        adaptive_timeout=True,
        backpressure=True,
        # Full monitoring
        collect_stats=True,
        async_metrics=True,
        enable_structured_logging=True,
        # Comprehensive health checks
        health_check_level=HealthLevel.FULL,
        health_cache_duration=30.0,
        # Conservative limits for reliability
        max_concurrent_requests=50,
        metrics_batch_size=50,  # Smaller batches for lower latency
        metrics_flush_interval=0.05,  # More frequent flushes
        # Full security
        mask_sensitive_data=True,
        # Full backpressure (including reads)
        backpressure_read_operations=True,
        backpressure_timeout=0.5,  # Longer timeout for reliability
        # Full logging
        lazy_pii_masking=True,
        log_level_threshold="DEBUG",
    ),
}


def get_profile_config(profile: ReliabilityProfile) -> ProfileConfig:
    """Get configuration for a reliability profile.

    Args:
        profile: The reliability profile to get config for

    Returns:
        Profile configuration

    Examples:
        >>> config = get_profile_config(ReliabilityProfile.MINIMAL)
        >>> config.adaptive_timeout
        False
        >>> config.collect_stats
        False

        >>> balanced = get_profile_config(ReliabilityProfile.BALANCED)
        >>> balanced.circuit_breaker
        True
        >>> balanced.async_metrics
        True
    """
    return PROFILE_CONFIGS[profile]


def get_decorator_kwargs(profile: ReliabilityProfile, overrides: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Get decorator kwargs for a reliability profile.

    Args:
        profile: The reliability profile
        overrides: Optional parameter overrides

    Returns:
        Dictionary of decorator parameters
    """
    config = get_profile_config(profile)

    # Convert profile config to decorator parameters
    kwargs = {
        # Core reliability features
        "circuit_breaker": config.circuit_breaker,
        "adaptive_timeout": config.adaptive_timeout,
        "backpressure": config.backpressure,
        "max_concurrent_requests": config.max_concurrent_requests,
        # Monitoring features
        "collect_stats": config.collect_stats,
        "enable_tracing": config.async_metrics,  # Map to tracing for now
        "enable_structured_logging": config.enable_structured_logging,
    }

    # Apply any overrides
    if overrides:
        kwargs.update(overrides)

    return kwargs


def create_optimized_decorator_config(profile: ReliabilityProfile = ReliabilityProfile.BALANCED, **overrides) -> dict[str, Any]:
    """Create optimized decorator configuration.

    This function creates a decorator configuration that uses the optimized
    reliability components (async metrics, lightweight health checks, etc.)

    Args:
        profile: Base reliability profile
        **overrides: Parameter overrides

    Returns:
        Optimized decorator configuration
    """
    config = get_profile_config(profile)

    # Build configuration with optimized components
    decorator_config = {
        # Core reliability (already optimized)
        "circuit_breaker": config.circuit_breaker,
        "adaptive_timeout": config.adaptive_timeout,
        "backpressure": config.backpressure,
        "max_concurrent_requests": config.max_concurrent_requests,
        # Use optimized monitoring
        "collect_stats": config.collect_stats,
        "enable_tracing": config.async_metrics,
        "enable_structured_logging": config.enable_structured_logging,
        # Performance optimizations
        "_use_async_metrics": config.async_metrics,
        "_use_lightweight_health": True,
        "_health_check_level": config.health_check_level.value,
        "_lazy_pii_masking": config.lazy_pii_masking,
        "_backpressure_read_ops": config.backpressure_read_operations,
        "_metrics_batch_size": config.metrics_batch_size,
        "_metrics_flush_interval": config.metrics_flush_interval,
    }

    # Apply overrides
    decorator_config.update(overrides)

    return decorator_config


def get_profile_description(profile: ReliabilityProfile) -> str:
    """Get human-readable description of a profile.

    Args:
        profile: The reliability profile

    Returns:
        Description string
    """
    descriptions = {
        ReliabilityProfile.MINIMAL: (
            "Maximum performance profile with minimal reliability features. "
            "Only circuit breaker enabled. Best for high-throughput, low-latency scenarios "
            "where some data loss is acceptable."
        ),
        ReliabilityProfile.BALANCED: (
            "Balanced performance and reliability profile. Core reliability features "
            "enabled with async monitoring for minimal overhead. Recommended for "
            "most production use cases."
        ),
        ReliabilityProfile.FULL: (
            "Maximum reliability profile with all features enabled. Comprehensive "
            "monitoring, health checks, and safety features. Best for critical "
            "applications where reliability is more important than performance."
        ),
    }
    return descriptions[profile]


def recommend_profile(throughput_rps: int, criticality: str = "medium", latency_sensitive: bool = False) -> ReliabilityProfile:
    """Recommend a reliability profile based on requirements.

    Args:
        throughput_rps: Expected requests per second
        criticality: Application criticality ("low", "medium", "high")
        latency_sensitive: Whether application is latency sensitive

    Returns:
        Recommended reliability profile

    Examples:
        High throughput with low criticality -> MINIMAL:

        >>> recommend_profile(2000, criticality="low")
        <ReliabilityProfile.MINIMAL: 'minimal'>

        Low throughput with high criticality -> FULL:

        >>> recommend_profile(50, criticality="high")
        <ReliabilityProfile.FULL: 'full'>

        Most use cases -> BALANCED:

        >>> recommend_profile(500, criticality="medium")
        <ReliabilityProfile.BALANCED: 'balanced'>

        Latency sensitive -> at most BALANCED:

        >>> recommend_profile(500, latency_sensitive=True)
        <ReliabilityProfile.BALANCED: 'balanced'>
    """
    # High throughput or latency sensitive -> minimal
    if throughput_rps > HIGH_THROUGHPUT_THRESHOLD_RPS or latency_sensitive:
        if criticality == "low":
            return ReliabilityProfile.MINIMAL
        else:
            return ReliabilityProfile.BALANCED

    # Low throughput with high criticality -> full features
    if throughput_rps < LOW_THROUGHPUT_THRESHOLD_RPS and criticality == "high":
        return ReliabilityProfile.FULL

    # Default to balanced for most use cases
    return ReliabilityProfile.BALANCED


# Convenience functions for common patterns
def minimal_reliability_decorator(**overrides):
    """Get minimal reliability decorator configuration."""
    return create_optimized_decorator_config(ReliabilityProfile.MINIMAL, **overrides)


def balanced_reliability_decorator(**overrides):
    """Get balanced reliability decorator configuration."""
    return create_optimized_decorator_config(ReliabilityProfile.BALANCED, **overrides)


def full_reliability_decorator(**overrides):
    """Get full reliability decorator configuration."""
    return create_optimized_decorator_config(ReliabilityProfile.FULL, **overrides)
