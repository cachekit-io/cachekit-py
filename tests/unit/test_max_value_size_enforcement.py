"""Issue #163: max_value_size is the L2 oversized-entry ceiling.

Before the fix the setting was defined but consumed nowhere — a multi-GB value
went to the backend unchecked. Every L2 write flows through
CacheSerializationHandler.serialize_data, so enforcement lives there: an
oversized envelope raises ValueError, which the store paths catch and degrade
to uncached execution (the wrapped function's result is still returned).
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from cachekit.cache_handler import CacheOperationHandler, CacheSerializationHandler
from cachekit.config.singleton import reset_settings
from cachekit.key_generator import CacheKeyGenerator


class RecordingHandler:
    """Minimal CacheHandlerStrategy stub that records set calls."""

    def __init__(self) -> None:
        self.set_calls: list[tuple[str, bytes]] = []

    def set(self, key: str, value: bytes, ttl: Any = None) -> None:
        self.set_calls.append((key, value))

    async def set_async(self, key: str, value: bytes, ttl: Any = None) -> None:
        self.set_calls.append((key, value))


@pytest.fixture
def small_limit(monkeypatch):
    """Cap max_value_size at 1KB for the duration of a test."""
    monkeypatch.setenv("CACHEKIT_MAX_VALUE_SIZE", "1024")
    reset_settings()
    yield 1024
    reset_settings()


@pytest.fixture
def default_limit(monkeypatch):
    """Ensure the 100MB default applies regardless of process env / singleton state."""
    monkeypatch.delenv("CACHEKIT_MAX_VALUE_SIZE", raising=False)
    reset_settings()
    yield
    reset_settings()


@pytest.mark.unit
class TestMaxValueSizeEnforcement:
    def test_oversized_value_raises(self, small_limit):
        handler = CacheSerializationHandler()
        with pytest.raises(ValueError, match="max_value_size"):
            handler.serialize_data(os.urandom(4 * 1024), cache_key="big:key")

    def test_value_under_limit_serializes(self, small_limit):
        handler = CacheSerializationHandler()
        assert isinstance(handler.serialize_data({"ok": 1}, cache_key="small:key"), bytes)

    def test_store_result_rejects_oversized_and_skips_backend(self, small_limit):
        backend = RecordingHandler()
        op = CacheOperationHandler(CacheSerializationHandler(), CacheKeyGenerator(), cache_handler=backend)

        result = op.store_result("big:key", os.urandom(4 * 1024), ttl=60)

        assert result is None  # rejected → nothing for L1 either
        assert backend.set_calls == []  # backend never touched

    def test_store_result_stores_normal_value(self, small_limit):
        backend = RecordingHandler()
        op = CacheOperationHandler(CacheSerializationHandler(), CacheKeyGenerator(), cache_handler=backend)

        result = op.store_result("small:key", {"ok": 1}, ttl=60)

        assert isinstance(result, bytes)
        assert len(backend.set_calls) == 1

    @pytest.mark.asyncio
    async def test_store_result_async_rejects_oversized(self, small_limit):
        backend = RecordingHandler()
        op = CacheOperationHandler(CacheSerializationHandler(), CacheKeyGenerator(), cache_handler=backend)

        result = await op.store_result_async("big:key", os.urandom(4 * 1024), ttl=60)

        assert result is None
        assert backend.set_calls == []

    def test_default_limit_allows_typical_values(self, default_limit):
        """With the 100MB default, ordinary payloads are unaffected."""
        handler = CacheSerializationHandler()
        assert isinstance(handler.serialize_data(list(range(1000)), cache_key="typical:key"), bytes)
