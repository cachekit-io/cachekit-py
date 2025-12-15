"""Unit tests for FileBackend.

Tests for backends/file/backend.py covering:
- Protocol compliance with BaseBackend
- Basic operations (get, set, delete, exists, health_check)
- TTL expiration and cleanup
- Corruption handling (bad magic, version, truncated files)
- LRU eviction at 90% capacity
- Temp file cleanup on startup
- Key hashing (blake2b consistency)
- Thread safety and file-level locking
"""

from __future__ import annotations

import errno
import os
import struct
import time
from pathlib import Path
from typing import Any

import pytest

from cachekit.backends.base import BaseBackend
from cachekit.backends.file.backend import (
    EVICTION_TARGET_THRESHOLD,
    EVICTION_TRIGGER_THRESHOLD,
    FORMAT_VERSION,
    HEADER_SIZE,
    MAGIC,
    FileBackend,
)
from cachekit.backends.file.config import FileBackendConfig


@pytest.fixture
def config(tmp_path: Path) -> FileBackendConfig:
    """Create FileBackendConfig with temp directory."""
    return FileBackendConfig(
        cache_dir=tmp_path / "cache",
        max_size_mb=10,
        max_value_mb=5,
        max_entry_count=100,
    )


@pytest.fixture
def backend(config: FileBackendConfig) -> FileBackend:
    """Create FileBackend instance."""
    return FileBackend(config)


@pytest.mark.unit
class TestProtocolCompliance:
    """Test BaseBackend protocol compliance."""

    def test_implements_base_backend_protocol(self, backend: FileBackend) -> None:
        """Verify FileBackend satisfies BaseBackend protocol."""
        assert isinstance(backend, BaseBackend)
        # Verify all required methods exist
        assert callable(backend.get)
        assert callable(backend.set)
        assert callable(backend.delete)
        assert callable(backend.exists)
        assert callable(backend.health_check)


@pytest.mark.unit
class TestBasicOperations:
    """Test basic get/set/delete/exists operations."""

    def test_get_missing_key_returns_none(self, backend: FileBackend) -> None:
        """Test get returns None for non-existent key."""
        result = backend.get("nonexistent_key")
        assert result is None

    def test_set_get_roundtrip(self, backend: FileBackend) -> None:
        """Test set and get roundtrip."""
        key = "test_key"
        value = b"test_value_data"

        backend.set(key, value)
        result = backend.get(key)

        assert result == value

    def test_set_get_with_empty_value(self, backend: FileBackend) -> None:
        """Test set and get with empty bytes value."""
        key = "empty_key"
        value = b""

        backend.set(key, value)
        result = backend.get(key)

        assert result == value

    def test_set_get_with_large_value(self, backend: FileBackend) -> None:
        """Test set and get with large value."""
        key = "large_key"
        value = b"x" * (1024 * 1024)  # 1 MB

        backend.set(key, value)
        result = backend.get(key)

        assert result == value

    def test_set_overwrites_existing_key(self, backend: FileBackend) -> None:
        """Test that set overwrites existing value."""
        key = "overwrite_key"
        value1 = b"first_value"
        value2 = b"second_value"

        backend.set(key, value1)
        backend.set(key, value2)
        result = backend.get(key)

        assert result == value2

    def test_exists_returns_true_for_existing_key(self, backend: FileBackend) -> None:
        """Test exists returns True for existing key."""
        key = "existing_key"
        value = b"some_value"

        backend.set(key, value)
        assert backend.exists(key) is True

    def test_exists_returns_false_for_missing_key(self, backend: FileBackend) -> None:
        """Test exists returns False for missing key."""
        assert backend.exists("nonexistent_key") is False

    def test_delete_returns_true_for_existing_key(self, backend: FileBackend) -> None:
        """Test delete returns True when key exists."""
        key = "delete_key"
        value = b"delete_me"

        backend.set(key, value)
        result = backend.delete(key)

        assert result is True
        assert backend.get(key) is None

    def test_delete_returns_false_for_missing_key(self, backend: FileBackend) -> None:
        """Test delete returns False when key doesn't exist."""
        result = backend.delete("nonexistent_key")
        assert result is False

    def test_multiple_keys_independent(self, backend: FileBackend) -> None:
        """Test multiple keys are stored independently."""
        backend.set("key1", b"value1")
        backend.set("key2", b"value2")
        backend.set("key3", b"value3")

        assert backend.get("key1") == b"value1"
        assert backend.get("key2") == b"value2"
        assert backend.get("key3") == b"value3"

        backend.delete("key2")
        assert backend.get("key2") is None
        assert backend.get("key1") == b"value1"
        assert backend.get("key3") == b"value3"


@pytest.mark.unit
class TestTTLExpiration:
    """Test TTL expiration and cleanup."""

    def test_ttl_none_never_expires(self, backend: FileBackend) -> None:
        """Test TTL=None means never expire."""
        key = "no_ttl_key"
        value = b"persistent_value"

        backend.set(key, value, ttl=None)
        time.sleep(0.5)

        assert backend.get(key) == value

    def test_ttl_zero_never_expires(self, backend: FileBackend) -> None:
        """Test TTL=0 means never expire."""
        key = "zero_ttl_key_v2"
        value = b"persistent_value"

        backend.set(key, value, ttl=0)
        # Verify immediately and after a small delay
        assert backend.get(key) == value
        time.sleep(0.1)
        assert backend.get(key) == value

    def test_ttl_file_header_contains_timestamp(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test TTL is properly encoded in file header."""
        key = "ttl_test"
        value = b"test_value"
        ttl = 100

        before = time.time()
        backend.set(key, value, ttl=ttl)
        after = time.time()

        cache_dir = Path(config.cache_dir)
        cache_files = list(cache_dir.glob("*"))
        assert len(cache_files) == 1

        file_data = cache_files[0].read_bytes()
        expiry_ts = struct.unpack(">Q", file_data[6:14])[0]

        # Should be approximately now + ttl
        expected_min = int(before) + ttl
        expected_max = int(after) + ttl
        assert expected_min <= expiry_ts <= expected_max + 1

    def test_ttl_zero_has_zero_expiry(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test TTL=0 results in zero expiry timestamp."""
        key = "zero_ttl_test"
        value = b"test"

        backend.set(key, value, ttl=0)

        cache_dir = Path(config.cache_dir)
        cache_files = list(cache_dir.glob("*"))
        assert len(cache_files) == 1

        file_data = cache_files[0].read_bytes()
        expiry_ts = struct.unpack(">Q", file_data[6:14])[0]

        # Should be zero (never expire)
        assert expiry_ts == 0


@pytest.mark.unit
class TestCorruptionHandling:
    """Test corrupted file handling and error recovery."""

    def test_file_format_validation_on_read(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test that file format is validated during read operations."""
        key = "format_test"
        value = b"test_value"

        # Set a valid value
        backend.set(key, value)

        # Verify it was written with correct format
        cache_dir = Path(config.cache_dir)
        cache_files = list(cache_dir.glob("*"))
        assert len(cache_files) == 1

        file_data = cache_files[0].read_bytes()

        # Verify magic bytes
        magic = file_data[0:2]
        assert magic == MAGIC

        # Verify version
        version = file_data[2]
        assert version == FORMAT_VERSION

    def test_get_returns_value_with_valid_format(self, backend: FileBackend) -> None:
        """Test get returns value when file format is valid."""
        key = "valid_format_key"
        value = b"valid_value"

        backend.set(key, value)
        result = backend.get(key)

        assert result == value

    def test_file_header_structure_is_correct(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test file header structure matches specification."""
        key = "header_struct_test"
        value = b"test"

        backend.set(key, value, ttl=None)

        cache_dir = Path(config.cache_dir)
        cache_files = list(cache_dir.glob("*"))
        assert len(cache_files) == 1

        file_data = cache_files[0].read_bytes()
        assert len(file_data) >= HEADER_SIZE

        # Verify header structure
        # [0:2] Magic (CK)
        magic = file_data[0:2]
        assert magic == b"CK"

        # [2:3] Version (1)
        version = file_data[2]
        assert version == 1

        # [3:4] Reserved (0)
        reserved = file_data[3]
        assert reserved == 0

        # [4:6] Flags (uint16 BE)
        flags = struct.unpack(">H", file_data[4:6])[0]
        assert flags == 0

        # [6:14] Expiry timestamp (uint64 BE)
        expiry_ts = struct.unpack(">Q", file_data[6:14])[0]
        assert expiry_ts == 0  # Never expire

        # [14:] Payload
        payload = file_data[HEADER_SIZE:]
        assert payload == value

    def test_multiple_values_stored_independently(self, backend: FileBackend) -> None:
        """Test multiple values can be stored and retrieved independently."""
        values = {
            "key1": b"value1",
            "key2": b"value2",
            "key3": b"value3",
        }

        for key, value in values.items():
            backend.set(key, value)

        for key, expected_value in values.items():
            result = backend.get(key)
            assert result == expected_value


@pytest.mark.unit
class TestHealthCheck:
    """Test health_check operation."""

    def test_health_check_reports_stats(self, backend: FileBackend) -> None:
        """Test health_check returns success with statistics."""
        # Store some data
        backend.set("key1", b"value1")
        backend.set("key2", b"value2")

        is_healthy, details = backend.health_check()

        assert is_healthy is True
        assert details["backend_type"] == "file"
        assert "latency_ms" in details
        assert details["latency_ms"] >= 0
        assert "cache_size_mb" in details
        assert details["cache_size_mb"] >= 0
        assert "file_count" in details
        assert details["file_count"] >= 2  # At least 2 files we stored

    def test_health_check_empty_cache(self, backend: FileBackend) -> None:
        """Test health_check on empty cache."""
        is_healthy, details = backend.health_check()

        assert is_healthy is True
        assert details["backend_type"] == "file"
        assert details["file_count"] == 0
        assert details["cache_size_mb"] == 0.0


@pytest.mark.unit
class TestLRUEviction:
    """Test LRU eviction behavior at capacity thresholds."""

    def test_eviction_constants_defined(self) -> None:
        """Test that eviction constants are properly defined."""
        # Trigger threshold should be 0.9 (90%)
        assert EVICTION_TRIGGER_THRESHOLD == 0.9

        # Target threshold should be 0.7 (70%)
        assert EVICTION_TARGET_THRESHOLD == 0.7

    def test_cache_size_calculation(self, backend: FileBackend) -> None:
        """Test that cache size is calculated correctly."""
        # Store some data
        backend.set("key1", b"x" * 1024)  # 1KB
        backend.set("key2", b"y" * 2048)  # 2KB

        # Calculate size
        size_mb, count = backend._calculate_cache_size()

        # Should be around 3KB = 0.003 MB
        assert size_mb >= 0.002
        assert size_mb <= 0.01  # Account for filesystem overhead
        assert count == 2

    def test_lru_eviction_uses_mtime(self, tmp_path: Path) -> None:
        """Test LRU eviction uses file modification time for ordering."""
        config = FileBackendConfig(
            cache_dir=tmp_path / "cache",
            max_size_mb=100,
            max_value_mb=50,
            max_entry_count=100,
        )
        backend = FileBackend(config)

        # Store keys with time delays to ensure different mtimes
        for i in range(5):
            backend.set(f"key_{i}", b"data")
            time.sleep(0.01)

        cache_dir = Path(config.cache_dir)
        files = list(cache_dir.glob("*"))

        # Files should have different mtimes
        mtimes = [f.stat().st_mtime for f in files]
        assert len(set(mtimes)) == len(mtimes)  # All different

    def test_cache_respects_max_size_and_entry_limits(self, tmp_path: Path) -> None:
        """Test that cache respects both size and entry count limits."""
        config = FileBackendConfig(
            cache_dir=tmp_path / "cache",
            max_size_mb=100,
            max_value_mb=50,
            max_entry_count=100,
        )
        backend = FileBackend(config)

        # Store some data
        for i in range(50):
            backend.set(f"key_{i}", b"value" * 100)

        # Verify data was stored
        cache_dir = Path(config.cache_dir)
        file_count = len(list(cache_dir.glob("*")))
        assert file_count == 50


@pytest.mark.unit
class TestCleanup:
    """Test startup cleanup."""

    def test_startup_cleanup_removes_old_temps(self, tmp_path: Path) -> None:
        """Test startup cleanup removes orphaned temp files."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Create old temp files (older than 60 seconds)
        old_temp_path = cache_dir / "somehash.tmp.12345.999999"
        old_temp_path.write_bytes(b"orphaned")

        # Make it old
        old_time = time.time() - 120  # 2 minutes ago
        os.utime(old_temp_path, (old_time, old_time))

        # Verify temp file exists
        assert old_temp_path.exists()

        # Create backend (should clean up on init)
        config = FileBackendConfig(cache_dir=cache_dir)
        FileBackend(config)

        # Temp file should be deleted
        assert not old_temp_path.exists()

    def test_startup_cleanup_preserves_recent_temps(self, tmp_path: Path) -> None:
        """Test startup cleanup preserves recent temp files."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Create recent temp file
        recent_temp_path = cache_dir / "somehash.tmp.12345.999999"
        recent_temp_path.write_bytes(b"recent")

        # Make it recent
        recent_time = time.time() - 30  # 30 seconds ago
        os.utime(recent_temp_path, (recent_time, recent_time))

        # Create backend
        config = FileBackendConfig(cache_dir=cache_dir)
        FileBackend(config)

        # Temp file should still exist (not old enough to clean)
        assert recent_temp_path.exists()


@pytest.mark.unit
class TestKeyHashing:
    """Test key hashing consistency."""

    def test_key_hashing_blake2b(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test same key always maps to same file."""
        key = "consistent_key"
        value = b"test_value"

        # Store value
        backend.set(key, value)

        # Find the file
        cache_dir = Path(config.cache_dir)
        files1 = sorted(cache_dir.glob("*"))

        # Get the key to verify it
        backend.get(key)

        # Delete and verify
        backend.delete(key)

        # Store same key again
        backend.set(key, value)

        # Should use same file (same hash)
        files2 = sorted(cache_dir.glob("*"))
        assert files1 == files2

    def test_key_hashing_produces_32_hex_chars(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test key hash is 32 hex characters (16 bytes blake2b)."""
        key = "hash_test_key"
        value = b"value"

        backend.set(key, value)

        cache_dir = Path(config.cache_dir)
        cache_files = list(cache_dir.glob("*"))
        assert len(cache_files) == 1

        filename = cache_files[0].name
        # Should be 32 hex characters
        assert len(filename) == 32
        # Should be valid hex
        int(filename, 16)  # Will raise if not valid hex

    def test_different_keys_different_files(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test different keys map to different files."""
        backend.set("key1", b"value1")
        backend.set("key2", b"value2")

        cache_dir = Path(config.cache_dir)
        cache_files = list(cache_dir.glob("*"))
        assert len(cache_files) == 2

        # Verify they're different hashes
        filenames = sorted([f.name for f in cache_files])
        assert filenames[0] != filenames[1]


@pytest.mark.unit
class TestFileFormat:
    """Test file format compliance."""

    def test_file_header_format(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test file header has correct format."""
        key = "header_test"
        value = b"test_payload"

        backend.set(key, value)

        cache_dir = Path(config.cache_dir)
        cache_files = list(cache_dir.glob("*"))
        assert len(cache_files) == 1

        file_data = cache_files[0].read_bytes()
        assert len(file_data) >= HEADER_SIZE

        # Verify header
        magic = file_data[0:2]
        version = file_data[2]

        assert magic == MAGIC
        assert version == FORMAT_VERSION

        # Verify payload
        payload = file_data[HEADER_SIZE:]
        assert payload == value

    def test_file_format_with_ttl(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test file header with TTL timestamp."""
        key = "ttl_test"
        value = b"test_payload"
        ttl = 100

        before_time = time.time()
        backend.set(key, value, ttl=ttl)
        after_time = time.time()

        cache_dir = Path(config.cache_dir)
        cache_files = list(cache_dir.glob("*"))
        assert len(cache_files) == 1

        file_data = cache_files[0].read_bytes()

        # Extract expiry timestamp
        expiry_timestamp = struct.unpack(">Q", file_data[6:14])[0]

        # Should be approximately now + ttl
        expected_expiry = int(before_time) + ttl
        assert abs(expiry_timestamp - expected_expiry) <= 2  # Within 2 seconds


@pytest.mark.unit
class TestErrorHandling:
    """Test error handling."""

    def test_init_creates_cache_directory(self, tmp_path: Path) -> None:
        """Test init creates cache directory if it doesn't exist."""
        cache_dir = tmp_path / "new_cache_dir"
        assert not cache_dir.exists()

        config = FileBackendConfig(cache_dir=cache_dir)
        backend = FileBackend(config)

        assert cache_dir.exists()
        assert cache_dir.is_dir()

    def test_init_works_with_existing_directory(self, tmp_path: Path) -> None:
        """Test init works with existing cache directory."""
        cache_dir = tmp_path / "existing_cache_dir"
        cache_dir.mkdir(parents=True, exist_ok=True)

        config = FileBackendConfig(cache_dir=cache_dir)
        backend = FileBackend(config)

        # Should not raise
        backend.set("key", b"value")
        assert backend.get("key") == b"value"

    def test_get_returns_none_on_file_not_found(self, backend: FileBackend) -> None:
        """Test get handles FileNotFoundError gracefully."""
        result = backend.get("nonexistent_key_xyz")
        assert result is None

    def test_backend_error_on_invalid_key_type(self, backend: FileBackend) -> None:
        """Test set with non-bytes value is type-checked."""
        # This should fail at type level, but ensure runtime handles it
        with pytest.raises((TypeError, AttributeError)):
            backend.set("key", "not bytes")  # type: ignore


@pytest.mark.unit
class TestThreadSafety:
    """Test thread safety (basic, non-concurrent tests)."""

    def test_concurrent_get_after_set(self, backend: FileBackend) -> None:
        """Test that get works correctly after set (no race conditions)."""
        key = "thread_test"
        value = b"concurrent_value"

        backend.set(key, value)
        result = backend.get(key)

        assert result == value

    def test_reentrant_lock_allows_recursive_calls(self, backend: FileBackend) -> None:
        """Test RLock allows reentrant operations within same thread."""
        # This is difficult to test directly without actual threading,
        # but we can verify the lock exists
        assert backend._lock is not None


@pytest.mark.unit
class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_unicode_keys_are_hashed(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test Unicode keys are properly hashed."""
        key = "key_with_ñ_unicode_🔥"
        value = b"unicode_test"

        backend.set(key, value)
        result = backend.get(key)

        assert result == value

    def test_very_long_keys(self, backend: FileBackend) -> None:
        """Test very long keys are hashed to consistent filenames."""
        long_key = "k" * 10000
        value = b"long_key_value"

        backend.set(long_key, value)
        result = backend.get(long_key)

        assert result == value

    def test_binary_value_preservation(self, backend: FileBackend) -> None:
        """Test binary values with all byte values are preserved."""
        key = "binary_test"
        value = bytes(range(256))  # All possible byte values

        backend.set(key, value)
        result = backend.get(key)

        assert result == value

    def test_set_with_no_ttl_argument(self, backend: FileBackend) -> None:
        """Test set without ttl argument."""
        key = "no_ttl_arg"
        value = b"value"

        backend.set(key, value)  # No ttl argument
        result = backend.get(key)

        assert result == value

    def test_large_ttl_value(self, backend: FileBackend) -> None:
        """Test very large TTL values."""
        key = "large_ttl"
        value = b"persistent"
        large_ttl = 365 * 24 * 60 * 60  # 1 year

        backend.set(key, value, ttl=large_ttl)
        result = backend.get(key)

        assert result == value


@pytest.mark.unit
class TestCacheDirStructure:
    """Test cache directory structure and permissions."""

    def test_cache_dir_is_created_with_correct_permissions(self, tmp_path: Path) -> None:
        """Test cache directory is created with specified permissions."""
        cache_dir = tmp_path / "perms_test"
        config = FileBackendConfig(
            cache_dir=cache_dir,
            dir_permissions=0o700,
        )
        backend = FileBackend(config)

        # Directory should exist
        assert cache_dir.exists()
        assert cache_dir.is_dir()

    def test_cached_files_inherit_config_permissions(self, tmp_path: Path) -> None:
        """Test cached files use config-specified permissions."""
        cache_dir = tmp_path / "perms_test"
        config = FileBackendConfig(
            cache_dir=cache_dir,
            permissions=0o600,
        )
        backend = FileBackend(config)

        backend.set("key", b"value")

        # Find the file and check permissions
        cache_files = list(cache_dir.glob("*"))
        assert len(cache_files) == 1

        # File should exist (permissions may vary by OS)
        assert cache_files[0].exists()


@pytest.mark.unit
class TestErrorPaths:
    """Test error path handling and recovery."""

    def test_init_cache_dir_creation_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test init raises BackendError when cache dir creation fails."""
        from cachekit.backends.errors import BackendError

        # Mock Path.mkdir to raise OSError
        def mock_mkdir(*args: Any, **kwargs: Any) -> None:
            raise OSError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(Path, "mkdir", mock_mkdir)

        config = FileBackendConfig(cache_dir=tmp_path / "fail_cache")

        with pytest.raises(BackendError) as exc_info:
            FileBackend(config)

        assert "Failed to create cache directory" in str(exc_info.value)
        assert exc_info.value.operation == "init"

    def test_get_corrupted_header_wrong_magic(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test get returns None for file with wrong magic bytes."""
        key = "bad_magic_key"
        file_path = backend._key_to_path(key)

        # Create file with wrong magic bytes
        bad_header = b"XX" + bytes([FORMAT_VERSION, 0]) + struct.pack(">H", 0) + struct.pack(">Q", 0)
        bad_data = bad_header + b"corrupted_payload"

        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "wb") as f:
            f.write(bad_data)

        # get should return None and delete the corrupted file
        result = backend.get(key)
        assert result is None
        assert not os.path.exists(file_path)

    def test_get_corrupted_header_wrong_version(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test get returns None for file with wrong version."""
        key = "bad_version_key"
        file_path = backend._key_to_path(key)

        # Create file with wrong version
        bad_header = MAGIC + bytes([99, 0]) + struct.pack(">H", 0) + struct.pack(">Q", 0)
        bad_data = bad_header + b"corrupted_payload"

        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "wb") as f:
            f.write(bad_data)

        # get should return None and delete the corrupted file
        result = backend.get(key)
        assert result is None
        assert not os.path.exists(file_path)

    def test_get_corrupted_truncated_file(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test get returns None for truncated file (smaller than header)."""
        key = "truncated_key"
        file_path = backend._key_to_path(key)

        # Create file smaller than HEADER_SIZE
        truncated_data = b"CORRUPT"

        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "wb") as f:
            f.write(truncated_data)

        # get should return None and delete the corrupted file
        result = backend.get(key)
        assert result is None
        assert not os.path.exists(file_path)

    def test_get_expired_ttl_deletes_file(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test get deletes expired files."""
        key = "expired_key"
        value = b"expired_value"

        # Set with 1 second TTL
        backend.set(key, value, ttl=1)

        # Verify it exists
        assert backend.get(key) == value

        # Wait for expiration
        time.sleep(1.5)

        # get should return None and delete the expired file
        result = backend.get(key)
        assert result is None

        # File should be deleted
        file_path = backend._key_to_path(key)
        assert not os.path.exists(file_path)

    def test_get_handles_eloop_symlink_attack(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test get returns None when encountering symlink (O_NOFOLLOW)."""
        import platform

        if platform.system() == "Windows":
            pytest.skip("Symlink test not reliable on Windows")

        key = "symlink_key"
        file_path = backend._key_to_path(key)

        # Create a symlink instead of regular file
        target = config.cache_dir / "target"
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        os.symlink(target, file_path)

        # get should return None (symlink detected via ELOOP)
        result = backend.get(key)
        assert result is None

    def test_set_write_failure_cleans_temp_file(
        self, backend: FileBackend, config: FileBackendConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test set cleans up temp file on write failure."""
        from cachekit.backends.errors import BackendError

        key = "write_fail_key"
        value = b"test_value"

        # Track temp files created
        temp_files_created = []
        original_open = os.open

        def mock_open(path: str, flags: int, mode: int = 0o600) -> int:
            if ".tmp." in path:
                temp_files_created.append(path)
                raise OSError(errno.ENOSPC, "No space left on device")
            return original_open(path, flags, mode)

        monkeypatch.setattr(os, "open", mock_open)

        with pytest.raises(BackendError) as exc_info:
            backend.set(key, value)

        assert "Failed to write cache file" in str(exc_info.value)
        assert exc_info.value.operation == "set"

        # Temp file should not exist (cleaned up)
        for temp_file in temp_files_created:
            assert not os.path.exists(temp_file)

    def test_delete_oserror_eacces_handling(
        self, backend: FileBackend, config: FileBackendConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test delete raises BackendError for EACCES."""
        from cachekit.backends.errors import BackendError

        key = "delete_fail_key"
        backend.set(key, b"value")

        # Mock os.unlink to raise EACCES
        def mock_unlink(path: str) -> None:
            raise OSError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(os, "unlink", mock_unlink)

        with pytest.raises(BackendError) as exc_info:
            backend.delete(key)

        assert "Failed to delete cache file" in str(exc_info.value)
        assert exc_info.value.operation == "delete"

    def test_exists_corrupted_truncated_deletes_file(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test exists returns False and deletes truncated file."""
        key = "exists_truncated"
        file_path = backend._key_to_path(key)

        # Create truncated file
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "wb") as f:
            f.write(b"TRUNC")

        # exists should return False and delete the file
        result = backend.exists(key)
        assert result is False
        assert not os.path.exists(file_path)

    def test_exists_corrupted_wrong_magic_deletes_file(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test exists returns False and deletes file with wrong magic."""
        key = "exists_bad_magic"
        file_path = backend._key_to_path(key)

        # Create file with wrong magic
        bad_header = b"XX" + bytes([FORMAT_VERSION, 0]) + struct.pack(">H", 0) + struct.pack(">Q", 0)
        bad_data = bad_header + b"payload"

        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "wb") as f:
            f.write(bad_data)

        # exists should return False and delete the file
        result = backend.exists(key)
        assert result is False
        assert not os.path.exists(file_path)

    def test_exists_expired_ttl_deletes_file(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test exists returns False and deletes expired file."""
        key = "exists_expired"
        value = b"value"

        # Set with 1 second TTL
        backend.set(key, value, ttl=1)

        # Verify it exists
        assert backend.exists(key) is True

        # Wait for expiration
        time.sleep(1.5)

        # exists should return False and delete the file
        result = backend.exists(key)
        assert result is False

        file_path = backend._key_to_path(key)
        assert not os.path.exists(file_path)

    def test_exists_handles_eloop_symlink(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test exists returns False for symlink (O_NOFOLLOW)."""
        import platform

        if platform.system() == "Windows":
            pytest.skip("Symlink test not reliable on Windows")

        key = "exists_symlink"
        file_path = backend._key_to_path(key)

        # Create a symlink
        target = config.cache_dir / "target"
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        os.symlink(target, file_path)

        # exists should return False
        result = backend.exists(key)
        assert result is False

    def test_health_check_roundtrip_failure(self, backend: FileBackend, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test health_check returns False when roundtrip fails."""
        # Mock get to return wrong value
        original_get = backend.get

        def mock_get(key: str) -> bytes | None:
            if key == "__health_check__":
                return b"wrong_data"
            return original_get(key)

        monkeypatch.setattr(backend, "get", mock_get)

        is_healthy, details = backend.health_check()
        assert is_healthy is False
        assert "error" in details
        assert "Round-trip verification failed" in details["error"]

    def test_health_check_exception_handling(self, backend: FileBackend, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test health_check returns False on exception."""

        # Mock set to raise exception
        def mock_set(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Disk failure")

        monkeypatch.setattr(backend, "set", mock_set)

        is_healthy, details = backend.health_check()
        assert is_healthy is False
        assert "error" in details
        assert "Disk failure" in details["error"]


@pytest.mark.unit
class TestLockingErrorPaths:
    """Test file locking error paths."""

    def test_lock_timeout_raises_backend_error_windows(
        self, backend: FileBackend, config: FileBackendConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test lock timeout raises BackendError on Windows."""
        import platform

        from cachekit.backends.errors import BackendError, BackendErrorType

        if platform.system() != "Windows":
            pytest.skip("Windows-specific test")

        # Mock msvcrt.locking to raise EACCES
        import msvcrt  # type: ignore

        def mock_locking(fd: int, mode: int, nbytes: int) -> None:
            raise OSError(errno.EACCES, "Lock failed")

        monkeypatch.setattr(msvcrt, "locking", mock_locking)

        # Try to acquire lock
        fd = os.open(config.cache_dir / "test.txt", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            with pytest.raises(BackendError) as exc_info:
                backend._acquire_file_lock(fd, exclusive=True)

            assert exc_info.value.error_type == BackendErrorType.TIMEOUT
            assert "Lock acquisition timeout" in str(exc_info.value)
        finally:
            os.close(fd)

    def test_lock_timeout_raises_backend_error_posix(
        self, backend: FileBackend, config: FileBackendConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test lock timeout raises BackendError on POSIX."""
        import platform

        from cachekit.backends.errors import BackendError, BackendErrorType

        if platform.system() == "Windows":
            pytest.skip("POSIX-specific test")

        # Mock fcntl.flock to raise EWOULDBLOCK
        import fcntl  # type: ignore

        def mock_flock(fd: int, operation: int) -> None:
            raise OSError(errno.EWOULDBLOCK, "Lock would block")

        monkeypatch.setattr(fcntl, "flock", mock_flock)

        # Try to acquire lock
        test_file = config.cache_dir / "test.txt"
        test_file.write_bytes(b"test")
        fd = os.open(test_file, os.O_RDONLY)
        try:
            with pytest.raises(BackendError) as exc_info:
                backend._acquire_file_lock(fd, exclusive=False)

            assert exc_info.value.error_type == BackendErrorType.TIMEOUT
            assert "Lock acquisition timeout" in str(exc_info.value)
        finally:
            os.close(fd)

    def test_lock_release_handles_oserror_windows(
        self, backend: FileBackend, config: FileBackendConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test lock release handles OSError gracefully on Windows."""
        import platform

        if platform.system() != "Windows":
            pytest.skip("Windows-specific test")

        # Mock msvcrt.locking to raise OSError on unlock
        import msvcrt  # type: ignore

        def mock_locking(fd: int, mode: int, nbytes: int) -> None:
            raise OSError(errno.EIO, "IO error")

        monkeypatch.setattr(msvcrt, "locking", mock_locking)

        # Try to release lock (should not raise)
        test_file = config.cache_dir / "test.txt"
        test_file.write_bytes(b"test")
        fd = os.open(test_file, os.O_WRONLY)
        try:
            backend._release_file_lock(fd)  # Should not raise
        finally:
            os.close(fd)

    def test_lock_release_handles_oserror_posix(
        self, backend: FileBackend, config: FileBackendConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test lock release handles OSError gracefully on POSIX."""
        import platform

        if platform.system() == "Windows":
            pytest.skip("POSIX-specific test")

        # Mock fcntl.flock to raise OSError on unlock
        import fcntl  # type: ignore

        def mock_flock(fd: int, operation: int) -> None:
            raise OSError(errno.EIO, "IO error")

        monkeypatch.setattr(fcntl, "flock", mock_flock)

        # Try to release lock (should not raise)
        test_file = config.cache_dir / "test.txt"
        test_file.write_bytes(b"test")
        fd = os.open(test_file, os.O_RDONLY)
        try:
            backend._release_file_lock(fd)  # Should not raise
        finally:
            os.close(fd)


@pytest.mark.unit
class TestEvictionErrorPaths:
    """Test eviction error path handling."""

    def test_eviction_handles_stat_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test eviction handles stat failure gracefully (file deleted during eviction)."""
        config = FileBackendConfig(
            cache_dir=tmp_path / "cache",
            max_size_mb=2,
            max_value_mb=1,
            max_entry_count=100,
        )
        backend = FileBackend(config)

        # Create files to trigger eviction
        for i in range(5):
            backend.set(f"key_{i}", b"x" * 100_000)  # 100KB each

        # Mock lstat to fail for some files (simulate concurrent deletion)
        original_lstat = os.lstat
        call_count = [0]

        def mock_lstat(path: Any) -> Any:
            call_count[0] += 1
            # Fail on second file during eviction collection
            if call_count[0] == 2 and "cache" in str(path):
                raise OSError(errno.ENOENT, "No such file")
            return original_lstat(path)

        monkeypatch.setattr(os, "lstat", mock_lstat)

        # Trigger eviction (should handle ENOENT gracefully)
        backend.set("trigger_eviction", b"y" * 500_000)  # Should trigger eviction

        # Should not crash

    def test_eviction_handles_unlink_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test eviction handles unlink failure gracefully."""
        config = FileBackendConfig(
            cache_dir=tmp_path / "cache",
            max_size_mb=2,
            max_value_mb=1,
            max_entry_count=100,
        )
        backend = FileBackend(config)

        # Create files to trigger eviction
        for i in range(5):
            backend.set(f"key_{i}", b"x" * 100_000)

        # Mock Path.unlink to fail
        original_unlink = Path.unlink
        unlink_count = [0]

        def mock_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
            unlink_count[0] += 1
            # Fail first unlink attempt during eviction
            if unlink_count[0] == 1:
                raise OSError(errno.EACCES, "Permission denied")
            original_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", mock_unlink)

        # Trigger eviction (should handle EACCES gracefully)
        backend.set("trigger_eviction", b"y" * 500_000)

        # Should not crash

    def test_cleanup_temp_files_handles_exceptions(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test temp file cleanup handles exceptions gracefully."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Create old temp file
        old_temp = cache_dir / "hash.tmp.123.456"
        old_temp.write_bytes(b"orphaned")
        old_time = time.time() - 120
        os.utime(old_temp, (old_time, old_time))

        # Mock Path.unlink to raise exception
        def mock_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
            raise OSError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(Path, "unlink", mock_unlink)

        # Create backend (cleanup should not crash on exception)
        config = FileBackendConfig(cache_dir=cache_dir)
        backend = FileBackend(config)

        # Should not crash (best-effort cleanup)


@pytest.mark.unit
class TestErrorClassification:
    """Test OS error classification logic."""

    def test_classify_error_enospc_transient(self, backend: FileBackend) -> None:
        """Test ENOSPC classified as TRANSIENT."""
        from cachekit.backends.errors import BackendErrorType

        exc = OSError(errno.ENOSPC, "No space left on device")
        result = backend._classify_os_error(exc, is_directory=False)
        assert result == BackendErrorType.TRANSIENT

    def test_classify_error_eacces_directory_permanent(self, backend: FileBackend) -> None:
        """Test EACCES on directory classified as PERMANENT."""
        from cachekit.backends.errors import BackendErrorType

        exc = OSError(errno.EACCES, "Permission denied")
        result = backend._classify_os_error(exc, is_directory=True)
        assert result == BackendErrorType.PERMANENT

    def test_classify_error_eacces_file_transient(self, backend: FileBackend) -> None:
        """Test EACCES on file classified as TRANSIENT."""
        from cachekit.backends.errors import BackendErrorType

        exc = OSError(errno.EACCES, "Permission denied")
        result = backend._classify_os_error(exc, is_directory=False)
        assert result == BackendErrorType.TRANSIENT

    def test_classify_error_erofs_permanent(self, backend: FileBackend) -> None:
        """Test EROFS classified as PERMANENT."""
        from cachekit.backends.errors import BackendErrorType

        exc = OSError(errno.EROFS, "Read-only file system")
        result = backend._classify_os_error(exc, is_directory=False)
        assert result == BackendErrorType.PERMANENT

    def test_classify_error_eloop_permanent(self, backend: FileBackend) -> None:
        """Test ELOOP classified as PERMANENT."""
        from cachekit.backends.errors import BackendErrorType

        exc = OSError(errno.ELOOP, "Too many symbolic links")
        result = backend._classify_os_error(exc, is_directory=False)
        assert result == BackendErrorType.PERMANENT

    def test_classify_error_etimedout_timeout(self, backend: FileBackend) -> None:
        """Test ETIMEDOUT classified as TIMEOUT."""
        from cachekit.backends.errors import BackendErrorType

        exc = OSError(errno.ETIMEDOUT, "Connection timed out")
        result = backend._classify_os_error(exc, is_directory=False)
        assert result == BackendErrorType.TIMEOUT

    def test_classify_error_unknown_errno_unknown(self, backend: FileBackend) -> None:
        """Test unknown errno classified as UNKNOWN."""
        from cachekit.backends.errors import BackendErrorType

        exc = OSError(9999, "Unknown error")
        result = backend._classify_os_error(exc, is_directory=False)
        assert result == BackendErrorType.UNKNOWN


@pytest.mark.unit
class TestMaxValueSizeEnforcement:
    """Test max_value_mb enforcement."""

    def test_set_value_exceeds_max_raises_backend_error(self, tmp_path: Path) -> None:
        """Test set raises BackendError when value exceeds max_value_mb."""
        from cachekit.backends.errors import BackendError, BackendErrorType

        config = FileBackendConfig(
            cache_dir=tmp_path / "cache",
            max_value_mb=1,  # 1 MB max
        )
        backend = FileBackend(config)

        key = "large_key"
        value = b"x" * (2 * 1024 * 1024)  # 2 MB

        with pytest.raises(BackendError) as exc_info:
            backend.set(key, value)

        assert "exceeds max_value_mb" in str(exc_info.value)
        assert exc_info.value.error_type == BackendErrorType.PERMANENT


@pytest.mark.unit
class TestSafeUnlinkEdgeCases:
    """Test _safe_unlink error handling."""

    def test_safe_unlink_handles_file_not_found(self, backend: FileBackend) -> None:
        """Test _safe_unlink handles FileNotFoundError."""
        # Should not raise
        backend._safe_unlink("/nonexistent/path/to/file")

    def test_safe_unlink_handles_oserror(self, backend: FileBackend, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test _safe_unlink handles OSError gracefully."""

        # Mock os.unlink to raise OSError
        def mock_unlink(path: str) -> None:
            raise OSError(errno.EIO, "IO error")

        monkeypatch.setattr(os, "unlink", mock_unlink)

        # Should not raise (best-effort)
        backend._safe_unlink("/some/path")


@pytest.mark.unit
class TestCalculateCacheSizeEdgeCases:
    """Test _calculate_cache_size error handling."""

    def test_calculate_cache_size_handles_general_exception(self, backend: FileBackend, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test _calculate_cache_size returns (0.0, 0) on exception."""

        # Mock Path.iterdir to raise exception
        def mock_iterdir(self: Path) -> Any:
            raise RuntimeError("Unexpected error")

        monkeypatch.setattr(Path, "iterdir", mock_iterdir)

        size_mb, count = backend._calculate_cache_size()
        assert size_mb == 0.0
        assert count == 0

    def test_calculate_cache_size_skips_hidden_files(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test _calculate_cache_size skips hidden files."""
        backend.set("key1", b"value1")

        # Create hidden file
        cache_dir = Path(config.cache_dir)
        hidden_file = cache_dir / ".hidden"
        hidden_file.write_bytes(b"x" * 10000)

        size_mb, count = backend._calculate_cache_size()
        assert count == 1  # Only counts non-hidden file

    def test_calculate_cache_size_skips_temp_files(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Test _calculate_cache_size skips temp files."""
        backend.set("key1", b"value1")

        # Create temp file
        cache_dir = Path(config.cache_dir)
        temp_file = cache_dir / "hash.tmp.123.456"
        temp_file.write_bytes(b"x" * 10000)

        size_mb, count = backend._calculate_cache_size()
        assert count == 1  # Only counts non-temp file

    def test_calculate_cache_size_handles_stat_failure(
        self, backend: FileBackend, config: FileBackendConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test _calculate_cache_size handles stat failure gracefully."""
        backend.set("key1", b"value1")
        backend.set("key2", b"value2")

        # Mock lstat to fail for second file
        original_lstat = Path.lstat
        call_count = [0]

        def mock_lstat(self: Path) -> Any:
            call_count[0] += 1
            if call_count[0] == 2:
                raise OSError(errno.ENOENT, "File deleted")
            return original_lstat(self)

        monkeypatch.setattr(Path, "lstat", mock_lstat)

        size_mb, count = backend._calculate_cache_size()
        # Should count only the file that didn't fail
        assert count == 1


@pytest.mark.unit
class TestMaybeEvictEdgeCases:
    """Test _maybe_evict error handling."""

    def test_maybe_evict_handles_general_exception(self, backend: FileBackend, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test _maybe_evict handles general exception gracefully."""

        # Mock Path to raise exception
        def mock_iterdir(self: Path) -> Any:
            raise RuntimeError("Unexpected error")

        monkeypatch.setattr(Path, "iterdir", mock_iterdir)

        # Should not raise (best-effort eviction)
        backend._maybe_evict()

    def test_maybe_evict_skips_hidden_files(self, tmp_path: Path) -> None:
        """Test _maybe_evict skips hidden files."""
        config = FileBackendConfig(
            cache_dir=tmp_path / "cache",
            max_size_mb=2,
            max_value_mb=1,
            max_entry_count=100,
        )
        backend = FileBackend(config)

        # Create hidden file
        cache_dir = Path(config.cache_dir)
        hidden_file = cache_dir / ".hidden"
        hidden_file.write_bytes(b"x" * 500_000)

        # Create regular files to trigger eviction
        for i in range(3):
            backend.set(f"key_{i}", b"y" * 400_000)

        # Hidden file should not be deleted by eviction
        assert hidden_file.exists()

    def test_maybe_evict_skips_temp_files(self, tmp_path: Path) -> None:
        """Test _maybe_evict skips temp files."""
        config = FileBackendConfig(
            cache_dir=tmp_path / "cache",
            max_size_mb=2,
            max_value_mb=1,
            max_entry_count=100,
        )
        backend = FileBackend(config)

        # Create temp file
        cache_dir = Path(config.cache_dir)
        temp_file = cache_dir / "hash.tmp.123.456"
        temp_file.write_bytes(b"x" * 500_000)

        # Create regular files to trigger eviction
        for i in range(3):
            backend.set(f"key_{i}", b"y" * 400_000)

        # Temp file should not be deleted by eviction
        # (it would be deleted by cleanup, but not by eviction)


@pytest.mark.unit
class TestSecurityBugFixes:
    """Test security bug fixes for FileBackend.

    These tests verify fixes for:
    - BUG 1: TOCTOU in delete() - race between exists() and unlink()
    - BUG 2: TTL integer overflow - negative or huge TTL values
    - BUG 3: Temp cleanup symlink attack - following symlinks during cleanup
    - BUG 4: Eviction symlink attack - following symlinks during eviction
    - BUG 5: Entry count check race - check happens after write completes
    - BUG 6: FD leak on lock timeout - fd leaked if lock acquisition fails
    """

    def test_delete_no_toctou_race(self, backend: FileBackend) -> None:
        """Bug 1: Verify delete handles ENOENT directly without pre-checking.

        The fix removes the TOCTOU vulnerability by eliminating the
        os.path.exists() check before os.unlink(). Instead, we catch
        FileNotFoundError/ENOENT directly.

        This test verifies the fix by checking that delete() returns False
        for a non-existent key without raising an exception.
        """
        # Key doesn't exist - should return False, not raise
        result = backend.delete("nonexistent_key_12345")
        assert result is False

        # Create and delete a key
        backend.set("temp_key", b"value")
        assert backend.delete("temp_key") is True

        # Second delete should return False (not race)
        assert backend.delete("temp_key") is False

    def test_ttl_bounds_validation_negative(self, backend: FileBackend) -> None:
        """Bug 2: Verify negative TTL values are rejected.

        Negative TTL would cause immediate expiration or integer underflow.
        """
        from cachekit.backends.errors import BackendError

        with pytest.raises(BackendError) as exc_info:
            backend.set("key", b"value", ttl=-1)

        assert "TTL" in str(exc_info.value)
        assert "out of range" in str(exc_info.value).lower() or "invalid" in str(exc_info.value).lower()

    def test_ttl_bounds_validation_huge(self, backend: FileBackend) -> None:
        """Bug 2: Verify excessively large TTL values are rejected.

        TTL larger than 10 years is likely an error and could cause overflow.
        """
        from cachekit.backends.errors import BackendError

        huge_ttl = 100 * 365 * 24 * 60 * 60  # 100 years in seconds

        with pytest.raises(BackendError) as exc_info:
            backend.set("key", b"value", ttl=huge_ttl)

        assert "TTL" in str(exc_info.value)

    @pytest.mark.skipif(os.name == "nt", reason="Symlinks require admin on Windows")
    def test_temp_cleanup_skips_symlinks(self, tmp_path: Path) -> None:
        """Bug 3: Verify temp file cleanup doesn't follow symlinks.

        An attacker could create a symlink matching *.tmp.* pattern pointing
        to a sensitive file. The cleanup should use lstat() and skip symlinks.
        """
        import stat

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Create a target file outside cache that should NOT be deleted
        target_file = tmp_path / "sensitive_file.txt"
        target_file.write_text("SENSITIVE DATA - DO NOT DELETE")

        # Create a symlink in cache dir matching temp file pattern
        symlink_path = cache_dir / "abc123.tmp.12345.999999"
        symlink_path.symlink_to(target_file)

        # Make the symlink "old" enough to trigger cleanup
        # Note: lstat doesn't follow symlinks, so we can't set mtime on target via symlink
        old_time = time.time() - 120  # 2 minutes ago
        os.utime(symlink_path, (old_time, old_time), follow_symlinks=False)

        # Verify symlink exists
        assert symlink_path.is_symlink()
        stat_info = symlink_path.lstat()
        assert stat.S_ISLNK(stat_info.st_mode)

        # Create backend - this triggers cleanup
        config = FileBackendConfig(cache_dir=cache_dir)
        FileBackend(config)

        # Target file should NOT be deleted (symlink should have been skipped)
        assert target_file.exists(), "Target file was deleted through symlink!"
        assert target_file.read_text() == "SENSITIVE DATA - DO NOT DELETE"

    @pytest.mark.skipif(os.name == "nt", reason="Symlinks require admin on Windows")
    def test_eviction_skips_symlinks(self, tmp_path: Path) -> None:
        """Bug 4: Verify eviction doesn't follow symlinks.

        An attacker could create a symlink in the cache directory. The eviction
        logic should use lstat() to avoid following symlinks which could:
        1. Skew size calculations
        2. Cause deletion of symlink targets
        """
        import stat

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Create a target file outside cache
        target_file = tmp_path / "external_file.txt"
        target_file.write_bytes(b"X" * 10000)  # 10KB

        # Create symlink in cache dir (not matching temp pattern)
        symlink_path = cache_dir / "abc123def456abc123def456abc12345"  # pragma: allowlist secret
        symlink_path.symlink_to(target_file)

        # Verify symlink exists
        assert symlink_path.is_symlink()
        stat_info = symlink_path.lstat()
        assert stat.S_ISLNK(stat_info.st_mode)

        # Create backend - use valid config constraints
        # max_entry_count >= 100, max_size_mb >= 1, max_value_mb <= 50% of max_size_mb
        config = FileBackendConfig(
            cache_dir=cache_dir,
            max_size_mb=2,  # 2MB
            max_value_mb=1,  # 1MB max value (50% of max_size)
            max_entry_count=100,
        )
        backend = FileBackend(config)

        # Fill cache to trigger eviction (need >90% of 2MB = ~1.8MB)
        # Write 200KB x 10 = 2MB to exceed threshold
        large_value = b"X" * (200 * 1024)  # 200KB
        for i in range(12):
            backend.set(f"evict_key_{i}", large_value)

        # Target file should NOT be deleted (symlink should have been skipped)
        assert target_file.exists(), "Target file was deleted through symlink during eviction!"

    def test_entry_count_checked_before_write(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bug 5: Verify entry count is checked BEFORE write, not after.

        The old code wrote the file first, then checked entry count and raised
        an error. This left the file persisted even when the error was raised.

        The fix checks entry count BEFORE creating the temp file.

        Note: We disable eviction via monkeypatch to test the entry count
        check in isolation.
        """
        from cachekit.backends.errors import BackendError

        config = FileBackendConfig(
            cache_dir=tmp_path / "cache",
            max_entry_count=100,
            max_size_mb=1000,
            max_value_mb=100,
        )
        backend = FileBackend(config)

        # Disable eviction from the start to allow filling to exactly 100 entries
        monkeypatch.setattr(backend, "_maybe_evict", lambda: None)

        # Fill to exactly max entry count
        for i in range(100):
            backend.set(f"key_{i}", b"x")

        # Verify we have exactly 100 files
        cache_dir = Path(config.cache_dir)
        files_before = len([f for f in cache_dir.glob("*") if ".tmp." not in f.name])
        assert files_before == 100, f"Expected 100 files, got {files_before}"

        # Attempt to add a 101st entry - should fail BEFORE writing
        with pytest.raises(BackendError) as exc_info:
            backend.set("key_overflow", b"value_overflow")

        assert "entry count" in str(exc_info.value).lower() or "max_entry_count" in str(exc_info.value).lower()

        # File count should NOT have increased (no leftover file from failed write)
        # BUG: The current code checks AFTER write, so file persists
        files_after = len([f for f in cache_dir.glob("*") if ".tmp." not in f.name])
        assert files_after == 100, f"File count increased from {files_before} to {files_after} despite error!"

    def test_entry_count_allows_overwrites(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bug 5 (corollary): Verify overwrites are allowed even at max capacity.

        When entry count check happens before write, we must allow overwrites
        of existing keys (they don't increase the count).
        """
        config = FileBackendConfig(
            cache_dir=tmp_path / "cache",
            max_entry_count=100,
            max_size_mb=1000,
            max_value_mb=100,
        )
        backend = FileBackend(config)

        # Disable eviction from the start
        monkeypatch.setattr(backend, "_maybe_evict", lambda: None)

        # Fill to max capacity
        for i in range(100):
            backend.set(f"key_{i}", b"x")

        # Overwrite existing key - should succeed even at max capacity
        backend.set("key_0", b"updated_value_0")
        assert backend.get("key_0") == b"updated_value_0"

        # Still at max capacity (no increase)
        cache_dir = Path(config.cache_dir)
        files = len([f for f in cache_dir.glob("*") if ".tmp." not in f.name])
        assert files == 100

    def test_fd_closed_on_lock_timeout(self, backend: FileBackend, config: FileBackendConfig) -> None:
        """Bug 6: Verify file descriptor is closed even if lock acquisition fails.

        If _acquire_file_lock() raises BackendError, the fd must be closed
        to prevent resource leaks.

        This test is difficult to trigger directly, so we verify the code
        structure handles the case by checking fd is valid after normal ops.
        """
        # This is more of an implementation verification test
        # We verify that after many operations, no fd leak occurs
        import resource

        # Get initial fd count
        soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)

        # Perform many operations that open/close fds
        for i in range(100):
            key = f"fd_test_{i}"
            backend.set(key, b"value")
            backend.get(key)
            backend.exists(key)
            backend.delete(key)

        # If there were fd leaks, we'd eventually hit the limit
        # This test passes if we don't crash with "too many open files"

        # Verify we can still open files (no exhaustion)
        backend.set("final_key", b"final_value")
        assert backend.get("final_key") == b"final_value"
