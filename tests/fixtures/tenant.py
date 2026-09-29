"""Tenant-context helper shared by the tenant-switching tests."""

from collections.abc import Iterator
from contextlib import contextmanager

from cachekit.backends.redis.provider import tenant_context


@contextmanager
def as_tenant(tenant: object) -> Iterator[None]:
    """Run the block with ``tenant_context`` set to ``tenant``; works around sync and async calls alike."""
    token = tenant_context.set(tenant)  # type: ignore[arg-type]  # unsupported types are the point of some tests
    try:
        yield
    finally:
        tenant_context.reset(token)
