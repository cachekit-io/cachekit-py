"""TTLInspectableBackend support for the File and Memcached backends (LAB-446).

Covers:
- File: full ``get_ttl`` / ``refresh_ttl`` off its on-disk expiry header (no format change).
- Memcached: ``refresh_ttl`` via the classic ``touch`` command; deliberately NO ``get_ttl``
  (pymemcache HashClient exposes no meta protocol), so Memcached stays out of
  TTLInspectableBackend by design.
- Degradation UX: ``refresh_ttl_on_get`` against a non-inspectable backend warns once
  instead of silently no-opping.
- End-to-end: a handler get with ``refresh_ttl`` measurably extends the File expiry.
"""

from __future__ import annotations

import asyncio
import struct
import warnings
from typing import Any, Optional

import pytest
import time_machine

from cachekit import cache_handler as ch
from cachekit.backends.base import TTLInspectableBackend
from cachekit.backends.errors import BackendError
from cachekit.backends.file.backend import HEADER_SIZE, MAGIC, FileBackend
from cachekit.backends.file.config import FileBackendConfig
from cachekit.cache_handler import StandardCacheHandler, supports_ttl_inspection


@pytest.fixture
def file_backend(tmp_path) -> FileBackend:
    """FileBackend isolated to a per-test cache dir."""
    config = FileBackendConfig(cache_dir=tmp_path / "cache", max_size_mb=10, max_value_mb=5)
    return FileBackend(config)


# --------------------------------------------------------------------------- File


@pytest.mark.unit
class TestFileBackendTTLInspection:
    async def test_get_ttl_returns_remaining_seconds(self, file_backend: FileBackend) -> None:
        with time_machine.travel(1000.0, tick=False):
            file_backend.set("k", b"v", ttl=100)
        with time_machine.travel(1040.0, tick=False):
            assert await file_backend.get_ttl("k") == 60

    async def test_get_ttl_missing_key_returns_none(self, file_backend: FileBackend) -> None:
        assert await file_backend.get_ttl("nope") is None

    async def test_get_ttl_permanent_key_returns_none(self, file_backend: FileBackend) -> None:
        file_backend.set("perm", b"v")  # ttl=None -> never expires (expiry field 0)
        assert await file_backend.get_ttl("perm") is None

    async def test_get_ttl_expired_key_returns_none(self, file_backend: FileBackend) -> None:
        with time_machine.travel(1000.0, tick=False):
            file_backend.set("k", b"v", ttl=10)
        with time_machine.travel(1020.0, tick=False):
            assert await file_backend.get_ttl("k") is None

    async def test_refresh_ttl_extends_and_returns_true(self, file_backend: FileBackend) -> None:
        with time_machine.travel(1000.0, tick=False):
            file_backend.set("k", b"v", ttl=100)
        with time_machine.travel(1090.0, tick=False):
            assert await file_backend.get_ttl("k") == 10  # nearly expired
            assert await file_backend.refresh_ttl("k", 100) is True
            assert await file_backend.get_ttl("k") == 100  # slid forward

    async def test_refresh_ttl_missing_key_returns_false(self, file_backend: FileBackend) -> None:
        assert await file_backend.refresh_ttl("nope", 100) is False

    async def test_refresh_ttl_expired_key_returns_false(self, file_backend: FileBackend) -> None:
        with time_machine.travel(1000.0, tick=False):
            file_backend.set("k", b"v", ttl=10)
        with time_machine.travel(1020.0, tick=False):
            assert await file_backend.refresh_ttl("k", 100) is False

    async def test_refresh_ttl_preserves_payload_and_header_format(self, file_backend: FileBackend) -> None:
        """Only the 8-byte expiry field changes; magic/version/flags/payload are untouched."""
        with time_machine.travel(1000.0, tick=False):
            file_backend.set("k", b"the-payload", ttl=100)
        path = file_backend._key_to_path("k")
        with open(path, "rb") as f:
            before = f.read()
        with time_machine.travel(1050.0, tick=False):
            assert await file_backend.refresh_ttl("k", 200) is True
            with open(path, "rb") as f:
                after = f.read()

            assert len(after) == len(before)
            assert after[0:6] == before[0:6]  # magic + version + reserved + flags unchanged
            assert after[HEADER_SIZE:] == before[HEADER_SIZE:] == b"the-payload"  # payload intact
            assert after[0:2] == MAGIC
            # expiry field moved from 1100 to 1250
            assert struct.unpack(">Q", before[6:14])[0] == 1100
            assert struct.unpack(">Q", after[6:14])[0] == 1250
            assert file_backend.get("k") == b"the-payload"  # readable at t=1050 (expiry 1250)

    async def test_refresh_ttl_zero_makes_permanent(self, file_backend: FileBackend) -> None:
        with time_machine.travel(1000.0, tick=False):
            file_backend.set("k", b"v", ttl=100)
            assert await file_backend.refresh_ttl("k", 0) is True
            assert await file_backend.get_ttl("k") is None  # 0 == never expire

    def test_file_backend_is_ttl_inspectable(self, file_backend: FileBackend) -> None:
        assert supports_ttl_inspection(file_backend) is True
        assert isinstance(file_backend, TTLInspectableBackend)


# ----------------------------------------------------------------------- Memcached


@pytest.fixture
def mc_backend(monkeypatch):
    """MemcachedBackend with a mocked pymemcache HashClient; returns (backend, fake_client)."""
    from unittest.mock import MagicMock

    from cachekit.backends.memcached.backend import MemcachedBackend
    from cachekit.backends.memcached.config import MemcachedBackendConfig

    fake = MagicMock()
    monkeypatch.setattr("pymemcache.client.hash.HashClient", MagicMock(return_value=fake))
    backend = MemcachedBackend(MemcachedBackendConfig(key_prefix="app:"))
    return backend, fake


@pytest.mark.unit
class TestMemcachedRefreshTTL:
    async def test_refresh_ttl_touches_prefixed_key_and_returns_true(self, mc_backend) -> None:
        backend, fake = mc_backend
        fake.touch.return_value = True
        assert await backend.refresh_ttl("k", 60) is True
        fake.touch.assert_called_once_with("app:k", expire=60, noreply=False)

    async def test_refresh_ttl_returns_false_for_missing_key(self, mc_backend) -> None:
        backend, fake = mc_backend
        fake.touch.return_value = False
        assert await backend.refresh_ttl("gone", 60) is False

    async def test_refresh_ttl_clamps_to_30_day_max(self, mc_backend) -> None:
        from cachekit.backends.memcached.config import MAX_MEMCACHED_TTL

        backend, fake = mc_backend
        fake.touch.return_value = True
        await backend.refresh_ttl("k", MAX_MEMCACHED_TTL + 9999)
        _, kwargs = fake.touch.call_args
        assert kwargs["expire"] == MAX_MEMCACHED_TTL

    async def test_refresh_ttl_wraps_client_errors(self, mc_backend) -> None:
        backend, fake = mc_backend
        fake.touch.side_effect = RuntimeError("boom")
        with pytest.raises(BackendError):
            await backend.refresh_ttl("k", 60)

    def test_memcached_is_not_ttl_inspectable(self, mc_backend) -> None:
        """Recorded decision: no get_ttl (no meta protocol) -> stays out of the interface."""
        backend, _ = mc_backend
        assert not hasattr(backend, "get_ttl")
        assert supports_ttl_inspection(backend) is False
        assert not isinstance(backend, TTLInspectableBackend)


# ----------------------------------------------------------- Degradation UX (warn-once)


class _NonInspectableBackend:
    """Minimal BaseBackend with no TTL-inspection capability (real class, not a MagicMock,
    so hasattr('get_ttl') is genuinely False)."""

    def get(self, key: str) -> Optional[bytes]:
        return b"hit"

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        pass

    def delete(self, key: str) -> bool:
        return False

    def exists(self, key: str) -> bool:
        return True

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {"backend_type": "fake"}


@pytest.mark.unit
class TestRefreshTTLDegradation:
    async def test_warns_once_for_non_inspectable_backend(self) -> None:
        ch._TTL_REFRESH_UNSUPPORTED_WARNED.clear()
        handler = StandardCacheHandler(_NonInspectableBackend())
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            await handler._maybe_refresh_ttl("k", 100)
            await handler._maybe_refresh_ttl("k", 100)  # second call must not re-warn
        user = [w for w in caught if issubclass(w.category, UserWarning)]
        assert len(user) == 1
        assert "refresh_ttl_on_get" in str(user[0].message)

    async def test_degradation_does_not_raise(self) -> None:
        ch._TTL_REFRESH_UNSUPPORTED_WARNED.clear()
        handler = StandardCacheHandler(_NonInspectableBackend())
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            await handler._maybe_refresh_ttl("k", 100)  # graceful: returns without error


# ---------------------------------------------------------------------- End-to-end


@pytest.mark.unit
class TestFileRefreshEndToEnd:
    async def test_get_async_refresh_ttl_extends_file_expiry(self, file_backend: FileBackend) -> None:
        """refresh_ttl_on_get path (via the handler) measurably slides the File TTL forward."""
        handler = StandardCacheHandler(file_backend, ttl_refresh_threshold=0.5)
        with time_machine.travel(2000.0, tick=False):
            file_backend.set("k", b"v", ttl=100)
        with time_machine.travel(2060.0, tick=False):
            # remaining = 40 < 100*0.5 = 50 -> should refresh
            assert await file_backend.get_ttl("k") == 40
            value = await handler.get_async("k", refresh_ttl=100)
            assert value == b"v"
            assert await file_backend.get_ttl("k") == 100  # slid to 2060+100

    async def test_get_async_no_refresh_when_above_threshold(self, file_backend: FileBackend) -> None:
        handler = StandardCacheHandler(file_backend, ttl_refresh_threshold=0.5)
        with time_machine.travel(2000.0, tick=False):
            file_backend.set("k", b"v", ttl=100)
        with time_machine.travel(2010.0, tick=False):
            # remaining = 90 > 50 -> no refresh
            await handler.get_async("k", refresh_ttl=100)
            assert await file_backend.get_ttl("k") == 90

    async def test_decorator_refresh_ttl_on_get_slides_file_expiry(self, file_backend: FileBackend) -> None:
        """Behavioural e2e through the real @cache decorator: a hit past the ORIGINAL expiry
        is still served (not recomputed) because refresh_ttl_on_get slid the File TTL forward.

        L1 is disabled so reads reach the File (L2) backend — that is exactly the scenario
        refresh_ttl_on_get targets (an L1 hit would never consult L2 and so never refresh it).
        Path-agnostic: whichever refresh path the async decorator uses (handler _maybe_refresh_ttl
        or the wrapper's background task), the observable outcome must be sliding expiration.
        """
        from cachekit import cache

        calls = {"n": 0}

        @cache(
            backend=file_backend,
            ttl=100,
            refresh_ttl_on_get=True,
            ttl_refresh_threshold=0.9,
            l1_enabled=False,
        )
        async def fetch() -> int:
            calls["n"] += 1
            return calls["n"]

        with time_machine.travel(4000.0, tick=False):
            assert await fetch() == 1  # miss -> compute + store, expiry=4100

        with time_machine.travel(4050.0, tick=False):
            assert await fetch() == 1  # hit; remaining 50 < 90 -> triggers refresh, expiry->4150
            # Drain any background refresh task the wrapper may have scheduled (File async
            # methods do sync work, so a couple of loop yields fully complete it).
            for _ in range(5):
                await asyncio.sleep(0)

        with time_machine.travel(4120.0, tick=False):
            # Original expiry (4100) has passed. If the refresh worked, expiry is 4150 > 4120,
            # so the value is still cached; otherwise it would expire and recompute to 2.
            assert await fetch() == 1
            assert calls["n"] == 1  # never recomputed -> TTL was genuinely extended
