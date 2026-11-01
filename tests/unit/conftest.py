"""Unit test configuration - no Redis required.

Unit tests are fast, in-memory tests that don't require Redis.
This conftest overrides the root conftest's autouse fixture to
skip Redis setup for pure unit tests.
"""

from typing import Any

import pytest

from cachekit.decorators import orchestrator as orchestrator_module
from cachekit.reliability.circuit_breaker import CircuitBreaker
from cachekit.serializers import standard_serializer

# check_msgpack_structure's error for a document outside its bounds, which unpackb_standard raises as its own.
_GUARD_ERROR_PREFIX = "Unpack failed: MessagePack document "


@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation(request):
    """Override root conftest's Redis isolation for pure unit tests.

    Unit tests don't need Redis - they test in-memory functionality.
    This fixture overrides the parent conftest's autouse fixture
    by having the same name.
    """
    # No-op for unit tests - just yield without Redis setup
    yield


@pytest.fixture
def live_breakers(monkeypatch: pytest.MonkeyPatch) -> list[CircuitBreaker]:
    """Capture every breaker a decorator builds (decorate AFTER requesting this)."""
    captured: list[CircuitBreaker] = []
    real = orchestrator_module.CircuitBreaker

    def spy(*args, **kwargs) -> CircuitBreaker:
        breaker = real(*args, **kwargs)
        captured.append(breaker)
        return breaker

    monkeypatch.setattr(orchestrator_module, "CircuitBreaker", spy)
    return captured


@pytest.fixture
def backend_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mark a backend as configured for tests that stand in for its resolution (_resolve_lazy_backend).

    With nothing configured a plaintext decorator runs L1-only and never resolves a backend, so a test
    that patches the resolver must say a backend is configured, or its stand-in is never asked.
    """
    monkeypatch.setenv("REDIS_URL", "redis://configured.invalid:6379")


@pytest.fixture
def decodes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Each StandardSerializer decode (unpackb_standard, which enforces the structure bounds in the same pass): "ok",
    "refused" when the bounds refused the document (the guard's own error), or the type of anything else it raised."""
    seen: list[str] = []
    real = standard_serializer.unpackb_standard

    def recording(data: Any, limit: int, object_hook: Any) -> Any:
        try:
            value = real(data, limit, object_hook)
        except Exception as e:
            seen.append("refused" if isinstance(e, ValueError) and str(e).startswith(_GUARD_ERROR_PREFIX) else type(e).__name__)
            raise
        seen.append("ok")
        return value

    monkeypatch.setattr(standard_serializer, "unpackb_standard", recording)
    return seen
