"""Modular decorator architecture for cachekit.

This package contains the refactored decorator implementation split into
focused, single-responsibility modules following SOLID principles.
"""

# Import the main decorator functions
from ..config import DecoratorConfig
from .main import FeatureOrchestrator, cache

__all__ = [
    "DecoratorConfig",
    "FeatureOrchestrator",
    "cache",
]
