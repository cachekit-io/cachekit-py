"""Comprehensive test coverage for per-request backend pattern.

Fix #10: Tests tenant isolation edge cases, concurrent async contexts,
fail-fast validation, URL-encoding collision prevention, and ContextVar isolation.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.backends.mock_backend import MockBackendProvider, PerRequestMockBackend, mock_tenant_context


class TestPerRequestBackend:
    """Test per-request backend wrapper behavior."""

    def test_fail_fast_validation_none_tenant(self):
        """Test fail-fast RuntimeError when tenant_id is None (Fix #9)."""
        store = {}

        with pytest.raises(RuntimeError, match="tenant_id cannot be None"):
            PerRequestMockBackend(store, tenant_id=None)

    def test_url_encoding_prevents_collision(self):
        """Test URL-encoding prevents key collision (Fix #2, Fix #10)."""
        store = {}

        # Two tenants with ':' in their IDs that would collide without encoding
        backend1 = PerRequestMockBackend(store, tenant_id="org:123")
        backend2 = PerRequestMockBackend(store, tenant_id="org%3A123")  # Already encoded

        # Set same key for both tenants
        asyncio.run(backend1.set("user:456", b"data1"))
        asyncio.run(backend2.set("user:456", b"data2"))

        # Verify no collision - each tenant has separate data
        data1 = asyncio.run(backend1.get("user:456"))
        data2 = asyncio.run(backend2.get("user:456"))

        assert data1 == b"data1"
        assert data2 == b"data2"

        # Verify scoped keys are different
        assert backend1._scoped_key("user:456") != backend2._scoped_key("user:456")

    def test_tenant_isolation_shared_store(self):
        """Test tenant isolation with shared store (Fix #7, Fix #10)."""
        store = {}

        # Two backends sharing same store but different tenants
        backend1 = PerRequestMockBackend(store, tenant_id="tenant1")
        backend2 = PerRequestMockBackend(store, tenant_id="tenant2")

        # Set same key for both tenants
        asyncio.run(backend1.set("key", b"value1"))
        asyncio.run(backend2.set("key", b"value2"))

        # Verify isolation - each tenant sees only their data
        assert asyncio.run(backend1.get("key")) == b"value1"
        assert asyncio.run(backend2.get("key")) == b"value2"

        # Verify both keys exist in shared store with different scoped keys
        assert len(store) == 2
        assert "t:tenant1:key" in store
        assert "t:tenant2:key" in store

    def test_health_check_no_tenant_leak(self):
        """Test health_check() doesn't leak tenant_id (Fix #5, Fix #10)."""
        store = {}
        backend = PerRequestMockBackend(store, tenant_id="secret-tenant-123")

        is_healthy, details = asyncio.run(backend.health_check())

        assert is_healthy is True
        assert "backend_type" in details
        # Verify tenant_id is NOT in response
        assert "tenant_id" not in details
        assert "secret-tenant-123" not in str(details)


class TestContextVarIsolation:
    """Test ContextVar isolation across async contexts (Fix #10)."""

    @pytest.mark.asyncio
    async def test_contextvar_isolation_single_task(self):
        """Test ContextVar isolation within single async task."""
        provider = MockBackendProvider()

        # Set tenant context
        mock_tenant_context.set("tenant1")
        backend = provider.get_backend()

        await backend.set("key", b"value1")

        # Change context
        mock_tenant_context.set("tenant2")
        backend2 = provider.get_backend()

        # Verify new backend sees new context
        await backend2.set("key", b"value2")

        # Original backend still uses tenant1 (context captured at creation)
        data = await backend.get("key")
        assert data == b"value1"

    @pytest.mark.asyncio
    async def test_contextvar_isolation_concurrent_tasks(self):
        """Test ContextVar isolation across 100+ concurrent async tasks (Fix #10)."""
        provider = MockBackendProvider()
        results = []

        async def tenant_task(tenant_id: str, task_num: int):
            """Task that sets and verifies tenant-specific data."""
            # Set tenant context for this task
            mock_tenant_context.set(tenant_id)

            # Get backend (should capture current context)
            backend = provider.get_backend()

            # Write tenant-specific data
            key = f"task:{task_num}"
            value = f"data-{tenant_id}-{task_num}".encode()
            await backend.set(key, value)

            # Small delay to simulate work and increase contention
            await asyncio.sleep(0.001)

            # Read back and verify - should get same tenant's data
            read_value = await backend.get(key)
            results.append((tenant_id, task_num, read_value == value))

        # Launch 120 concurrent tasks across 3 tenants
        tasks = []
        for i in range(120):
            tenant_id = f"tenant{i % 3}"  # 3 tenants: tenant0, tenant1, tenant2
            tasks.append(tenant_task(tenant_id, i))

        await asyncio.gather(*tasks)

        # Verify all tasks succeeded (no cross-tenant contamination)
        assert len(results) == 120
        assert all(success for _, _, success in results)

    @pytest.mark.asyncio
    async def test_contextvar_isolation_nested_contexts(self):
        """Test ContextVar isolation with nested context changes."""
        provider = MockBackendProvider()

        mock_tenant_context.set("outer-tenant")
        outer_backend = provider.get_backend()

        async def inner_task():
            """Inner task with different tenant context."""
            mock_tenant_context.set("inner-tenant")
            inner_backend = provider.get_backend()

            await inner_backend.set("key", b"inner-value")
            return await inner_backend.get("key")

        # Run inner task
        inner_value = await inner_task()
        assert inner_value == b"inner-value"

        # Outer context should still be isolated
        await outer_backend.set("key", b"outer-value")
        outer_value = await outer_backend.get("key")
        assert outer_value == b"outer-value"

        # Verify both values exist in separate tenant scopes
        assert len(provider._store) == 2


class TestMockBackendProvider:
    """Test MockBackendProvider behavior (Fix #7)."""

    def test_provider_fail_fast_no_context(self):
        """Test provider fail-fast when tenant context not set (Fix #9)."""
        provider = MockBackendProvider()

        # Clear context
        mock_tenant_context.set(None)

        # Should fail fast when getting backend
        with pytest.raises(RuntimeError, match="tenant_id cannot be None"):
            provider.get_backend()

    def test_provider_per_request_wrapper(self):
        """Test provider creates new wrapper per call (Fix #7)."""
        provider = MockBackendProvider()
        mock_tenant_context.set("tenant1")

        # Get multiple backends
        backend1 = provider.get_backend()
        backend2 = provider.get_backend()

        # Different wrapper instances
        assert backend1 is not backend2

        # But share same store
        asyncio.run(backend1.set("key", b"value"))
        assert asyncio.run(backend2.get("key")) == b"value"

    def test_provider_shared_store(self):
        """Test provider uses singleton shared store (Fix #7)."""
        provider = MockBackendProvider()

        mock_tenant_context.set("tenant1")
        backend1 = provider.get_backend()

        asyncio.run(backend1.set("key", b"value"))

        # Get new backend - should see same data
        mock_tenant_context.set("tenant1")
        backend2 = provider.get_backend()

        assert asyncio.run(backend2.get("key")) == b"value"

    def test_provider_clear(self):
        """Test provider clear() removes all data."""
        provider = MockBackendProvider()
        mock_tenant_context.set("tenant1")

        backend = provider.get_backend()
        asyncio.run(backend.set("key", b"value"))

        # Clear provider
        provider.clear()

        # Data should be gone
        assert asyncio.run(backend.get("key")) is None


class TestOptionalProtocols:
    """Test optional protocol implementations (Fix #6)."""

    @pytest.mark.asyncio
    async def test_ttl_inspectable_protocol(self):
        """Test TTLInspectableBackend protocol implementation."""
        store = {}
        backend = PerRequestMockBackend(store, tenant_id="tenant1")

        # Set key with TTL
        await backend.set("key", b"value", ttl=60)

        # Get TTL
        ttl = await backend.get_ttl("key")
        assert ttl == 60

        # Refresh TTL
        refreshed = await backend.refresh_ttl("key", 120)
        assert refreshed is True

        new_ttl = await backend.get_ttl("key")
        assert new_ttl == 120

        # Test non-existent key
        ttl = await backend.get_ttl("nonexistent")
        assert ttl is None

        refreshed = await backend.refresh_ttl("nonexistent", 60)
        assert refreshed is False

    @pytest.mark.asyncio
    async def test_lockable_protocol(self):
        """Test LockableBackend protocol implementation."""
        store = {}
        backend = PerRequestMockBackend(store, tenant_id="tenant1")

        # Acquire lock
        async with backend.acquire_lock("lock:key", timeout=30) as acquired:
            assert acquired is True

            # Try to acquire same lock (should fail in non-blocking mode)
            async with backend.acquire_lock("lock:key", timeout=30, blocking_timeout=0.01) as acquired2:
                assert acquired2 is False

        # Lock should be released, can acquire again
        async with backend.acquire_lock("lock:key", timeout=30) as acquired:
            assert acquired is True

    @pytest.mark.asyncio
    async def test_timeout_configurable_protocol(self):
        """Test TimeoutConfigurableBackend protocol implementation."""

        store = {}
        backend = PerRequestMockBackend(store, tenant_id="tenant1")

        # Test timeout (should raise BackendError with TIMEOUT type)
        with pytest.raises(Exception) as exc_info:
            async with backend.with_timeout("get", timeout_ms=1):
                await asyncio.sleep(0.01)  # Sleep longer than timeout

        assert "timeout" in str(exc_info.value).lower()


class TestTenantScoping:
    """Test tenant scoping behavior."""

    def test_scoped_key_format(self):
        """Test scoped key format matches spec: t:{tenant}:{key}."""
        store = {}
        backend = PerRequestMockBackend(store, tenant_id="tenant1")

        scoped = backend._scoped_key("user:123")
        assert scoped == "t:tenant1:user:123"

    def test_scoped_key_url_encoding(self):
        """Test scoped key uses URL-encoded tenant ID."""
        store = {}
        backend = PerRequestMockBackend(store, tenant_id="org:123")

        scoped = backend._scoped_key("key")
        # ':' in tenant ID should be encoded to %3A
        assert scoped == "t:org%3A123:key"
        assert "org:123" not in scoped  # Original ':' should not appear

    def test_tenant_isolation_operations(self):
        """Test all operations respect tenant scoping."""
        store = {}

        backend1 = PerRequestMockBackend(store, tenant_id="tenant1")
        backend2 = PerRequestMockBackend(store, tenant_id="tenant2")

        # Test set/get
        asyncio.run(backend1.set("key", b"value1"))
        assert asyncio.run(backend1.get("key")) == b"value1"
        assert asyncio.run(backend2.get("key")) is None

        # Test exists
        assert asyncio.run(backend1.exists("key")) is True
        assert asyncio.run(backend2.exists("key")) is False

        # Test delete
        asyncio.run(backend2.set("key", b"value2"))
        deleted = asyncio.run(backend1.delete("key"))
        assert deleted is True

        # Backend1's key deleted, backend2's key still exists
        assert asyncio.run(backend1.get("key")) is None
        assert asyncio.run(backend2.get("key")) == b"value2"
