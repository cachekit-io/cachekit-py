"""Shared helpers for the protocol vector modules."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Optional

import pytest

from cachekit.config.singleton import reset_settings
from cachekit.l1_cache import get_l1_cache_manager
from tests.unit.protocol.test_interop_decorator import DictBackend


class KeyRecordingBackend(DictBackend):
    """DictBackend that records every key it is asked for, so a key shows up even when the write fails."""

    def __init__(self) -> None:
        super().__init__()
        self.keys: list[str] = []

    def get(self, key: str) -> Optional[bytes]:
        self.keys.append(key)
        return super().get(key)

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        self.keys.append(key)
        super().set(key, value, ttl)


@pytest.fixture
def isolated_keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No tenant and no key from the environment unless a test sets one; L1 is process-global per key."""
    for name in ("CACHEKIT_DEPLOYMENT_UUID", "CACHEKIT_MASTER_KEY", "CACHEKIT_PREVIOUS_MASTER_KEYS"):
        monkeypatch.delenv(name, raising=False)
    reset_settings()
    get_l1_cache_manager().clear_all()
    yield
    get_l1_cache_manager().clear_all()
    reset_settings()
