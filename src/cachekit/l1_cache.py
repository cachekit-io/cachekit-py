"""L1 in-memory cache implementation with TTL respect and memory bounds.

This module provides a thread-safe L1 cache that sits in front of Redis (L2),
dramatically reducing network latency while maintaining Redis as the source of truth.
"""

import logging
import random
import threading
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """L1 cache entry with value, TTL, and size tracking.

    Storage: Stores bytes (encrypted or plaintext msgpack) for unified
    encrypted-at-rest architecture. Decryption/deserialization happens at
    read time in CacheHandler, not storage time.
    """

    value: bytes
    expires_at: float
    size_bytes: int
    cached_at: Optional[float] = None
    namespace: Optional[str] = None

    def is_expired(self) -> bool:
        """Check if entry has expired."""
        return time.time() >= self.expires_at


class L1Cache:
    """Thread-safe L1 in-memory cache with TTL and memory management.

    Key features:
    - Thread-safe with RLock for concurrent access
    - Respects Redis TTL (entries expire at Redis TTL time)
    - Memory bounded (100MB default limit)
    - LRU eviction when memory limit reached
    - Fast lookups (~50ns for hits)
    - Background TTL synchronization
    - Stores bytes (encrypted or plaintext msgpack), not Python objects

    Storage Architecture:
    - Stores serialized bytes for unified encrypted-at-rest
    - Supports both encrypted bytes (when encryption enabled) and plaintext msgpack
    - Decryption/deserialization happens at read time (not storage time)

    This cache eliminates the 1,000μs network latency for cache hits
    while maintaining eventual consistency with Redis.
    """

    def __init__(
        self,
        max_memory_mb: int = 100,
        ttl_buffer_seconds: float = 1.0,
        namespace: str = "default",
        config: Optional[Any] = None,
    ):
        """Initialize L1 cache.

        Args:
            max_memory_mb: Maximum memory usage in MB (default 100MB)
            ttl_buffer_seconds: Buffer time before Redis TTL expiry (default 1s)
            namespace: Cache namespace for isolation
            config: Optional L1CacheConfig for SWR and invalidation features
        """
        self.max_memory_bytes = max_memory_mb * 1024 * 1024
        self.ttl_buffer_seconds = ttl_buffer_seconds
        self.namespace = namespace

        # Thread-safe cache storage
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.RLock()

        # Memory tracking
        self._current_memory_bytes = 0
        self._eviction_count = 0

        # Performance metrics
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._expired_evictions = 0

        # SWR configuration
        self._swr_enabled = config.swr_enabled if config else True
        self._swr_threshold_ratio = config.swr_threshold_ratio if config else 0.5

        # SWR state tracking
        self._refreshing_keys: set[str] = set()
        self._entry_version: dict[str, int] = {}

        # Namespace index for O(1) invalidation (optional)
        if config and config.namespace_index:
            self._namespace_index: dict[str, set[str]] = defaultdict(set)

        logger.info(
            "L1Cache initialized: namespace=%s, max_memory=%dMB, ttl_buffer=%.1fs, swr=%s",
            namespace,
            max_memory_mb,
            ttl_buffer_seconds,
            self._swr_enabled,
        )

    def _estimate_size(self, value: bytes) -> int:
        """Estimate memory size of bytes value.

        Simplified from recursive sys.getsizeof() - faster and more accurate
        for bytes storage.

        Args:
            value: Bytes to estimate size for

        Returns:
            Size in bytes
        """
        return len(value)

    def get(self, key: str) -> tuple[bool, Optional[bytes]]:
        """Get value from L1 cache if present and not expired.

        Args:
            key: Cache key

        Returns:
            Tuple of (found, value) where found is True if hit.
            Value is bytes (encrypted or plaintext msgpack), not deserialized object.
            Caller (CacheHandler) is responsible for decryption/deserialization.
        """
        with self._lock:
            entry = self._cache.get(key)

            if entry is None:
                self._misses += 1
                return False, None

            # Check TTL
            if entry.is_expired():
                # Remove expired entry
                self._remove_entry(key)
                self._misses += 1
                self._expired_evictions += 1
                return False, None

            # LRU: Move to end
            self._cache.move_to_end(key)
            self._hits += 1

            return True, entry.value

    def get_with_swr(self, key: str, ttl: float) -> tuple[bool, Optional[bytes], bool, int]:
        """Get value with stale-while-revalidate support.

        Args:
            key: Cache key
            ttl: TTL in seconds for SWR threshold calculation

        Returns:
            Tuple of (hit, value, needs_refresh, version):
            - hit: Whether key was found in cache
            - value: Cached bytes or None
            - needs_refresh: Whether background refresh should be triggered
            - version: Entry version at time of read (for refresh completion check)
        """
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return False, None, False, 0

            entry = self._cache[key]

            # Check hard expiry
            if entry.is_expired():
                self._remove_entry(key)
                self._misses += 1
                self._expired_evictions += 1
                return False, None, False, 0

            # LRU: Move to end
            self._cache.move_to_end(key)
            self._hits += 1

            # Current version for this key
            version = self._entry_version.get(key, 0)

            # Check SWR threshold with jitter to prevent cross-pod stampede
            needs_refresh = False
            if self._swr_enabled and entry.cached_at:
                # Add ±10% jitter to threshold to stagger refreshes across pods
                jitter = random.uniform(0.9, 1.1)  # noqa: S311
                threshold = ttl * self._swr_threshold_ratio * jitter
                elapsed = time.time() - entry.cached_at

                # Check if refresh needed and not already refreshing
                if elapsed > threshold and key not in self._refreshing_keys:
                    self._refreshing_keys.add(key)
                    needs_refresh = True

            return True, entry.value, needs_refresh, version

    def complete_refresh(self, key: str, version: int, new_value: bytes, new_cached_at: float) -> bool:
        """Complete a background refresh.

        Args:
            key: Cache key
            version: Version token from get_with_swr call
            new_value: New value bytes
            new_cached_at: Timestamp when value was cached

        Returns:
            True if write succeeded, False if version mismatch (entry was invalidated)
        """
        with self._lock:
            # Remove from refreshing set regardless of outcome
            self._refreshing_keys.discard(key)

            # Version check: was this entry invalidated while we were refreshing?
            current_version = self._entry_version.get(key, 0)
            if current_version != version:
                # Entry was invalidated during refresh - don't resurrect stale data
                logger.debug("Refresh aborted for key %s: version mismatch (%d != %d)", key, version, current_version)
                return False

            # Update entry in place
            if key in self._cache:
                entry = self._cache[key]
                entry.value = new_value
                entry.cached_at = new_cached_at
                logger.debug("Refresh completed for key %s", key)
                return True

            # Entry was evicted but version matches - still safe to write
            # This handles the case where LRU eviction happened during refresh
            return False

    def cancel_refresh(self, key: str) -> None:
        """Cancel a background refresh.

        Args:
            key: Cache key to cancel refresh for
        """
        with self._lock:
            self._refreshing_keys.discard(key)

    def put(
        self,
        key: str,
        value: bytes,
        redis_ttl: Optional[float] = None,
        expires_at: Optional[float] = None,
        namespace: Optional[str] = None,
    ) -> None:
        """Store value in L1 cache with TTL.

        Args:
            key: Cache key
            value: Bytes to cache (encrypted or plaintext msgpack, not deserialized object)
            redis_ttl: TTL in seconds from Redis (used to calculate expiry)
            expires_at: Absolute expiry timestamp (overrides redis_ttl)
            namespace: Optional namespace for invalidation support
        """
        # Calculate expiry time
        current_time = time.time()
        if expires_at is not None:
            expiry = expires_at - self.ttl_buffer_seconds
        elif redis_ttl is not None:
            expiry = current_time + redis_ttl - self.ttl_buffer_seconds
        else:
            # Default 5 minute TTL if not specified
            expiry = current_time + 300 - self.ttl_buffer_seconds

        # Skip caching if TTL is too short (would expire immediately or already expired)
        if expiry <= current_time:
            logger.debug("Skipping L1 cache for key %s - TTL too short (effective TTL: %.2fs)", key, expiry - current_time)
            return

        # Estimate size
        size = self._estimate_size(value)

        with self._lock:
            # Check if key already exists
            if key in self._cache:
                old_entry = self._cache[key]
                self._current_memory_bytes -= old_entry.size_bytes
                # Remove old namespace index entry
                if hasattr(self, "_namespace_index") and old_entry.namespace:
                    self._namespace_index[old_entry.namespace].discard(key)

            # Evict entries if needed to make room
            self._evict_for_space(size)

            # Store new entry
            entry = CacheEntry(value=value, expires_at=expiry, size_bytes=size, cached_at=current_time, namespace=namespace)
            self._cache[key] = entry
            self._current_memory_bytes += size

            # Update namespace index
            if hasattr(self, "_namespace_index") and namespace:
                self._namespace_index[namespace].add(key)

            # Move to end (most recently used)
            self._cache.move_to_end(key)

    def _remove_entry(self, key: str) -> None:
        """Remove entry from cache and update memory tracking.

        Args:
            key: Key to remove
        """
        if key in self._cache:
            entry = self._cache.pop(key)
            self._current_memory_bytes -= entry.size_bytes

            # Increment version to prevent stale refresh resurrection
            self._entry_version[key] = self._entry_version.get(key, 0) + 1

            # Cancel any in-progress refresh
            self._refreshing_keys.discard(key)

            # Update namespace index
            if hasattr(self, "_namespace_index") and entry.namespace:
                self._namespace_index[entry.namespace].discard(key)

    def _evict_for_space(self, needed_bytes: int) -> None:
        """Evict LRU entries to make space for new entry.

        Args:
            needed_bytes: Bytes needed for new entry
        """
        # Check if we need to evict
        if self._current_memory_bytes + needed_bytes <= self.max_memory_bytes:
            return

        # Evict LRU entries until we have space
        entries_to_remove = []

        for key, entry in self._cache.items():
            if self._current_memory_bytes + needed_bytes <= self.max_memory_bytes:
                break

            entries_to_remove.append(key)
            self._current_memory_bytes -= entry.size_bytes
            self._evictions += 1

        # Remove entries
        for key in entries_to_remove:
            self._cache.pop(key, None)

        if entries_to_remove:
            logger.debug("L1Cache evicted %d entries to free %d bytes", len(entries_to_remove), needed_bytes)

    def invalidate(self, key: str) -> None:
        """Invalidate (remove) entry from L1 cache.

        Args:
            key: Key to invalidate
        """
        with self._lock:
            self._remove_entry(key)

    def invalidate_by_key(self, key: str) -> bool:
        """Invalidate a specific cache key.

        Args:
            key: Cache key to invalidate

        Returns:
            True if key was found and removed, False otherwise
        """
        with self._lock:
            if key in self._cache:
                self._remove_entry(key)
                return True
            return False

    def invalidate_by_namespace(self, namespace: str) -> int:
        """Invalidate all entries in a namespace.

        Args:
            namespace: Namespace to invalidate

        Returns:
            Number of entries invalidated
        """
        with self._lock:
            # Use namespace index if available (O(1) lookup + O(k) delete)
            if hasattr(self, "_namespace_index"):
                keys_to_remove = list(self._namespace_index.get(namespace, set()))
                for key in keys_to_remove:
                    self._remove_entry(key)
                # Clear namespace index entry
                if namespace in self._namespace_index:
                    del self._namespace_index[namespace]
                return len(keys_to_remove)

            # Fallback: scan all entries (O(n))
            keys_to_remove = [key for key, entry in self._cache.items() if entry.namespace == namespace]
            for key in keys_to_remove:
                self._remove_entry(key)
            return len(keys_to_remove)

    def invalidate_all(self) -> int:
        """Invalidate all entries in cache.

        Returns:
            Number of entries invalidated
        """
        with self._lock:
            count = len(self._cache)

            # Increment version for all keys to prevent stale refresh resurrection
            for key in self._cache:
                self._entry_version[key] = self._entry_version.get(key, 0) + 1

            # Clear all data structures
            self._cache.clear()
            self._current_memory_bytes = 0
            self._refreshing_keys.clear()

            # Clear namespace index
            if hasattr(self, "_namespace_index"):
                self._namespace_index.clear()

            logger.info("L1Cache invalidated all %d entries for namespace: %s", count, self.namespace)
            return count

    def clear(self) -> None:
        """Clear all entries from L1 cache."""
        with self._lock:
            self._cache.clear()
            self._current_memory_bytes = 0
            logger.info("L1Cache cleared for namespace: %s", self.namespace)

    def cleanup_expired(self) -> int:
        """Remove expired entries from cache.

        Returns:
            Number of entries removed
        """
        current_time = time.time()
        expired_keys = []

        with self._lock:
            for key, entry in self._cache.items():
                if current_time >= entry.expires_at:
                    expired_keys.append(key)

            for key in expired_keys:
                self._remove_entry(key)
                self._expired_evictions += 1

        if expired_keys:
            logger.debug("L1Cache cleaned up %d expired entries", len(expired_keys))

        return len(expired_keys)

    def get_stats(self) -> dict[str, Any]:
        """Get cache statistics.

        Returns:
            Dictionary of cache metrics
        """
        with self._lock:
            total_requests = self._hits + self._misses
            hit_rate = self._hits / total_requests if total_requests > 0 else 0.0

            return {
                "namespace": self.namespace,
                "entries": len(self._cache),
                "memory_used_mb": self._current_memory_bytes / (1024 * 1024),
                "memory_limit_mb": self.max_memory_bytes / (1024 * 1024),
                "memory_usage_percent": (self._current_memory_bytes / self.max_memory_bytes) * 100,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": hit_rate,
                "evictions": self._evictions,
                "expired_evictions": self._expired_evictions,
                "total_requests": total_requests,
            }

    def __repr__(self) -> str:
        """String representation of cache state."""
        stats = self.get_stats()
        return (
            f"L1Cache(namespace={self.namespace}, "
            f"entries={stats['entries']}, "
            f"memory={stats['memory_used_mb']:.1f}/{stats['memory_limit_mb']}MB, "
            f"hit_rate={stats['hit_rate']:.1%})"
        )


class L1CacheManager:
    """Manager for multiple L1 cache instances by namespace."""

    def __init__(self, default_max_memory_mb: int = 100):
        """Initialize L1 cache manager.

        Args:
            default_max_memory_mb: Default memory limit per namespace
        """
        self._caches: dict[str, L1Cache] = {}
        self._lock = threading.Lock()
        self._default_max_memory_mb = default_max_memory_mb

        # Background cleanup thread state
        self._cleanup_thread: Optional[threading.Thread] = None
        self._stop_cleanup = threading.Event()

    def get_cache(self, namespace: str = "default") -> L1Cache:
        """Get or create L1 cache for namespace.

        Args:
            namespace: Cache namespace

        Returns:
            L1Cache instance for namespace
        """
        with self._lock:
            if namespace not in self._caches:
                cache = L1Cache(max_memory_mb=self._default_max_memory_mb, namespace=namespace)
                self._caches[namespace] = cache
                logger.info("Created L1 cache for namespace: %s", namespace)

            return self._caches[namespace]

    def start_background_cleanup(self, interval_seconds: float = 30.0) -> None:
        """Start background thread to clean up expired entries.

        Args:
            interval_seconds: Cleanup interval in seconds
        """
        if self._cleanup_thread is not None:
            logger.warning("Background cleanup already running")
            return

        def cleanup_worker():
            logger.info("L1 cache background cleanup started (interval: %.1fs)", interval_seconds)

            while not self._stop_cleanup.wait(interval_seconds):
                try:
                    total_cleaned = 0

                    with self._lock:
                        for cache in self._caches.values():
                            cleaned = cache.cleanup_expired()
                            total_cleaned += cleaned

                    if total_cleaned > 0:
                        logger.debug("Background cleanup removed %d expired entries", total_cleaned)

                except Exception as e:
                    logger.error("Error in background cleanup: %s", e)

            logger.info("L1 cache background cleanup stopped")

        self._stop_cleanup.clear()
        self._cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True)
        self._cleanup_thread.start()

    def stop_background_cleanup(self) -> None:
        """Stop background cleanup thread."""
        if self._cleanup_thread is None:
            return

        self._stop_cleanup.set()
        self._cleanup_thread.join(timeout=5.0)
        self._cleanup_thread = None

    def get_all_stats(self) -> dict[str, dict[str, Any]]:
        """Get statistics for all cache namespaces.

        Returns:
            Dictionary mapping namespace to stats
        """
        with self._lock:
            return {namespace: cache.get_stats() for namespace, cache in self._caches.items()}

    def clear_all(self) -> None:
        """Clear all L1 caches."""
        with self._lock:
            for cache in self._caches.values():
                cache.clear()
            logger.info("Cleared all L1 caches")


# Global L1 cache manager instance
_global_l1_manager: Optional[L1CacheManager] = None


def get_l1_cache_manager() -> L1CacheManager:
    """Get or create global L1 cache manager.

    Returns:
        Global L1CacheManager instance
    """
    global _global_l1_manager

    if _global_l1_manager is None:
        _global_l1_manager = L1CacheManager()
        # Start background cleanup by default
        _global_l1_manager.start_background_cleanup()

    return _global_l1_manager


def get_l1_cache(namespace: str = "default") -> L1Cache:
    """Get L1 cache for namespace.

    Args:
        namespace: Cache namespace

    Returns:
        L1Cache instance
    """
    manager = get_l1_cache_manager()
    return manager.get_cache(namespace)
