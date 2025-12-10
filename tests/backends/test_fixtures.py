"""Test fixture helpers for backend testing."""

from __future__ import annotations

import pytest

from tests.backends.fixtures import (
    create_backend_with_capabilities,
    create_backend_without_capabilities,
    create_failing_backend,
    create_multi_tenant_provider,
    verify_backend_capabilities,
)
from tests.backends.mock_backend import mock_tenant_context


@pytest.mark.asyncio
class TestBackendFixtureHelpers:
    """Test fixture helper functions."""

    async def test_create_backend_with_all_capabilities(self):
        """Test creating backend with all capabilities."""
        backend = create_backend_with_capabilities(ttl_support=True, locking_support=True, timeout_support=True)

        # Verify all protocols present
        assert hasattr(backend, "get_ttl")
        assert hasattr(backend, "refresh_ttl")
        assert hasattr(backend, "acquire_lock")
        assert hasattr(backend, "with_timeout")

    async def test_create_backend_without_ttl(self):
        """Test creating backend without TTL support."""
        backend = create_backend_with_capabilities(ttl_support=False)

        # Verify TTL methods removed
        assert not hasattr(backend, "get_ttl")
        assert not hasattr(backend, "refresh_ttl")

        # Other capabilities still present
        assert hasattr(backend, "acquire_lock")
        assert hasattr(backend, "with_timeout")

    async def test_create_backend_without_locking(self):
        """Test creating backend without locking support."""
        backend = create_backend_with_capabilities(locking_support=False)

        # Verify locking removed
        assert not hasattr(backend, "acquire_lock")

        # Other capabilities still present
        assert hasattr(backend, "get_ttl")
        assert hasattr(backend, "with_timeout")

    async def test_create_backend_without_timeout(self):
        """Test creating backend without timeout support."""
        backend = create_backend_with_capabilities(timeout_support=False)

        # Verify timeout removed
        assert not hasattr(backend, "with_timeout")

        # Other capabilities still present
        assert hasattr(backend, "get_ttl")
        assert hasattr(backend, "acquire_lock")

    async def test_create_backend_without_capabilities_list(self):
        """Test creating backend without specific capabilities by name."""
        backend = create_backend_without_capabilities("ttl", "locking")

        # Verify specified capabilities removed
        assert not hasattr(backend, "get_ttl")
        assert not hasattr(backend, "acquire_lock")

        # Timeout still present
        assert hasattr(backend, "with_timeout")

    async def test_create_backend_without_capabilities_invalid(self):
        """Test error handling for invalid capability name."""
        with pytest.raises(ValueError, match="Unknown capability"):
            create_backend_without_capabilities("invalid_capability")

    async def test_create_multi_tenant_provider(self):
        """Test creating multi-tenant provider."""
        provider = create_multi_tenant_provider()

        # Set tenant 1
        mock_tenant_context.set("tenant-1")
        backend1 = provider.get_backend()
        await backend1.set("key", b"value1")

        # Set tenant 2
        mock_tenant_context.set("tenant-2")
        backend2 = provider.get_backend()
        await backend2.set("key", b"value2")

        # Verify isolation
        mock_tenant_context.set("tenant-1")
        backend1_check = provider.get_backend()
        result1 = await backend1_check.get("key")
        assert result1 == b"value1"

        mock_tenant_context.set("tenant-2")
        backend2_check = provider.get_backend()
        result2 = await backend2_check.get("key")
        assert result2 == b"value2"

    async def test_create_failing_backend_on_get(self):
        """Test creating backend that fails on get."""
        from cachekit.backends.errors import BackendError

        backend = create_failing_backend(fail_on_get=True)

        # Verify get fails
        with pytest.raises(BackendError):
            await backend.get("key")

        # Verify other operations work
        await backend.set("key", b"value")
        assert await backend.exists("key")

    async def test_create_failing_backend_on_set(self):
        """Test creating backend that fails on set."""
        from cachekit.backends.errors import BackendError

        backend = create_failing_backend(fail_on_set=True)

        # Verify set fails
        with pytest.raises(BackendError):
            await backend.set("key", b"value")

        # Verify other operations work (get returns None since nothing set)
        result = await backend.get("key")
        assert result is None

    async def test_verify_backend_capabilities_all(self):
        """Test verifying backend with all capabilities."""
        backend = create_backend_with_capabilities(ttl_support=True, locking_support=True, timeout_support=True)

        caps = verify_backend_capabilities(backend)
        assert caps["ttl"] is True
        assert caps["locking"] is True
        assert caps["timeout"] is True

    async def test_verify_backend_capabilities_partial(self):
        """Test verifying backend with partial capabilities."""
        backend = create_backend_with_capabilities(ttl_support=True, locking_support=False, timeout_support=True)

        caps = verify_backend_capabilities(backend)
        assert caps["ttl"] is True
        assert caps["locking"] is False
        assert caps["timeout"] is True

    async def test_verify_backend_capabilities_none(self):
        """Test verifying backend with no optional capabilities."""
        backend = create_backend_with_capabilities(ttl_support=False, locking_support=False, timeout_support=False)

        caps = verify_backend_capabilities(backend)
        assert caps["ttl"] is False
        assert caps["locking"] is False
        assert caps["timeout"] is False


@pytest.mark.asyncio
class TestConfTestFixtures:
    """Test conftest.py fixtures work correctly."""

    async def test_backend_fixture(self, backend):
        """Test basic backend fixture."""
        # Verify it's a PerRequestMockBackend
        assert hasattr(backend, "get")
        assert hasattr(backend, "set")
        assert hasattr(backend, "delete")
        assert hasattr(backend, "exists")

        # Test basic operations
        await backend.set("test_key", b"test_value")
        result = await backend.get("test_key")
        assert result == b"test_value"

    async def test_backend_provider_fixture(self, backend_provider):
        """Test backend provider fixture."""
        # Verify it's a MockBackendProvider
        assert hasattr(backend_provider, "get_backend")

        # Get backend and test
        backend = backend_provider.get_backend()
        await backend.set("test_key", b"test_value")
        result = await backend.get("test_key")
        assert result == b"test_value"

    async def test_mock_backend_with_ttl_support_fixture(self, mock_backend_with_ttl_support):
        """Test mock backend with TTL support fixture."""
        # Verify TTL support
        assert hasattr(mock_backend_with_ttl_support, "get_ttl")
        assert hasattr(mock_backend_with_ttl_support, "refresh_ttl")

        # Test TTL operations
        await mock_backend_with_ttl_support.set("key", b"value", ttl=60)
        ttl = await mock_backend_with_ttl_support.get_ttl("key")
        assert ttl == 60

    async def test_mock_backend_without_locking_fixture(self, mock_backend_without_locking):
        """Test mock backend without locking fixture."""
        # Verify locking not supported
        assert not hasattr(mock_backend_without_locking, "acquire_lock")

        # Verify other operations work
        await mock_backend_without_locking.set("key", b"value")
        result = await mock_backend_without_locking.get("key")
        assert result == b"value"

    async def test_mock_backend_without_timeout_fixture(self, mock_backend_without_timeout):
        """Test mock backend without timeout fixture."""
        # Verify timeout not supported
        assert not hasattr(mock_backend_without_timeout, "with_timeout")

        # Verify other operations work
        await mock_backend_without_timeout.set("key", b"value")
        result = await mock_backend_without_timeout.get("key")
        assert result == b"value"
