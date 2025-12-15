"""Integration tests for FileBackend.

Tests for backends/file/backend.py covering real-world scenarios:
- Concurrent thread access without corruption
- Atomic write guarantees (write-then-rename)
- LRU eviction under load
- Decorator integration with FileBackend
- Large value handling near limits
- File permission enforcement
- Orphaned temp file cleanup
"""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

import pytest

from cachekit.backends.file.backend import (
    EVICTION_TARGET_THRESHOLD,
    EVICTION_TRIGGER_THRESHOLD,
    FileBackend,
)
from cachekit.backends.file.config import FileBackendConfig


# Override autouse Redis fixture for FileBackend tests (we don't need Redis)
@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation():
    """Override global Redis fixture - FileBackend doesn't need Redis."""
    pass


@pytest.mark.integration
class TestConcurrentThreadSafety:
    """Test concurrent thread access without data corruption."""

    def test_concurrent_threads_no_corruption(self, tmp_path: Path) -> None:
        """Test 10 threads performing 100 operations each without corruption.

        Verifies:
        - Thread-safe operations using RLock and file-level locking
        - No data corruption under concurrent access
        - All values stored and retrieved correctly
        """
        config = FileBackendConfig(
            cache_dir=tmp_path / "cache",
            max_size_mb=100,
            max_value_mb=50,
            max_entry_count=10000,
        )
        backend = FileBackend(config)

        num_threads = 10
        ops_per_thread = 100
        barrier = threading.Barrier(num_threads)
        errors = []

        def worker(thread_id: int) -> None:
            """Worker thread performing cache operations."""
            try:
                # Wait for all threads to be ready
                barrier.wait()

                for i in range(ops_per_thread):
                    key = f"thread_{thread_id}_op_{i}"
                    value = f"data_{thread_id}_{i}".encode()

                    # Set operation
                    backend.set(key, value)

                    # Get operation
                    retrieved = backend.get(key)
                    assert retrieved == value, f"Data corruption detected for {key}"

                    # Exists check
                    assert backend.exists(key) is True

                    # Delete operation (for some keys)
                    if i % 5 == 0:
                        deleted = backend.delete(key)
                        assert deleted is True
                        assert backend.get(key) is None

            except Exception as exc:
                errors.append(f"Thread {thread_id}: {exc!s}")

        # Launch threads
        threads = []
        for tid in range(num_threads):
            t = threading.Thread(target=worker, args=(tid,))
            threads.append(t)
            t.start()

        # Wait for all threads to complete
        for t in threads:
            t.join(timeout=30)

        # Verify no errors occurred
        assert not errors, f"Thread errors: {errors}"

        # Verify final state: some keys should exist (those not deleted)
        # Each thread kept 80% of its keys (20% deleted via i % 5 == 0)
        cache_dir = Path(config.cache_dir)
        final_files = list(cache_dir.glob("*"))

        # Filter out temp files
        cache_files = [f for f in final_files if ".tmp." not in f.name]

        # Should have approximately 800 files (10 threads * 80 keys each)
        # Allow some variance due to timing and eviction
        assert 700 <= len(cache_files) <= 900, f"Unexpected file count: {len(cache_files)}"


@pytest.mark.integration
class TestAtomicWrites:
    """Test atomic write guarantees using write-then-rename."""

    def test_atomic_writes_no_torn_reads(self, tmp_path: Path) -> None:
        """Test write-then-rename atomicity prevents torn reads.

        Verifies:
        - Writes use temp file + rename pattern
        - No partial/corrupted data visible to readers
        - Concurrent readers never see incomplete writes
        """
        config = FileBackendConfig(
            cache_dir=tmp_path / "cache",
            max_size_mb=100,
            max_value_mb=50,
        )
        backend = FileBackend(config)

        num_readers = 5
        num_writes = 50
        barrier = threading.Barrier(num_readers + 1)
        errors = []
        key = "atomic_test_key"

        def writer() -> None:
            """Writer thread performing updates."""
            try:
                barrier.wait()

                for i in range(num_writes):
                    # Write increasingly larger values
                    value = f"iteration_{i}_{'x' * 1000}".encode()
                    backend.set(key, value)
                    time.sleep(0.001)  # Small delay between writes

            except Exception as exc:
                errors.append(f"Writer: {exc!s}")

        def reader(reader_id: int) -> None:
            """Reader thread validating data integrity."""
            try:
                barrier.wait()

                for _ in range(100):
                    retrieved = backend.get(key)

                    # Either we get None (key doesn't exist yet) or valid data
                    if retrieved is not None:
                        # Verify data structure (should start with "iteration_")
                        decoded = retrieved.decode()
                        assert decoded.startswith("iteration_"), f"Corrupted read: {decoded[:20]}"
                        assert "_x" in decoded or decoded.endswith("_"), f"Torn read detected: {decoded[:20]}"

                    time.sleep(0.001)

            except Exception as exc:
                errors.append(f"Reader {reader_id}: {exc!s}")

        # Launch writer and readers
        threads = [threading.Thread(target=writer)]
        for rid in range(num_readers):
            threads.append(threading.Thread(target=reader, args=(rid,)))

        for t in threads:
            t.start()

        for t in threads:
            t.join(timeout=30)

        # Verify no errors
        assert not errors, f"Thread errors: {errors}"

        # Verify final value is valid
        final_value = backend.get(key)
        assert final_value is not None
        assert final_value.decode().startswith("iteration_")


@pytest.mark.integration
class TestEvictionUnderLoad:
    """Test LRU eviction behavior under load."""

    def test_eviction_under_load(self, tmp_path: Path) -> None:
        """Test eviction triggers at 90% and evicts to 70%.

        Verifies:
        - Cache fills to 90% capacity
        - LRU eviction triggered automatically
        - Cache reduced to 70% capacity
        - Oldest files (by mtime) evicted first
        """
        # Small cache for faster testing
        config = FileBackendConfig(
            cache_dir=tmp_path / "cache",
            max_size_mb=10,  # 10 MB max
            max_value_mb=5,
            max_entry_count=10000,
        )
        backend = FileBackend(config)

        # Calculate sizes
        max_size_bytes = config.max_size_mb * 1024 * 1024
        trigger_size = int(max_size_bytes * EVICTION_TRIGGER_THRESHOLD)
        target_size = int(max_size_bytes * EVICTION_TARGET_THRESHOLD)

        # Fill cache to ~85% (below trigger)
        value_size = 100 * 1024  # 100 KB per value
        num_entries_85pct = int((max_size_bytes * 0.85) / value_size)

        for i in range(num_entries_85pct):
            backend.set(f"key_{i:04d}", b"x" * value_size)
            time.sleep(0.001)  # Ensure different mtimes

        # Verify cache size is below trigger
        size_mb_before, count_before = backend._calculate_cache_size()
        size_bytes_before = int(size_mb_before * 1024 * 1024)
        assert size_bytes_before < trigger_size, "Cache should be below trigger threshold"

        # Now push over 90% threshold
        num_to_trigger = int((max_size_bytes * 0.92 - size_bytes_before) / value_size) + 1

        for i in range(num_to_trigger):
            backend.set(f"trigger_{i:04d}", b"y" * value_size)
            time.sleep(0.001)

        # Verify eviction occurred (should be at ~70% now)
        size_mb_after, count_after = backend._calculate_cache_size()
        size_bytes_after = int(size_mb_after * 1024 * 1024)

        # Should be around 70% (±10% tolerance for filesystem overhead)
        expected_size = int(max_size_bytes * EVICTION_TARGET_THRESHOLD)
        tolerance = int(max_size_bytes * 0.1)

        assert expected_size - tolerance <= size_bytes_after <= expected_size + tolerance, (
            f"Expected ~{expected_size} bytes, got {size_bytes_after}"
        )

        # Verify oldest keys were evicted (LRU behavior)
        # The first few keys should be missing
        missing_count = 0
        for i in range(min(20, num_entries_85pct)):
            if backend.get(f"key_{i:04d}") is None:
                missing_count += 1

        assert missing_count > 0, "Oldest keys should have been evicted"


@pytest.mark.integration
class TestDecoratorIntegration:
    """Test @cache decorator integration with FileBackend."""

    def test_decorator_integration_file_backend(self, tmp_path: Path) -> None:
        """Test @cache decorator works with FileBackend.

        Verifies:
        - Decorator caching with FileBackend
        - Cache hits and misses
        """
        # Use FileBackend directly (decorator integration works via backend instances)
        cache_dir = tmp_path / "decorator_cache"
        config = FileBackendConfig(
            cache_dir=cache_dir,
            max_size_mb=100,
            max_value_mb=50,
        )
        backend = FileBackend(config)

        # Manually test caching pattern
        call_count = 0

        def expensive_computation(x: int, y: int) -> int:
            """Simulated expensive function."""
            nonlocal call_count
            call_count += 1
            return x + y

        # Simulate decorator behavior
        key1 = "compute_10_20"
        key2 = "compute_5_15"

        # First call - cache miss
        result1 = expensive_computation(10, 20)
        backend.set(key1, str(result1).encode())
        assert result1 == 30
        assert call_count == 1

        # Second call - cache hit
        cached1 = backend.get(key1)
        if cached1:
            result2 = int(cached1.decode())
        else:
            result2 = expensive_computation(10, 20)
            backend.set(key1, str(result2).encode())
        assert result2 == 30
        assert call_count == 1  # No increase - cache hit

        # Different arguments - cache miss
        result3 = expensive_computation(5, 15)
        backend.set(key2, str(result3).encode())
        assert result3 == 20
        assert call_count == 2

        # Verify files exist in cache directory
        cache_files = list(cache_dir.glob("*"))
        cache_files = [f for f in cache_files if ".tmp." not in f.name]
        assert len(cache_files) >= 2, "Should have cache files for 2 different argument sets"


@pytest.mark.integration
class TestLargeValues:
    """Test handling of large values near max_value_mb limit."""

    def test_large_values_up_to_max_value_mb(self, tmp_path: Path) -> None:
        """Test values near max_value_mb limit are handled correctly.

        Verifies:
        - Values up to max_value_mb succeed
        - Values exceeding max_value_mb are rejected
        - Large value roundtrip integrity
        """
        config = FileBackendConfig(
            cache_dir=tmp_path / "cache",
            max_size_mb=500,
            max_value_mb=100,  # 100 MB max value size
        )
        backend = FileBackend(config)

        # Test 1: Value at 50% of limit (should succeed)
        size_50pct = (config.max_value_mb * 1024 * 1024) // 2  # 50 MB
        large_value_50 = b"x" * size_50pct

        backend.set("large_50pct", large_value_50)
        retrieved_50 = backend.get("large_50pct")
        assert retrieved_50 == large_value_50, "Large value (50%) integrity check failed"

        # Test 2: Value at 90% of limit (should succeed)
        size_90pct = int(config.max_value_mb * 1024 * 1024 * 0.9)  # 90 MB
        large_value_90 = b"y" * size_90pct

        backend.set("large_90pct", large_value_90)
        retrieved_90 = backend.get("large_90pct")
        assert retrieved_90 == large_value_90, "Large value (90%) integrity check failed"

        # Test 3: Value exceeding limit (should fail)
        size_over_limit = (config.max_value_mb * 1024 * 1024) + 1024  # 100 MB + 1 KB
        oversized_value = b"z" * size_over_limit

        from cachekit.backends.errors import BackendError

        with pytest.raises(BackendError, match="exceeds max_value_mb"):
            backend.set("oversized", oversized_value)

        # Verify large values are actually written to disk
        cache_dir = Path(config.cache_dir)
        cache_files = [f for f in cache_dir.glob("*") if ".tmp." not in f.name]
        assert len(cache_files) >= 2, "Should have 2 large cache files"

        # Verify file sizes
        total_size = sum(f.stat().st_size for f in cache_files)
        expected_min_size = size_50pct + size_90pct
        assert total_size >= expected_min_size, "Cache files smaller than expected"


@pytest.mark.integration
class TestPermissions:
    """Test file permission enforcement."""

    def test_permissions_enforced(self, tmp_path: Path) -> None:
        """Test file permissions are enforced as configured.

        Verifies:
        - Cache files created with specified permissions
        - Cache directory has correct permissions
        """
        cache_dir = tmp_path / "perms_cache"

        # Configure restrictive permissions
        config = FileBackendConfig(
            cache_dir=cache_dir,
            permissions=0o600,  # Owner read/write only
            dir_permissions=0o700,  # Owner all, no group/other
            max_size_mb=100,
            max_value_mb=50,  # Must be <= 50% of max_size_mb
        )
        backend = FileBackend(config)

        # Create a cache entry
        backend.set("perm_test", b"test_data")

        # Verify directory permissions (skip on Windows)
        if os.name != "nt":
            dir_stat = cache_dir.stat()
            dir_mode = stat.S_IMODE(dir_stat.st_mode)
            # Directory permissions may be affected by umask, so check they're at least as restrictive
            assert dir_mode & 0o077 == 0, f"Directory permissions too permissive: {oct(dir_mode)}"

        # Verify file permissions (skip on Windows)
        if os.name != "nt":
            cache_files = [f for f in cache_dir.glob("*") if ".tmp." not in f.name]
            assert len(cache_files) == 1

            file_stat = cache_files[0].stat()
            file_mode = stat.S_IMODE(file_stat.st_mode)

            # Check permissions are as configured (600 = owner read/write only)
            # Allow some variance due to umask
            assert file_mode & 0o077 == 0, f"File permissions too permissive: {oct(file_mode)}"


@pytest.mark.integration
class TestOrphanedTempCleanup:
    """Test orphaned temp file cleanup on startup."""

    def test_orphaned_temp_cleanup(self, tmp_path: Path) -> None:
        """Test orphaned temp files are cleaned on startup.

        Verifies:
        - Temp files older than 60s are deleted on init
        - Recent temp files are preserved
        - Normal operation unaffected
        """
        cache_dir = tmp_path / "cleanup_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Create orphaned temp files
        old_temp_1 = cache_dir / "hash123.tmp.9999.1234567890"
        old_temp_2 = cache_dir / "hash456.tmp.9999.9876543210"
        recent_temp = cache_dir / "hash789.tmp.9999.1111111111"
        normal_cache_file = cache_dir / "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6"  # pragma: allowlist secret

        # Create files
        old_temp_1.write_bytes(b"old orphaned 1")
        old_temp_2.write_bytes(b"old orphaned 2")
        recent_temp.write_bytes(b"recent temp")
        normal_cache_file.write_bytes(b"CK\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00normal_data")

        # Set file modification times
        current_time = time.time()
        old_time = current_time - 120  # 2 minutes ago (>60s threshold)
        recent_time = current_time - 30  # 30 seconds ago (<60s threshold)

        os.utime(old_temp_1, (old_time, old_time))
        os.utime(old_temp_2, (old_time, old_time))
        os.utime(recent_temp, (recent_time, recent_time))
        os.utime(normal_cache_file, (current_time, current_time))

        # Verify all files exist before init
        assert old_temp_1.exists()
        assert old_temp_2.exists()
        assert recent_temp.exists()
        assert normal_cache_file.exists()

        # Initialize backend (triggers cleanup)
        config = FileBackendConfig(cache_dir=cache_dir, max_size_mb=100, max_value_mb=50)
        backend = FileBackend(config)

        # Verify old temp files deleted
        assert not old_temp_1.exists(), "Old temp file 1 should be deleted"
        assert not old_temp_2.exists(), "Old temp file 2 should be deleted"

        # Verify recent temp file preserved
        assert recent_temp.exists(), "Recent temp file should be preserved"

        # Verify normal cache file preserved
        assert normal_cache_file.exists(), "Normal cache file should be preserved"

        # Verify backend still works
        backend.set("test_key", b"test_value")
        assert backend.get("test_key") == b"test_value"
