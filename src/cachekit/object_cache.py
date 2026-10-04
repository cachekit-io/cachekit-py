"""Thread-safe in-memory object cache with TTL, LRU eviction, byte bounds, and SWR.

Stores Python object references directly — no serialization. Used by @cache.local()
and by @cache(backend=None) (L1-only mode) to provide low-latency (~50ns)
caching for objects that do not need to cross process boundaries or survive restarts.
"""

from __future__ import annotations

import math
import os
import random
import sys
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast


def _estimate_object_size(obj: Any) -> int:
    """Best-effort byte-size estimate of a Python object graph.

    Iteratively walks builtin containers (dict/list/tuple/set/frozenset) with
    cycle detection, so a list of large strings is counted at its real weight
    instead of pointer-size. Non-container objects are counted shallow via
    ``sys.getsizeof`` (pandas/numpy implement ``__sizeof__`` and report real
    memory), so arbitrary instances holding large attribute graphs are
    under-estimated. This is a memory *bound* heuristic, not exact accounting.

    Args:
        obj: Object to estimate.

    Returns:
        Estimated size in bytes.
    """
    seen: set[int] = set()
    stack: list[Any] = [obj]
    total = 0
    while stack:
        item = stack.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        total += sys.getsizeof(item, 64)  # 64: fallback for objects without __sizeof__
        if isinstance(item, dict):
            mapping = cast("dict[Any, Any]", item)
            stack.extend(mapping.keys())
            stack.extend(mapping.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            stack.extend(cast("Iterable[Any]", item))
    return total


@dataclass(slots=True)
class _Entry:
    """Cache entry: value reference plus timing and size bookkeeping."""

    value: Any
    expires_at: float  # time.monotonic() hard-expiry deadline
    cached_at: float  # time.monotonic() write timestamp (SWR freshness clock)
    size_bytes: int  # 0 when the cache is not byte-bounded
    generation: int  # anti-resurrection token: allocated per stored entry, never reused
    refresh_failed_at: float | None = None  # time.monotonic() of the last failed SWR refresh (retry back-off)


class _ObjectCacheState:
    """The entries, their byte total, their in-flight refresh markers, and the lock guarding all three.

    One object so the at-fork hook can replace all of it with a single attribute store: a thread
    still inside a critical section finishes on the state it bound. The stats and the generation
    counter stay on the cache, so a swap keeps them.
    """

    __slots__ = ("lock", "refreshing", "size_bytes", "store")

    def __init__(self) -> None:
        self.store: OrderedDict[str, _Entry] = OrderedDict()
        self.size_bytes = 0
        # In-flight refresh ownership: key -> owning generation. A refresh captures the entry's
        # generation at read time and only lands — or clears/cancels its marker — for that exact
        # generation, so a stale refresh from a replaced entry can neither resurrect data nor
        # release a newer refresh's marker (which would allow duplicate concurrent refreshes racing
        # last-write-wins). Invariant: a present marker always equals the live entry's generation,
        # because every single-key entry change funnels through remove(), which pops it, and
        # clear() and the fork reset empty every marker at once. Removal leaves no per-key residue.
        self.refreshing: dict[str, int] = {}
        self.lock = threading.RLock()

    def remove(self, key: str) -> None:
        """Remove an entry and update all bookkeeping; call with lock held.

        Every single-key removal funnels here so byte accounting and in-flight refresh cancellation
        stay consistent. Anti-resurrection needs no per-key residue: a refresh can only land on the exact
        entry (generation) it was started against.
        """
        entry = self.store.pop(key, None)
        if entry is None:
            return
        self.size_bytes -= entry.size_bytes
        self.refreshing.pop(key, None)


class ObjectCache:
    """Thread-safe in-memory cache storing Python object references directly.

    Uses an OrderedDict for O(1) LRU ordering. On a put() when a bound is
    exceeded, expired entries are swept first; if still over, the oldest fresh
    entries are evicted (LRU).

    Bounds (at least one required):
    - max_entries: entry-count bound (default 256, pass None to disable)
    - max_size_bytes: byte bound using a best-effort recursive size estimate
      (default None = disabled). Values larger than the whole budget are never
      cached — the function result is still returned, just not stored.

    Stale-while-revalidate (SWR): ``get_with_swr`` serves a fresh-enough entry
    while flagging it for background refresh once past
    ``ttl * swr_threshold_ratio`` (±10% jitter). The caller runs the refresh and
    finishes the cycle with ``complete_refresh``, ``fail_refresh`` when the
    refresh ran and raised, or ``cancel_refresh`` when it never ran. After a
    failure no refresh is flagged for that entry until ``swr_retry_interval``
    seconds have passed, so a failing upstream is not called on every read.
    The failure time lives on the entry, so it goes when the entry goes. Each stored entry carries a generation token from a monotonic
    counter; a refresh only lands if the same entry (same generation) is still
    live, so a refresh that completes after an invalidation, eviction, or
    replacement can never resurrect stale data — without retaining any per-key
    state for removed entries.

    Thread safety: RLock on every public method so callers need no external
    synchronisation.

    Fork safety: in a forked child an at-fork hook (``_reset_caches_after_fork``)
    repairs every live cache before the child's first call. The parent's threads
    did not survive the fork, so their in-flight refresh markers are emptied and
    the next stale read refreshes again. Entries carry over, unless a thread held
    the lock at fork and so may have left them half-updated: then the cache starts
    empty, with a fresh lock. A child forked without at-fork hooks (uWSGI without
    ``--py-call-uwsgi-fork-hooks``) gets no repair: its first call to a cached
    function hangs if a parent thread held that cache's lock at fork, and a key
    whose refresh was in flight in the parent is served stale until it expires.

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

    def __init__(
        self,
        max_entries: int | None = 256,
        max_size_bytes: int | None = None,
        swr_threshold_ratio: float = 0.5,
        swr_retry_interval: float = 0.0,
    ) -> None:
        """Initialise the object cache.

        Args:
            max_entries: Maximum number of entries to hold (>= 1), or None to
                disable the entry-count bound.
            max_size_bytes: Maximum estimated bytes to hold (>= 1), or None to
                disable the byte bound. At least one bound must be set.
            swr_threshold_ratio: Fraction of TTL after which ``get_with_swr``
                flags an entry for background refresh. Must be in (0.0, 1.0].
            swr_retry_interval: Seconds after a failed refresh before
                ``get_with_swr`` flags that entry again. 0 retries on the next
                stale read. Must be >= 0.

        Raises:
            ValueError: If both bounds are None, a bound is < 1,
                swr_threshold_ratio is outside (0.0, 1.0], or
                swr_retry_interval is negative or NaN.
        """
        if max_entries is None and max_size_bytes is None:
            raise ValueError("ObjectCache requires at least one bound: max_entries or max_size_bytes")
        if max_entries is not None and max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}")
        if max_size_bytes is not None and max_size_bytes < 1:
            raise ValueError(f"max_size_bytes must be >= 1, got {max_size_bytes}")
        if not (0.0 < swr_threshold_ratio <= 1.0):
            raise ValueError(f"swr_threshold_ratio must be in (0.0, 1.0], got {swr_threshold_ratio}")
        if not swr_retry_interval >= 0:  # also rejects NaN
            raise ValueError(f"swr_retry_interval must be >= 0, got {swr_retry_interval}")

        self._max_entries = max_entries
        self._max_size_bytes = max_size_bytes
        self._swr_threshold_ratio = swr_threshold_ratio
        self._swr_retry_interval = swr_retry_interval
        # Every critical section binds this once: `s = self._state; with s.lock: ...`. Reading
        # self._state again inside one would mix states if the at-fork hook replaced it meanwhile.
        self._state = _ObjectCacheState()
        # Kept outside _state, so the at-fork hook keeps them: lifetime stats, and the generation
        # counter stamped onto every stored entry. A generation is never reused across states, so a
        # refresh started before a swap cannot land on an entry stored after it.
        self._hits = 0
        self._misses = 0
        self._generation = 0
        _caches.add(self)

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
        s = self._state
        with s.lock:
            entry = s.store.get(key)
            if entry is None:
                self._misses += 1
                return False, None

            if time.monotonic() >= entry.expires_at:
                # Lazy expiry — remove and report miss
                s.remove(key)
                self._misses += 1
                return False, None

            # Move to end (most-recently-used)
            s.store.move_to_end(key)
            self._hits += 1
            return True, entry.value

    def get_with_swr(self, key: str, ttl: float) -> tuple[bool, Any, bool, int]:
        """Get value with stale-while-revalidate support.

        Once an entry is older than ``ttl * swr_threshold_ratio`` (±10% jitter
        to stagger refreshes), the first caller is told to refresh it in the
        background while the cached value keeps being served. Within
        ``swr_retry_interval`` of a failed refresh nobody is told to refresh;
        the cached value is still served until it hard-expires. When
        ``needs_refresh`` is True, the key is marked as refreshing — the caller
        MUST finish the cycle with ``complete_refresh`` or ``cancel_refresh``,
        otherwise no further refresh is ever flagged for that key.

        Args:
            key: Cache key.
            ttl: TTL in seconds used for the refresh-threshold calculation.

        Returns:
            Tuple of (hit, value, needs_refresh, version):
            - hit: Whether key was found and not hard-expired
            - value: Cached object or None
            - needs_refresh: Whether the caller should trigger a background refresh
            - version: Entry version at read time (pass to complete_refresh)
        """
        s = self._state
        with s.lock:
            entry = s.store.get(key)
            if entry is None:
                self._misses += 1
                return False, None, False, 0

            now = time.monotonic()
            if now >= entry.expires_at:
                s.remove(key)
                self._misses += 1
                return False, None, False, 0

            s.store.move_to_end(key)
            self._hits += 1

            version = entry.generation
            needs_refresh = False
            # ±10% jitter staggers refreshes when many keys cross the threshold together
            jitter = random.uniform(0.9, 1.1)  # noqa: S311 - not cryptographic
            backing_off = entry.refresh_failed_at is not None and now - entry.refresh_failed_at < self._swr_retry_interval
            if (
                (now - entry.cached_at) > ttl * self._swr_threshold_ratio * jitter
                and key not in s.refreshing
                and not backing_off
            ):
                s.refreshing[key] = entry.generation
                needs_refresh = True

            return True, entry.value, needs_refresh, version

    def complete_refresh(self, key: str, version: int, value: Any, ttl: float) -> bool:
        """Complete a background refresh started by ``get_with_swr``.

        Unlike L1Cache (where Redis owns expiry), there is no L2 source of
        truth here — the refreshed value restarts both the freshness clock and
        the hard-expiry deadline.

        Args:
            key: Cache key.
            version: Version token returned by ``get_with_swr``.
            value: Freshly computed value.
            ttl: TTL in seconds for the refreshed entry.

        Returns:
            True if the write succeeded; False if the entry was invalidated or
            evicted while the refresh ran (stale data is never resurrected).

        Raises:
            ValueError: If ttl is not a finite number >= 1.
        """
        if not math.isfinite(ttl) or ttl < 1:
            raise ValueError(f"ttl must be a finite number >= 1, got {ttl!r}")

        size = _estimate_object_size(value) if self._max_size_bytes is not None else 0
        s = self._state
        with s.lock:
            # Clear the in-flight marker only if this refresh still owns it — a
            # stale refresh must not release a newer refresh's marker (that
            # would let a third reader schedule a duplicate concurrent refresh)
            if s.refreshing.get(key) == version:
                del s.refreshing[key]

            entry = s.store.get(key)
            if entry is None:
                # Entry was invalidated or evicted during refresh — don't resurrect it
                return False
            if entry.generation != version:
                # Entry was replaced (e.g. by put()) during refresh — the newer
                # value wins; the stale refresh result is discarded
                return False

            if self._max_size_bytes is not None and size > self._max_size_bytes:
                # Refreshed value can no longer fit — drop the entry rather than
                # keep serving the stale one forever
                s.remove(key)
                return False

            now = time.monotonic()
            s.size_bytes += size - entry.size_bytes
            entry.value = value
            entry.cached_at = now
            entry.expires_at = now + ttl
            entry.size_bytes = size
            entry.refresh_failed_at = None  # success ends any retry back-off
            s.store.move_to_end(key)
            # New value may be larger — restore the byte bound by evicting LRU others
            self._evict(s, extra_bytes=0, need_slot=False)
            return True

    def cancel_refresh(self, key: str, version: int) -> None:
        """Cancel a background refresh so a later call can retry it.

        Only the refresh that owns the in-flight marker (same generation) may
        release it — a stale refresh cancelling after the entry was replaced
        must not release a newer refresh's marker.

        Args:
            key: Cache key whose refresh failed or was abandoned.
            version: Version token returned by ``get_with_swr``.
        """
        s = self._state
        with s.lock:
            if s.refreshing.get(key) == version:
                del s.refreshing[key]

    def fail_refresh(self, key: str, version: int) -> None:
        """Finish a background refresh that ran and raised.

        Releases the in-flight marker like ``cancel_refresh`` and also records
        the failure time on the entry, so ``get_with_swr`` flags no new refresh
        for it until ``swr_retry_interval`` has passed. Ownership rules match
        ``cancel_refresh``: a stale refresh (older generation) changes nothing.

        Args:
            key: Cache key whose refresh failed.
            version: Version token returned by ``get_with_swr``.
        """
        s = self._state
        with s.lock:
            if s.refreshing.get(key) != version:
                return
            del s.refreshing[key]
            # The marker invariant guarantees the live entry has this generation
            entry = s.store[key]
            entry.refresh_failed_at = time.monotonic()

    def put(self, key: str, value: Any, ttl: int) -> None:
        """Store a value in the cache.

        When a bound is exceeded:
        1. Expired entries are removed first.
        2. If still over, the oldest (LRU) fresh entries are evicted.

        A value whose estimated size exceeds the entire byte budget is never
        cached (any smaller stale entry under the same key is dropped so it
        stops being served).

        Args:
            key: Cache key.
            value: Python object to store (no serialisation performed).
            ttl: Time-to-live in seconds (must be >= 1).

        Raises:
            ValueError: If ttl is less than 1.
        """
        if not math.isfinite(ttl) or ttl < 1:
            raise ValueError(f"ttl must be a finite number >= 1, got {ttl!r}")

        size = _estimate_object_size(value) if self._max_size_bytes is not None else 0
        s = self._state
        if self._max_size_bytes is not None and size > self._max_size_bytes:
            with s.lock:
                s.remove(key)
            return

        with s.lock:
            # Replacing? Remove through remove() so byte accounting and any
            # in-flight refresh marker stay consistent (the new entry re-appends
            # at MRU below). The fresh generation below makes an older in-flight
            # refresh unable to overwrite this newer value.
            s.remove(key)

            self._evict(s, extra_bytes=size, need_slot=True)

            now = time.monotonic()
            self._generation += 1
            s.store[key] = _Entry(value=value, expires_at=now + ttl, cached_at=now, size_bytes=size, generation=self._generation)
            s.size_bytes += size
            # No move_to_end needed — OrderedDict.__setitem__ appends new keys to end

    def delete(self, key: str) -> bool:
        """Remove a single entry from the cache.

        An in-flight SWR refresh cannot resurrect it: the refresh only lands
        on the exact entry (generation) it was started against.

        Args:
            key: Cache key to remove.

        Returns:
            True if the key existed and was removed, False otherwise.
        """
        s = self._state
        with s.lock:
            if key in s.store:
                s.remove(key)
                return True
            return False

    def clear(self) -> None:
        """Remove all entries from the cache.

        Hit/miss counters are NOT reset; they represent lifetime statistics.
        In-flight SWR refreshes cannot resurrect cleared entries — their
        target entries no longer exist.
        """
        s = self._state
        with s.lock:
            s.store.clear()
            s.size_bytes = 0
            s.refreshing.clear()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def hits(self) -> int:
        """Total number of successful cache lookups since creation."""
        with self._state.lock:
            return self._hits

    @property
    def misses(self) -> int:
        """Total number of failed cache lookups (including expired) since creation."""
        with self._state.lock:
            return self._misses

    @property
    def size(self) -> int:
        """Current number of entries (may include not-yet-evicted expired entries)."""
        s = self._state
        with s.lock:
            return len(s.store)

    @property
    def size_bytes(self) -> int:
        """Current estimated bytes held (always 0 when not byte-bounded)."""
        s = self._state
        with s.lock:
            return s.size_bytes

    @property
    def max_entries(self) -> int | None:
        """Maximum number of entries this cache will hold (None = unbounded count)."""
        return self._max_entries

    @property
    def max_size_bytes(self) -> int | None:
        """Maximum estimated bytes this cache will hold (None = no byte bound)."""
        return self._max_size_bytes

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _evict(self, s: _ObjectCacheState, extra_bytes: int, need_slot: bool) -> None:
        """Evict entries until both bounds accommodate the pending write.

        Must be called with s.lock held.

        Strategy:
        1. If any bound is exceeded, remove all expired entries first.
        2. While still over a bound, evict the oldest (LRU) fresh entry.

        Args:
            s: The state the caller bound and holds the lock of.
            extra_bytes: Estimated size of the value about to be stored.
            need_slot: Whether the pending write adds a new entry (entry-count
                bound only applies then).
        """

        def over_bounds() -> bool:
            over_entries = need_slot and self._max_entries is not None and len(s.store) >= self._max_entries
            over_bytes = self._max_size_bytes is not None and s.size_bytes + extra_bytes > self._max_size_bytes
            return over_entries or over_bytes

        if not over_bounds():
            return

        # Sweep expired entries first
        now = time.monotonic()
        expired_keys = [k for k, e in s.store.items() if now >= e.expires_at]
        for k in expired_keys:
            s.remove(k)

        # Still over a bound — evict the least-recently-used fresh entries
        while s.store and over_bounds():
            s.remove(next(iter(s.store)))

    def _reset_after_fork(self) -> None:
        """Retire what the parent's threads left in this cache; call only from _reset_caches_after_fork.

        A free lock means nothing was mid-update: the entries stay, and the in-flight refresh
        markers, whose threads did not survive the fork, are emptied. A held lock may never come
        free and its holder may have left the entries half-updated, so the cache gets a fresh state
        in one attribute store, never a clear in place: a forking thread that returns into its
        critical section finishes on the state it bound. _is_owned() comes first because the
        forking thread's own hold is reentrant, and a try-acquire alone would report it free.
        """
        s = self._state
        if not s.lock._is_owned() and s.lock.acquire(blocking=False):  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
            s.lock.release()
            s.refreshing.clear()
            return
        self._state = _ObjectCacheState()


# Every live ObjectCache, for the at-fork hook. Weak: each decorated function owns one, and callers
# and tests may build and drop more.
_caches: weakref.WeakSet[ObjectCache] = weakref.WeakSet()


def _reset_caches_after_fork() -> None:
    """Repair every live ObjectCache in a forked child, before the child's first call.

    Runs while the child is single-threaded, so it takes no lock that can block, starts no
    thread and logs nothing. A fork made from C without at-fork hooks (uWSGI without
    --py-call-uwsgi-fork-hooks) never runs it, and nothing repairs the caches there: the check
    that could, an owner-PID check, would cost a getpid() on every cached call.
    """
    for oc in list(_caches):
        oc._reset_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_caches_after_fork)
