"""Sync L2-hit ``size_bytes`` measures the served envelope (LAB-3768).

Every other hit site (sync L1, async L1, async L2) records ``len(envelope)``; the sync L2
site recorded ``len(str(value).encode())``, a repr estimate of the deserialized value,
because the sync handler returned no envelope. One process mixing sync and async callers,
or a secure cache (ciphertext envelope vs plaintext repr), got a bimodal histogram on one
series. A miss primes L2 (and L1 via the miss-store); clearing L1 forces the second call
past L1 to the L2 site.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from cachekit import cache
from cachekit.decorators.orchestrator import FeatureOrchestrator
from cachekit.l1_cache import get_l1_cache_manager


class _ByteStore:
    """Plain in-memory byte store standing in for L2."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        self.store[key] = value

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {}


@pytest.fixture(autouse=True)
def _clear_l1() -> Iterator[None]:
    """tests/unit/conftest.py's no-op override of the root fixture dropped its clear_all()."""
    yield
    get_l1_cache_manager().clear_all()


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the wrapper's explicit record_cache_operation(...) calls, keyword args as passed.

    Patched at the orchestrator, not the collector: record_success() also forwards an
    operation-context record to the collector, which would shadow the value under test.
    """
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(FeatureOrchestrator, "record_cache_operation", lambda self, **kw: calls.append(kw))
    return calls


@pytest.mark.unit
def test_sync_l2_hit_records_envelope_size(recorded: list[dict[str, Any]]) -> None:
    backend = _ByteStore()

    @cache(backend=backend, ttl=60, namespace="sync-l2-size")
    def compute() -> dict[str, int]:
        return {"answer": 42}

    assert compute() == {"answer": 42}  # miss: primes L2, and L1 via the miss-store
    (envelope,) = backend.store.values()
    get_l1_cache_manager().clear_all()  # force the hit past L1 to the L2 site
    recorded.clear()

    assert compute() == {"answer": 42}

    gets = [c for c in recorded if c["operation"] == "get"]
    assert len(gets) == 1
    assert gets[0]["size_bytes"] == len(envelope)
    assert len(envelope) != len(str({"answer": 42}).encode())  # non-vacuous: the old repr estimate differs
