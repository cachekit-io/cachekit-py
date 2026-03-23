"""Integration tests for intent decorators with FileBackend.

Verifies that @cache.minimal, @cache.production, @cache.secure, @cache.dev,
@cache.test all work correctly when passed a FileBackend via backend= kwarg,
and that set_default_backend() works with FileBackend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cachekit import cache
from cachekit.backends.file import FileBackend, FileBackendConfig
from cachekit.config.decorator import get_default_backend, set_default_backend


# Override autouse Redis fixture - FileBackend tests don't need Redis
@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation():
    """Override global Redis fixture - FileBackend doesn't need Redis."""
    pass


def _make_file_backend(tmp_path: Path, subdir: str = "cache") -> FileBackend:
    """Create a FileBackend for testing."""
    return FileBackend(
        FileBackendConfig(
            cache_dir=tmp_path / subdir,
            max_size_mb=10,
            max_value_mb=5,
        )
    )


@pytest.mark.integration
class TestIntentDecoratorsWithFileBackend:
    """Test that each intent decorator works with FileBackend via explicit backend= kwarg."""

    def test_cache_minimal_with_file_backend(self, tmp_path: Path) -> None:
        """@cache.minimal(backend=file_backend) caches and returns correct results."""
        backend = _make_file_backend(tmp_path)
        call_count = 0

        @cache.minimal(ttl=300, backend=backend)
        def compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        # First call — cache miss
        assert compute(5) == 10
        assert call_count == 1

        # Second call — cache hit
        assert compute(5) == 10
        assert call_count == 1

        # Different arg — cache miss
        assert compute(7) == 14
        assert call_count == 2

    def test_cache_production_with_file_backend(self, tmp_path: Path) -> None:
        """@cache.production(backend=file_backend) caches with full protections."""
        backend = _make_file_backend(tmp_path)
        call_count = 0

        @cache.production(ttl=600, backend=backend)
        def fetch_data(key: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"key": key, "value": "data"}

        result1 = fetch_data("abc")
        assert result1 == {"key": "abc", "value": "data"}
        assert call_count == 1

        result2 = fetch_data("abc")
        assert result2 == {"key": "abc", "value": "data"}
        assert call_count == 1

    def test_cache_secure_with_file_backend(self, tmp_path: Path) -> None:
        """@cache.secure(backend=file_backend) encrypts and caches."""
        backend = _make_file_backend(tmp_path)
        master_key = "a" * 64  # 32-byte hex key
        call_count = 0

        @cache.secure(master_key=master_key, ttl=300, backend=backend)
        def get_secret(user_id: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"user_id": user_id, "ssn": "123-45-6789"}

        result1 = get_secret("user1")
        assert result1 == {"user_id": "user1", "ssn": "123-45-6789"}
        assert call_count == 1

        result2 = get_secret("user1")
        assert result2 == {"user_id": "user1", "ssn": "123-45-6789"}
        assert call_count == 1

    def test_cache_dev_with_file_backend(self, tmp_path: Path) -> None:
        """@cache.dev(backend=file_backend) caches in dev mode."""
        backend = _make_file_backend(tmp_path)
        call_count = 0

        @cache.dev(ttl=60, backend=backend)
        def dev_fn(x: int) -> str:
            nonlocal call_count
            call_count += 1
            return f"result-{x}"

        assert dev_fn(1) == "result-1"
        assert call_count == 1

        assert dev_fn(1) == "result-1"
        assert call_count == 1

    def test_cache_test_with_file_backend(self, tmp_path: Path) -> None:
        """@cache.test(backend=file_backend) caches in test mode."""
        backend = _make_file_backend(tmp_path)
        call_count = 0

        @cache.test(ttl=10, backend=backend)
        def test_fn(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x + 100

        assert test_fn(5) == 105
        assert call_count == 1

        assert test_fn(5) == 105
        assert call_count == 1

    def test_cache_minimal_with_namespace(self, tmp_path: Path) -> None:
        """@cache.minimal with namespace isolates keys between decorated functions."""
        backend = _make_file_backend(tmp_path)

        @cache.minimal(ttl=300, namespace="ns_a", backend=backend)
        def fn_a(x: int) -> str:
            return f"a-{x}"

        @cache.minimal(ttl=300, namespace="ns_b", backend=backend)
        def fn_b(x: int) -> str:
            return f"b-{x}"

        assert fn_a(1) == "a-1"
        assert fn_b(1) == "b-1"

        # Same arg, different namespace — independent results
        assert fn_a(1) == "a-1"
        assert fn_b(1) == "b-1"


@pytest.mark.integration
class TestSetDefaultBackendWithFileBackend:
    """Test set_default_backend() with FileBackend."""

    def test_set_default_backend_used_by_decorator(self, tmp_path: Path) -> None:
        """set_default_backend(file_backend) is picked up by @cache.minimal without explicit backend=."""
        backend = _make_file_backend(tmp_path)
        original = get_default_backend()

        try:
            set_default_backend(backend)
            call_count = 0

            @cache.minimal(ttl=300)
            def compute(x: int) -> int:
                nonlocal call_count
                call_count += 1
                return x * 3

            assert compute(4) == 12
            assert call_count == 1

            assert compute(4) == 12
            assert call_count == 1
        finally:
            set_default_backend(original)

    def test_explicit_backend_overrides_default(self, tmp_path: Path) -> None:
        """Explicit backend= kwarg takes precedence over set_default_backend()."""
        default_backend = _make_file_backend(tmp_path, subdir="default_cache")
        explicit_backend = _make_file_backend(tmp_path, subdir="explicit_cache")
        original = get_default_backend()

        try:
            set_default_backend(default_backend)
            call_count = 0

            @cache.minimal(ttl=300, backend=explicit_backend)
            def compute(x: int) -> int:
                nonlocal call_count
                call_count += 1
                return x * 5

            assert compute(2) == 10
            assert call_count == 1

            assert compute(2) == 10
            assert call_count == 1

            # Verify data is in explicit backend's directory, not default's
            explicit_files = list((tmp_path / "explicit_cache").glob("*"))
            explicit_files = [f for f in explicit_files if ".tmp." not in f.name]
            assert len(explicit_files) >= 1, "Cache data should be in explicit backend directory"
        finally:
            set_default_backend(original)

    def test_clear_default_backend(self, tmp_path: Path) -> None:
        """set_default_backend(None) clears the default."""
        backend = _make_file_backend(tmp_path)
        original = get_default_backend()

        try:
            set_default_backend(backend)
            assert get_default_backend() is backend

            set_default_backend(None)
            assert get_default_backend() is None
        finally:
            set_default_backend(original)
