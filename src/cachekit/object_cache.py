"""Thread-safe in-memory object cache with TTL and entry-count LRU eviction.

Stores Python object references directly — no serialization. Used by @cache.local()
to provide ultra-low-latency (~50ns) caching for objects that do not need to cross
process boundaries or survive restarts.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any


class ObjectCache:
    """Thread-safe in-memory cache storing Python object references directly.

    Uses an OrderedDict for O(1) LRU ordering. On a put() when the cache is
    full, expired entries are swept first; if still full, the oldest fresh
    entry is evicted (LRU).

    Thread safety: RLock on every public method so callers need no external
    synchronisation.

    Examples:
        Basic usage with TTL:

        >>> oc = ObjectCache(max_entries=3)
        >>> oc.put("a", 1, ttl=60)
        >>> found, val = oc.get("a")
        >>> found
        True
        >>> val
        1
        >>> oc.size
        1
    """

    def __init__(self, max_entries: int = 256) -> None:
        """Initialise the object cache.

        Args:
            max_entries: Maximum number of entries to hold. Must be >= 1.

        Raises:
            ValueError: If max_entries is less than 1.
        """
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}")

        self._max_entries = max_entries
        # value: (stored_value, expires_at)
        self._store: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str) -> tuple[bool, Any]:
        """Retrieve a value from the cache.

        Expired entries are removed on read (lazy expiry).

        Args:
            key: Cache key to look up.

        Returns:
            A (found, value) tuple. found is False on miss or expiry;
            value is None in that case.
        """
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return False, None

            value, expires_at = entry
            if time.monotonic() >= expires_at:
                # Lazy expiry — remove and report miss
                del self._store[key]
                self._misses += 1
                return False, None

            # Move to end (most-recently-used)
            self._store.move_to_end(key)
            self._hits += 1
            return True, value

    def put(self, key: str, value: Any, ttl: int) -> None:
        """Store a value in the cache.

        When the cache is at capacity:
        1. Expired entries are removed first.
        2. If still at capacity, the oldest (LRU) fresh entry is evicted.

        Args:
            key: Cache key.
            value: Python object to store (no serialisation performed).
            ttl: Time-to-live in seconds (must be >= 1).

        Raises:
            ValueError: If ttl is less than 1.
        """
        if ttl < 1:
            raise ValueError(f"ttl must be >= 1, got {ttl}")
        expires_at = time.monotonic() + ttl
        with self._lock:
            # If key already present, update in-place and move to end
            if key in self._store:
                self._store[key] = (value, expires_at)
                self._store.move_to_end(key)
                return

            # Need a slot — evict if at capacity
            if len(self._store) >= self._max_entries:
                self._evict_to_make_room()

            self._store[key] = (value, expires_at)
            # No move_to_end needed — OrderedDict.__setitem__ appends new keys to end

    def delete(self, key: str) -> bool:
        """Remove a single entry from the cache.

        Args:
            key: Cache key to remove.

        Returns:
            True if the key existed and was removed, False otherwise.
        """
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    def clear(self) -> None:
        """Remove all entries from the cache.

        Hit/miss counters are NOT reset; they represent lifetime statistics.
        """
        with self._lock:
            self._store.clear()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def hits(self) -> int:
        """Total number of successful cache lookups since creation."""
        with self._lock:
            return self._hits

    @property
    def misses(self) -> int:
        """Total number of failed cache lookups (including expired) since creation."""
        with self._lock:
            return self._misses

    @property
    def size(self) -> int:
        """Current number of entries (may include not-yet-evicted expired entries)."""
        with self._lock:
            return len(self._store)

    @property
    def max_entries(self) -> int:
        """Maximum number of entries this cache will hold."""
        return self._max_entries

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _evict_to_make_room(self) -> None:
        """Evict entries to make room for one new entry.

        Must be called with self._lock held.

        Strategy:
        1. Remove all expired entries.
        2. If still at capacity, evict the oldest (LRU) fresh entry.
        """
        now = time.monotonic()
        expired_keys = [k for k, (_, exp) in self._store.items() if now >= exp]
        for k in expired_keys:
            del self._store[k]

        # If removing expired entries freed a slot, we are done
        if len(self._store) < self._max_entries:
            return

        # Still full — evict the least-recently-used fresh entry
        self._store.popitem(last=False)
