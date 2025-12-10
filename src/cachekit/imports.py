"""Optional dependency import utilities.

This module provides utilities for safely importing optional dependencies
with consistent error handling and helpful diagnostics.
"""

import logging
from typing import Optional


class OptionalImport:
    """Handles optional dependency imports with consistent error handling."""

    def __init__(self, module_name: str, install_hint: Optional[str] = None):
        self.module_name = module_name
        self.install_hint = install_hint
        self._module = None
        self._available = None
        self._logger = logging.getLogger(__name__)

    @property
    def available(self) -> bool:
        """Check if the optional module is available."""
        if self._available is None:
            try:
                self._module = __import__(self.module_name)
                self._available = True
            except ImportError:
                self._available = False
                if self.install_hint:
                    self._logger.debug(f"{self.module_name} not available - {self.install_hint}")
        return self._available

    @available.deleter
    def available(self):
        """Reset availability cache for testing."""
        self._available = None
        self._module = None

    @property
    def module(self):
        """Get the imported module or raise ImportError."""
        if self.available:
            return self._module
        raise ImportError(f"{self.module_name} not available")


__all__ = ["OptionalImport"]
