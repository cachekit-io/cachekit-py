from __future__ import annotations

from .intent import cache
from .orchestrator import FeatureOrchestrator

# Backward compatibility exports - tests import these
DecoratorFeatures = FeatureOrchestrator

# Export the intelligent cache interface
__all__ = ["DecoratorFeatures", "FeatureOrchestrator", "cache"]
