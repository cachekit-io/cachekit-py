"""Sync/async L2-hit parity (cachekit-py#164, LAB-348).

An L2 hit must (a) backfill L1 so the next read of the same key never
re-pays the L2 round-trip, and (b) record ``size_bytes`` as the serialized
envelope's length, not ``len(str(value))`` — a repr is neither the payload
size nor cheap to build on every hit. The async wrapper always did (a); the
sync wrapper did neither. Both are pinned here through the real decorator
stack against a plain (non-SWR, non-locking) byte store.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from cachekit import cache
from cachekit.reliability.async_metrics import AsyncMetricsCollector

VALUE = {"answer": 42}


class _CountingBackend:
    """Plain byte store that counts L2 reads."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.gets = 0

    def get(self, key: str) -> bytes | None:
        self.gets += 1
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        self.store[key] = value

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {}


class _BytearrayBackend(_CountingBackend):
    """Out-of-contract backend: a bytearray envelope deserializes fine but L1Cache.put refuses it."""

    def get(self, key: str) -> bytearray | None:  # type: ignore[override]
        raw = super().get(key)
        return None if raw is None else bytearray(raw)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every features.record_cache_operation(...) call (all keyword args)."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(AsyncMetricsCollector, "record_cache_operation", lambda self, **kw: calls.append(kw))
    return calls


def _force_l2_only(backend: _CountingBackend, l2_snapshot: dict[str, bytes]) -> None:
    """After invalidate_cache() wiped L1 + L2, restore L2 alone and zero the read counter."""
    backend.store.update(l2_snapshot)
    backend.gets = 0


def _assert_parity(backend: _CountingBackend, recorded: list[dict[str, Any]], info: Any) -> None:
    envelope = next(iter(backend.store.values()))
    get_hits = [c for c in recorded if c["operation"] == "get" and c.get("hit")]
    assert len(get_hits) == 2  # one L2 hit, one L1 hit
    assert [c["serializer"] for c in get_hits] == ["rust", "l1_memory"]
    assert [c["size_bytes"] for c in get_hits] == [len(envelope)] * 2
    assert len(envelope) != len(str(VALUE).encode("utf-8"))  # the old str(value) accounting would differ
    assert backend.gets == 1  # second read never reached L2
    assert (info.l1_hits, info.l2_hits) == (1, 1)


@pytest.mark.unit
class TestL2HitParity:
    def test_sync_l2_hit_backfills_l1_and_records_envelope_size(self, recorded: list[dict[str, Any]]) -> None:
        backend = _CountingBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, namespace="l2-parity-sync")
        def compute() -> dict[str, int]:
            calls["n"] += 1
            return dict(VALUE)

        assert compute() == VALUE  # miss -> L2 + L1 store
        l2_snapshot = dict(backend.store)
        compute.invalidate_cache()  # type: ignore[attr-defined]
        _force_l2_only(backend, l2_snapshot)
        recorded.clear()

        assert compute() == VALUE  # L1 miss -> L2 hit -> backfill L1
        assert compute() == VALUE  # served from L1
        assert calls["n"] == 1
        _assert_parity(backend, recorded, compute.cache_info())  # type: ignore[attr-defined]

    async def test_async_l2_hit_backfills_l1_and_records_envelope_size(self, recorded: list[dict[str, Any]]) -> None:
        backend = _CountingBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, namespace="l2-parity-async")
        async def compute() -> dict[str, int]:
            calls["n"] += 1
            return dict(VALUE)

        assert await compute() == VALUE
        l2_snapshot = dict(backend.store)
        await compute.invalidate_cache()  # type: ignore[attr-defined]
        _force_l2_only(backend, l2_snapshot)
        recorded.clear()

        assert await compute() == VALUE
        assert await compute() == VALUE
        assert calls["n"] == 1
        _assert_parity(backend, recorded, compute.cache_info())  # type: ignore[attr-defined]

    def test_sync_l2_hit_survives_refused_l1_backfill(self, caplog: pytest.LogCaptureFixture) -> None:
        """A refused backfill is logged and skipped; the served hit is never demoted to a recompute."""
        backend = _BytearrayBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, namespace="l2-parity-refused")
        def compute() -> dict[str, int]:
            calls["n"] += 1
            return dict(VALUE)

        assert compute() == VALUE
        l2_snapshot = dict(backend.store)
        compute.invalidate_cache()  # type: ignore[attr-defined]
        _force_l2_only(backend, l2_snapshot)

        with caplog.at_level(logging.WARNING):
            assert compute() == VALUE  # L2 hit served, backfill refused
            assert compute() == VALUE  # L2 again: nothing landed in L1
        assert calls["n"] == 1
        assert backend.gets == 2
        assert any("L1 backfill skipped" in r.message for r in caplog.records)
