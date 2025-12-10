"""Modular decorator architecture for cachekit.

This package contains the refactored decorator implementation split into
focused, single-responsibility modules following SOLID principles.
"""

# Import the main decorator functions for backward compatibility
from ..config import DecoratorConfig
from .main import DecoratorFeatures, FeatureOrchestrator, cache

__all__ = [
    "DecoratorConfig",
    "DecoratorFeatures",
    "FeatureOrchestrator",
    "cache",
]
