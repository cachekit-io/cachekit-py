"""Thread-safe in-memory object cache with TTL, LRU eviction, byte bounds, and SWR.

Stores Python object references directly — no serialization. Used by @cache.local()
and by @cache(backend=None) (L1-only mode) to cache objects that do not need to
cross process boundaries or survive restarts, with no serialization or network round trip.
"""

from __future__ import annotations

import heapq
import math
import os
import random
import sys
import threading
import weakref
from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

from cachekit._rust_serializer import HitEntry, HitMemo
from cachekit._rust_serializer import monotonic as _monotonic


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
    """Cache entry: value reference plus timing, size and lock-free-hit bookkeeping."""

    value: Any
    expires_at: float  # clock (_monotonic) hard-expiry deadline
    cached_at: float  # clock write timestamp (SWR freshness clock)
    used_at: float  # clock reading of the latest read under the lock, or the write (recency, with hit.used_at)
    size_bytes: int  # 0 when the cache is not byte-bounded
    generation: int  # anti-resurrection token: allocated per stored entry, never reused
    hit: HitEntry | None = None  # what a lock-free hit reads, in a cache with a memo; replaced on refresh
    memo_keys: tuple[Any, ...] = ()  # the memo slots pointing at hit, forgotten when it is killed
    refresh_failed_at: float | None = None  # clock time of the last failed SWR refresh (retry back-off)


def _used_at(entry: _Entry) -> float:
    """An entry's recency in a cache with a memo: its latest use under the lock or, if later, by a lock-free hit."""
    hit = entry.hit
    assert hit is not None  # noqa: S101 - every entry of a cache with a memo has a hit
    return max(entry.used_at, hit.used_at)


class _ObjectCacheState:
    """The entries, their byte total, their deadline and recency records, their in-flight refresh markers, and the lock.

    One object so the at-fork hook can replace all of it with a single attribute store: a thread
    still inside a critical section finishes on the state it bound. The stats, the generation
    counter and the memo stay with the cache, so a swap keeps them.
    """

    __slots__ = ("expiry", "filed", "lock", "memo", "refiled", "refreshing", "size_bytes", "store")

    def __init__(self, memo: HitMemo | None) -> None:
        # Without a memo every read takes the lock, so the store's order is the LRU order: a put
        # appends its entry and a read moves its entry to the end.
        self.store: OrderedDict[str, _Entry] = OrderedDict()
        self.size_bytes = 0
        # Min-heap of (expires_at, key), one per stored or refreshed deadline, so the over-bound
        # eviction finds expired entries without scanning the store. Deletion is lazy: a removal or a
        # refresh leaves the old tuple behind. A tuple is live while its key holds an entry with that
        # deadline, and every entry's current deadline has one, so a dead tuple is safe to drop.
        # push() rebuilds the heap from the store once dead tuples outnumber live entries.
        self.expiry: list[tuple[float, str]] = []
        # With a memo, a lock-free hit stamps its entry's HitEntry but cannot move the entry in the
        # store, so recency records hold the LRU order instead: (used_at, generation, key), exactly one
        # live record per entry. A put files its entry's record at the end of `filed`, so `filed` runs
        # in clock order. A use stamps only its entry, so a record can hold a time older than its
        # entry's latest use: evict_lru() re-files such a record at that use when it is the oldest, in
        # the min-heap `refiled`, and evicts the first entry whose record is current. Every other entry
        # was used at or after its own record's time, which is no earlier than that one's, so that
        # entry is the least recently used. A removed or replaced entry's record is dead (its key holds
        # no entry of its generation) and is dropped where it surfaces; push() rebuilds the records as
        # it does the expiry heap.
        self.filed: deque[tuple[float, int, str]] = deque()
        self.refiled: list[tuple[float, int, str]] = []
        # In-flight refresh ownership: key -> owning generation. A refresh captures the entry's
        # generation at read time and only lands — or clears/cancels its marker — for that exact
        # generation, so a stale refresh from a replaced entry can neither resurrect data nor
        # release a newer refresh's marker (which would allow duplicate concurrent refreshes racing
        # last-write-wins). Invariant: a present marker always equals the live entry's generation,
        # because every single-key entry change funnels through remove(), which pops it, and
        # clear() and the fork reset empty every marker at once. Removal leaves no per-key residue.
        self.refreshing: dict[str, int] = {}
        self.memo = memo
        self.lock = threading.RLock()

    def remove(self, key: str) -> None:
        """Remove an entry and update all bookkeeping; call with lock held.

        Every single-key removal funnels here so byte accounting, in-flight refresh cancellation and
        the memo stay consistent. Anti-resurrection needs no per-key residue: a refresh can only land
        on the exact entry (generation) it was started against.

        A lock-free hit of the entry ends before the entry leaves the store. The kill comes first, so
        a hit that found the entry before its memo slots go reads it dead, and the slots go next, so
        the memo holds no removed value.
        """
        entry = self.store.get(key)
        if entry is None:
            return
        hit = entry.hit
        if hit is not None:
            hit.kill()
            memo = self.memo
            assert memo is not None  # noqa: S101 - an entry has a hit only in a cache with a memo
            for memo_key in entry.memo_keys:
                memo.forget(memo_key, hit)
            entry.memo_keys = ()
        del self.store[key]
        self.size_bytes -= entry.size_bytes
        self.refreshing.pop(key, None)

    def push(self, key: str, entry: _Entry, *, new: bool = True) -> None:
        """Record an entry's deadline and, when it is new and the cache has a memo, its recency; call with lock held.

        Call after the store write. A refreshed entry (new=False) keeps its recency record, which is
        re-filed at the refresh when it is the oldest, as at any use. A rebuild from the store drops
        the dead records once they outnumber the live entries. It costs O(n) in the entries left for
        the deadlines and O(n log n) for the recency records, which it sorts back into clock order, and
        it at least halves the records, so it amortises to O(1) or O(log n) per push.
        """
        if len(self.expiry) >= 2 * len(self.store):
            self.expiry = [(e.expires_at, k) for k, e in self.store.items()]
            heapq.heapify(self.expiry)
        else:
            heapq.heappush(self.expiry, (entry.expires_at, key))
        if self.memo is None or not new:
            return
        if len(self.filed) + len(self.refiled) >= 2 * len(self.store):
            self.filed = deque(sorted((_used_at(e), e.generation, k) for k, e in self.store.items()))
            self.refiled = []
        else:
            self.filed.append((entry.used_at, entry.generation, key))

    def remove_expired(self, now: float) -> None:
        """Remove every entry whose deadline has passed; call with lock held.

        Pops heap tuples only while the earliest deadline is due, so when nothing has expired it
        returns after one comparison. Each due tuple costs O(log n). A due tuple is dead when its key
        holds no entry with that deadline, as a replaced or refreshed entry leaves behind; it is dropped.
        """
        heap = self.expiry
        while heap and heap[0][0] <= now:
            expires_at, key = heapq.heappop(heap)
            entry = self.store.get(key)
            if entry is not None and entry.expires_at == expires_at:
                self.remove(key)

    def evict_lru(self) -> None:
        """Remove the least recently used entry; call with lock held and at least one entry stored.

        Without a memo that is the store's first entry. With one it is the entry of the oldest
        current recency record, the older of the first filed and the first re-filed: O(1) per record
        popped from `filed`, O(log n) per record re-filed or popped from `refiled`. Without concurrent
        lock-free hits each live record is re-filed at most once per call, as its entry's use time
        holds still. Such hits can move it while this runs, so after as many re-files as there are
        entries the oldest record's entry goes, re-filed or not: hits that race an eviction have no
        order to keep, and the eviction ends.
        """
        if self.memo is None:
            self.remove(next(iter(self.store)))
            return
        filed, refiled, store = self.filed, self.refiled, self.store
        refiles = len(store)
        while True:
            if refiled and (not filed or refiled[0] < filed[0]):
                used_at, generation, key = heapq.heappop(refiled)
            else:
                used_at, generation, key = filed.popleft()
            entry = store.get(key)
            if entry is None or entry.generation != generation:
                continue  # a removed or replaced entry's record
            last_used = _used_at(entry)
            if last_used > used_at and refiles:
                refiles -= 1
                heapq.heappush(refiled, (last_used, generation, key))  # used since it was filed
                continue
            self.remove(key)
            return

    def kill_all(self) -> None:
        """End every entry's lock-free hits at once: kill them all, then empty the memo; call with lock held."""
        memo = self.memo
        if memo is None:
            return
        for entry in list(self.store.values()):
            if entry.hit is not None:
                entry.hit.kill()
                entry.memo_keys = ()
        memo.clear()


class ObjectCache:
    """Thread-safe in-memory cache storing Python object references directly.

    On a put() when a bound is exceeded, expired entries are removed first; if
    still over, the least recently used fresh entries are evicted. A min-heap of
    deadlines finds the expired entries, so a put at capacity costs O(log n) per
    expired entry it removes and does not scan for them. The least recently used
    entry is the first in the store, an OrderedDict that every read reorders:
    O(1). A cache with a memo (lock-free hits, below) keeps recency records
    instead, as a lock-free hit cannot reorder the store: one per entry, filed
    in clock order at its put, and re-filed in a min-heap once an eviction finds
    the entry used since. Evicting an entry unused since its put costs O(1), and
    each re-filed record O(log n). The records are memory the byte bound does
    not count: on 64-bit CPython about 64 to 72 bytes per entry for the deadline
    record and, with a memo, 80 to 100 for the recency record (a tuple, its slot,
    and the time it was re-filed at), plus the records of removed or replaced
    entries. Once those outnumber the live ones, the put that finds it rebuilds
    them: an O(n) pause once per about n puts at capacity (O(n log n) for the
    recency records), against a full scan on every one before the heap.

    Bounds (at least one required):
    - max_entries: entry-count bound (default 256, pass None to disable)
    - max_size_bytes: byte bound using a best-effort recursive size estimate
      (default None = disabled). Values larger than the whole budget are never
      cached — the function result is still returned, just not stored.

    Times are read on ``cachekit._rust_serializer.monotonic``, the clock a
    lock-free hit (below) reads too, so a deadline means the same instant to
    both. With a memo, an entry's LRU recency is the clock reading of its latest
    use: exact wherever the clock advances between two uses, as the OS monotonic
    clock it reads does within nanoseconds. Of two entries last used at one
    reading, the one stored first is evicted first.

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

    Lock-free hits: a cache built with a ``HitMemo`` (the decorator's L1-only
    mode) gives each entry a ``HitEntry``, and ``get``, ``get_with_swr`` and
    ``put`` take the caller's memo key, under which they point the memo at the
    entry they find or store. ``HitMemo.get`` then answers the next such call
    without the lock, while the entry is stored and before its ``fresh_until``:
    its hard expiry, or with ``swr=True`` the earliest point ``get_with_swr``
    could flag it for refresh (``ttl * swr_threshold_ratio * 0.9`` after it was
    stored, with the ttl it was stored with). Every removal (expiry, eviction,
    ``delete``, replacement, ``clear``, the at-fork reset) kills the entry's
    ``HitEntry`` and drops its memo slots before it returns, so no lock-free hit
    serves a removed entry once its removal has returned, and the memo keeps no
    removed value. ``hits`` counts lookups through the lock only.

    Fork safety: in a forked child an at-fork hook (``_reset_caches_after_fork``)
    repairs every live cache before the child's first call. The parent's threads
    did not survive the fork, so their in-flight refresh markers are emptied and
    the next stale read refreshes again. Entries carry over, unless a thread held
    the lock at fork and so may have left them half-updated: then the cache starts
    empty, with a fresh lock and an empty memo. A child forked without at-fork hooks (uWSGI without
    ``--py-call-uwsgi-fork-hooks``) gets no repair: its first call to a cached
    function hangs if a parent thread held that cache's lock at fork, and a key
    whose refresh was in flight in the parent is served stale until it expires.
    A sync function's decorator starts no refresh thread there at all.

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
        *,
        memo: HitMemo | None = None,
        swr: bool = False,
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
            memo: The ``HitMemo`` that answers hits without the lock, which
                this cache keeps in step with its entries; None for none.
            swr: Whether reads go through ``get_with_swr`` with the ttl the
                entries were stored with: a lock-free hit then stops at the
                earliest point a refresh could be flagged, rather than at the
                entry's hard expiry, and leaves that decision to the lock.

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
        # The part of an entry's ttl a lock-free hit may serve it for: up to the jitter's low end of the
        # SWR threshold, short of which get_with_swr never flags a refresh, or all of it.
        self._fresh_ratio = swr_threshold_ratio * 0.9 if swr else 1.0
        self._memo = memo
        # Every critical section binds this once: `s = self._state; with s.lock: ...`. Reading
        # self._state again inside one would mix states if the at-fork hook replaced it meanwhile.
        self._state = _ObjectCacheState(memo)
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

    def get(self, key: str, memo_key: Any = None) -> tuple[bool, Any]:
        """Retrieve a value from the cache.

        Expired entries are removed on read (lazy expiry).

        Args:
            key: Cache key to look up.
            memo_key: The memo key of the call asking, under which the memo is pointed at the
                entry found (see "Lock-free hits"); None leaves the memo as it is.

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

            now = _monotonic()
            if now >= entry.expires_at:
                # Lazy expiry — remove and report miss
                s.remove(key)
                self._misses += 1
                return False, None

            entry.used_at = now  # most recently used
            if s.memo is None:
                s.store.move_to_end(key)
            elif memo_key is not None:
                self._memoise(s, entry, memo_key)
            self._hits += 1
            return True, entry.value

    def peek(self, key: str) -> tuple[bool, Any]:
        """Look up a value without counting a hit or miss or moving it in LRU order.

        For a second lookup within one call, such as the single-flight re-check after a miss,
        which the call's first lookup already counted. An expired entry reads as absent and is
        left for ``get`` or ``put`` to remove.

        Args:
            key: Cache key to look up.

        Returns:
            A (found, value) tuple, as ``get`` returns.
        """
        s = self._state
        with s.lock:
            entry = s.store.get(key)
            if entry is None or _monotonic() >= entry.expires_at:
                return False, None
            return True, entry.value

    def get_with_swr(self, key: str, ttl: float, memo_key: Any = None) -> tuple[bool, Any, bool, int]:
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
            memo_key: As ``get`` takes it.

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

            now = _monotonic()
            if now >= entry.expires_at:
                s.remove(key)
                self._misses += 1
                return False, None, False, 0

            entry.used_at = now
            if s.memo is None:
                s.store.move_to_end(key)
            elif memo_key is not None:
                self._memoise(s, entry, memo_key)
            self._hits += 1

            version = entry.generation
            age = now - entry.cached_at
            threshold = ttl * self._swr_threshold_ratio
            # Not stale even at the jitter's low end (0.9), so no draw from random could flag it:
            # the common hit returns here without one.
            if age <= threshold * 0.9:
                return True, entry.value, False, version
            needs_refresh = False
            # ±10% jitter staggers refreshes when many keys cross the threshold together
            jitter = random.uniform(0.9, 1.1)  # noqa: S311 - not cryptographic
            backing_off = entry.refresh_failed_at is not None and now - entry.refresh_failed_at < self._swr_retry_interval
            if age > threshold * jitter and key not in s.refreshing and not backing_off:
                s.refreshing[key] = entry.generation
                needs_refresh = True

            return True, entry.value, needs_refresh, version

    def complete_refresh(self, key: str, version: int, value: Any, ttl: float) -> bool:
        """Complete a background refresh started by ``get_with_swr``.

        Unlike L1Cache (where Redis owns expiry), there is no L2 source of
        truth here — the refreshed value restarts both the freshness clock and
        the hard-expiry deadline. In a cache with a memo the entry gets a new
        ``HitEntry``, which its memo slots point at before the old one is killed.

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

            now = _monotonic()
            s.size_bytes += size - entry.size_bytes
            entry.value = value
            entry.cached_at = now
            entry.expires_at = now + ttl
            entry.used_at = now
            entry.size_bytes = size
            entry.refresh_failed_at = None  # success ends any retry back-off
            stale = entry.hit
            if stale is not None:
                # A HitEntry's value never changes, so the refresh gets a new one. Its memo slots
                # point at the new one before the stale one is killed: a lock-free hit that finds
                # either reads it alive or misses, and none serves the stale value after this returns.
                memo_keys = entry.memo_keys
                entry.hit = HitEntry(value, now + ttl * self._fresh_ratio, now)
                entry.memo_keys = ()
                for memo_key in memo_keys:
                    self._memoise(s, entry, memo_key)
                stale.kill()
            else:
                s.store.move_to_end(key)
            s.push(key, entry, new=False)  # the old deadline's record is now dead
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
            entry.refresh_failed_at = _monotonic()

    def put(self, key: str, value: Any, ttl: int, memo_key: Any = None) -> None:
        """Store a value in the cache.

        When a bound is exceeded:
        1. Expired entries are removed first, wherever they sit in LRU order.
           The expiry heap finds them, so no entry is scanned to look for them; the one
           put in about n that rebuilds the heap makes one pass over the entries.
        2. If still over, the least recently used fresh entries are evicted.

        A value whose estimated size exceeds the entire byte budget is never
        cached (any smaller stale entry under the same key is dropped so it
        stops being served).

        Args:
            key: Cache key.
            value: Python object to store (no serialisation performed).
            ttl: Time-to-live in seconds (must be >= 1).
            memo_key: As ``get`` takes it.

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
            # Replacing? Remove through remove() so byte accounting, any in-flight
            # refresh marker and the memo stay consistent. The fresh generation
            # below makes an older in-flight refresh unable to overwrite this newer value.
            s.remove(key)

            self._evict(s, extra_bytes=size, need_slot=True)

            now = _monotonic()
            self._generation += 1
            entry = _Entry(
                value=value,
                expires_at=now + ttl,
                cached_at=now,
                used_at=now,
                size_bytes=size,
                generation=self._generation,
                hit=None if self._memo is None else HitEntry(value, now + ttl * self._fresh_ratio, now),
            )
            s.store[key] = entry
            s.size_bytes += size
            s.push(key, entry)
            if memo_key is not None:
                self._memoise(s, entry, memo_key)

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
            s.kill_all()
            s.store.clear()
            s.size_bytes = 0
            s.expiry.clear()
            s.filed.clear()
            s.refiled.clear()
            s.refreshing.clear()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def hits(self) -> int:
        """Total number of successful lookups under the lock since creation (lock-free hits are not counted)."""
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

    def _memoise(self, s: _ObjectCacheState, entry: _Entry, memo_key: Any) -> None:
        """Point memo_key's memo slot at the entry, for the next such call's lock-free hit; call with s.lock held.

        A thread that forked inside this critical section, from a signal handler or a finalizer,
        returns here in the child on the state the at-fork hook retired, after the hook emptied the
        memo. The check after the write takes back a slot it set for an entry of that state.
        """
        hit = entry.hit
        if hit is None:
            return
        memo = s.memo
        assert memo is not None  # noqa: S101 - an entry has a hit only in a cache with a memo
        memo.set(memo_key, hit)
        if memo_key not in entry.memo_keys:
            entry.memo_keys += (memo_key,)
        if self._state is not s:
            hit.kill()
            memo.forget(memo_key, hit)

    def _evict(self, s: _ObjectCacheState, extra_bytes: int, need_slot: bool) -> None:
        """Evict entries until both bounds accommodate the pending write.

        Must be called with s.lock held.

        Strategy:
        1. If any bound is exceeded, remove all expired entries first, popping them off the
           expiry heap: one comparison when none has expired, never a scan of the store.
        2. While still over a bound, evict the least recently used fresh entry (evict_lru).

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

        s.remove_expired(_monotonic())

        # Still over a bound — evict the least-recently-used fresh entries
        while s.store and over_bounds():
            s.evict_lru()

    def _reset_after_fork(self) -> None:
        """Retire what the parent's threads left in this cache; call only from _reset_caches_after_fork.

        A free lock means nothing was mid-update: the entries stay, and the in-flight refresh
        markers, whose threads did not survive the fork, are emptied. A held lock may never come
        free and its holder may have left the entries half-updated, so the cache gets a fresh state
        in one attribute store, never a clear in place: a forking thread that returns into its
        critical section finishes on the state it bound. The dropped entries' lock-free hits end
        first, as any removal's do. _is_owned() comes first because the forking thread's own hold
        is reentrant, and a try-acquire alone would report it free.
        """
        s = self._state
        if not s.lock._is_owned() and s.lock.acquire(blocking=False):  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
            s.lock.release()
            s.refreshing.clear()
            return
        s.kill_all()
        self._state = _ObjectCacheState(s.memo)


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
