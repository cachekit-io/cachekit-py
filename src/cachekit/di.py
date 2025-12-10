"""Dependency injection container following SOLID principles.

This module provides a simple yet powerful DI container for managing
service registration and resolution throughout the cachekit library.
"""


class DIContainer:
    """Simple dependency injection container following SOLID principles."""

    _instance = None

    def __new__(cls):
        """Ensure DIContainer is a singleton."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """Initialize container only once."""
        if self._initialized:
            return
        self._services = {}
        self._singletons = {}
        self._initialized = True

    def register(self, interface: type, implementation: type, singleton: bool = True):
        """Register a service implementation for an interface."""
        self._services[interface] = {
            "implementation": implementation,
            "singleton": singleton,
        }

    def get(self, interface: type):
        """Get an instance of the registered service."""
        if interface not in self._services:
            raise ValueError(f"Service {interface.__name__} not registered")

        service_config = self._services[interface]
        implementation = service_config["implementation"]

        if service_config["singleton"]:
            if interface not in self._singletons:
                self._singletons[interface] = implementation()
            return self._singletons[interface]
        else:
            return implementation()

    def clear_singletons(self):
        """Clear singleton instances (useful for testing)."""
        self._singletons.clear()


__all__ = ["DIContainer"]
