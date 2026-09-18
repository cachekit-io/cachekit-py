"""Sync L2-hit ``size_bytes`` measures the served envelope (LAB-3768).

The sync L1 hit site records ``len(l1_bytes)``; the sync L2 site recorded
``len(str(value).encode())``, a repr estimate of the deserialized value, because the sync
handler returned no envelope. A secure cache (ciphertext envelope vs plaintext repr) got a
bimodal histogram on one series.
"""

from __future__ import annotations

from typing import Any

import pytest

from cachekit import cache
from cachekit.decorators.orchestrator import FeatureOrchestrator


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


@pytest.mark.unit
def test_sync_l2_hit_records_envelope_size(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patched at the orchestrator, not the collector: record_success() also forwards an
    # operation-context record to the collector, which would shadow the value under test.
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(FeatureOrchestrator, "record_cache_operation", lambda self, **kw: recorded.append(kw))
    backend = _ByteStore()

    @cache(backend=backend, l1_enabled=False, ttl=60, namespace="sync-l2-size")
    def compute() -> dict[str, int]:
        return {"answer": 42}

    assert compute() == {"answer": 42}  # miss: primes L2
    (envelope,) = backend.store.values()
    recorded.clear()

    assert compute() == {"answer": 42}

    gets = [c for c in recorded if c["operation"] == "get"]
    assert len(gets) == 1
    assert gets[0]["size_bytes"] == len(envelope)
    assert len(envelope) != len(str({"answer": 42}).encode())  # non-vacuous: the old repr estimate differs
