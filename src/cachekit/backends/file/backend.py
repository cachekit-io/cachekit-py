"""File-based backend implementation with thread-safe operations and LRU eviction.

This module implements BaseBackend protocol for filesystem-based caching with:
- Thread-safe operations using RLock and file-level locking (fcntl/msvcrt)
- Atomic writes via write-then-rename pattern
- LRU eviction triggered at 90% capacity, evicting to 70%
- TTL-based expiration with secure 14-byte header format
- Security features: O_NOFOLLOW, realpath resolution, permission enforcement
- Blake2b key hashing (16 bytes hex = 32 chars) for filename safety
"""

from __future__ import annotations

import errno
import hashlib
import os
import platform
import struct
import threading
import time
from pathlib import Path
from typing import Any

from cachekit.backends.errors import BackendError, BackendErrorType

# Conditional imports for platform-specific locking
if platform.system() == "Windows":
    pass
else:
    pass  # type: ignore[import-not-found]

# Header format constants (14 bytes total)
MAGIC: bytes = b"CK"  # [0:2] File identification
FORMAT_VERSION: int = 1  # [2:3] Version byte
RESERVED: int = 0  # [3:4] Reserved for future use
FLAGS_SIZE: int = 2  # [4:6] Compression/encryption flags (uint16 BE)
TIMESTAMP_SIZE: int = 8  # [6:14] Expiry timestamp (uint64 BE, 0 = never expire)
HEADER_SIZE: int = 14

# Eviction thresholds
EVICTION_TRIGGER_THRESHOLD: float = 0.9  # Trigger at 90% capacity
EVICTION_TARGET_THRESHOLD: float = 0.7  # Evict to 70% capacity

# Cleanup settings
TEMP_FILE_MAX_AGE_SECONDS: int = 60  # Delete orphaned temp files older than 60s

# TTL bounds (security: prevent integer overflow)
MAX_TTL_SECONDS: int = 10 * 365 * 24 * 60 * 60  # 10 years max


class FileBackend:
    """File-based backend for local disk caching.

    Implements BaseBackend protocol with thread-safe operations, atomic writes,
    LRU eviction, and TTL-based expiration.

    Thread Safety:
        - Uses threading.RLock() for reentrant locking of internal state
        - Uses fcntl.flock() (Linux/macOS) or msvcrt.locking() (Windows) for file-level locks
        - Safe for concurrent access from multiple threads in same process

    Security:
        - Uses O_NOFOLLOW to prevent symlink attacks
        - Uses os.path.realpath() to resolve paths
        - Respects permissions and dir_permissions from config
        - Blake2b hashing prevents directory traversal attacks

    LRU Eviction:
        - Triggered when cache size exceeds 90% of max_size_mb
        - Evicts least-recently-used files until cache is at 70% capacity
        - Based on file mtime (modification time)

    Example:
        >>> from cachekit.backends.file import FileBackend  # doctest: +SKIP
        >>> from cachekit.backends.file.config import FileBackendConfig  # doctest: +SKIP
        >>> config = FileBackendConfig(cache_dir="/tmp/cachekit", max_size_mb=100)  # doctest: +SKIP
        >>> backend = FileBackend(config)  # doctest: +SKIP
        >>> backend.set("user:123", b"data", ttl=60)  # doctest: +SKIP
        >>> data = backend.get("user:123")  # doctest: +SKIP
    """

    def __init__(self, config: Any) -> None:  # Type will be FileBackendConfig once Task 1 completes
        """Initialize FileBackend with configuration.

        Args:
            config: FileBackendConfig instance with cache directory, size limits, etc.

        Raises:
            BackendError: If cache directory creation fails
        """
        self.config = config
        self._lock = threading.RLock()  # Reentrant lock for internal state

        # Ensure cache directory exists
        try:
            cache_path = Path(config.cache_dir)
            cache_path.mkdir(parents=True, exist_ok=True, mode=config.dir_permissions)
        except OSError as exc:
            raise BackendError(
                f"Failed to create cache directory: {exc}",
                error_type=self._classify_os_error(exc, is_directory=True),
                original_exception=exc,
                operation="init",
            ) from exc

        # Cleanup orphaned temp files on startup
        self._cleanup_temp_files()

    def get(self, key: str) -> bytes | None:
        """Retrieve value from file storage.

        Args:
            key: Cache key to retrieve

        Returns:
            Bytes value if found and not expired, None if key doesn't exist or expired

        Raises:
            BackendError: If file read fails (permissions, disk error, etc.)
        """
        file_path = self._key_to_path(key)

        with self._lock:
            try:
                # Open with O_NOFOLLOW for security (prevents symlink attacks)
                fd = os.open(file_path, os.O_RDONLY | os.O_NOFOLLOW)
                fd_closed = False
                try:
                    # Acquire shared read lock
                    self._acquire_file_lock(fd, exclusive=False)

                    try:
                        # Read entire file
                        file_data = os.read(fd, os.fstat(fd).st_size)

                        # Validate header
                        if len(file_data) < HEADER_SIZE:
                            # Corrupted file, delete it
                            os.close(fd)
                            fd_closed = True
                            self._safe_unlink(file_path)
                            return None

                        # Parse header
                        magic = file_data[0:2]
                        version = file_data[2]
                        # flags = struct.unpack(">H", file_data[4:6])[0]  # uint16 BE (reserved for future)
                        expiry_timestamp = struct.unpack(">Q", file_data[6:14])[0]  # uint64 BE

                        # Validate magic and version
                        if magic != MAGIC or version != FORMAT_VERSION:
                            # Corrupted or wrong version, delete it
                            os.close(fd)
                            fd_closed = True
                            self._safe_unlink(file_path)
                            return None

                        # Check expiration (0 means never expire)
                        if expiry_timestamp > 0 and time.time() > expiry_timestamp:
                            # Expired, delete it
                            os.close(fd)
                            fd_closed = True
                            self._safe_unlink(file_path)
                            return None

                        # Extract payload
                        payload = file_data[HEADER_SIZE:]
                        return payload

                    finally:
                        self._release_file_lock(fd)
                finally:
                    if not fd_closed:
                        os.close(fd)

            except FileNotFoundError:
                return None
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return None
                if exc.errno == errno.ELOOP:
                    # Symlink detected (O_NOFOLLOW), treat as not found
                    return None
                raise BackendError(
                    f"Failed to read cache file: {exc}",
                    error_type=self._classify_os_error(exc, is_directory=False),
                    original_exception=exc,
                    operation="get",
                    key=key,
                ) from exc

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        """Store value in file storage with atomic write.

        Uses write-then-rename pattern for atomicity:
        1. Write to temp file: {hash}.tmp.{pid}.{ns}
        2. fsync the file
        3. rename to final path (atomic on POSIX)

        Args:
            key: Cache key to store
            value: Bytes value to store
            ttl: Time-to-live in seconds (None or 0 = never expire)

        Raises:
            BackendError: If write fails (disk full, permissions, etc.)
        """
        # Enforce max_value_mb
        max_bytes = self.config.max_value_mb * 1024 * 1024
        if len(value) > max_bytes:
            raise BackendError(
                f"Value size {len(value)} exceeds max_value_mb ({self.config.max_value_mb}MB)",
                BackendErrorType.PERMANENT,
            )

        file_path = self._key_to_path(key)

        # Calculate expiry timestamp (0 = never expire)
        if ttl is None or ttl == 0:
            expiry_timestamp = 0
        else:
            # Validate TTL bounds (security: prevent integer overflow/underflow)
            if ttl < 0 or ttl > MAX_TTL_SECONDS:
                raise BackendError(
                    f"TTL {ttl} out of range [0, {MAX_TTL_SECONDS}] (max 10 years)",
                    BackendErrorType.PERMANENT,
                )
            expiry_timestamp = int(time.time() + ttl)

        # Build header (14 bytes)
        header = (
            MAGIC  # [0:2] Magic bytes
            + bytes([FORMAT_VERSION])  # [2:3] Version
            + bytes([RESERVED])  # [3:4] Reserved
            + struct.pack(">H", 0)  # [4:6] Flags (no compression/encryption yet)
            + struct.pack(">Q", expiry_timestamp)  # [6:14] Expiry timestamp
        )

        # Combine header + payload
        file_data = header + value

        # Generate temp file name
        temp_path = self._generate_temp_path(file_path)

        with self._lock:
            try:
                # Check entry count BEFORE write (security: prevent file persisting on error)
                # Allow overwrites (existing key doesn't increase count)
                if self.config.max_entry_count > 0:
                    _, entry_count = self._calculate_cache_size()
                    # Only check if this is a NEW entry (not overwriting existing)
                    if not os.path.exists(file_path) and entry_count >= self.config.max_entry_count:
                        raise BackendError(
                            f"Entry count {entry_count} would exceed max_entry_count ({self.config.max_entry_count})",
                            BackendErrorType.PERMANENT,
                        )

                # Write to temp file with O_NOFOLLOW for security
                fd = os.open(
                    temp_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    self.config.permissions,
                )
                try:
                    # Acquire exclusive write lock
                    self._acquire_file_lock(fd, exclusive=True)

                    try:
                        # Write all data
                        os.write(fd, file_data)

                        # fsync to ensure data is on disk
                        os.fsync(fd)

                    finally:
                        self._release_file_lock(fd)
                finally:
                    os.close(fd)

                # Atomic rename (POSIX guarantees atomicity)
                os.rename(temp_path, file_path)

                # Trigger eviction if over threshold
                self._maybe_evict()

            except OSError as exc:
                # Clean up temp file if it exists
                self._safe_unlink(temp_path)

                raise BackendError(
                    f"Failed to write cache file: {exc}",
                    error_type=self._classify_os_error(exc, is_directory=False),
                    original_exception=exc,
                    operation="set",
                    key=key,
                ) from exc

    def delete(self, key: str) -> bool:
        """Delete key from file storage.

        Args:
            key: Cache key to delete

        Returns:
            True if key was deleted, False if key didn't exist

        Raises:
            BackendError: If delete fails (permissions, etc.)
        """
        file_path = self._key_to_path(key)

        with self._lock:
            try:
                os.unlink(file_path)
                return True
            except FileNotFoundError:
                return False
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return False
                raise BackendError(
                    f"Failed to delete cache file: {exc}",
                    error_type=self._classify_os_error(exc, is_directory=False),
                    original_exception=exc,
                    operation="delete",
                    key=key,
                ) from exc

    def exists(self, key: str) -> bool:
        """Check if key exists in file storage (not expired).

        Args:
            key: Cache key to check

        Returns:
            True if key exists and not expired, False otherwise

        Raises:
            BackendError: If check fails
        """
        file_path = self._key_to_path(key)

        with self._lock:
            try:
                # Open with O_NOFOLLOW for security (prevents symlink attacks)
                fd = os.open(file_path, os.O_RDONLY | os.O_NOFOLLOW)
                fd_closed = False
                try:
                    # Acquire shared read lock
                    self._acquire_file_lock(fd, exclusive=False)

                    try:
                        # Read header only
                        header_data = os.read(fd, HEADER_SIZE)

                        if len(header_data) < HEADER_SIZE:
                            # Corrupted, clean up
                            os.close(fd)
                            fd_closed = True
                            self._safe_unlink(file_path)
                            return False

                        # Parse expiry timestamp
                        magic = header_data[0:2]
                        version = header_data[2]
                        expiry_timestamp = struct.unpack(">Q", header_data[6:14])[0]

                        # Validate magic and version
                        if magic != MAGIC or version != FORMAT_VERSION:
                            os.close(fd)
                            fd_closed = True
                            self._safe_unlink(file_path)
                            return False

                        # Check expiration
                        if expiry_timestamp > 0 and time.time() > expiry_timestamp:
                            # Expired, clean up
                            os.close(fd)
                            fd_closed = True
                            self._safe_unlink(file_path)
                            return False

                        return True

                    finally:
                        self._release_file_lock(fd)
                finally:
                    if not fd_closed:
                        os.close(fd)

            except FileNotFoundError:
                return False
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return False
                if exc.errno == errno.ELOOP:
                    # Symlink detected (O_NOFOLLOW), treat as not found
                    return False
                raise BackendError(
                    f"Failed to check cache file existence: {exc}",
                    error_type=self._classify_os_error(exc, is_directory=False),
                    original_exception=exc,
                    operation="exists",
                    key=key,
                ) from exc

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        """Check backend health status.

        Returns:
            Tuple of (is_healthy, details_dict)
            Details include: latency_ms, backend_type, cache_size_mb, file_count

        Example:
            >>> backend = FileBackend(config)  # doctest: +SKIP
            >>> is_healthy, details = backend.health_check()  # doctest: +SKIP
            >>> print(details["backend_type"])  # doctest: +SKIP
            file
        """
        start_time = time.time()

        try:
            # Test write/read/delete cycle
            test_key = "__health_check__"
            test_value = b"health_check_data"

            self.set(test_key, test_value, ttl=60)
            retrieved = self.get(test_key)
            self.delete(test_key)

            # Verify round-trip
            if retrieved != test_value:
                return False, {
                    "backend_type": "file",
                    "latency_ms": (time.time() - start_time) * 1000,
                    "error": "Round-trip verification failed",
                }

            # Calculate cache statistics
            cache_size_mb, file_count = self._calculate_cache_size()

            latency_ms = (time.time() - start_time) * 1000

            return True, {
                "backend_type": "file",
                "latency_ms": latency_ms,
                "cache_size_mb": cache_size_mb,
                "file_count": file_count,
                "max_size_mb": self.config.max_size_mb,
                "max_entry_count": self.config.max_entry_count,
            }

        except Exception as exc:
            return False, {
                "backend_type": "file",
                "latency_ms": (time.time() - start_time) * 1000,
                "error": str(exc),
            }

    # Private helper methods

    def _key_to_path(self, key: str) -> str:
        """Convert cache key to file path using blake2b hash.

        Args:
            key: Cache key

        Returns:
            Absolute file path (32-char hex hash)
        """
        # Use blake2b with 16 bytes digest = 32 hex chars
        key_hash = hashlib.blake2b(key.encode("utf-8"), digest_size=16).hexdigest()
        return os.path.join(os.path.realpath(self.config.cache_dir), key_hash)

    def _generate_temp_path(self, target_path: str) -> str:
        """Generate unique temp file path for atomic write.

        Args:
            target_path: Final target file path

        Returns:
            Temp file path: {hash}.tmp.{pid}.{ns}
        """
        base = os.path.basename(target_path)
        dirname = os.path.dirname(target_path)
        pid = os.getpid()
        ns = time.time_ns()
        return os.path.join(dirname, f"{base}.tmp.{pid}.{ns}")

    def _safe_unlink(self, path: str) -> None:
        """Safely delete file, ignoring ENOENT errors.

        Args:
            path: File path to delete
        """
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            pass  # Best-effort cleanup

    def _cleanup_temp_files(self) -> None:
        """Delete orphaned temp files older than 60 seconds on startup."""
        import stat as stat_module

        try:
            cache_dir = Path(self.config.cache_dir)
            current_time = time.time()

            for temp_file in cache_dir.glob("*.tmp.*"):
                try:
                    # Use lstat() to avoid following symlinks (security: prevent symlink attacks)
                    stat_info = temp_file.lstat()

                    # Skip symlinks entirely (security: never operate on symlinks)
                    if stat_module.S_ISLNK(stat_info.st_mode):
                        continue

                    if current_time - stat_info.st_mtime > TEMP_FILE_MAX_AGE_SECONDS:
                        temp_file.unlink()
                except OSError:
                    pass  # Best-effort cleanup
        except Exception:  # noqa: S110
            pass  # Don't fail init on cleanup errors

    def _calculate_cache_size(self) -> tuple[float, int]:
        """Calculate total cache size in MB and file count.

        Returns:
            Tuple of (size_mb, file_count)
        """
        import stat as stat_module

        try:
            cache_dir = Path(self.config.cache_dir)
            total_bytes = 0
            file_count = 0

            for file_path in cache_dir.iterdir():
                if file_path.name.startswith("."):
                    continue
                # Skip temp files
                if ".tmp." in file_path.name:
                    continue
                try:
                    # Use lstat() to avoid following symlinks (security)
                    stat_info = file_path.lstat()
                    # Skip symlinks and non-regular files
                    if not stat_module.S_ISREG(stat_info.st_mode):
                        continue
                    total_bytes += stat_info.st_size
                    file_count += 1
                except OSError:
                    pass  # File might have been deleted

            return total_bytes / (1024 * 1024), file_count

        except Exception:
            return 0.0, 0

    def _maybe_evict(self) -> None:
        """Trigger LRU eviction if cache exceeds 90% capacity.

        Evicts least-recently-used files (by mtime) until cache is at 70% capacity.
        Respects both max_size_mb and max_entry_count limits.
        """
        import stat as stat_module

        cache_size_mb, file_count = self._calculate_cache_size()

        # Check if eviction needed (90% threshold)
        size_trigger = cache_size_mb > (self.config.max_size_mb * EVICTION_TRIGGER_THRESHOLD)
        count_trigger = file_count > (self.config.max_entry_count * EVICTION_TRIGGER_THRESHOLD)

        if not (size_trigger or count_trigger):
            return

        # Calculate target thresholds (70%)
        target_size_mb = self.config.max_size_mb * EVICTION_TARGET_THRESHOLD
        target_count = int(self.config.max_entry_count * EVICTION_TARGET_THRESHOLD)

        try:
            cache_dir = Path(self.config.cache_dir)

            # Collect all cache files with mtime
            files_with_mtime = []
            for file_path in cache_dir.iterdir():
                if file_path.name.startswith("."):
                    continue
                # Skip temp files
                if ".tmp." in file_path.name:
                    continue
                try:
                    # Use lstat() to avoid following symlinks (security)
                    stat_info = file_path.lstat()
                    # Skip symlinks and non-regular files
                    if not stat_module.S_ISREG(stat_info.st_mode):
                        continue
                    files_with_mtime.append((file_path, stat_info.st_mtime, stat_info.st_size))
                except OSError:
                    pass  # File might have been deleted

            # Sort by mtime (oldest first)
            files_with_mtime.sort(key=lambda x: x[1])

            # Evict files until below target thresholds
            current_size_mb = cache_size_mb
            current_count = file_count

            for file_path, _, file_size in files_with_mtime:
                # Check if we've reached target thresholds
                if current_size_mb <= target_size_mb and current_count <= target_count:
                    break

                # Delete file
                try:
                    file_path.unlink()
                    current_size_mb -= file_size / (1024 * 1024)
                    current_count -= 1
                except OSError:
                    pass  # File might have been deleted by another thread

        except Exception:  # noqa: S110
            pass  # Best-effort eviction, don't fail the operation

    def _acquire_file_lock(self, fd: int, exclusive: bool) -> None:
        """Acquire file-level lock (fcntl on POSIX, msvcrt on Windows).

        Args:
            fd: File descriptor
            exclusive: True for exclusive lock, False for shared lock

        Raises:
            BackendError: If lock acquisition times out
        """
        if platform.system() == "Windows":
            # Windows: msvcrt.locking (always exclusive)
            import msvcrt  # type: ignore[import-not-found]

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            except OSError as exc:
                if exc.errno == errno.EACCES or exc.errno == errno.EAGAIN:
                    raise BackendError(
                        "Lock acquisition timeout",
                        error_type=BackendErrorType.TIMEOUT,
                        original_exception=exc,
                        operation="lock",
                    ) from exc
                raise
        else:
            # POSIX: fcntl.flock
            import fcntl  # type: ignore[import-not-found]

            lock_type = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            try:
                fcntl.flock(fd, lock_type | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno == errno.EWOULDBLOCK or exc.errno == errno.EAGAIN:
                    raise BackendError(
                        "Lock acquisition timeout",
                        error_type=BackendErrorType.TIMEOUT,
                        original_exception=exc,
                        operation="lock",
                    ) from exc
                raise

    def _release_file_lock(self, fd: int) -> None:
        """Release file-level lock.

        Args:
            fd: File descriptor
        """
        if platform.system() == "Windows":
            import msvcrt  # type: ignore[import-not-found]

            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            except OSError:
                pass  # Best-effort unlock
        else:
            import fcntl  # type: ignore[import-not-found]

            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass  # Best-effort unlock

    def _classify_os_error(self, exc: OSError, is_directory: bool) -> BackendErrorType:
        """Classify OSError into BackendErrorType for retry logic.

        Args:
            exc: OSError to classify
            is_directory: True if error is on directory, False if on file

        Returns:
            BackendErrorType for circuit breaker decisions
        """
        # ENOSPC (disk full) → TRANSIENT (might clear up)
        if exc.errno == errno.ENOSPC:
            return BackendErrorType.TRANSIENT

        # EACCES (permission denied)
        if exc.errno == errno.EACCES:
            # On directory: PERMANENT (won't fix itself)
            # On file: TRANSIENT (might be locked temporarily)
            return BackendErrorType.PERMANENT if is_directory else BackendErrorType.TRANSIENT

        # EROFS (read-only filesystem) → PERMANENT
        if exc.errno == errno.EROFS:
            return BackendErrorType.PERMANENT

        # ELOOP (symlink loop) → PERMANENT
        if exc.errno == errno.ELOOP:
            return BackendErrorType.PERMANENT

        # ETIMEDOUT → TIMEOUT
        if exc.errno == errno.ETIMEDOUT:
            return BackendErrorType.TIMEOUT

        # Default: UNKNOWN (assume transient)
        return BackendErrorType.UNKNOWN
