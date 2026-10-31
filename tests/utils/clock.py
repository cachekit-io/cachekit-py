"""Move the clock ObjectCache and the lock-free L1-only hit share, for tests of expiry, refresh and LRU order.

Both read ``cachekit._rust_serializer.monotonic``, so a test that moves time must move it there: a clock
replaced on the Python side only would leave the lock-free hit (``HitMemo.get``) judging deadlines on
the real one. ``fake_clock`` installs a replacement through the extension's ``_set_clock`` and
monkeypatch takes it back out when the test ends.
"""

from __future__ import annotations

import types
from collections.abc import Callable
from typing import Any

import pytest

from cachekit._rust_serializer import _set_clock


class _SharedClock:
    """``monkeypatch.setattr(shared_clock, "fake", fn)`` makes fn the shared clock; its undo restores the real one."""

    def __init__(self) -> None:
        self._fake: Callable[[], float] | None = None

    @property
    def fake(self) -> Callable[[], float] | None:
        return self._fake

    @fake.setter
    def fake(self, clock: Callable[[], float] | None) -> None:
        self._fake = clock
        _set_clock(clock)


shared_clock = _SharedClock()


def fake_clock(monkeypatch: pytest.MonkeyPatch, start: float = 1000.0) -> Any:
    """Replace the shared clock with one that reads ``start`` until the test sets the returned ``monotonic``."""
    fake = types.SimpleNamespace(monotonic=lambda: start)
    monkeypatch.setattr(shared_clock, "fake", lambda: fake.monotonic())
    return fake
