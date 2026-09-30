"""Multi-tenant encrypted reads select the decryption tenant from the caller.

With a tenant_extractor, a read resolves the caller's tenant exactly as the write
does, from the call's args/kwargs or context, so the wrapper's tenant check compares
the entry's tenant with the caller's. The cache key has no tenant segment, so a
context-sourced tenant (ContextVarExtractor) shares one entry across tenants; an entry
written for another tenant is then a tamper-class failure (miss + evict by default,
DecryptionAuthenticationError under fail_closed=True), never a decrypt. An
argument-sourced tenant (ArgumentNameExtractor) is part of the key already, so its
reads keep hitting on every path.

Handlers without a tenant_extractor keep selecting the tenant from the entry's
header, so single-tenant entries written under another deployment tenant stay
readable.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from cachekit import cache
from cachekit.backends.file import FileBackend
from cachekit.backends.file.config import FileBackendConfig
from cachekit.cache_handler import (
    CacheOperationHandler,
    CacheSerializationHandler,
    StandardCacheHandler,
    handle_decrypt_failure,
)
from cachekit.decorators.tenant_context import ArgumentNameExtractor, ContextVarExtractor
from cachekit.key_generator import CacheKeyGenerator
from cachekit.l1_cache import get_l1_cache
from cachekit.serializers.encryption_wrapper import DecryptionAuthenticationError

MASTER_KEY = "61" * 32
TENANT_A = "0a0a0a0a-0000-4000-8000-00000000000a"
TENANT_B = "0b0b0b0b-0000-4000-8000-00000000000b"
CACHE_KEY = "ns:t:func:m.f:args:0:1s"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Default fail policy and no ambient key material, whatever the shell exports."""
    from cachekit.config.singleton import reset_settings

    for var in (
        "CACHEKIT_ENCRYPTION_FAIL_CLOSED",
        "CACHEKIT_MASTER_KEY",
        "CACHEKIT_PREVIOUS_MASTER_KEYS",
        "CACHEKIT_DEPLOYMENT_UUID",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_settings()
    yield
    reset_settings()


def _config(path: Path) -> FileBackendConfig:
    return FileBackendConfig(cache_dir=path, max_size_mb=64, max_value_mb=32)


class _FreshnessFileBackend(FileBackend):
    """SWR-capable, so the decorator reads through get_cached_value_with_freshness*."""

    def get_with_freshness(self, key: str) -> tuple[bytes, bool, int | None] | None:
        value = self.get(key)
        return None if value is None else (value, False, None)


class _LockingFileBackend(FileBackend):
    """Lockable backend where another worker writes ``source``'s entry while the lock
    is taken: the pre-lock read misses, and the double-check read finds that entry."""

    def __init__(self, config: FileBackendConfig, source: FileBackend, *, acquired: bool) -> None:
        super().__init__(config)
        self._source, self._acquired = source, acquired

    @asynccontextmanager
    async def acquire_lock(self, key: str, timeout: float, blocking_timeout: float | None = None) -> AsyncIterator[bool]:
        entry = self._source.get(key)
        assert entry is not None, "the writer must have stored this key"
        self.set(key, entry)
        yield self._acquired


class _LockingFreshnessFileBackend(_LockingFileBackend, _FreshnessFileBackend):
    """Lockable and SWR-capable: the double-check reads through get_cached_value_with_freshness_async."""


BACKENDS = [
    pytest.param(FileBackend, id="file"),
    pytest.param(_FreshnessFileBackend, id="file-swr-capable"),
]
LOCK_BACKENDS = [
    pytest.param(_LockingFileBackend, id="file"),
    pytest.param(_LockingFreshnessFileBackend, id="file-swr-capable"),
]
READ_PATHS = [
    pytest.param(False, False, id="sync-l2"),
    pytest.param(False, True, id="sync-l1"),
    pytest.param(True, False, id="async-l2"),
    pytest.param(True, True, id="async-l1"),
]


def _as(tenant: str, fn: Any, *args: Any) -> Any:
    """Run ``fn`` as ``tenant`` in a copied context, so no tenant leaks into later calls or tests."""

    def run() -> Any:
        ContextVarExtractor.set_tenant_id(tenant)
        return fn(*args)

    return contextvars.copy_context().run(run)


async def _call_as(tenant: str, fn: Any, *args: Any) -> Any:
    """:func:`_as` for a decorated function, awaiting it when it is async."""
    if not inspect.iscoroutinefunction(fn):
        return _as(tenant, fn, *args)

    async def run() -> Any:
        ContextVarExtractor.set_tenant_id(tenant)
        return await fn(*args)

    return await asyncio.create_task(run())  # the task runs in a copy of this context


async def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a decorated function, awaiting it when it is async."""
    result = fn(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def _decorate(func: Any, backend: FileBackend, *, l1_enabled: bool, extractor: Any, **secure: Any) -> Any:
    get_l1_cache("tenant-read").clear()
    return cache.secure(
        master_key=MASTER_KEY,
        backend=backend,
        ttl=300,
        namespace="tenant-read",
        l1_enabled=l1_enabled,
        tenant_extractor=extractor,
        **secure,
    )(func)


def _context_tenant_fn(backend: FileBackend, runs: list[str], *, is_async: bool, l1_enabled: bool, **secure: Any) -> Any:
    """Tenant from the context: every tenant's profile(42) shares one cache key."""
    if is_async:

        async def profile(user_id: int) -> dict[str, Any]:
            runs.append(ContextVarExtractor().extract((), {}))
            return {"user_id": user_id, "tenant": runs[-1]}

    else:

        def profile(user_id: int) -> dict[str, Any]:
            runs.append(ContextVarExtractor().extract((), {}))
            return {"user_id": user_id, "tenant": runs[-1]}

    return _decorate(profile, backend, l1_enabled=l1_enabled, extractor=ContextVarExtractor(), **secure)


def _argument_tenant_fn(backend: FileBackend, runs: list[str], *, is_async: bool, l1_enabled: bool) -> Any:
    """Tenant from a keyword argument, which the extractor needs on every read."""
    if is_async:

        async def secret(item: int, tenant_id: str) -> dict[str, Any]:
            runs.append(tenant_id)
            return {"item": item, "tenant": tenant_id}

    else:

        def secret(item: int, tenant_id: str) -> dict[str, Any]:
            runs.append(tenant_id)
            return {"item": item, "tenant": tenant_id}

    return _decorate(secret, backend, l1_enabled=l1_enabled, extractor=ArgumentNameExtractor("tenant_id"))


class TestContextTenantReads:
    """Two tenants, one key: every decorator read path refuses the other tenant's entry."""

    @pytest.mark.parametrize("backend_cls", BACKENDS)
    @pytest.mark.parametrize(("is_async", "l1_enabled"), READ_PATHS)
    async def test_default_policy_recomputes_for_the_other_tenant(
        self, tmp_path: Path, backend_cls: type[FileBackend], is_async: bool, l1_enabled: bool
    ) -> None:
        runs: list[str] = []
        fn = _context_tenant_fn(backend_cls(_config(tmp_path)), runs, is_async=is_async, l1_enabled=l1_enabled)

        assert await _call_as(TENANT_A, fn, 42) == {"user_id": 42, "tenant": TENANT_A}
        assert await _call_as(TENANT_B, fn, 42) == {"user_id": 42, "tenant": TENANT_B}
        assert runs == [TENANT_A, TENANT_B]

        # The last writer still reads its own entry back.
        assert await _call_as(TENANT_B, fn, 42) == {"user_id": 42, "tenant": TENANT_B}
        assert runs == [TENANT_A, TENANT_B]

    @pytest.mark.parametrize("backend_cls", BACKENDS)
    @pytest.mark.parametrize(("is_async", "l1_enabled"), READ_PATHS)
    async def test_fail_closed_raises_and_keeps_the_entry(
        self, tmp_path: Path, backend_cls: type[FileBackend], is_async: bool, l1_enabled: bool
    ) -> None:
        runs: list[str] = []
        fn = _context_tenant_fn(backend_cls(_config(tmp_path)), runs, is_async=is_async, l1_enabled=l1_enabled, fail_closed=True)

        assert await _call_as(TENANT_A, fn, 42) == {"user_id": 42, "tenant": TENANT_A}
        with pytest.raises(DecryptionAuthenticationError, match="Tenant mismatch"):
            await _call_as(TENANT_B, fn, 42)
        assert runs == [TENANT_A]

        # The entry is retained as evidence, and its own tenant still reads it.
        assert await _call_as(TENANT_A, fn, 42) == {"user_id": 42, "tenant": TENANT_A}
        assert runs == [TENANT_A]

    @pytest.mark.parametrize("fail_closed", [False, True], ids=["default", "fail-closed"])
    @pytest.mark.parametrize("acquired", [True, False], ids=["lock-acquired", "lock-timeout"])
    @pytest.mark.parametrize("lock_backend_cls", LOCK_BACKENDS)
    async def test_lock_double_check_refuses_the_other_tenant(
        self, tmp_path: Path, lock_backend_cls: type[_LockingFileBackend], acquired: bool, fail_closed: bool
    ) -> None:
        writer = FileBackend(_config(tmp_path / "writer"))
        await _call_as(TENANT_A, _context_tenant_fn(writer, [], is_async=True, l1_enabled=False), 42)
        runs: list[str] = []
        backend = lock_backend_cls(_config(tmp_path / "reader"), writer, acquired=acquired)
        fn = _context_tenant_fn(backend, runs, is_async=True, l1_enabled=False, fail_closed=fail_closed)

        if fail_closed:
            with pytest.raises(DecryptionAuthenticationError, match="Tenant mismatch"):
                await _call_as(TENANT_B, fn, 42)
            assert runs == []
        else:
            assert await _call_as(TENANT_B, fn, 42) == {"user_id": 42, "tenant": TENANT_B}
            assert runs == [TENANT_B]


class TestArgumentTenantReads:
    """The tenant is a call argument: it reaches the key hash and, now, every read site."""

    @pytest.mark.parametrize("backend_cls", BACKENDS)
    @pytest.mark.parametrize(("is_async", "l1_enabled"), READ_PATHS)
    async def test_every_read_path_hits(
        self, tmp_path: Path, backend_cls: type[FileBackend], is_async: bool, l1_enabled: bool
    ) -> None:
        runs: list[str] = []
        fn = _argument_tenant_fn(backend_cls(_config(tmp_path)), runs, is_async=is_async, l1_enabled=l1_enabled)
        before = fn.cache_info()  # stats are shared by every decoration of this qualname

        for _ in range(3):
            assert await _call(fn, 1, tenant_id=TENANT_A) == {"item": 1, "tenant": TENANT_A}
        assert runs == [TENANT_A]
        after = fn.cache_info()
        hits = (after.l1_hits - before.l1_hits, after.l2_hits - before.l2_hits)
        assert hits == ((2, 0) if l1_enabled else (0, 2))  # each read hit the tier it should

    @pytest.mark.parametrize("acquired", [True, False], ids=["lock-acquired", "lock-timeout"])
    @pytest.mark.parametrize("lock_backend_cls", LOCK_BACKENDS)
    async def test_lock_double_check_hits(
        self, tmp_path: Path, lock_backend_cls: type[_LockingFileBackend], acquired: bool
    ) -> None:
        writer = FileBackend(_config(tmp_path / "writer"))
        await _call(_argument_tenant_fn(writer, [], is_async=True, l1_enabled=False), 1, tenant_id=TENANT_A)
        runs: list[str] = []
        backend = lock_backend_cls(_config(tmp_path / "reader"), writer, acquired=acquired)
        fn = _argument_tenant_fn(backend, runs, is_async=True, l1_enabled=False)

        assert await fn(1, tenant_id=TENANT_A) == {"item": 1, "tenant": TENANT_A}
        assert runs == []

    def test_key_shape_is_unchanged(self, tmp_path: Path) -> None:
        """Entries sit at the key the generator has always produced, one per tenant, so
        existing entries stay readable: no silent invalidation."""
        backend = FileBackend(_config(tmp_path))
        runs: list[str] = []
        fn = _argument_tenant_fn(backend, runs, is_async=False, l1_enabled=False)

        def key_for(tenant: str) -> str:
            return CacheKeyGenerator().generate_key(fn, (1,), {"tenant_id": tenant}, "tenant-read", True)

        assert key_for(TENANT_A) != key_for(TENANT_B)
        for tenant in (TENANT_A, TENANT_B, TENANT_A, TENANT_B):
            assert fn(1, tenant_id=tenant) == {"item": 1, "tenant": tenant}
        assert runs == [TENANT_A, TENANT_B]
        assert backend.exists(key_for(TENANT_A)) and backend.exists(key_for(TENANT_B))


class TestHandlerReadTenant:
    """CacheSerializationHandler / CacheOperationHandler level."""

    def _handler(self, extractor: Any, **kwargs: Any) -> CacheSerializationHandler:
        return CacheSerializationHandler(encryption=True, master_key=MASTER_KEY, tenant_extractor=extractor, **kwargs)

    def test_cross_tenant_entry_is_auth_tamper(self) -> None:
        """The wrapper's tenant check compares the entry's tenant with the caller's."""
        handler = self._handler(ContextVarExtractor())
        entry = _as(TENANT_A, handler.serialize_data, {"v": 1}, (), None, CACHE_KEY)

        with pytest.raises(DecryptionAuthenticationError, match="Tenant mismatch") as exc_info:
            _as(TENANT_B, handler.deserialize_data, entry, CACHE_KEY)
        assert handle_decrypt_failure(exc_info.value, tier="l2", cache_key=CACHE_KEY, fail_closed=False) == "auth_tamper"
        assert _as(TENANT_A, handler.deserialize_data, entry, CACHE_KEY) == {"v": 1}

    def test_argument_extractor_reads_its_tenant_from_the_call_arguments(self) -> None:
        handler = self._handler(ArgumentNameExtractor("tenant_id"))
        entry = handler.serialize_data({"v": 1}, (), {"tenant_id": TENANT_A}, cache_key=CACHE_KEY)

        assert handler.deserialize_data(entry, CACHE_KEY, (), {"tenant_id": TENANT_A}) == {"v": 1}
        with pytest.raises(DecryptionAuthenticationError, match="Tenant mismatch"):
            handler.deserialize_data(entry, CACHE_KEY, (), {"tenant_id": TENANT_B})

    @pytest.mark.parametrize(
        "extractor",
        [
            pytest.param(ContextVarExtractor(), id="tenant-not-set"),
            pytest.param(type("_Broken", (), {"extract": lambda self, args, kwargs: None.lower()})(), id="extractor-bug"),
        ],
    )
    def test_unresolvable_tenant_is_a_miss_without_eviction(self, tmp_path: Path, extractor: Any) -> None:
        """A read that cannot resolve the caller's tenant decrypts nothing and reads as a
        plain miss. The entry may be valid for its own tenant, so it stays."""
        backend = FileBackend(_config(tmp_path))
        writer = self._handler(ContextVarExtractor())
        backend.set(CACHE_KEY, _as(TENANT_A, writer.serialize_data, {"v": 1}, (), None, CACHE_KEY))
        entry = backend.get(CACHE_KEY)
        reader = CacheOperationHandler(
            self._handler(extractor), CacheKeyGenerator(), cache_handler=StandardCacheHandler(backend)
        )

        assert contextvars.Context().run(reader.get_cached_value, CACHE_KEY) is None
        assert backend.get(CACHE_KEY) == entry
        # Not vacuous: the entry's own tenant, through a working extractor, hits.
        owner = CacheOperationHandler(writer, CacheKeyGenerator(), cache_handler=StandardCacheHandler(backend))
        hit = _as(TENANT_A, owner.get_cached_value, CACHE_KEY)
        assert hit is not None and hit.value == {"v": 1}

    def test_mmap_drift_read_resolves_the_callers_tenant(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The mmap fast path serves only encryption-disabled handlers, so it meets an
        encrypted entry only on a config-drift read. With a tenant_extractor that read
        decrypts as the caller's tenant too."""
        pd = pytest.importorskip("pandas")
        from cachekit.config.singleton import reset_settings

        monkeypatch.setenv("CACHEKIT_MASTER_KEY", MASTER_KEY)  # drift reads decrypt via the global key
        reset_settings()
        extractor = ArgumentNameExtractor("tenant_id")
        frame = pd.DataFrame({"v": [1, 2, 3]})
        backend = FileBackend(_config(tmp_path))
        writer = self._handler(extractor, serializer_name="arrow")
        backend.set(CACHE_KEY, writer.serialize_data(frame, (), {"tenant_id": TENANT_A}, cache_key=CACHE_KEY))
        drift_reader = CacheSerializationHandler(serializer_name="arrow", encryption=False, tenant_extractor=extractor)
        assert drift_reader.supports_mmap_read()
        op = CacheOperationHandler(drift_reader, CacheKeyGenerator(), cache_handler=StandardCacheHandler(backend))

        hit = op.get_cached_value(CACHE_KEY, None, (), {"tenant_id": TENANT_A})
        assert hit is not None and hit.value.equals(frame)
        assert op.get_cached_value(CACHE_KEY, None, (), {"tenant_id": TENANT_B}) is None
        assert backend.get(CACHE_KEY) is None  # default policy: the cross-tenant entry was evicted


class TestHeaderTenantWithoutExtractor:
    """Handlers without a tenant_extractor keep header-derived tenant selection."""

    def test_single_tenant_reads_an_entry_written_under_another_deployment_tenant(self) -> None:
        """Entries written under an explicit deployment UUID stay readable after the
        handler's resolved tenant changes to the protocol default."""
        writer = CacheSerializationHandler(
            encryption=True,
            single_tenant_mode=True,
            master_key=MASTER_KEY,
            deployment_uuid="00000000-0000-4000-8000-00000000abcd",
        )
        reader = CacheSerializationHandler(encryption=True, single_tenant_mode=True, master_key=MASTER_KEY)
        assert reader._single_tenant_id == "default"

        assert reader.deserialize_data(writer.serialize_data({"v": 1}, cache_key=CACHE_KEY), CACHE_KEY) == {"v": 1}
