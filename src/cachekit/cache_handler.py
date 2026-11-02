"""Focused cache handling classes following SOLID principles.

This module breaks down the cache decorator into focused,
single-responsibility classes that are easier to test and maintain.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import contextvars
import sys
import threading
import types
import warnings
from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING, Any, BinaryIO, NamedTuple, Optional, Protocol, TypeGuard, Union, runtime_checkable

from pydantic import SecretBytes, SecretStr

from cachekit.backends.base import (
    _WRITE_DEADLINES,
    BackendError,
    BaseBackend,
    BufferHandle,
    BufferReadableBackend,
    BufferWritableBackend,
    KeyTrackableBackend,
    LockableBackend,
    TTLInspectableBackend,
)
from cachekit.backends.file.backend import HEADER_SIZE as FILE_HEADER_SIZE
from cachekit.backends.file.backend import MMAP_RELEASES_FD
from cachekit.backends.provider import (
    BackendProviderInterface,
    DefaultBackendProvider,
    DefaultLoggerProvider,
    LoggerProvider,
)
from cachekit.config import ConfigurationError, get_settings
from cachekit.config.validation import (
    hide_any_secret,
    refuse_bytes_key,
    refuse_current_key_in_previous_keys,
    reveal_secret,
)
from cachekit.di import DIContainer

# Re-exported for backwards compatibility — redact_cache_key moved to the hash_utils
# leaf module so backend/L1 modules can redact without importing this module (cycle).
from cachekit.hash_utils import redact_cache_key, redact_error_for_log
from cachekit.interop import InteropError
from cachekit.key_generator import CacheKeyGenerator
from cachekit.l1_cache import DEFAULT_L1_TTL_SECONDS
from cachekit.serializers.auto_serializer import AutoSerializer
from cachekit.serializers.base import (
    EnvelopeShapeError,
    SerializationError,
    SerializationFormat,
    SerializationMetadata,
    SuspiciousCacheEntryError,
    VerifiedPayload,
    bounded_error,
)
from cachekit.serializers.encryption_wrapper import (
    DecryptionAuthenticationError,
    EncryptionError,
    KeyringConfigurationError,
    require_cross_sdk_serializer,
)
from cachekit.serializers.standard_serializer import StandardSerializer
from cachekit.serializers.wrapper import SerializationWrapper

if TYPE_CHECKING:
    from cachekit.reliability.load_control import BackpressureController
    from cachekit.serializers.base import SerializerProtocol

# Serializer string names allowed under encryption (Issue #134): each decodes one fixed
# format after decryption, as protocol ENC-2 requires. 'auto' is intentionally excluded:
# it chooses its decoder by inspecting the bytes.
CROSS_SDK_SERIALIZER_NAMES = ("default", "std", "standard", "orjson", "arrow")

# Uncompressed Arrow IPC alignment: the body follows the File header, the CK frame
# prefix and ArrowSerializer's 8-byte xxHash3-64 checksum; 64 is Arrow's recommended alignment.
_ARROW_IPC_ALIGN = 64
_ARROW_CHECKSUM_SIZE = 8
# Frame tags an ArrowSerializer can record: its string name, or the class name of an instance. A set
# lookup on the tag screens every other serializer out before the type check, on every read and write.
_ARROW_FRAME_TAGS = frozenset({"arrow", "ArrowSerializer"})

# Global DI container instance with default registrations
container = DIContainer()
container.register(LoggerProvider, DefaultLoggerProvider)
container.register(BackendProviderInterface, DefaultBackendProvider)

# Dependencies are injected at runtime, not module load time
# This ensures test isolation works properly with DI


def get_logger_provider():
    """Get the current LoggerProvider from DI container."""
    return container.get(LoggerProvider)


def get_backend_provider():
    """Get the current BackendProviderInterface from DI container."""
    return container.get(BackendProviderInterface)


# Lazy logger initialization to avoid import-time container access
_logger = None


def get_logger():
    """Get or initialize logger lazily."""
    global _logger
    if _logger is None:
        _logger = get_logger_provider().get_logger(__name__)
    return _logger


# Constants for locking
LOCK_TIMEOUT = 10  # Lock expires after 10 seconds to prevent deadlocks
LOCK_BLOCKING_TIMEOUT = 5  # Wait max 5 seconds to acquire the lock
LOCK_RETRY_INTERVAL = 0.1  # Sleep for 100ms between retries after lock fails


class TenantResolutionError(ValueError):
    """An encrypted read on a handler with a tenant_extractor could not extract the caller's tenant.

    The caller's context is at fault, not the entry, which may be valid for its own
    tenant: every read site treats this as a miss that keeps the entry, L1 and L2 alike.
    """


def _record_security_counter(name: str, labels: dict[str, str]) -> None:
    """Best-effort security-telemetry counter — must never break the read path.

    The reliability import stays inside the guard deliberately: if the metrics
    stack itself fails to import, that failure is swallowed like any other
    telemetry error rather than taking down cache reads.
    """
    try:
        from cachekit.reliability.async_metrics import get_async_metrics_collector

        get_async_metrics_collector().record_counter(name, labels=labels)
    except Exception:  # noqa: S110 — telemetry must never break the read path
        pass


def handle_decrypt_failure(error: Exception, *, tier: str, cache_key: str, fail_closed: bool) -> str:
    """Classify a decrypt/integrity failure, record telemetry, and apply the fail policy.

    This is the SINGLE implementation of the read-path security policy
    (cachekit-py#170); every catch site delegates here so the sites cannot
    drift. Classification is type-based, the metric is recorded before any
    raise (fail-closed events are always counted), and the fail-open/fail-closed
    decision lives in exactly one place.

    Failure classes — the ``reason`` label on ``cachekit_decrypt_failures_total``:

    - ``auth_tamper``: DecryptionAuthenticationError — AES-GCM tag verification
      failed (tampered ciphertext, wrong key, AAD/cache_key mismatch), tenant
      mismatch, or key-fingerprint mismatch under fail-closed. The signal an
      active attack produces; honored by the fail-closed policy.
    - ``suspicious_envelope``: SuspiciousCacheEntryError — the unauthenticated
      envelope is inconsistent with handler config (plaintext claim under
      encryption, missing tenant_id). Benign during lazy plaintext→encrypted
      migration, so it ALWAYS fails open (miss + evict) — but spikes
      outside a migration window warrant investigation.
    - ``envelope_shape``: EnvelopeShapeError — an entry nothing verified decoded to
      the shape of a ByteStorage envelope and was refused. Either a rotted
      integrity-on envelope or a legitimate top-level 4-element list; the read path
      cannot tell them apart, so this is NOT reliable corruption evidence and is kept
      out of ``corruption``. Always fails open. An integrity-off AutoSerializer never writes
      the second case, so a redacted key repeating in the WARNING log is another writer's.
    - ``corruption``: any other SerializationError — checksum mismatch, malformed
      frame, serializer mismatch, deserialize failure on authenticated
      plaintext. Not tamper evidence; always fails open.

    Args:
        error: The exception raised by the deserialize path.
        tier: Cache tier where the failure surfaced ("l1" or "l2").
        cache_key: Cache key being read. Logged only in redacted form
            (redact_cache_key, issue #163 — keys can embed tenant/user
            identifiers); metric labels stay bounded and never carry it.
        fail_closed: The handler's resolved encryption fail-closed policy.

    Returns:
        The reason label, when the policy is fail-open.

    Raises:
        DecryptionAuthenticationError: re-raises ``error`` when ``fail_closed``
            is True and the failure is tamper-class (fail closed). Callers do
            their tier-specific cleanup (L1 invalidation happens BEFORE calling
            this; L2 eviction happens after a fail-open return, so a fail-closed
            raise retains the entry as evidence).
    """
    if isinstance(error, DecryptionAuthenticationError):
        reason = "auth_tamper"
    elif isinstance(error, SuspiciousCacheEntryError):
        reason = "suspicious_envelope"
    elif isinstance(error, EnvelopeShapeError):
        reason = "envelope_shape"
    else:
        reason = "corruption"

    _record_security_counter("cachekit_decrypt_failures_total", {"reason": reason, "tier": tier})

    if fail_closed and isinstance(error, DecryptionAuthenticationError):
        get_logger().error(
            f"{tier.upper()} cache decrypt AUTHENTICATION failure for {redact_cache_key(cache_key)}; "
            f"failing closed (encryption.fail_closed=True): {redact_error_for_log(error)}"
        )
        raise error
    get_logger().warning(
        f"{tier.upper()} cache decrypt/integrity failure ({reason}) for {redact_cache_key(cache_key)}: {redact_error_for_log(error)}"
    )
    return reason


def supports_ttl_inspection(backend: BaseBackend) -> TypeGuard[TTLInspectableBackend]:
    """Type guard to check if backend supports TTL inspection and refresh.

    Args:
        backend: Backend instance to check

    Returns:
        True if backend implements TTLInspectableBackend protocol

    Note:
        This uses TypeGuard to enable proper type narrowing in conditional blocks.
        After this check, the type checker knows backend is TTLInspectableBackend.
    """
    return hasattr(backend, "get_ttl") and hasattr(backend, "refresh_ttl")


def supports_locking(backend: object) -> TypeGuard[LockableBackend]:
    """Type guard: backend provides distributed locking (stampede prevention).

    Takes ``object``, not ``BaseBackend`` like its siblings, because the
    decorator probes the lazily-resolved ``_backend`` cell, which is ``None``
    until first call and may already be narrowed by another capability guard.

    Checked on the INSTANCE, deliberately — unlike ``supports_swr`` below, which
    is class-level to keep ``__getattr__`` proxies and mocks off the freshness
    read path. The asymmetry is the failure direction: a false negative silently
    drops stampede protection on a hot key, while a false positive raises out of
    the ``async with`` — and does NOT fail open. The wrapper's handler degrades
    to lockless execution only for a ``BackendError``; a ``TypeError`` from
    calling a non-callable propagates to the caller and breaks the decorated
    function. Hence ``callable``, not ``hasattr``: a backend carrying
    ``acquire_lock = None`` is not lockable, and must take the lockless path
    rather than crash the call it was meant to protect.

    Equally deliberate: not ``isinstance(backend, LockableBackend)``. Since
    CPython 3.12 a ``runtime_checkable`` Protocol check resolves members with
    ``inspect.getattr_static``, which does not consult ``__getattr__`` — so a
    delegating backend proxy locks on 3.10/3.11 and silently stops locking on
    3.12+. Plain ``getattr`` consults ``__getattr__`` and is stable across
    every supported interpreter.
    """
    return callable(getattr(backend, "acquire_lock", None))


class FillLockBackend(Protocol):
    """Backend whose miss-path lock reports contention and releases without blocking.

    Not part of LockableBackend: only the decorator's miss path uses it. CachekitIOBackend and
    PerRequestRedisBackend. ``acquire_fill_lock`` yields ``(acquired, uncontended)``.

    Contract: the backend's read (``get_with_freshness`` if it has one, else ``get``) raises on a
    failed read and returns None only for an absent entry. An uncontended grant after a None read
    skips the post-lock re-read (see ``L2MissProbe``); a read that hid a failure as None would lose
    its only retry.
    """

    def acquire_fill_lock(
        self, key: str, timeout: float, blocking_timeout: Optional[float] = None
    ) -> contextlib.AbstractAsyncContextManager[tuple[bool, bool]]: ...


def supports_fill_lock(backend: object) -> TypeGuard[FillLockBackend]:
    """Type guard: backend provides ``acquire_fill_lock``.

    Checked on the CLASS, like ``supports_swr``: a Mock or ``__getattr__`` proxy must keep the
    plain ``acquire_lock`` path, and with it the post-lock double-check read. A false negative
    costs one read and one blocking release; a false positive would skip a read on a guess.
    """
    return callable(getattr(type(backend), "acquire_fill_lock", None))


# Backend type names already warned about, so refresh_ttl_on_get degradation warns at most
# once per backend type per process (avoids per-hit log spam). Tests clear this set.
_TTL_REFRESH_UNSUPPORTED_WARNED: set[str] = set()


def warn_ttl_refresh_unsupported(backend: BaseBackend) -> None:
    """Warn ONCE per backend type that ``refresh_ttl_on_get=True`` has no effect here.

    Backends without TTLInspectableBackend (both get_ttl and refresh_ttl) cannot do
    threshold-based TTL refresh, so the flag is silently ignored. A silent no-op on a
    feature the user explicitly opted into is a footgun — surface it once, while
    still degrading gracefully (the caller returns without failing the cache op).
    """
    name = type(backend).__name__
    if name in _TTL_REFRESH_UNSUPPORTED_WARNED:
        return
    _TTL_REFRESH_UNSUPPORTED_WARNED.add(name)
    warnings.warn(
        f"refresh_ttl_on_get=True has no effect on {name}: it does not support TTL "
        f"inspection (needs both get_ttl and refresh_ttl), so the setting is ignored. Use "
        f"the Redis, CachekitIO, or File backend for TTL refresh.",
        UserWarning,
        stacklevel=3,
    )


def supports_buffer_read(backend: BaseBackend) -> TypeGuard[BufferReadableBackend]:
    """Type guard: backend can return a zero-copy buffer via get_buffer (#171, File/POSIX only).

    Returns:
        True if backend implements BufferReadableBackend (used for the mmap Arrow read fast path).
    """
    return hasattr(backend, "get_buffer")


def supports_streaming_write(backend: BaseBackend) -> TypeGuard[BufferWritableBackend]:
    """Type guard: backend can accept an incrementally-streamed value via set_streaming (File only).

    Returns:
        True if backend implements BufferWritableBackend (used for the streaming Arrow write path).
    """
    return hasattr(backend, "set_streaming")


class SWRCapableBackend(Protocol):
    """Backend with server-signaled stale-while-revalidate reads.

    Reads report whether the entry is in its stale-grace window plus the
    server's remaining freshness in seconds (None from a pre-signal
    server); writes accept the window length. Currently only CachekitIOBackend
    (the SaaS signals freshness on read — see protocol
    spec/saas-api.md#stale-while-revalidate and #remaining-freshness).
    """

    def get_with_freshness(self, key: str) -> Optional[tuple[bytes, bool, Optional[int]]]: ...

    def set(self, key: str, value: bytes, ttl: Optional[int] = None, stale_ttl: Optional[int] = None) -> None: ...


def supports_swr(backend: BaseBackend) -> TypeGuard[SWRCapableBackend]:
    """Type guard: backend supports server-signaled SWR stale-grace reads.

    Checked on the backend's CLASS, not the instance: protocol methods live on
    classes, while instance-level hasattr reads dynamic-attribute objects
    (unittest.mock.Mock, __getattr__ proxies) as SWR-capable and silently
    reroutes their reads through the freshness path — reachable without any SWR
    config since the gate widening (the freshness read now runs for
    every capable backend, not only when stale_ttl is set).
    """
    return callable(getattr(type(backend), "get_with_freshness", None))


def supports_key_tracking(backend: object) -> TypeGuard[KeyTrackableBackend]:
    """Type guard: backend keeps a server-side key registry (KeyTrackableBackend).

    Checked on the backend's CLASS, like ``supports_swr``: an instance-level check
    reads ``unittest.mock.Mock`` and ``__getattr__`` proxies as trackable, and
    ``isinstance`` against the runtime-checkable Protocol answers differently on
    3.10/3.11 (``hasattr``) than on 3.12+ (``getattr_static``) for exactly those
    objects. A false negative only keeps today's process-local invalidation; a false
    positive would route every whole-function invalidation through a drain that is
    not there.
    """
    cls = type(backend)
    return callable(getattr(cls, "track_key", None)) and callable(getattr(cls, "drain_tracked", None))


def _fuses_tracked_write(backend: object) -> bool:
    """Whether the backend writes a value and its registry entry in one call: a private ``_set_tracked``,
    found on the CLASS as ``supports_key_tracking`` finds ``track_key``.

    Only while the instance resolves ``set`` and ``track_key`` to the functions in the owner's
    ``_set_tracked_replaces``, as plain methods bound to the backend itself. An override, patch, wrapper or
    delegation anywhere (a subclass, the class itself, the instance, ``__getattribute__``, a proxy) keeps
    both calls, so it still runs on every write. An SWR-capable backend keeps ``set`` too: the fused write
    has no slot for a stale-grace window.
    """
    owner = next((klass for klass in type(backend).__mro__ if "_set_tracked" in vars(klass)), None)
    if owner is None or not supports_key_tracking(backend) or supports_swr(backend):  # pyright: ignore[reportArgumentType]
        return False
    replaces = vars(owner).get("_set_tracked_replaces", {})
    for name in ("set", "track_key"):
        func = replaces.get(name)  # None when the owner records nothing for it: nothing to match
        bound = getattr(backend, name, None)
        if func is None or type(bound) is not types.MethodType:  # a wrapper or proxy (wrapt, APM) must run: not fused
            return False
        if bound.__func__ is not func or bound.__self__ is not backend:  # another function, or another backend's
            return False
    return True


_unreported_write_classes: set[type] = set()
# Three wrapper levels covers a metrics wrapper around a router around a backend; deeper nesting is rare.
_REACH_DEPTH = 3
# Values read at most (attribute values, unset slots, container items and the type entries scanned to find them) before
# the search gives up and answers True. The answer is cached per handler, so a large object graph costs the
# deadline list, never a missed bound.
_REACH_READS = 4096
_REACH_LEAVES = (str, bytes, bytearray, memoryview, int, float, complex, bool, type(None), type)
# Containers are walked with the base type's own iterator, so a subclass's __iter__ never runs.
_REACH_CONTAINERS: tuple[tuple[type, Callable[[Any], Iterator[object]]], ...] = (
    (dict, lambda d: iter(dict.values(d))),  # a C view; iter() on it runs no user code
    (list, list.__iter__),
    (tuple, tuple.__iter__),
    (set, set.__iter__),
    (frozenset, frozenset.__iter__),
)
# Type internals read through type's own descriptors, so a metaclass property or __getattribute__ never runs.
_TYPE_MRO: Any = vars(type)["__mro__"]
_TYPE_DICT: Any = vars(type)["__dict__"]
_TYPE_MODULE: Any = vars(type)["__module__"]
_TYPE_QUALNAME: Any = vars(type)["__qualname__"]
_unreachable_wrapper_classes: set[tuple[str, str]] = set()  # names, not classes: a class per mock is not kept
_MISSING = object()  # not found yet: None is a value a namespace can hold
# A type's scan: (the type, kept alive so its id is not reused; member descriptors; getset __dict__;
# class-level flag; entries scanned).
_TypeScan = tuple[type, list[Any], object, object, int, list[object]]
# Class-namespace values that are code or descriptors, not held objects: skipped by type identity, so no
# metaclass __hash__ runs, and never called.
_CLASS_CODE = (
    types.FunctionType,
    types.BuiltinFunctionType,
    types.MemberDescriptorType,
    types.GetSetDescriptorType,
    types.WrapperDescriptorType,
    types.MethodDescriptorType,
    types.ClassMethodDescriptorType,
    types.MethodWrapperType,
    classmethod,
    staticmethod,
    property,
)


def _scan_type(kind: type) -> _TypeScan:
    """What reading an instance of ``kind`` needs, from one scan of its MRO: its member descriptors, its getset
    ``__dict__`` descriptor, its class-level ``_reports_write_deadline`` value, the entries scanned, and the
    objects its classes hold (``inner = backend`` on a wrapper class), read from the namespaces as stored, so no
    descriptor runs; functions and descriptors are skipped."""
    members: list[Any] = []
    class_values: list[object] = []
    scanned = 0
    instance_dict: object = _MISSING
    flag: object = _MISSING
    for klass in _TYPE_MRO.__get__(kind):
        namespace = _TYPE_DICT.__get__(klass)
        scanned += 1 + len(namespace)
        members += [d for d in namespace.values() if type(d) is types.MemberDescriptorType]
        # Every name, dunders included (``__wrapped__ = backend``): interpreter metadata is strings, ints, types
        # and tuples of them, which the walk treats as leaves, or code and descriptors, skipped here.
        class_values += [
            value
            for value in namespace.values()
            if not any(type(value) is code for code in _CLASS_CODE) and not issubclass(type(value), _REACH_LEAVES)
        ]
        if instance_dict is _MISSING and "__dict__" in namespace:
            instance_dict = namespace["__dict__"]
        if flag is _MISSING and "_reports_write_deadline" in namespace:
            flag = namespace["_reports_write_deadline"]
    return kind, members, instance_dict, flag, scanned, class_values


def _read_object(
    obj: object, types_read: dict[int, _TypeScan], class_depths: dict[int, int], depth: int
) -> tuple[bool, list[object], int]:
    """One read of ``obj`` at ``depth``: (it says it reports a write deadline, its attribute values, reads spent).
    A type is scanned once per search (``types_read``), and charged then. Values are read only below
    ``_REACH_DEPTH``. A type's class values are yielded whenever one of its instances is read shallower than any
    earlier yield of them (``class_depths``), so a type first met at the depth limit, or first met by a longer
    path, still has its class-held backend searched from the shallowest route: at most ``_REACH_DEPTH`` yields
    per type.

    Storage is read only through the C descriptors Python makes for it: the getset ``__dict__`` and every
    member descriptor along the MRO, so a property named ``__dict__`` or shadowing a slot is skipped, never
    called. Member descriptors are deliberately every C member field, not only ``__slots__``: that is how a
    bound method's ``__self__`` is found (``self.write = backend.set``), and a function's ``__globals__``
    only spends the budget, which fails open. Do not make functions leaves: that turns a True into a False.
    """
    kind = type(obj)
    spent = 0
    scan = types_read.get(id(kind))  # by id: hashing the type could run a metaclass __hash__
    if scan is None:
        scan = types_read[id(kind)] = _scan_type(kind)
        spent = scan[4]
    _, members, instance_dict, flag, _, class_values = scan
    want_values = depth < _REACH_DEPTH  # at the limit only the flag is read
    values: list[object] = []
    unset = 0
    for descriptor in members if want_values else ():
        try:
            values.append(descriptor.__get__(obj, kind))
        except AttributeError:  # an unset slot: a read the caller never walks, so it is charged here
            unset += 1
    if type(instance_dict) is types.GetSetDescriptorType:
        held = instance_dict.__get__(obj, kind)  # type: ignore[attr-defined]
        if type(held) is dict:
            if "_reports_write_deadline" in held:  # an instance value shadows the class's
                flag = held["_reports_write_deadline"]
            if want_values:
                values += dict.values(held)
    if want_values and depth < class_depths.get(id(kind), _REACH_DEPTH):
        class_depths[id(kind)] = depth
        values += class_values
    return flag is True, values, spent + unset  # the caller charges each value it walks


def _reaches_write_deadlines(backend: object) -> bool:
    """Whether a write through ``backend`` can report a write deadline (API-103): the backend says it reports
    (read with ``getattr``, so an attribute-forwarding proxy forwards it), or it holds a backend that says so,
    up to ``_REACH_DEPTH`` wrappers down, in an attribute (``__dict__`` or ``__slots__``) or in a dict, list,
    tuple or set there (a metrics, timeout, router or shard-ring wrapper over a CachekitIOBackend).

    One breadth-first walk with one counter: every value read counts, container items and the type entries
    scanned included. An object is expanded again only when reached by a shorter path (``seen`` keeps the
    shallowest depth), so a cycle ends and a wrapper shared by a long and a short path is searched to its
    full reach. Below the root everything is read through Python's own descriptors (``_read_object``), so a
    lazy object is not evaluated and no property, ``__getattr__``, ``__iter__`` override or metaclass hook
    runs. If the walk raises, or spends ``_REACH_READS`` reads without an answer, the answer is True: opening
    the deadline list is always safe, skipping it is not. A wrapper holding its backend only in a closure, or
    creating it on first use (even into an attribute: the search runs before the first write), is out of
    reach: logged once per class, and its L1 copies keep the stated ttl.
    """
    if backend is None:
        return False
    try:
        if getattr(backend, "_reports_write_deadline", False) is True:
            return True
        queue: collections.deque[tuple[object, int]] = collections.deque([(backend, 0)])
        seen = {id(backend): 0}  # id -> the shallowest depth it was reached at
        types_read: dict[int, _TypeScan] = {}  # each type's MRO is scanned once per search
        class_depths: dict[int, int] = {}  # type id -> the shallowest depth its class values were yielded at
        reads = 0
        while queue:
            obj, depth = queue.popleft()
            kind = type(obj)
            walk = next((it for base, it in _REACH_CONTAINERS if issubclass(kind, base)), None)
            batches: list[tuple[Iterable[object], int]] = []
            if walk is not None:
                batches.append((walk(obj), depth))  # a container: its items stay at the holder's depth
            # An object, or a container subclass (a dict router with self.inner): read its own storage too.
            # Identity, not `in`: hashing the type could run a metaclass __hash__.
            if walk is None or not any(kind is base for base, _ in _REACH_CONTAINERS):
                reports, values, scanned = _read_object(obj, types_read, class_depths, depth)
                if reports and obj is not backend:  # the root's own flag was read with getattr above
                    return True
                reads += scanned
                if reads > _REACH_READS:
                    return True
                if depth < _REACH_DEPTH:  # at the limit its flag is read; its own attributes are past the reach
                    batches.append((values, depth + 1))
            for children, child_depth in batches:
                for child in children:
                    reads += 1
                    if reads > _REACH_READS:
                        return True
                    if issubclass(type(child), _REACH_LEAVES) or seen.get(id(child), _REACH_DEPTH + 1) <= child_depth:
                        continue
                    seen[id(child)] = child_depth
                    queue.append((child, child_depth))
        kind = type(backend)
        module = _TYPE_MODULE.__get__(kind)
        name = (module if isinstance(module, str) else "", _TYPE_QUALNAME.__get__(kind))
        if name not in _unreachable_wrapper_classes and not name[0].startswith("cachekit."):
            _unreachable_wrapper_classes.add(name)
            get_logger().info(
                f"{name[1]} holds no CachekitIOBackend in its attributes: its writes keep L1 copies for their "
                "ttl. If it wraps one, hold it in an attribute before the first write."
            )
    except Exception as exc:  # noqa: BLE001 - the safe answer: open the list
        get_logger().debug(f"write-deadline search failed: {type(exc).__name__}")
        return True
    return False


def write_deadlines_reachable(handler: Any) -> bool:
    """``_reaches_write_deadlines`` for the handler's backend, cached on the handler. The decorator always
    installs a ``StandardCacheHandler``; a custom ``CacheHandlerStrategy`` (a public extension point) that
    cannot take the attribute (``__slots__``) is searched again on each write, bounded by ``_REACH_READS``."""
    reachable = _reaches_write_deadlines(getattr(handler, "backend", None))
    with contextlib.suppress(AttributeError):
        handler._write_deadlines_reachable = reachable
    return reachable


def _written_copy_expiry(
    backend: object, seen: list[tuple[float, float]], stored: bool, ttl: Optional[float]
) -> Optional[float]:
    """When the writer's L1 copy of a value it wrote expires (API-103), from what the write reported into
    ``seen`` (``_WRITE_DEADLINES``); None keeps the stated ttl from the put.

    ``min(local_ttl, effective_ttl, fresh_for)``, every term timed from the 2xx's receipt: ttl stands for
    local_ttl and effective_ttl (the L1 default for ttl=None), Fresh-For for the last, unbounded when
    absent. A report wins even when the write then failed (an override that raised after its 2xx). With
    none: None for a failed write (API-36/37 bound the copy) or a backend that never reports, and 0.0, no
    copy, when the backend says it reports but the write never reached its own ``set`` (an override that
    does not delegate, or delegates on another thread without the context), so the bound is unknown: that
    turns L1 off for the backend's writes, logged once per class. A nested write that reports into the
    same list can only shorten the bound.
    """
    if seen:
        local_ttl = DEFAULT_L1_TTL_SECONDS if ttl is None else ttl
        return min(min(received + local_ttl, fresh_until) for received, fresh_until in seen)
    if not stored or getattr(backend, "_reports_write_deadline", False) is not True:
        return None
    if type(backend) not in _unreported_write_classes:
        _unreported_write_classes.add(type(backend))
        get_logger().warning(
            f"{type(backend).__name__}.set reported no write deadline: its writes keep no L1 copy. "
            "Delegate to the base set on the same thread, or under contextvars.copy_context().run."
        )
    return 0.0


def _set_l2(
    handler: Any,
    key: str,
    value: bytes,
    ttl: int | None,
    stale_ttl: int | None,
    registry_id: str | None,
    on_track_error: Callable[[str, Exception], None],
    on_report: Optional[Callable[[tuple[float, float]], None]] = None,
) -> tuple[bool, bool, Optional[float]]:
    """A buffered L2 write through ``handler``: (stored, tracked, l1_until). ``on_report`` is called with each
    ``(received, fresh_until)`` the backend reports, as it reports it (``_ReportedDeadlines``).

    With a ``registry_id``, the key-registry write rides in the same round trip where the handler can
    (``set_tracked``), and tracked is True: its failure has gone to ``on_track_error``. Otherwise tracked is
    False and the caller tracks a stored key itself. l1_until bounds the writer's L1 copy
    (``_written_copy_expiry``), None where the backend reports no freshness. getattr: custom CacheHandlerStrategy
    impls predate set_tracked, as they predate set_streaming.
    """
    set_tracked = None if registry_id is None else getattr(handler, "set_tracked", None)
    if set_tracked is not None:
        stored = set_tracked(key, value, ttl, registry_id, on_track_error)
        if stored is not None:
            return stored, True, None
    # The instruction-gated miss path: the cached answer is a plain attribute load (getattr's call costs the
    # gate measurably; try is free until it raises), and a backend that cannot report takes its own early return
    # (one set call under a conditional token measured +1.02% on `miss`, this +0.79%).
    try:
        reachable = handler._write_deadlines_reachable
    except AttributeError:  # a custom strategy that does not carry the cache
        reachable = None
    if not (write_deadlines_reachable(handler) if reachable is None else reachable):
        # Only thread the SWR kwarg when set: strategy implementations without
        # **metadata (tests, custom handlers) must keep working unchanged.
        if stale_ttl is not None:
            return bool(handler.set(key, value, ttl, stale_ttl=stale_ttl)), False, None
        return bool(handler.set(key, value, ttl)), False, None
    # Around the handler's own set, so an override of it still runs.
    seen: list[tuple[float, float]] = [] if on_report is None else _ReportedDeadlines(on_report)
    token = _WRITE_DEADLINES.set(seen)
    try:
        if stale_ttl is not None:
            stored = bool(handler.set(key, value, ttl, stale_ttl=stale_ttl))
        else:
            stored = bool(handler.set(key, value, ttl))
    finally:
        _WRITE_DEADLINES.reset(token)
    return stored, False, _written_copy_expiry(getattr(handler, "backend", None), seen, stored, ttl)


class _ReportedDeadlines(list[tuple[float, float]]):
    """A ``_WRITE_DEADLINES`` list that applies each report the moment the backend appends it, from inside the
    backend's ``set``: a write-behind copy is bounded at the 2xx's receipt, not when a ``set`` override's
    post-response work (an audit hook) returns."""

    __slots__ = ("_on_report",)

    def __init__(self, on_report: Callable[[tuple[float, float]], None]) -> None:
        super().__init__()
        self._on_report = on_report

    def append(self, entry: tuple[float, float]) -> None:
        super().append(entry)
        self._on_report(entry)


class _MultiDeleteBackend(Protocol):
    """Internal: a backend that deletes many keys in one multi-key call (the backend may split
    it into several sends; Memcached sends per server, 1,000 keys a send).

    ``_delete_many`` returns the keys it could not confirm deleted. A key only counts as
    deleted once the server acknowledged its delete; a key already absent counts as
    deleted. It may also raise as a whole, in which case the outcome of every key is
    unknown. Deliberately private: not part of the backend extension surface.
    """

    def _delete_many(self, keys: list[str]) -> set[str]: ...


def _supports_multi_delete(backend: object) -> TypeGuard[_MultiDeleteBackend]:
    """Type guard for ``_MultiDeleteBackend``: true only when the batch provably deletes what
    ``backend.delete`` would.

    The batch sends the raw keys it is given. A ``delete`` that rewrites keys (a subclass
    adding a prefix, an instance attribute, a slot, a property, a custom
    ``__getattribute__``) would have the sweep batch-delete keys that do not exist, then
    untrack entries whose real L2 keys survive. So rather than inspect every way lookup can
    be customised, this resolves both names exactly as the sweep will call them and requires
    each to be a genuine bound method (``types.MethodType``) of this backend whose function
    is the class's own plain function, with ``_delete_many`` defined at or below the class that defines ``delete``.
    Anything else takes the per-key path: a false negative only costs round trips, a false
    positive loses erasure.
    """
    cls = type(backend)
    if cls.__getattribute__ is not object.__getattribute__:
        return False  # lookup is not stable between this check and the sweep's calls
    owners: dict[str, type] = {}
    for name in ("delete", "_delete_many"):
        owner = next((c for c in cls.__mro__ if name in c.__dict__), None)
        func = owner.__dict__[name] if owner is not None else None
        if not isinstance(func, types.FunctionType):
            return False
        resolved = getattr(backend, name, None)
        # type() first: a MethodType's __func__/__self__ are C slots no object can fake, and
        # reading them runs no user code, so a forged or raising proxy is rejected untouched.
        if type(resolved) is not types.MethodType or resolved.__func__ is not func or resolved.__self__ is not backend:
            return False  # the instance resolves this name to something other than the class's function
        owners[name] = owner  # type: ignore[assignment]
    return issubclass(owners["_delete_many"], owners["delete"])


class L2MissProbe:
    """Whether the async L2 read in a ``probe_l2_miss`` scope found no entry, cleanly.

    ``clean_miss`` is set only where the backend's own read returned None: its freshness read
    (CachekitIO), or ``get`` on a backend without one (Redis). Every backend with
    ``acquire_fill_lock`` raises on a failed read rather than returning None. Any other read leaves
    it False, which costs only a re-read. A read that failed is reported to the caller as a miss too, but leaves it
    False, so anything other than a confirmed miss reads as "not known to be absent".
    """

    __slots__ = ("clean_miss",)

    def __init__(self) -> None:
        self.clean_miss = False


_l2_miss_probe: contextvars.ContextVar[L2MissProbe | None] = contextvars.ContextVar("cachekit_l2_miss_probe", default=None)


@contextlib.contextmanager
def probe_l2_miss(probe: L2MissProbe) -> Iterator[None]:
    """Scope an async L2 read so ``probe`` tells a clean miss from a swallowed read failure.

    The read paths report both as None; the decorator's miss path needs the difference, because
    after a failed read the entry may still be live and the post-lock double-check is its only
    retry.
    """
    token = _l2_miss_probe.set(probe)
    try:
        yield
    finally:
        _l2_miss_probe.reset(token)


def _note_clean_l2_miss() -> None:
    if (probe := _l2_miss_probe.get()) is not None:
        probe.clean_miss = True


def _normalize_freshness_hit(hit: Any) -> Optional[tuple[bytes, bool, Optional[int]]]:
    """Pad a released 2-tuple ``(bytes, is_stale)`` hit to ``(bytes, is_stale, None)``.

    Third-party SWR backends (0.5.x) and custom CacheHandlerStrategy implementations
    (0.18.0) built against the 2-tuple signature still return it; None passes through.
    """
    if hit is None:
        return None
    value, is_stale, *rest = hit
    return (value, is_stale, rest[0] if rest else None)


# Import caching for serializer modules
#
# PERFORMANCE OPTIMIZATION: Dynamic imports are expensive
# which matters significantly for a caching library called frequently.
#
# THREAD SAFETY: Multiple threads importing the same serializer simultaneously
# could cause race conditions. RLock prevents this with double-checked locking.
#
# MEMORY EFFICIENCY: Avoids keeping multiple copies of the same serializer
# class in memory across different parts of the application.
#
# GIL CONTENTION: Reduces Python's Global Interpreter Lock contention from
# repeated module loading operations in multi-threaded environments.

_serializer_cache_lock = threading.RLock()
_serializer_cache: dict[str, type] = {}

# Cache serializer instances, created on first lookup, so repeat lookups skip instantiation
_serializer_instance_cache = {}
_serializer_instance_lock = threading.RLock()


def _get_cached_serializer_class(serializer_name: str, import_path: str):
    """Thread-safe cached import of serializer classes.

    Args:
        serializer_name: Name of the serializer (e.g., 'rust', 'msgpack')
        import_path: Full import path (e.g., 'cachekit.serializers.RustSerializer')

    Returns:
        Cached serializer class
    """
    cache_key = f"{serializer_name}:{import_path}"

    # Fast path: check if already cached (without lock for read performance)
    if cache_key in _serializer_cache:
        return _serializer_cache[cache_key]

    # Slow path: acquire lock and import
    with _serializer_cache_lock:
        # Double-checked locking pattern - verify still not cached after acquiring lock
        # This prevents race conditions where two threads both pass the first check
        # but only one should actually perform the import
        if cache_key in _serializer_cache:
            return _serializer_cache[cache_key]

        # Import the module and get the class
        # This is the expensive operation we're caching
        try:
            module_path, class_name = import_path.rsplit(".", 1)
            module = __import__(module_path, fromlist=[class_name])
            serializer_class = getattr(module, class_name)

            # Cache the imported class for future use
            # Key format: "serializer_name:full.import.path" for uniqueness
            _serializer_cache[cache_key] = serializer_class
            get_logger().debug(f"Cached serializer import: {serializer_name} -> {import_path}")

            return serializer_class
        except (ImportError, AttributeError) as e:
            get_logger().warning(f"Failed to import serializer {import_path}: {redact_error_for_log(e)}")
            raise


def _keeps_verified(serializer: Any) -> bool:
    """Whether ``serializer`` is exactly a built-in that mints :class:`VerifiedPayload` for L1 to keep
    (``_deserialize_keeping`` / ``_deserialize_verified``), with integrity checking on: integrity off, there is
    no check for a kept entry to skip. Exact types: a subclass may override the decode. OrjsonSerializer's
    module needs the [json] extra, so it is looked up only once loaded, as it is wherever an instance exists.
    """
    orjson_module = sys.modules.get("cachekit.serializers.orjson_serializer")
    keeping = (StandardSerializer, AutoSerializer, *((orjson_module.OrjsonSerializer,) if orjson_module else ()))
    return type(serializer) in keeping and serializer.enable_integrity_checking


def _get_cached_serializer_instance(
    serializer: Union[str, SerializerProtocol], enable_integrity_checking: bool = True
) -> SerializerProtocol:  # type: ignore[name-defined]
    """Get serializer instance with configurable integrity checking.

    This eliminates the expensive SerializerFactory.create_serializer() path
    that was doing validation, imports, and instance creation on every call.

    Now uses the unified get_serializer() factory from serializers/__init__.py,
    which supports pluggable serializers via SERIALIZER_REGISTRY.

    Note: Encryption is now a separate layer (not a serializer type).
    Use encryption=True parameter instead of serializer="encrypted".

    Args:
        serializer: Either a string name ("default", "arrow", "orjson") or SerializerProtocol instance
        enable_integrity_checking: Enable integrity checking (default: True)
            Uses xxHash3-64 for all serializers (Rust ByteStorage for default/auto,
            Python xxhash for arrow/orjson).

    Returns:
        Serializer instance implementing SerializerProtocol

    Raises:
        ValueError: If serializer_name not in SERIALIZER_REGISTRY
        TypeError: If serializer is not a string or SerializerProtocol instance, or is a
            serializer class rather than an instance of one
    """
    # If already a protocol instance, validate and return directly
    if not isinstance(serializer, str):
        from cachekit.serializers.base import SerializerProtocol

        # A class passes the runtime_checkable protocol check (the class object has the
        # methods), but its identity would be its METACLASS name — 'type' — so every class
        # passed this way would share one frame tag and one key code, the shared bucket the
        # serializer code exists to prevent; and serialize() would be an unbound call that
        # fails on every write. Almost always a missing "()".
        if isinstance(serializer, type):
            raise TypeError(
                f"serializer must be an instance, not the class {serializer.__name__}: "
                f"pass {serializer.__name__}(), not {serializer.__name__}"
            )
        if not isinstance(serializer, SerializerProtocol):
            raise TypeError(
                f"serializer must be a string name or SerializerProtocol instance, got {type(serializer).__name__}. "
                f"Valid string names: 'default', 'arrow', 'orjson'. For custom serializers, implement SerializerProtocol."
            )
        # Return protocol instance directly (no caching for custom instances)
        return serializer

    # String name - use cached lookup with integrity_checking setting
    serializer_name = serializer
    cache_key = f"{serializer_name}:{enable_integrity_checking}"

    # Fast path: check if already cached (lock-free read)
    if cache_key in _serializer_instance_cache:
        return _serializer_instance_cache[cache_key]

    # Slow path: create and cache instance (with lock)
    with _serializer_instance_lock:
        # Double-checked locking pattern
        if cache_key in _serializer_instance_cache:
            return _serializer_instance_cache[cache_key]

        # Use unified serializer factory (supports "default", "arrow", "orjson", etc.)
        try:
            from cachekit.serializers import get_serializer

            instance = get_serializer(serializer_name, enable_integrity_checking=enable_integrity_checking)
        except ValueError as e:
            # Re-raise with additional context about encryption
            raise ValueError(f"{e} For encryption, use encryption=True parameter (not serializer='encrypted').") from e

        # Cache the instance
        _serializer_instance_cache[cache_key] = instance
        return instance


class CacheSerializationHandler:
    """Handles serialization/deserialization with optional encryption layer.

    Architecture:
    - Serializer: Defines HOW to serialize (default/msgpack, future: pickle, json)
    - Encryption: Defines WHETHER to encrypt (security layer on top, orthogonal)
    - Tenant extraction: selects the per-tenant derived key (not a tenancy boundary; no shared-key fallback)

    Modes (encryption is tri-state: None=unset / True=force-on / False=hard opt-out):
    - encryption=None: no intent stated — plaintext when no master key is present. Raises ConfigurationError
      when one is (master_key= or CACHEKIT_MASTER_KEY): a key is a key source, not an activation switch
    - encryption=False: Explicit opt-out — direct serialization (plaintext), even if a master key is set
    - encryption=True, tenant_extractor=None: Single-tenant encrypted (tenant_id "default"
      unless deployment_uuid / CACHEKIT_DEPLOYMENT_UUID is set)
    - encryption=True, tenant_extractor provided: Multi-tenant encrypted (no shared-key fallback)

    Examples:
        Basic usage without encryption:

        >>> handler = CacheSerializationHandler(serializer_name="default")
        >>> data = {"user_id": 123, "name": "Alice"}
        >>> serialized = handler.serialize_data(data, cache_key="user:123")
        >>> isinstance(serialized, bytes)
        True

        With encryption (single-tenant mode) - requires resetting the settings singleton
        first to pick up the new environment variable:

        >>> import os
        >>> from cachekit.config.singleton import reset_settings
        >>> reset_settings()  # Clear cached settings
        >>> os.environ["CACHEKIT_MASTER_KEY"] = secret_key  # 32-byte hex key
        >>> handler = CacheSerializationHandler(
        ...     serializer_name="default",
        ...     encryption=True,
        ...     single_tenant_mode=True,
        ...     deployment_uuid="00000000-0000-0000-0000-000000000001",
        ... )
        >>> # Encryption requires cache_key for AAD binding
        >>> serialized = handler.serialize_data(
        ...     {"secret": "password"},
        ...     cache_key="user:123:credentials"
        ... )
        >>> isinstance(serialized, bytes)
        True
        >>> reset_settings()  # Cleanup
        >>> del os.environ["CACHEKIT_MASTER_KEY"]
    """

    def __init__(
        self,
        serializer_name: Union[str, SerializerProtocol] = "default",  # type: ignore[name-defined]
        encryption: bool | None = None,
        tenant_extractor: Any | None = None,
        single_tenant_mode: bool = False,
        deployment_uuid: Optional[str] = None,
        master_key: str | SecretStr | None = None,
        enable_integrity_checking: bool = True,
        encryption_fail_closed: bool | None = None,
        interop_mode: bool = False,
        backend_key_prefix: Callable[[], str] | None = None,
    ):
        """Initialize with serializer strategy and optional encryption.

        Args:
            serializer_name: Serializer instance or name. Accepts either:
                            - String name: "default" (MessagePack), "arrow" (DataFrames as Arrow IPC), "orjson" (JSON)
                            - SerializerProtocol instance: Custom serializer implementing the protocol
            encryption: Tri-state encryption control (wraps serializer with EncryptionWrapper):
                        - None (default): no intent stated. Plaintext when no master key is present;
                          raises ConfigurationError when one is, from master_key= or
                          CACHEKIT_MASTER_KEY, whatever tenant_extractor is. Pass True or False.
                        - True: force encryption ON (requires a master key + explicit tenant mode).
                        - False: explicit hard opt-out. Never encrypts, even when CACHEKIT_MASTER_KEY
                          is set; stale ciphertext is still decrypted on read (legacy-decrypt), except
                          in interop mode, where stored bytes are plain-decoded and never decrypted.
            tenant_extractor: Optional TenantContextExtractor for multi-tenant encryption.
                             Only used if encryption=True.
                             If None: single-tenant mode (tenant_id "default" unless overridden).
                             If provided: multi-tenant mode (extracts tenant_id; no shared-key fallback).
            single_tenant_mode: Explicitly enable single-tenant mode (requires encryption=True).
                               Mutually exclusive with tenant_extractor.
            deployment_uuid: Optional explicit tenant_id override for single-tenant mode
                            (validated UUID). Falls back to CACHEKIT_DEPLOYMENT_UUID, then to the
                            protocol literal "default" — the cross-SDK default every SDK derives from.
            master_key: Optional master key for encryption (hex-encoded). If not provided,
                       reads from CACHEKIT_MASTER_KEY environment variable.
            enable_integrity_checking: Enable integrity checking (default: True)
                                      Uses xxHash3-64 (8 bytes) for all serializers.
                                      Set to False for @cache.minimal (speed-first, no checksums)
                                      NOTE: xxHash3-64 is CORRUPTION detection only — it is not
                                      cryptographic and offers no tamper resistance. Tamper
                                      resistance requires encryption (AES-256-GCM).
            encryption_fail_closed: Tri-state tamper-failure policy:
                                   - None (default): defer to CACHEKIT_ENCRYPTION_FAIL_CLOSED
                                     env setting (defaults to False = fail open).
                                   - True: raise DecryptionAuthenticationError to the caller on
                                     AES-GCM authentication failure or key-fingerprint mismatch
                                     instead of silently recomputing.
                                   - False: explicit fail-open opt-out (warn + metric + recompute).
            backend_key_prefix: Returns the backend's key_prefix in the calling context ("" when it
                               has none). The AAD binds it ahead of the cache key (see _aad_key).
                               None: no prefix, for a handler with no backend behind it.

        Raises:
            ConfigurationError: If encryption config is invalid (missing mode or both modes), a
                master key is present while encryption is None, or an encrypting handler's current key
                (master_key= or CACHEKIT_MASTER_KEY) is also among the previous master keys.
            TypeError: If serializer_name is not a string or SerializerProtocol instance, or master_key is
                bytes (pass ``key.hex()``).

        Note:
            No shared-key fallback: If encryption=True and tenant_extractor provided
            but extraction fails, ValueError propagates to caller (no fallback to shared key).
        """
        master_key = hide_any_secret(master_key)  # rebound before the refusal below can raise (CWE-532)
        master_key = refuse_bytes_key(master_key)
        self.serializer_name = serializer_name
        self.enable_integrity_checking = enable_integrity_checking
        self.interop_mode = interop_mode
        self._single_tenant_id: Optional[str] = None
        self._backend_key_prefix: Callable[[], str] = backend_key_prefix or (lambda: "")

        # Interop mode (interop/v1, spec/interop-mode.md): values are ONE plain
        # MessagePack document — no ByteStorage envelope and no CK v3 frame, so
        # there is no stored metadata header at all. Whether an entry is
        # encrypted is decided by THIS handler's configuration, never by
        # sniffing stored bytes (fail closed by construction; the plaintext
        # downgrade cannot be reintroduced because there is no header to forge).
        if interop_mode:
            if tenant_extractor is not None:
                raise ConfigurationError(
                    "interop mode does not support tenant_extractor (multi-tenant) encryption: "
                    "interop entries store no metadata header, so the read path cannot recover "
                    "a per-call tenant. Use single_tenant_mode=True instead: the tenant is the "
                    "protocol literal 'default', so the same master key is enough across SDKs. "
                    "To scope keys to a deployment, set the same canonical deployment_uuid in every SDK."
                )
            if (
                isinstance(serializer_name, str)
                and CacheKeyGenerator.SERIALIZER_NAME_ALIASES.get(serializer_name, serializer_name) != "default"
            ):
                raise ConfigurationError(
                    f"interop mode requires the default (MessagePack) serializer, got '{serializer_name}': "
                    f"interop/v1 values are plain MessagePack by specification."
                )
            if not isinstance(serializer_name, str):
                raise ConfigurationError(
                    "interop mode does not accept a custom serializer instance: interop/v1 values "
                    "are canonical plain MessagePack produced by the built-in interop encoder."
                )

        # Tri-state encryption resolution. `encryption` is None/True/False:
        #   None  -> no intent stated: plaintext when no master key is present, an error when one is
        #   True  -> explicit force-on (validated below)
        #   False -> explicit hard opt-out; honored even when a master key is present
        #
        # Why (protocol spec/intent-presets.md § Encryption Activation): a master key is a key SOURCE, never
        # an activation SWITCH, so a key with no stated intent is refused rather than guessed, whatever
        # tenant_extractor or backend is. An explicit False MUST NOT be promoted to True (issue #128).
        if encryption is None:
            key_source = "master_key=" if master_key else "CACHEKIT_MASTER_KEY" if get_settings().master_key else None
            if key_source is not None:
                raise ConfigurationError(
                    f"A master key is present ({key_source}) but this cache states no encryption intent (no "
                    f"encryption=, or an EncryptionConfig without enabled=). A master key is a key source, not an "
                    f"activation switch. State the intent: @cache.secure(...) to require encryption; "
                    f"encryption=True with single_tenant_mode=True{'' if interop_mode else ' or a tenant_extractor'} "
                    f"(on a preset: encryption=EncryptionConfig(enabled=True, single_tenant_mode=True)) to force it "
                    f"on; or encryption=False to store plaintext"
                    f"{'' if interop_mode else ', the only choice with backend=None, which stores raw objects'}. "
                    + (
                        "In this interop cache encryption=False never decrypts stale ciphertext: move every SDK that "
                        "binds it to encryption=False and a new namespace together, or stale entries come back as "
                        "wrong values."
                        if interop_mode
                        else "encryption=False still decrypts stale ciphertext on read."
                    )
                )
            encryption = False

        # The keyring's forward-only rule, refused here at decoration before any backend call, whichever route the
        # current key took. Settings run the same check at load and on assignment, but never see master_key=; without
        # this, the native keyring refuses only when the first read or write builds it. An encryption-disabled handler
        # never encrypts, so it spends no nonce budget: its keyring is built only for a config-drift read, whose fault
        # is a miss by design. This check loads no settings for a plaintext handler.
        if encryption:
            settings = get_settings()
            try:
                refuse_current_key_in_previous_keys(master_key or settings.master_key, settings.previous_master_keys)
            except ValueError as e:
                raise ConfigurationError(str(e)) from e

        self.encryption = encryption
        self.tenant_extractor = tenant_extractor
        self.single_tenant_mode = single_tenant_mode
        self.deployment_uuid = deployment_uuid
        self.master_key = reveal_secret(master_key)

        # Tri-state fail-closed resolution (mirrors the `encryption` tri-state, issue #128):
        # an explicit True/False from EncryptionConfig wins; None defers to the fleet-wide
        # CACHEKIT_ENCRYPTION_FAIL_CLOSED env setting (default False = historical fail-open).
        # Resolved independently of self.encryption because an encryption-disabled handler can
        # still decrypt stale encrypted entries (config-drift reads) and must honor the policy.
        if encryption_fail_closed is None:
            encryption_fail_closed = get_settings().encryption_fail_closed
        self.encryption_fail_closed = encryption_fail_closed

        # Config-drift warn-once bookkeeping: first drift read per cache_key warns,
        # the rest only increment cachekit_config_drift_reads_total (log-flood guard).
        # Bounded set — beyond 1024 distinct drifted keys, counter-only.
        self._drift_warned_keys: set[str] = set()

        # Extract string name for metadata storage (for protocol instances, use class name)
        if isinstance(serializer_name, str):
            # Canonicalize aliases so 'pythonic' is written and read back as 'auto' (and
            # 'std'/'standard' as 'default'): either spelling reads the other's entries instead
            # of a serializer mismatch on every read (#167). Resolved through the key generator's
            # map so the frame tag and the key's serializer code cannot drift apart.
            self._serializer_string_name = CacheKeyGenerator.SERIALIZER_NAME_ALIASES.get(serializer_name, serializer_name)
        else:
            # Protocol instance - use class name for metadata
            self._serializer_string_name = type(serializer_name).__name__

        # Cache-key identity. Deliberately NOT the frame tag: the frame tag is a wire-format
        # value and must stay the class name for instances, but a class name is attacker-
        # choosable, and one literally named `auto` would otherwise take AutoSerializer's key
        # code AND pass the deserialize-time guard (it compares this same string) — reading
        # another decorator's bytes through the wrong serializer. Every instance resolves to
        # one reserved identity no class name can equal, so their keyspaces stay separate.
        self._serializer_key_name = (
            self._serializer_string_name
            if isinstance(serializer_name, str)
            else CacheKeyGenerator.CUSTOM_SERIALIZER_PREFIX + self._serializer_string_name
        )

        # Validate single-tenant mode configuration
        if self.encryption:
            # Require explicit tenant mode (either extractor OR single_tenant_mode)
            if not self.tenant_extractor and not self.single_tenant_mode:
                raise ConfigurationError(
                    "Encryption requires explicit tenant mode. "
                    "Provide tenant_extractor for multi-tenant OR "
                    "set single_tenant_mode=True for single-tenant."
                )

            # Prevent both modes from being enabled simultaneously
            if self.tenant_extractor and self.single_tenant_mode:
                raise ConfigurationError(
                    "Cannot use both tenant_extractor and single_tenant_mode. "
                    "Choose multi-tenant (tenant_extractor) OR single-tenant (single_tenant_mode=True)."
                )

            # Issue #134: protocol ENC-2 requires the step after decryption to be the
            # reader's configured serializer, never a choice made by inspecting the
            # decrypted bytes. The user's serializer is threaded into EncryptionWrapper (see
            # _get_cached_encryption_wrapper); it is NOT silently replaced. We therefore
            # allow serializers that decode one fixed format and reject the rest (notably
            # 'auto' and unmarked custom instances) with a clear error.
            if isinstance(serializer_name, str):
                if serializer_name not in CROSS_SDK_SERIALIZER_NAMES:
                    raise ConfigurationError(
                        f"Encryption requires a serializer that decodes one fixed format after "
                        f"decryption (protocol ENC-2), got serializer='{serializer_name}'. Allowed under "
                        f"encryption: {', '.join(CROSS_SDK_SERIALIZER_NAMES)}. The 'auto' serializer "
                        f"chooses its decoder by inspecting the bytes, so it cannot be used with encryption."
                    )
            else:
                require_cross_sdk_serializer(serializer_name)

            # Resolve the single-tenant tenant_id (protocol intent-presets.md § Master Key
            # Input, rule 5): explicit deployment_uuid → CACHEKIT_DEPLOYMENT_UUID → "default".
            if self.single_tenant_mode:
                self._single_tenant_id, source = self._resolve_single_tenant_id(provided_uuid=self.deployment_uuid)
                get_logger().info(
                    "Single-tenant mode initialized",
                    extra={"tenant_id": self._single_tenant_id, "source": source},
                )

        # Use cached base serializer instance with integrity_checking setting
        # Encryption wrapper is created per-request with tenant_id (if encryption=True)
        if interop_mode:
            # Interop values bypass ByteStorage entirely; integrity checking is
            # meaningless here (tamper protection comes from AES-GCM when
            # encryption is on). InteropSerializer is stateless — no cache needed.
            from cachekit.serializers.interop_serializer import InteropSerializer

            self._base_serializer = InteropSerializer()
        else:
            self._base_serializer = _get_cached_serializer_instance(serializer_name, enable_integrity_checking)
        self._custom_serializer = not isinstance(serializer_name, str)
        # Whether an L1 hit may swap this handler's entry for a kept one (deserialize_l1): plaintext
        # CK-frame entries of a built-in serializer that keeps one, with integrity on (_keeps_verified; interop
        # mode's InteropSerializer is not one). Encrypted handlers keep ciphertext in L1 (protocol PRE-10, PRE-11).
        self.keeps_verified_in_l1 = not encryption and not self._custom_serializer and _keeps_verified(self._base_serializer)

        # Cache EncryptionWrapper instances per tenant to prevent
        # 360K key copies/hour at 100 req/sec. Uses thread-safe LRU cache (maxsize=256)
        # with double-checked locking pattern (OrderedDict + RLock, NOT functools.lru_cache)
        from collections import OrderedDict

        self._encryption_wrapper_cache: OrderedDict[str, Any] = OrderedDict()  # tenant_id -> EncryptionWrapper
        self._encryption_cache_lock = threading.RLock()
        self._encryption_cache_maxsize = 256

    @property
    def serializer_key_name(self) -> str:
        """Serializer identity for the cache key, as CacheKeyGenerator.SERIALIZER_CODES keys it."""
        return self._serializer_key_name

    DEFAULT_TENANT_ID = "default"
    """Implicit single-tenant ``tenant_id`` — the protocol's cross-SDK literal.

    protocol ``spec/intent-presets.md`` § Master Key Input, rule 5: with no caller-supplied
    tenant, every SDK MUST derive keys and build AAD with the literal ``"default"``, so a
    py, rs and ts client on one master key produce mutually decryptable ciphertext. A
    per-machine value here (the persisted ``~/.cachekit/deployment_uuid`` earlier releases auto-generated) is
    a silent, permanent cross-SDK authentication failure — not a miss.
    """

    def _resolve_single_tenant_id(self, provided_uuid: Optional[str]) -> tuple[str, str]:
        """Resolve the tenant_id used for HKDF derivation and AAD in single-tenant mode.

        Returns ``(tenant_id, source)``; ``source`` names which of the three rungs below won.

        Priority order:
        1. Explicit ``deployment_uuid`` parameter (validated UUID)
        2. ``CACHEKIT_DEPLOYMENT_UUID`` configuration (validated UUID)
        3. The protocol literal ``"default"`` (:attr:`DEFAULT_TENANT_ID`)

        There is deliberately no machine-local source: the tenant is a key-derivation
        input, so anything that differs per host differs per key.

        Raises:
            ConfigurationError: If an explicit UUID is malformed (or, in interop mode,
                not in canonical lowercase-hyphenated form).
        """
        import uuid

        for raw, source in (
            (provided_uuid, "deployment_uuid parameter"),
            (get_settings().deployment_uuid, "CACHEKIT_DEPLOYMENT_UUID"),
        ):
            if not raw:
                continue
            try:
                validated_uuid = str(uuid.UUID(raw))
            except ValueError:
                validated_uuid = None
            if validated_uuid is None:
                # Names the source, never the value: a key pasted in its place would ship in the message (CWE-532).
                # Raised outside the except, so the parse error, which can quote the value, is not its __context__.
                raise ConfigurationError(f"Invalid {source} (must be valid UUID)")
            self._require_canonical_tenant_form(raw, validated_uuid, source=source)
            return validated_uuid, source

        return self.DEFAULT_TENANT_ID, "protocol default"

    def _require_canonical_tenant_form(self, raw: str, canonical: str, source: str) -> None:
        """Interop mode: reject a deployment UUID that is not already canonical.

        The tenant string feeds HKDF key derivation and AAD component 1. Python
        normalizes through uuid.UUID() (lowercases, strips braces/URN); another
        SDK reading the same shared config would use the raw string — different
        derived keys, silent cross-SDK auth failures, mutual entry eviction.
        Interop mode therefore requires the configured value to be byte-equal to
        its canonical lowercase-hyphenated form, so what Python derives from IS
        the literal string every other SDK sees. Auto mode is unaffected
        (normalization there is long-shipped behavior).
        """
        if self.interop_mode and raw.strip() != canonical:
            raise ConfigurationError(
                f"interop mode requires the deployment UUID from {source} to be in canonical "
                f"lowercase-hyphenated form (got {raw!r}, canonical {canonical!r}): other SDKs "
                f"derive tenant keys from the raw string, so any normalization on the Python "
                f"side silently breaks cross-SDK decryption."
            )

    def _aad_key(self, cache_key: str) -> str:
        """The ``cache_key`` the AAD binds: the backend's key_prefix followed by the call key.

        protocol ``spec/encryption.md`` defines it as the logical backend key, every client-side
        prefix included, so an entry copied to another prefix (another Memcached ``key_prefix``,
        another tenant's ``t:{tenant}:``) fails authentication. A tenant-scoped backend resolves
        its prefix per context, so this is read on the same call as the backend operation.
        Interop mode keeps the bare key, which its read path uses too: interop refuses any
        prefixing backend. An empty or non-string key is passed through as is: the wrapper's own
        checks refuse it.
        """
        if self.interop_mode or not cache_key or not isinstance(cache_key, str):  # pyright: ignore[reportUnnecessaryIsInstance] — runtime arg
            return cache_key
        return f"{self._backend_key_prefix()}{cache_key}"

    def _get_cached_encryption_wrapper(self, tenant_id: str) -> Any:
        """Get or create cached EncryptionWrapper for tenant_id.

        Thread-safe LRU cache with double-checked locking.
        Prevents 360K key copies/hour at 100 req/sec by reusing EncryptionWrapper
        instances (which internally cache derived keys).

        Args:
            tenant_id: Tenant identifier for per-tenant key derivation

        Returns:
            Cached or newly created EncryptionWrapper instance
        """
        # Fast path: check cache without lock (read-only, safe)
        if tenant_id in self._encryption_wrapper_cache:
            # Move to end for LRU (requires lock)
            with self._encryption_cache_lock:
                if tenant_id in self._encryption_wrapper_cache:
                    self._encryption_wrapper_cache.move_to_end(tenant_id)
                    return self._encryption_wrapper_cache[tenant_id]

        # Slow path: create new wrapper with lock
        with self._encryption_cache_lock:
            # Double-checked locking: verify still not cached after acquiring lock
            if tenant_id in self._encryption_wrapper_cache:
                self._encryption_wrapper_cache.move_to_end(tenant_id)
                return self._encryption_wrapper_cache[tenant_id]

            # Create new EncryptionWrapper
            from cachekit.serializers.encryption_wrapper import EncryptionWrapper

            # Issue #134: thread the user's base serializer into the wrapper so an
            # allowed serializer (Arrow/orjson) is actually used under
            # encryption instead of being silently replaced by StandardSerializer.
            # The base serializer is fixed per handler instance, so the per-tenant
            # cache key (tenant_id) remains correct — every wrapper for this handler
            # wraps the same _base_serializer.
            wrapper = EncryptionWrapper(
                serializer=self._base_serializer,
                tenant_id=tenant_id,
                # hex string to bytes, wrapped inline: no local holds the decoded key
                master_key=SecretBytes(bytes.fromhex(self.master_key)) if self.master_key else None,
                fail_closed=self.encryption_fail_closed,
                # A hex key may decode past 32 bytes; only a key passed to the wrapper raw must be exactly 32.
                _master_key_from_hex=True,
            )

            # Enforce LRU cache size limit
            if len(self._encryption_wrapper_cache) >= self._encryption_cache_maxsize:
                # Remove oldest (first) entry
                self._encryption_wrapper_cache.popitem(last=False)

            # Add new wrapper (at end)
            self._encryption_wrapper_cache[tenant_id] = wrapper
            get_logger().debug(f"Created and cached EncryptionWrapper for tenant: {tenant_id}")

            return wrapper

    def serialize_data(
        self,
        data: Any,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        cache_key: str = "",
    ) -> bytes:
        """Serialize data for cache storage with optional tenant context for encryption.

        Args:
            data: Data to serialize
            args: Positional arguments from cached function (for tenant extraction)
            kwargs: Keyword arguments from cached function (for tenant extraction)
            cache_key: Cache key for AAD binding (SECURITY CRITICAL for encryption).
                      Required when encryption is enabled to prevent ciphertext substitution.
                      The AAD binds it behind the backend's key_prefix (see _aad_key).

        Returns:
            Serialized data wrapped for cache storage

        Raises:
            ValueError: If tenant extraction fails in multi-tenant mode (no shared-key fallback)
            ValueError: If cache_key is empty when encryption is enabled
            ValueError: If the serialized envelope exceeds max_value_size
                (CACHEKIT_MAX_VALUE_SIZE) — the L2 oversized-entry ceiling
            SerializationError: If serialization fails

        Note:
            Tenant extraction has no shared-key fallback:
            - If tenant_extractor provided: extracts tenant_id from args/kwargs or raises ValueError
            - If single_tenant_mode=True: uses the tenant_id resolved in __init__ (explicit UUID, else "default")

        Examples:
            Serialize a dictionary (no encryption):

            >>> handler = CacheSerializationHandler(serializer_name="default")
            >>> data = {"user": "alice", "score": 42}
            >>> result = handler.serialize_data(data, cache_key="scores:alice")
            >>> isinstance(result, bytes)
            True

            Serialize with different data types:

            >>> handler = CacheSerializationHandler()
            >>> handler.serialize_data([1, 2, 3], cache_key="list:test")  # doctest: +ELLIPSIS
            b'...'
            >>> handler.serialize_data("hello", cache_key="str:test")  # doctest: +ELLIPSIS
            b'...'
            >>> handler.serialize_data(None, cache_key="none:test")  # doctest: +ELLIPSIS
            b'...'

            Round-trip serialization:

            >>> handler = CacheSerializationHandler()
            >>> original = {"nested": {"list": [1, 2, 3]}, "flag": True}
            >>> serialized = handler.serialize_data(original, cache_key="complex:data")
            >>> recovered = handler.deserialize_data(serialized, cache_key="complex:data")
            >>> recovered == original
            True
        """
        kwargs = kwargs or {}

        try:
            # Wrap with encryption layer if requested (defines WHETHER to encrypt)
            if self.encryption:
                # Extract tenant_id based on configuration (no shared-key fallback)
                if self.tenant_extractor:
                    # Multi-tenant mode: MUST extract tenant_id
                    # A failed extraction raises out of serialize_data (no shared-key fallback); the
                    # decorator's store path catches it, logs it and skips the write
                    tenant_id = self.tenant_extractor.extract(args, kwargs)
                else:
                    # Single-tenant mode: tenant_id resolved once in __init__
                    # (explicit deployment_uuid / CACHEKIT_DEPLOYMENT_UUID, else "default").
                    if self._single_tenant_id is None:
                        raise RuntimeError("single-tenant tenant_id should be set in __init__")
                    tenant_id = self._single_tenant_id

                # Use cached EncryptionWrapper to prevent 360K key copies/hour
                # Gets cached instance (thread-safe LRU, maxsize=256) instead of creating new one
                serializer = self._get_cached_encryption_wrapper(tenant_id)

                # EncryptionWrapper.serialize() requires cache_key for AAD v0x03 binding
                serialized_data, metadata = serializer.serialize(data, self._aad_key(cache_key))
            else:
                # No encryption - use base serializer directly (no cache_key needed)
                serializer = self._base_serializer
                serialized_data, metadata = serializer.serialize(data)

            if self.interop_mode:
                # Interop/v1: the stored bytes ARE the document — plain MessagePack,
                # or nonce||ciphertext||tag when encrypted. No CK frame, no metadata
                # header; other SDKs must be able to read these bytes as-is.
                wrapped = serialized_data
            else:
                # Convert metadata to dict if needed
                metadata_dict = metadata.to_dict() if hasattr(metadata, "to_dict") else {}
                if self._serializer_string_name in _ARROW_FRAME_TAGS:
                    # One prefix builder for this path and write_serialized_to, so buffered and streamed
                    # entries stay byte-identical (the payload is copied once, as in wrap()).
                    wrapped = self._frame_prefix(metadata_dict) + serialized_data
                else:
                    wrapped = SerializationWrapper.wrap(serialized_data, metadata_dict, self._serializer_string_name)
        except ValueError:
            # Tenant extraction, cache_key missing, or an interop data-model
            # rejection (InteropError) — FAIL CLOSED / fail loud (re-raise).
            raise
        except Exception as e:
            # Don't silently fallback - log error and raise to prevent data loss
            get_logger().error(f"Serialization failed with {self.serializer_name}: {redact_error_for_log(e)}")
            raise SerializationError(f"Failed to serialize data with {self.serializer_name}: {e}") from e

        # L2 oversized-entry ceiling (issue #163): every L2 write flows through here,
        # so this is the single enforcement point for max_value_size. Callers catch the
        # raise and degrade to uncached execution (warning logged, function result still
        # returned) — a cache must never break the wrapped function.
        max_value_size = get_settings().max_value_size
        if len(wrapped) > max_value_size:
            # Redact the raw cache key: it can carry caller-supplied tenant/user identifiers, and
            # this message reaches the fallback warning log path (issue #163 review).
            raise ValueError(
                f"Serialized value for key {redact_cache_key(cache_key)} is {len(wrapped)} bytes, exceeding "
                f"max_value_size ({max_value_size} bytes); refusing to cache. Increase "
                f"CACHEKIT_MAX_VALUE_SIZE to cache larger values."
            )
        return wrapped

    def _is_cachekit_arrow(self) -> bool:
        """True iff the base serializer is exactly cachekit's ArrowSerializer, by object, not frame tag.

        ``serializer="arrow"`` and an ``ArrowSerializer(...)`` instance both qualify; the instance's
        frame tag is its class name, which is why the old ``== "arrow"`` test missed it. Exact type:
        a subclass may override ``serialize`` and stays on the buffered paths. Read through
        ``sys.modules`` so a handler that never loaded the module never imports pyarrow here.
        """
        if self._serializer_string_name not in _ARROW_FRAME_TAGS:
            return False
        module = sys.modules.get("cachekit.serializers.arrow_serializer")
        return module is not None and type(self._base_serializer) is module.ArrowSerializer

    def _frame_prefix(self, metadata: dict[str, Any]) -> bytes:
        """The CK frame prefix for a plaintext payload with ``metadata``.

        Uncompressed cachekit Arrow gets its header padded so the Arrow IPC body starts on a
        64-byte boundary of a File-backend cache file (File header + prefix + 8-byte checksum):
        polars copies every misaligned numeric column, and pyarrow prefers aligned buffers.
        Compressed, encrypted and every other payload are left unpadded. The prefix depends only on
        the serializer configuration, so other backends carry the same padding (and an unaligned body).
        """
        if not self.encryption and self._is_cachekit_arrow() and not metadata.get("compressed"):
            return SerializationWrapper.wrap_prefix(
                metadata, self._serializer_string_name, align=_ARROW_IPC_ALIGN, offset=FILE_HEADER_SIZE + _ARROW_CHECKSUM_SIZE
            )
        return SerializationWrapper.wrap_prefix(metadata, self._serializer_string_name)

    def supports_mmap_read(self) -> bool:
        """True iff reads can use the mmap fast path (#171).

        Eligible only for PLAINTEXT cachekit Arrow (``serializer="arrow"`` or an
        ``ArrowSerializer(...)`` instance): encrypted values can never mmap (AES-GCM decrypt owns
        its buffer), and other serializers copy at their Rust/C boundary anyway. Every return_format
        is eligible on Python 3.13+, pandas only before it (see below). A result may keep the mapping
        alive past the read; ``_MmapHandle`` states that lifetime contract.

        The backend must also support buffer reads (File/POSIX); that is checked separately, so a
        True here on a non-File backend simply means get_buffer returns None and we fall back.
        """
        # The tag screen first: it keeps every other serializer's read free of the helper call.
        if self.encryption or self._serializer_string_name not in _ARROW_FRAME_TAGS or not self._is_cachekit_arrow():
            return False
        # An Arrow or polars result keeps the mapping, and before Python 3.13 each mapping also keeps a
        # file descriptor, so those return formats map only where the mapping holds none. A pandas
        # result is meant to copy out; any pandas column that stays a view of the mapped Arrow
        # buffers still keeps the mapping (and, before 3.13, its descriptor) until it is released.
        return MMAP_RELEASES_FD or getattr(self._base_serializer, "return_format", None) == "pandas"

    def supports_streaming_write(self) -> bool:
        """True iff writes can stream the envelope to a capable backend.

        Eligible only for PLAINTEXT cachekit Arrow (``serializer="arrow"`` or an
        ``ArrowSerializer(...)`` instance):
        - encrypted values can never stream (AES-256-GCM emits its auth tag only after the
          whole ciphertext exists, so the secure path stays on buffered ``set(bytes)``);
        - interop/v1 stores raw documents with no CK envelope (different serializer, no benefit);
        - non-Arrow serializers materialize their output in one shot anyway.

        The backend must also support streaming writes (File only today); that is checked
        separately at the cache-handler layer, so True here on a non-streaming backend simply
        means the write falls back to the buffered path.
        """
        return (
            not self.encryption
            and not self.interop_mode
            and self._serializer_string_name in _ARROW_FRAME_TAGS
            and self._is_cachekit_arrow()
        )

    def write_serialized_to(self, sink: BinaryIO, data: Any) -> None:
        """Streaming twin of :meth:`serialize_data`: write the SAME envelope bytes into
        ``sink`` without ever materializing the payload in memory.

        Only valid when :meth:`supports_streaming_write` is True (plaintext Arrow — no
        encryption wrapper, so no tenant extraction / AAD / cache_key binding applies). The
        envelope is byte-identical to a buffered ``serialize_data`` of the same value: frame
        prefix first (metadata is deterministic per serializer config), then the Arrow
        serializer streams ``[checksum][IPC record batches]`` directly into the sink.

        The L2 oversized-entry ceiling (``max_value_size``, issue #163) is enforced
        incrementally mid-stream instead of on the finished blob — same limit, same ValueError,
        but an over-limit stream aborts early instead of materializing first.

        Raises:
            TypeError: If data is not a DataFrame or dict of arrays
            ValueError: If the envelope would exceed max_value_size
            SerializationError: If Arrow serialization fails
        """
        from cachekit.serializers.arrow_serializer import ArrowSerializer  # already loaded on the arrow path

        serializer = self._base_serializer
        if not isinstance(serializer, ArrowSerializer):
            # supports_streaming_write() gates callers to the arrow serializer; anything else
            # here is a caller bug — fail loud rather than stream a wrong envelope.
            raise SerializationError(f"write_serialized_to requires the arrow serializer, got {type(serializer).__name__}")

        metadata = serializer.serialization_metadata()
        prefix = self._frame_prefix(metadata.to_dict())
        sink.write(prefix)
        budget = get_settings().max_value_size - len(prefix)
        serializer.serialize_to_sink(data, sink, max_bytes=budget)

    def deserialize_l1(
        self,
        data: bytes | VerifiedPayload,
        cache_key: str = "",
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> tuple[Any, VerifiedPayload | None]:
        """:meth:`deserialize_data` for an entry read from L1, plus what L1 should hold in its place, if anything.

        When this handler keeps verified payloads (``keeps_verified_in_l1``), the entry is its own plaintext envelope
        of a kind its serializer keeps, and the payload this decode verified is no longer than the entry, that payload
        (:class:`VerifiedPayload`: the MessagePack ``ByteStorage.retrieve()`` returned for the default and auto
        serializers, the checksummed JSON for orjson), for the caller to swap in (``L1Cache.shrink_if_held``) at no
        second decode: later hits skip the integrity check (retrieve()'s decompress and checksum, or orjson's
        checksum). Everything else gets None: a kept entry decodes that way, a longer payload would grow the entry
        so L1 keeps the envelope, and any other entry goes to deserialize_data, which refuses another handler's kept
        entry. Every decode, kept or not, runs its serializer's bounded decode. Raises as deserialize_data.

        Examples:
            >>> handler = CacheSerializationHandler()
            >>> envelope = handler.serialize_data("hello", cache_key="greeting")
            >>> value, entry = handler.deserialize_l1(envelope, cache_key="greeting")
            >>> value, type(entry).__name__, len(entry) <= len(envelope)
            ('hello', 'VerifiedPayload', True)
            >>> handler.deserialize_l1(entry, cache_key="greeting")  # already verified MessagePack
            ('hello', None)
            >>> compressible = handler.serialize_data("ab" * 600, cache_key="k")
            >>> handler.deserialize_l1(compressible, cache_key="k")[1] is None  # its MessagePack is longer: L1 keeps the envelope
            True
        """
        # Inline, not helpers: this is every L1 hit's decode, and an envelope it keeps must cost what
        # deserialize_data costs.
        if not self.keeps_verified_in_l1:
            return self.deserialize_data(data, cache_key, args, kwargs), None
        serializer = self._base_serializer
        # Only L1 holds this type, and only a keeping handler swapped it in, after a decode that accepted it. Its
        # owner decodes it; another serializer's (a key shared across serializers) goes to deserialize_data, which
        # refuses it.
        if type(data) is VerifiedPayload:
            if data.owner is not type(serializer):
                return self.deserialize_data(data, cache_key, args, kwargs), None
            return serializer._deserialize_verified(data.data), None  # type: ignore[union-attr]
        try:
            payload, metadata, serializer_name = SerializationWrapper.unwrap_metadata(data)
        except (AttributeError, KeyError, TypeError, ValueError):
            return self.deserialize_data(data, cache_key, args, kwargs), None  # a corrupt frame: it raises
        # Only this handler's own plaintext envelope. Anything else, such as a config-drift entry whose ciphertext
        # must stay in L1, is decoded by deserialize_data, not kept; so is an entry the serializer does not keep
        # (_deserialize_keeping returns None), such as one whose header misdescribes what this handler writes.
        if serializer_name != self._serializer_string_name or metadata.encrypted:
            return self.deserialize_data(data, cache_key, args, kwargs), None
        keeping = serializer._deserialize_keeping(payload, metadata)  # type: ignore[union-attr]
        if keeping is None:
            return self.deserialize_data(data, cache_key, args, kwargs), None
        value, kept = keeping
        return value, kept if len(kept) <= len(data) else None

    def deserialize_data(
        self,
        data: str | bytes | memoryview | VerifiedPayload,
        cache_key: str = "",
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        """Deserialize data from cache storage with cache_key verification.

        Args:
            data: Serialized data from cache (may be encrypted)
            cache_key: Cache key for AAD verification (SECURITY CRITICAL for encrypted data).
                      Required when data is encrypted to verify ciphertext binding.
                      The AAD binds it behind the backend's key_prefix (see _aad_key).
            args: Positional arguments from cached function (for tenant extraction)
            kwargs: Keyword arguments from cached function (for tenant extraction)

        Returns:
            Deserialized Python object

        Raises:
            ValueError: If cache_key is empty when data is encrypted
            TenantResolutionError: If the handler has a tenant_extractor and the caller's
                tenant cannot be extracted for an encrypted entry. Nothing is decrypted;
                callers treat this as a miss that keeps the entry.
            SerializationError: If deserialization fails (including AAD mismatch or a
                corrupt/unparseable envelope frame header), or if this handler has
                encryption enabled and the entry's header claims plaintext — the
                header is unauthenticated, so an encryption-enabled handler never
                routes to the plaintext deserializer (fail closed, CWE-757 downgrade
                protection). Callers treat this as a cache miss.
            DecryptionAuthenticationError: A SerializationError subclass, raised on any
                authentication failure: tampered ciphertext, wrong key, AAD mismatch, or
                (TenantMismatchError) an entry encrypted for a tenant other than the one this
                read decrypts as (see Note). The read sites re-raise it under fail-closed.

        Note:
            The decryption tenant mirrors :meth:`serialize_data`:
            - With a tenant_extractor: the caller's tenant, extracted from args/kwargs, on
              every encrypted read, config-drift reads included. The cache key carries no
              tenant, so an entry another tenant wrote at the same key is refused as a
              tenant mismatch, never decrypted.
            - Without one (single-tenant mode, and drift reads by an extractor-less handler):
              the tenant recorded in the entry's header. It is bound into the AAD, so a
              forged value fails authentication.

        Examples:
            Basic round-trip (serialize then deserialize):

            >>> handler = CacheSerializationHandler()
            >>> original = {"name": "Bob", "age": 30, "active": True}
            >>> cache_key = "user:bob"
            >>> serialized = handler.serialize_data(original, cache_key=cache_key)
            >>> handler.deserialize_data(serialized, cache_key=cache_key)
            {'name': 'Bob', 'age': 30, 'active': True}

            Handles nested structures:

            >>> handler = CacheSerializationHandler()
            >>> nested = {"users": [{"id": 1}, {"id": 2}], "meta": {"count": 2}}
            >>> serialized = handler.serialize_data(nested, cache_key="users:all")
            >>> result = handler.deserialize_data(serialized, cache_key="users:all")
            >>> result["users"][0]["id"]
            1
            >>> result["meta"]["count"]
            2

            Preserves None and boolean types:

            >>> handler = CacheSerializationHandler()
            >>> data = {"value": None, "flag": False, "count": 0}
            >>> serialized = handler.serialize_data(data, cache_key="types:test")
            >>> result = handler.deserialize_data(serialized, cache_key="types:test")
            >>> result["value"] is None
            True
            >>> result["flag"] is False
            True
        """
        if self.interop_mode:
            # A VerifiedPayload here is another handler's swapped L1 entry: the bytes-only decode refuses it.
            return self._deserialize_interop(data, cache_key)  # pyright: ignore[reportArgumentType]

        try:
            # unwrap/from_dict raise bare ValueError subclasses on frame faults. The
            # `except ValueError: raise` arm below exists only for the missing-cache_key
            # fail-closed check, so convert here or a corrupt header bypasses the L2
            # `except SerializationError` eviction and survives to TTL.
            try:
                # A custom SerializerProtocol may write to the metadata it is handed, so it gets its own
                # copy rather than the header memo's shared, read-only instance.
                serialized_data, metadata, serializer_name = SerializationWrapper.unwrap_metadata(
                    data,  # pyright: ignore[reportArgumentType]  # a VerifiedPayload raises here and is refused below
                    shared=not self._custom_serializer,
                )
            except (AttributeError, KeyError, TypeError, ValueError) as e:
                if self.encryption and type(data) is VerifiedPayload:
                    # A plaintext handler's swapped L1 entry under this key: refused as the plaintext
                    # envelope it replaced would be, and counted the same way (suspicious_envelope).
                    raise SuspiciousCacheEntryError(
                        "Encryption is enabled but the L1 entry is another handler's plaintext entry. Refusing it (fail closed)."
                    ) from e
                raise SerializationError(f"Corrupt cache envelope: {bounded_error(e)}") from e

            # Get base serializer
            base_serializer = self._base_serializer

            # Validate serializer compatibility - cached data must match decorator's serializer
            # This prevents deserialization errors when switching serializers
            if serializer_name != self._serializer_string_name:
                raise SerializationError(
                    f"Serializer mismatch: cached data uses '{serializer_name}', "
                    f"but decorator configured with '{self._serializer_string_name}'. "
                    f"Cache entry is incompatible. Either flush cache or use cache key namespacing "
                    f"for gradual migrations: @cache(namespace='v2-{self._serializer_string_name}')"
                )

            # SECURITY (CWE-757): the CK frame header is plaintext and NOT
            # covered by the AES-GCM tag (AAD v0x03 binds tenant/cache_key/format/
            # compressed/original_type — not the header itself). A backend-write attacker
            # can plant a frame claiming `encrypted: false` with an arbitrary plaintext payload;
            # an encryption-enabled reader must never let that header downgrade it to
            # the unauthenticated plaintext path. Fail closed — callers treat
            # SerializationError as a miss and evict, so legacy plaintext entries
            # written before encryption was enabled are recomputed and re-stored
            # encrypted rather than silently accepted
            # (see docs/features/zero-knowledge-encryption.md, "Fail-Closed Read Path").
            if self.encryption and not metadata.encrypted:
                # SuspiciousCacheEntryError → telemetry reason "suspicious_envelope":
                # legitimate during lazy plaintext→encrypted migration, an attack
                # signature otherwise. Behavior stays miss+evict either way.
                raise SuspiciousCacheEntryError(
                    "Encryption is enabled but the cache entry's header claims plaintext. "
                    "Refusing the unauthenticated plaintext read path (fail closed): the "
                    "header is not covered by the AES-GCM tag and may be forged. If this "
                    "entry predates enabling encryption, it will be recomputed and "
                    "re-stored encrypted on the next access; see "
                    "docs/features/zero-knowledge-encryption.md for eager-migration guidance."
                )

            # Determine serializer based on whether data is encrypted.
            # metadata.encrypted may still be True while self.encryption is False
            # (handler config changed but old encrypted data exists) — decrypting
            # is safe in that direction because it stays on the authenticated path.
            if metadata.encrypted:
                # SECURITY EVENT (cachekit-py#170, config-drift read): this handler has
                # encryption DISABLED yet is being handed an encrypted entry. Legitimate
                # cause: encryption was recently turned off and stale entries remain until
                # TTL expiry. But the same signature appears when an attacker plants an
                # encrypted entry or when configuration has silently drifted, and the
                # decrypt below falls back to the global CACHEKIT_MASTER_KEY — so this must
                # never happen silently. Warn on every occurrence; decryption itself stays
                # on the authenticated (AES-GCM) path, so a forged entry still fails auth.
                if not self.encryption:
                    _record_security_counter("cachekit_config_drift_reads_total", {"reason": "encryption_disabled"})
                    if cache_key not in self._drift_warned_keys and len(self._drift_warned_keys) < 1024:
                        self._drift_warned_keys.add(cache_key)
                        get_logger().warning(
                            f"Config-drift read for {redact_cache_key(cache_key) if cache_key else 'unknown key'}: "
                            f"handler has encryption disabled but the cache entry is encrypted (tenant "
                            f"'{metadata.tenant_id or 'unknown'}'). Decrypting via the globally configured "
                            f"master key. If encryption was not recently disabled for this function, "
                            f"investigate for misconfiguration or cache tampering. Further reads of this "
                            f"key count on cachekit_config_drift_reads_total without logging."
                        )

                # Data is encrypted - use cached EncryptionWrapper for decryption
                if not metadata.tenant_id:
                    # Envelope claims encryption but lacks the tenant it was encrypted for —
                    # malformed or field-stripped. suspicious_envelope telemetry.
                    raise SuspiciousCacheEntryError(
                        "Encrypted cache entry is missing tenant_id in metadata. Cannot decrypt without tenant context."
                    )
                if self.tenant_extractor:
                    # Multi-tenant mode: decrypt as the caller's tenant, extracted as on the write
                    # path, never as the header's. The key has no tenant segment, so another
                    # tenant's entry can sit at this key; the wrapper's tenant check refuses it.
                    # A failed extraction is the caller's fault, not the entry's: every read site
                    # treats TenantResolutionError as a miss that keeps the entry (anything else
                    # would be wrapped below and evicted as corruption).
                    try:
                        tenant_id = self.tenant_extractor.extract(args, kwargs or {})
                    except Exception as e:
                        raise TenantResolutionError(
                            f"Cannot resolve the caller's tenant for an encrypted read: {type(e).__name__}"
                        ) from e
                else:
                    # No tenant_extractor (single-tenant mode, or a drift read by an extractor-less
                    # handler): the header's tenant selects the key. It is bound into the AAD, so a
                    # forged value fails authentication.
                    tenant_id = metadata.tenant_id
                try:
                    serializer = self._get_cached_encryption_wrapper(tenant_id)
                    # EncryptionWrapper.deserialize() requires cache_key for AAD v0x03 verification.
                    # One AAD, no retry with another key form (spec/encryption.md).
                    return serializer.deserialize(serialized_data, metadata, self._aad_key(cache_key))
                except KeyringConfigurationError as e:
                    if self.encryption:
                        raise
                    # Drift read: only the unauthenticated header's encrypted flag brought
                    # us here, so a keyring fault must heal as miss + evict (corruption),
                    # never fail loud — a raise would let a planted frame block recompute
                    # and overwrite of its key until TTL.
                    raise EncryptionError(f"Config-drift read hit a keyring fault: {e}") from e
            else:
                # Data is not encrypted - use base serializer directly (no cache_key needed)
                return base_serializer.deserialize(serialized_data, metadata)
        except (ValueError, SerializationError):
            # Re-raised unwrapped for the read sites to classify: only
            # KeyringConfigurationError fails loud there; a plain ValueError is a miss,
            # and SerializationError goes through handle_decrypt_failure.
            raise
        except Exception as e:
            get_logger().error(f"Deserialization failed with {self.serializer_name}: {redact_error_for_log(e)}")
            raise SerializationError(f"Failed to deserialize data with {self.serializer_name}: {bounded_error(e)}") from e

    def _deserialize_interop(self, data: str | bytes | memoryview, cache_key: str) -> Any:
        """Interop/v1 read path: config decides encryption, never the stored bytes.

        Interop entries carry no metadata header, so there is nothing to sniff
        and nothing to forge: with encryption enabled the bytes are ALWAYS
        treated as nonce||ciphertext||tag and authenticated before any decode
        (fail closed — same CWE-757 posture as the auto-mode fix);
        without encryption they are decoded as one plain MessagePack document.
        Decode/auth failures raise SerializationError, which callers treat as
        a miss and evict (self-healing overwrite).
        """
        if isinstance(data, str):
            raise SerializationError("interop cache entries are binary; got str from backend")
        try:
            if self.encryption:
                if self._single_tenant_id is None:
                    # Our own config is broken, not the entry: fail loud. A SerializationError
                    # would read as corruption and miss + evict a valid shared entry.
                    raise KeyringConfigurationError("interop encryption requires single-tenant mode (tenant_id missing)")
                tenant_id = self._single_tenant_id
                wrapper = self._get_cached_encryption_wrapper(tenant_id)
                # Synthesize the metadata the wrapper needs: interop AAD is pinned
                # to format=msgpack, compressed=False, NO original_type (exactly
                # four AAD components). tenant comes from config — an attacker
                # cannot influence it because nothing is read from the stored
                # bytes except the ciphertext itself. Interop entries carry no
                # per-entry key fingerprint, so keyring rotation uses sequential
                # attempts (current key first, identical AAD per attempt) via
                # deserialize_without_key_identity — the spec's "Decrypt —
                # without per-entry key identity" row — instead of the
                # fingerprint selection CK-frame entries get.
                metadata = SerializationMetadata(
                    serialization_format=SerializationFormat.MSGPACK,
                    compressed=False,
                    original_type=None,
                    encrypted=True,
                    tenant_id=tenant_id,
                    encryption_algorithm="AES-256-GCM",
                )
                return wrapper.deserialize_without_key_identity(data, metadata, cache_key)
            return self._base_serializer.deserialize(data)
        except (ValueError, SerializationError):
            # Same contract as deserialize_data: only KeyringConfigurationError
            # fails loud at the read sites; a plain ValueError there is a miss.
            raise
        except Exception as e:
            get_logger().error(f"Interop deserialization failed for {redact_cache_key(cache_key)}: {redact_error_for_log(e)}")
            raise SerializationError(f"Failed to deserialize interop cache entry: {bounded_error(e)}") from e


class CacheHit(NamedTuple):
    """A successful L2 read from :class:`CacheOperationHandler`.

    envelope is the serialized bytes so the decorator can backfill L1 without
    re-serializing (re-encrypting) — None on the mmap fast path (sync reads
    only), whose handle is confined to its frame and whose bytes never reach
    L1 (#171); the value itself may be an Arrow or polars result
    over the mapping (see ``_MmapHandle``). size_bytes is the envelope's byte length on every hit, mmap
    included, so payload-size stats never depend on holding the bytes.
    A hit is distinguished from "no cache entry" by getting a CacheHit at
    all vs. None — there is no boolean slot.
    """

    value: Any
    envelope: Optional[bytes]
    size_bytes: int


class StoreOutcome(NamedTuple):
    """The result of :meth:`CacheOperationHandler.store_result`.

    envelope is the serialized bytes when the buffered path produced them (eligible for L1
    backfill) and None otherwise — a streamed value never reaches L1, and a failed
    serialization has no bytes. stored says whether the backend write succeeded, which the
    envelope cannot say: a buffered write whose backend set failed still has an envelope, and
    a successful stream has none. Key tracking follows stored, L1 follows envelope. deferred says the
    backend write was handed to a write-behind writer (stored is then False: the writer tracks it).
    tracked says the key-registry write rode with the backend write (``set_tracked``), its failure
    already reported: the caller must not track the key again. l1_until is the instant the
    caller's L1 copy must stop serving by (``_written_copy_expiry``; None keeps the stated ttl).
    """

    envelope: Optional[bytes]
    stored: bool
    deferred: bool = False
    tracked: bool = False
    l1_until: Optional[float] = None


class CacheOperationHandler:
    """Handles core cache operations - Single Responsibility.

    Orchestrates cache key generation, serialization, and backend storage.
    Follows the Strategy pattern for backend abstraction.

    Examples:
        Create handler with dependencies:

        >>> from cachekit.key_generator import CacheKeyGenerator
        >>> serialization = CacheSerializationHandler()
        >>> key_gen = CacheKeyGenerator()
        >>> handler = CacheOperationHandler(serialization, key_gen)

        Generate cache keys for function calls (format: ns:{namespace}:func:{module}.{name}:args:{hash}:{flags}):

        >>> def get_user(user_id: int): pass
        >>> handler.get_cache_key(get_user, (123,), {}, namespace="users")  # doctest: +ELLIPSIS
        'ns:users:func:...'

        Cache key includes function arguments:

        >>> def search(query: str, limit: int): pass
        >>> key1 = handler.get_cache_key(search, ("hello",), {"limit": 10}, namespace=None)
        >>> key2 = handler.get_cache_key(search, ("world",), {"limit": 10}, namespace=None)
        >>> key1 != key2  # Different args = different keys
        True

        Same args produce same key (deterministic):

        >>> key3 = handler.get_cache_key(search, ("hello",), {"limit": 10}, namespace=None)
        >>> key1 == key3
        True
    """

    def __init__(
        self,
        serialization_handler: CacheSerializationHandler,
        key_generator: CacheKeyGenerator,
        cache_handler: Optional[CacheHandlerStrategy] = None,
        on_deserialize_error: Optional[Callable[[Exception, str], None]] = None,
    ):
        """Initialize with dependencies.

        Args:
            on_deserialize_error: Optional hook invoked as (error, cache_key) when an
                L2 read hits a corrupt/tampered entry (SerializationError). The decorator
                wires this to its metrics pipeline so the cache_get_deserialize signal
                fires from one place for both sync and async paths (#159). Best-effort:
                hook failures are logged and never mask the miss/recompute.
        """
        self.serialization_handler = serialization_handler
        self.key_generator = key_generator
        self._cache_handler = cache_handler
        self.on_deserialize_error = on_deserialize_error

    def _notify_deserialize_error(self, error: Exception, cache_key: str) -> None:
        """Report a corrupt L2 entry to the observability hook (best-effort)."""
        if self.on_deserialize_error is None:
            return
        try:
            self.on_deserialize_error(error, cache_key)
        except Exception as hook_err:  # observability must never break the miss path
            get_logger().warning(
                f"on_deserialize_error hook failed for {redact_cache_key(cache_key)}: {redact_error_for_log(hook_err)}"
            )

    def get_cache_key(
        self,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        namespace: str | None,
        integrity_checking: bool = True,
    ) -> str:
        """Generate cache key for function call.

        Args:
            func: Function being cached
            args: Positional arguments
            kwargs: Keyword arguments
            namespace: Optional namespace prefix
            integrity_checking: Whether integrity checking is enabled (affects cache key)

        Returns:
            Cache key string

        Examples:
            Basic key generation (format: func:{module}.{name}:args:{hash}:{flags}):

            >>> from cachekit.key_generator import CacheKeyGenerator
            >>> handler = CacheOperationHandler(CacheSerializationHandler(), CacheKeyGenerator())
            >>> def my_func(x): return x * 2
            >>> key = handler.get_cache_key(my_func, (42,), {}, namespace=None)
            >>> key.startswith("func:")
            True

            Namespace prefixes the key (format: ns:{namespace}:func:...):

            >>> key_ns = handler.get_cache_key(my_func, (42,), {}, namespace="v2")
            >>> key_ns.startswith("ns:v2:")
            True

            _bypass_cache kwarg is filtered out:

            >>> key1 = handler.get_cache_key(my_func, (1,), {"_bypass_cache": True}, None)
            >>> key2 = handler.get_cache_key(my_func, (1,), {}, None)
            >>> key1 == key2  # _bypass_cache doesn't affect key
            True
        """
        return self._generated_key(
            func, args, kwargs, namespace, integrity_checking, self.serialization_handler.serializer_key_name
        )

    # Pre-0.20.0 releases never passed the serializer to generate_key, so every generated
    # key ended in the default code `s` whatever the serializer was. A
    # deployment upgraded from one still holds those entries — and old replicas keep
    # writing them during a rolling deploy — at a key the current code never computes.
    # Invalidating only the current key would let an erasure return normally while the
    # pre-upgrade copy survives to its TTL, or forever at ttl=None, so single-key
    # invalidation also deletes this one. Over-deleting costs a `default`-serializer
    # decorator on the same function and arguments one recompute; under-deleting leaks
    # retained data. Remove only in a major release whose notes declare upgrades from below
    # 0.20.0 unsupported: ttl=None entries never age out, so no TTL clock can retire this.
    def get_legacy_cache_key(
        self,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        namespace: str | None,
        integrity_checking: bool = True,
    ) -> str:
        """The key a pre-0.20.0 release wrote for this call; equal to get_cache_key's on the default serializer.

        Examples:
            >>> from cachekit.key_generator import CacheKeyGenerator
            >>> def my_func(x): return x
            >>> auto = CacheOperationHandler(CacheSerializationHandler("auto"), CacheKeyGenerator())
            >>> auto.get_cache_key(my_func, (1,), {}, None)[-3:], auto.get_legacy_cache_key(my_func, (1,), {}, None)[-3:]
            (':1a', ':1s')
            >>> std = CacheOperationHandler(CacheSerializationHandler(), CacheKeyGenerator())
            >>> std.get_legacy_cache_key(my_func, (1,), {}, None) == std.get_cache_key(my_func, (1,), {}, None)
            True
        """
        return self._generated_key(func, args, kwargs, namespace, integrity_checking, "default")

    def _generated_key(
        self,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        namespace: str | None,
        integrity_checking: bool,
        serializer_type: str,
    ) -> str:
        """generate_key minus reserved kwargs: one filter, so a legacy key hashes the same arguments."""
        filtered_kwargs = {k: v for k, v in kwargs.items() if k != "_bypass_cache"}
        return self.key_generator.generate_key(
            func, args, filtered_kwargs, namespace, integrity_checking, serializer_type=serializer_type
        )

    def _handle_l2_read_error(self, e: SerializationError, cache_key: str) -> None:
        """Shared decrypt/integrity failure tail for sync L2 reads.

        Routes through the single policy point (raises DecryptionAuthenticationError
        when fail-closed — poisoned entry retained as evidence), else best-effort
        evicts the poisoned entry and notifies, so the caller treats it as a miss.
        """
        handle_decrypt_failure(e, tier="l2", cache_key=cache_key, fail_closed=self.serialization_handler.encryption_fail_closed)
        try:
            if self._cache_handler is not None:
                self._cache_handler.delete(cache_key)
        except Exception as del_err:  # best-effort eviction; never mask the miss/recompute
            get_logger().warning(
                f"Failed to evict poisoned L2 entry {redact_cache_key(cache_key)}: {redact_error_for_log(del_err)}"
            )
        self._notify_deserialize_error(e, cache_key)

    async def _handle_l2_read_error_async(self, e: SerializationError, cache_key: str) -> None:
        """Async twin of :meth:`_handle_l2_read_error` (delete_async eviction)."""
        handle_decrypt_failure(e, tier="l2", cache_key=cache_key, fail_closed=self.serialization_handler.encryption_fail_closed)
        try:
            if self._cache_handler is not None:
                await self._cache_handler.delete_async(cache_key)
        except Exception as del_err:  # best-effort eviction; never mask the miss/recompute
            get_logger().warning(
                f"Failed to evict poisoned L2 entry {redact_cache_key(cache_key)}: {redact_error_for_log(del_err)}"
            )
        self._notify_deserialize_error(e, cache_key)

    def get_cached_value(
        self,
        cache_key: str,
        refresh_ttl: Optional[int] = None,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> Optional[CacheHit]:
        """Get value from cache if it exists.

        Args:
            cache_key: Cache key to retrieve (also used for AAD verification if encrypted)
            refresh_ttl: Optional TTL to refresh on hit
            args: Function args (for tenant extraction in encryption)
            kwargs: Function kwargs (for tenant extraction in encryption)

        Returns:
            A :class:`CacheHit` on a cache hit, None on cache miss or error. See
            :class:`CacheHit` for the field semantics (mmap fast path, size_bytes).

        Note:
            Requires cache_handler to be set via set_cache_handler() before calling.
            For encrypted data, cache_key is used for AAD v0x03 verification.
        """
        try:
            if self._cache_handler is None:
                raise RuntimeError("Cache handler must be set before calling get_cached_value")

            # mmap fast path (#171): plaintext cachekit Arrow on a buffer-readable backend (File,
            # POSIX) reads zero-copy. The handle is confined to this frame and closed in `finally`,
            # so the mmap never becomes the returned value and never reaches L1. An
            # Arrow or polars result that aliases the mapping keeps it alive past that close (see
            # _MmapHandle). A None from get_buffer (ineligible file, or a non-buffer backend)
            # falls through to bytes.
            if self.serialization_handler.supports_mmap_read():
                handle = self._cache_handler.get_buffer(cache_key)
                if handle is not None:
                    try:
                        get_logger().cache_hit(cache_key, "Backend(mmap)")
                        size_bytes = handle.view.nbytes  # payload length; the view is released in `finally`
                        value = self.serialization_handler.deserialize_data(handle.view, cache_key, args, kwargs)
                        return CacheHit(value, None, size_bytes)
                    finally:
                        handle.close()

            cached_data = self._cache_handler.get(cache_key, refresh_ttl)
            if cached_data is not None:
                get_logger().cache_hit(cache_key, "Backend")
                # Pass cache_key for AAD verification (required for encrypted data)
                deserialized = self.serialization_handler.deserialize_data(cached_data, cache_key, args, kwargs)
                return CacheHit(deserialized, cached_data, len(cached_data))
            return None
        except KeyringConfigurationError:
            # LOCAL keyring config fault (bad tenant_id, bad keyring entry index, or
            # an EncryptionWrapper construction fault) — never a legitimate miss, and not tamper. Re-raised past the broad
            # `except Exception` below, which would otherwise swallow it into
            # `return None`: a silent fail-open miss with no metric and no
            # eviction, even under fail_closed=True. That is precisely the
            # config-vs-crypto failure class. Not routed through
            # handle_decrypt_failure because there is no policy decision to make
            # here — a misconfigured keyring always raises.
            raise
        except SerializationError as e:
            self._handle_l2_read_error(e, cache_key)  # raises when fail-closed
            return None
        except TenantResolutionError:
            get_logger().warning(f"Encrypted read of {redact_cache_key(cache_key)} skipped: caller's tenant unresolved (miss)")
            return None
        except Exception as e:
            get_logger().warning(f"Backend operation failed for get on {redact_cache_key(cache_key)}: {redact_error_for_log(e)}")
            return None

    def get_cached_value_with_freshness(
        self, cache_key: str, args: tuple[Any, ...] = (), kwargs: dict[str, Any] | None = None
    ) -> Optional[tuple[CacheHit, bool, Optional[int]]]:
        """SWR variant of :meth:`get_cached_value`: also reports
        staleness and the server's remaining freshness in seconds.

        Returns ``(CacheHit, is_stale, fresh_for)`` on a hit — the CacheHit
        matches the async variant so the sync decorator backfills L1 without
        re-serializing — None on miss/error. fresh_for is None when
        no signal exists (pre-signal server, non-SWR backend) — the caller
        applies legacy L1 TTL behavior. The mmap fast path is skipped — SWR is
        CachekitIO-only, which is not buffer-readable. Error semantics mirror
        get_cached_value: the decrypt-failure policy point raises
        DecryptionAuthenticationError when fail-closed (poisoned entry retained as
        evidence); fail-open evicts and reads as a miss so the caller recomputes.
        """
        try:
            if self._cache_handler is None:
                raise RuntimeError("Cache handler must be set before calling get_cached_value_with_freshness")

            # Normalised here too: a custom CacheHandlerStrategy built against the
            # v0.18.0 2-tuple signature must degrade to fresh_for=None, not have a
            # strict 3-unpack ValueError swallowed below into a permanent
            # every-hit-is-a-miss cache bypass.
            hit = _normalize_freshness_hit(self._cache_handler.get_with_freshness(cache_key))
            if hit is None:
                return None
            cached_data, is_stale, fresh_for = hit
            get_logger().cache_hit(cache_key, "Backend(stale)" if is_stale else "Backend")
            deserialized = self.serialization_handler.deserialize_data(cached_data, cache_key, args, kwargs)
            return (CacheHit(deserialized, cached_data, len(cached_data)), is_stale, fresh_for)
        except KeyringConfigurationError:
            # LOCAL keyring config fault (bad tenant_id, bad keyring entry index, or
            # an EncryptionWrapper construction fault) — never a legitimate miss, and not tamper. Re-raised past the broad
            # `except Exception` below, which would otherwise swallow it into
            # `return None`: a silent fail-open miss with no metric and no
            # eviction, even under fail_closed=True. That is precisely the
            # config-vs-crypto failure class. Not routed through
            # handle_decrypt_failure because there is no policy decision to make
            # here — a misconfigured keyring always raises.
            raise
        except SerializationError as e:
            self._handle_l2_read_error(e, cache_key)  # raises when fail-closed
            return None
        except TenantResolutionError:
            get_logger().warning(f"Encrypted read of {redact_cache_key(cache_key)} skipped: caller's tenant unresolved (miss)")
            return None
        except Exception as e:
            get_logger().warning(f"Backend operation failed for get on {redact_cache_key(cache_key)}: {redact_error_for_log(e)}")
            return None

    async def get_cached_value_with_freshness_async(
        self, cache_key: str, args: tuple[Any, ...] = (), kwargs: dict[str, Any] | None = None
    ) -> Optional[tuple[CacheHit, bool, Optional[int]]]:
        """Async SWR variant: staleness + remaining freshness +
        the raw envelope for L1 backfill.

        Returns ``(CacheHit, is_stale, fresh_for)`` on a hit — the CacheHit
        matches :meth:`get_cached_value_async` so the async
        decorator backfills L1 without re-serializing; fresh_for (seconds,
        None = no signal) bounds that backfill to the server's remaining
        freshness. None on miss/error; the fail-closed policy
        propagates DecryptionAuthenticationError.
        """
        try:
            if self._cache_handler is None:
                raise RuntimeError("Cache handler must be set before calling get_cached_value_with_freshness_async")

            hit = _normalize_freshness_hit(await self._cache_handler.get_with_freshness_async(cache_key))
            if hit is None:
                return None
            cached_data, is_stale, fresh_for = hit  # same 2-tuple contract as the sync variant above
            get_logger().cache_hit(cache_key, "Backend(stale)" if is_stale else "Backend")
            deserialized = self.serialization_handler.deserialize_data(cached_data, cache_key, args, kwargs)
            return (CacheHit(deserialized, cached_data, len(cached_data)), is_stale, fresh_for)
        except KeyringConfigurationError:
            # LOCAL keyring config fault (bad tenant_id, bad keyring entry index, or
            # an EncryptionWrapper construction fault) — never a legitimate miss, and not tamper. Re-raised past the broad
            # `except Exception` below, which would otherwise swallow it into
            # `return None`: a silent fail-open miss with no metric and no
            # eviction, even under fail_closed=True. That is precisely the
            # config-vs-crypto failure class. Not routed through
            # handle_decrypt_failure because there is no policy decision to make
            # here — a misconfigured keyring always raises.
            raise
        except SerializationError as e:
            await self._handle_l2_read_error_async(e, cache_key)  # raises when fail-closed
            return None
        except TenantResolutionError:
            get_logger().warning(f"Encrypted read of {redact_cache_key(cache_key)} skipped: caller's tenant unresolved (miss)")
            return None
        except Exception as e:
            get_logger().warning(f"Backend operation failed for get on {redact_cache_key(cache_key)}: {redact_error_for_log(e)}")
            return None

    async def get_cached_value_async(
        self,
        cache_key: str,
        refresh_ttl: Optional[int] = None,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> Optional[CacheHit]:
        """Get value from cache if it exists (async version).

        Args:
            cache_key: Cache key to retrieve (also used for AAD verification if encrypted)
            refresh_ttl: Optional TTL to refresh on hit
            args: Function args (for tenant extraction in encryption)
            kwargs: Function kwargs (for tenant extraction in encryption)

        Returns:
            A :class:`CacheHit` on a cache hit, None on cache miss or error. Same
            shape as the sync variant; see :class:`CacheHit`.

        Note:
            Requires cache_handler to be set via set_cache_handler() before calling.
            For encrypted data, cache_key is used for AAD v0x03 verification.
        """
        try:
            if self._cache_handler is None:
                raise RuntimeError("Cache handler must be set before calling get_cached_value_async")

            # NOTE: no mmap fast path here yet. The async decorator routes through this method
            # (#159), but buffer reads are sync-only today; the async mmap read is #171 scope.
            cached_data = await self._cache_handler.get_async(cache_key, refresh_ttl)
            if cached_data is not None:
                get_logger().cache_hit(cache_key, "Backend")
                # Pass cache_key for AAD verification (required for encrypted data)
                deserialized = self.serialization_handler.deserialize_data(cached_data, cache_key, args, kwargs)
                return CacheHit(deserialized, cached_data, len(cached_data))
            return None
        except KeyringConfigurationError:
            # LOCAL keyring config fault (bad tenant_id, bad keyring entry index, or
            # an EncryptionWrapper construction fault) — never a legitimate miss, and not tamper. Re-raised past the broad
            # `except Exception` below, which would otherwise swallow it into
            # `return None`: a silent fail-open miss with no metric and no
            # eviction, even under fail_closed=True. That is precisely the
            # config-vs-crypto failure class. Not routed through
            # handle_decrypt_failure because there is no policy decision to make
            # here — a misconfigured keyring always raises.
            raise
        except SerializationError as e:
            await self._handle_l2_read_error_async(e, cache_key)  # raises when fail-closed
            return None
        except TenantResolutionError:
            get_logger().warning(f"Encrypted read of {redact_cache_key(cache_key)} skipped: caller's tenant unresolved (miss)")
            return None
        except Exception as e:
            get_logger().warning(f"Backend operation failed for get on {redact_cache_key(cache_key)}: {redact_error_for_log(e)}")
            return None

    def store_result(
        self,
        cache_key: str,
        result: Any,
        ttl: int | None,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        stale_ttl: int | None = None,
        defer: Callable[[bytes], bool] | None = None,
        registry_id: str | None = None,
        on_track_error: Callable[[str, Exception], None] = lambda _key, _error: None,
    ) -> StoreOutcome:
        """Store result in backend cache with optional tenant context for encryption.

        Args:
            cache_key: Cache key (also used for AAD binding if encryption enabled)
            result: Result to cache
            ttl: Time-to-live in seconds
            args: Function args (for tenant extraction in encryption)
            kwargs: Function kwargs (for tenant extraction in encryption)
            defer: Write-behind hook. On the buffered path, called with the serialized bytes instead of
                the backend write; True means it took the write (StoreOutcome.deferred), False that it
                refused and the write runs here. Serialization stays here either way, so its errors
                reach the caller as without it; the streaming path never defers.
            registry_id: Key registry to record a stored key in, in the same round trip as the buffered
                write when the handler can (``set_tracked``; StoreOutcome.tracked). None writes only.
            on_track_error: Called with the key and the error when that registry write fails; the write
                still counts as stored.

        Returns:
            StoreOutcome. envelope: serialized bytes when the buffered path produced them
            (eligible for L1 backfill), or None when there is nothing for L1 — the value was
            STREAMED to the backend (L1 intentionally skipped), the streaming attempt
            failed (logged, never retried buffered), or serialization failed. stored: whether
            the backend write succeeded; an envelope alone is NOT a success signal.

        Note:
            Requires cache_handler to be set via set_cache_handler() before calling.
            For encrypted data, cache_key is bound to ciphertext via AAD v0x03.
        """
        try:
            if self._cache_handler is None:
                raise RuntimeError("Cache handler must be set before calling store_result")

            # Streaming write fast path: plaintext Arrow to a streaming-writable
            # backend (File) writes the envelope without ever materializing it. SWR
            # (stale_ttl) stays buffered — no streaming backend signals freshness today.
            # getattr guards custom CacheHandlerStrategy impls that predate set_streaming.
            if stale_ttl is None and self.serialization_handler.supports_streaming_write():
                set_streaming = getattr(self._cache_handler, "set_streaming", None)
                if set_streaming is not None:
                    streamed = set_streaming(
                        cache_key,
                        lambda sink: self.serialization_handler.write_serialized_to(sink, result),
                        ttl,
                    )
                    if streamed is not None:
                        # Streamed (True) or attempt failed and was logged (False — do NOT
                        # retry buffered: that re-materializes the payload this path exists
                        # to avoid). Either way return None: a multi-GB envelope must never
                        # be copied into L1 (mirrors the mmap read path's L1 exclusion, #171).
                        if streamed:
                            get_logger().cache_stored(cache_key, ttl)
                        return StoreOutcome(envelope=None, stored=streamed)
                    # None: backend can't stream — fall through to the buffered path.

            # Pass cache_key for AAD binding (required for encrypted data)
            serialized_data = self.serialization_handler.serialize_data(result, args, kwargs, cache_key)
            if defer is not None and defer(serialized_data):
                return StoreOutcome(envelope=serialized_data, stored=False, deferred=True)
            stored, tracked, l1_until = _set_l2(
                self._cache_handler, cache_key, serialized_data, ttl, stale_ttl, registry_id, on_track_error
            )
            if stored:
                get_logger().cache_stored(cache_key, ttl)

            # The envelope goes to L1 even when the backend write failed (L1 serves it only within the per-entry share)
            return StoreOutcome(envelope=serialized_data, stored=bool(stored), tracked=tracked, l1_until=l1_until)
        except (InteropError, KeyringConfigurationError):
            # Interop/v1 data-model rejection (spec-mandated; matches cachekit-ts), or
            # a LOCAL keyring config fault, which a cold key first hits here, on the
            # write: fail loud, never "computed but silently never cached".
            raise
        except Exception as e:
            get_logger().warning(
                f"Failed to store in backend cache for {redact_cache_key(cache_key)}: {redact_error_for_log(e)}"
            )
            return StoreOutcome(envelope=None, stored=False)

    async def store_result_async(
        self,
        cache_key: str,
        result: Any,
        ttl: int | None,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> StoreOutcome:
        """Store result in backend cache (async version) with optional tenant context for encryption.

        Args:
            cache_key: Cache key (also used for AAD binding if encryption enabled)
            result: Result to cache
            ttl: Time-to-live in seconds
            args: Function args (for tenant extraction in encryption)
            kwargs: Function kwargs (for tenant extraction in encryption)

        Returns:
            StoreOutcome, as for :meth:`store_result`.

        Note:
            Requires cache_handler to be set via set_cache_handler() before calling.
            For encrypted data, cache_key is bound to ciphertext via AAD v0x03.
        """
        try:
            if self._cache_handler is None:
                raise RuntimeError("Cache handler must be set before calling store_result_async")

            # Streaming write fast path — async twin of store_result's gate. The
            # whole stream (pyarrow serialization included) runs in the handler's thread
            # pool, which also moves the blocking serialize OFF the event loop (the buffered
            # path below serializes on the loop thread).
            if self.serialization_handler.supports_streaming_write():
                set_streaming_async = getattr(self._cache_handler, "set_streaming_async", None)
                if set_streaming_async is not None:
                    streamed = await set_streaming_async(
                        cache_key,
                        lambda sink: self.serialization_handler.write_serialized_to(sink, result),
                        ttl,
                    )
                    if streamed is not None:
                        # See store_result: never retry buffered on failure, never hand the
                        # envelope to L1 on success.
                        if streamed:
                            get_logger().cache_stored(cache_key, ttl)
                        return StoreOutcome(envelope=None, stored=streamed)

            # Pass cache_key for AAD binding (required for encrypted data)
            serialized_data = self.serialization_handler.serialize_data(result, args, kwargs, cache_key)
            stored = await self._cache_handler.set_async(cache_key, serialized_data, ttl)
            if stored:
                get_logger().cache_stored(cache_key, ttl)

            return StoreOutcome(envelope=serialized_data, stored=bool(stored))
        except (InteropError, KeyringConfigurationError):
            # As in store_result: fail loud, never "computed but silently never cached".
            raise
        except Exception as e:
            get_logger().warning(
                f"Failed to store in backend cache for {redact_cache_key(cache_key)}: {redact_error_for_log(e)}"
            )
            return StoreOutcome(envelope=None, stored=False)

    def set_cache_handler(self, handler: CacheHandlerStrategy):
        """Set a specific cache handler strategy.

        Args:
            handler: Cache handler implementing CacheHandlerStrategy protocol
        """
        self._cache_handler = handler

    @property
    def cache_handler(self) -> Optional[CacheHandlerStrategy]:
        """Get the current cache handler."""
        return self._cache_handler


@runtime_checkable
class CacheHandlerStrategy(Protocol):
    """Protocol for cache handlers - supports both standard and pipelined operations."""

    def get(self, key: str, refresh_ttl: Optional[int] = None) -> Optional[bytes]:
        """Get value from cache with optional TTL refresh."""
        ...

    def get_buffer(self, key: str) -> Optional[BufferHandle]:
        """Return a zero-copy buffer handle if the backend supports it (#171), else None."""
        ...

    def set(self, key: str, value: Union[str, bytes], ttl: Optional[int] = None, **metadata) -> bool:
        """Set value in cache with TTL and optional metadata."""
        ...

    def set_streaming(self, key: str, write_payload: Callable[[BinaryIO], None], ttl: Optional[int] = None) -> Optional[bool]:
        """Stream a value to a streaming-writable backend; None when unsupported."""
        ...

    async def set_streaming_async(
        self, key: str, write_payload: Callable[[BinaryIO], None], ttl: Optional[int] = None
    ) -> Optional[bool]:
        """Async variant of set_streaming."""
        ...

    def delete(self, key: str) -> bool:
        """Delete key from cache."""
        ...

    async def get_async(self, key: str, refresh_ttl: Optional[int] = None) -> Optional[bytes]:
        """Get value from cache asynchronously."""
        ...

    async def set_async(self, key: str, value: Union[str, bytes], ttl: Optional[int] = None, **metadata) -> bool:
        """Set value in cache asynchronously."""
        ...

    async def delete_async(self, key: str) -> bool:
        """Delete key from cache asynchronously."""
        ...

    def get_with_freshness(self, key: str) -> Optional[tuple[bytes, bool, Optional[int]]]:
        """Get value plus SWR staleness and remaining freshness;
        (bytes, is_stale, fresh_for) or None. fresh_for is None from a pre-signal
        server or a non-SWR backend."""
        ...

    async def get_with_freshness_async(self, key: str) -> Optional[tuple[bytes, bool, Optional[int]]]:
        """Async variant of get_with_freshness."""
        ...


class StandardCacheHandler:
    """Standard cache handler with backend abstraction.

    Note: L1 (in-memory) caching is handled at the decorator wrapper layer.
    This class handles L2 (backend) abstraction for Redis/HTTP/DynamoDB/etc.

    This class implements the CacheHandlerStrategy protocol and provides:
    - Backpressure control for rate limiting
    - TTL refresh for cache warming
    - Graceful error handling with logging

    Examples:
        With a mock backend (for testing):

        >>> class MockBackend:
        ...     def __init__(self):
        ...         self._store = {}
        ...     def get(self, key):
        ...         return self._store.get(key)
        ...     def set(self, key, value, ttl=None):
        ...         self._store[key] = value
        ...     def delete(self, key):
        ...         return self._store.pop(key, None) is not None
        >>> backend = MockBackend()
        >>> handler = StandardCacheHandler(backend)

        Store and retrieve values:

        >>> handler.set("user:123", b'{"name": "Alice"}', ttl=300)
        True
        >>> handler.get("user:123")
        b'{"name": "Alice"}'

        Delete cached values:

        >>> handler.delete("user:123")
        True
        >>> handler.get("user:123") is None
        True

        TTL refresh threshold (default 50%):

        >>> handler = StandardCacheHandler(backend, ttl_refresh_threshold=0.5)
        >>> handler.ttl_refresh_threshold
        0.5
    """

    def __init__(
        self,
        backend: BaseBackend,
        backpressure_controller: BackpressureController | None = None,
        ttl_refresh_threshold: float = 0.5,
    ):
        """Initialize with backend and optional features.

        Args:
            backend: Backend instance (implements BaseBackend protocol)
            backpressure_controller: Optional BackpressureController for request limiting
            ttl_refresh_threshold: Threshold for TTL refresh (0.0-1.0, default 0.5 = 50%)
        """
        self.backend = backend
        self._write_deadlines_reachable: Optional[bool] = None  # see write_deadlines_reachable
        self.backpressure_controller = backpressure_controller
        self.ttl_refresh_threshold = ttl_refresh_threshold

    def _with_backpressure_and_timeout(self, operation, *args, **kwargs):
        """Execute operation under backpressure control.

        Operation timeouts are enforced at the backend connection layer
        (e.g. redis-py socket_timeout), not here.
        """
        if self.backpressure_controller:
            # Apply backpressure control first
            with self.backpressure_controller.acquire():
                return self._with_timeout(operation, *args, **kwargs)
        else:
            # No backpressure control, just apply timeout
            return self._with_timeout(operation, *args, **kwargs)

    def _with_timeout(self, operation, *args, **kwargs):
        """Run the operation.

        Operation timeouts are enforced at the backend connection layer
        (e.g. redis-py socket_timeout), not here.
        """
        return operation(*args, **kwargs)

    async def _maybe_refresh_ttl(self, key: str, refresh_ttl: int) -> None:
        """Refresh TTL on key if backend supports it and threshold is met.

        This implements graceful degradation: silently skips if backend doesn't
        support TTL inspection (TTLInspectableBackend protocol).

        Args:
            key: Cache key to potentially refresh
            refresh_ttl: Target TTL value in seconds

        Note:
            Uses TypeGuard pattern for proper type narrowing. Logs at debug level
            when skipping due to lack of backend support.
        """
        # Check if backend supports TTL inspection (graceful degradation).
        # Warn once (not silently no-op) so the ignored flag is discoverable.
        if not supports_ttl_inspection(self.backend):
            warn_ttl_refresh_unsupported(self.backend)
            return

        # Type checker now knows self.backend is TTLInspectableBackend
        try:
            remaining_ttl = await self.backend.get_ttl(key)
            if remaining_ttl is not None and remaining_ttl < refresh_ttl * self.ttl_refresh_threshold:
                await self.backend.refresh_ttl(key, refresh_ttl)
                get_logger().debug(
                    f"Refreshed TTL for {redact_cache_key(key)}: {refresh_ttl}s "
                    f"(remaining: {remaining_ttl}s, threshold: {self.ttl_refresh_threshold})"
                )
        except Exception as e:
            # Log but don't fail the cache operation
            get_logger().debug(f"Failed to refresh TTL for {redact_cache_key(key)}: {redact_error_for_log(e)}")

    def get(self, key: str, refresh_ttl: Optional[int] = None) -> Optional[bytes]:
        """Get value from cache using backend.

        Args:
            key: Cache key
            refresh_ttl: Optional TTL to refresh on hit

        Returns:
            Bytes value (encrypted or plaintext msgpack) if found, None if miss
        """
        try:
            value = self._with_backpressure_and_timeout(self.backend.get, key)

            # Note: TTL refresh is async, but we're in sync context
            # TTL refresh will be handled in async path (get_async)
            # For sync operations, we skip TTL refresh to avoid blocking

            return value
        except BackendError as e:
            get_logger().error(f"Backend error getting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return None
        except Exception as e:
            get_logger().error(f"Unexpected error getting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return None

    def get_buffer(self, key: str) -> Optional[BufferHandle]:
        """Return a zero-copy buffer handle for key if the backend supports it (#171), else None.

        Mirrors get()'s backpressure/timeout wrapping. Returns None when the backend can't map the
        value (or on any backend error) so the caller transparently falls back to get().
        """
        if not supports_buffer_read(self.backend):
            return None
        try:
            return self._with_backpressure_and_timeout(self.backend.get_buffer, key)
        except BackendError as e:
            get_logger().error(f"Backend error mmapping key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return None
        except Exception as e:
            get_logger().error(f"Unexpected error mmapping key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return None

    def get_with_freshness(self, key: str) -> Optional[tuple[bytes, bool, Optional[int]]]:
        """Get value plus SWR staleness and remaining freshness.

        Returns ``(bytes, is_stale, fresh_for)`` on a hit, or None on miss/error
        (same degradation contract as :meth:`get` — an error reads as a miss and
        the caller takes the synchronous recompute path). Non-SWR backends read
        as ``(bytes, False, None)`` — no freshness signal, legacy L1 behavior.
        """
        if not supports_swr(self.backend):
            value = self.get(key)
            return (value, False, None) if value is not None else None
        try:
            return _normalize_freshness_hit(self._with_backpressure_and_timeout(self.backend.get_with_freshness, key))
        except BackendError as e:
            get_logger().error(f"Backend error getting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return None
        except Exception as e:
            get_logger().error(f"Unexpected error getting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return None

    async def get_with_freshness_async(self, key: str) -> Optional[tuple[bytes, bool, Optional[int]]]:
        """Async variant of :meth:`get_with_freshness` (sync backend call in the thread pool)."""
        if not supports_swr(self.backend):
            value = await self.get_async(key)
            return (value, False, None) if value is not None else None
        try:
            hit = await self._with_backpressure_and_timeout_async(self.backend.get_with_freshness, key)
            if hit is None:
                _note_clean_l2_miss()
            return _normalize_freshness_hit(hit)
        except BackendError as e:
            get_logger().error(f"Backend error getting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return None
        except Exception as e:
            get_logger().error(f"Unexpected error getting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return None

    def set(
        self, key: str, value: Union[str, bytes], ttl: Optional[int] = None, stale_ttl: Optional[int] = None, **metadata
    ) -> bool:
        """Set value in cache using backend.

        Args:
            key: Cache key
            value: Bytes value to store (encrypted or plaintext msgpack)
            ttl: Time-to-live in seconds
            stale_ttl: SWR stale-grace window in seconds past the fresh TTL;
                silently ignored on backends without SWR support.
            **metadata: Additional metadata (ignored, for compatibility)

        Returns:
            True if successfully stored, False otherwise
        """
        # Ensure value is bytes
        if isinstance(value, str):
            value = value.encode("utf-8")

        try:
            if stale_ttl is not None and supports_swr(self.backend):
                self._with_backpressure_and_timeout(self.backend.set, key, value, ttl, stale_ttl)
            else:
                self._with_backpressure_and_timeout(self.backend.set, key, value, ttl)
            return True
        except BackendError as e:
            get_logger().error(f"Backend error setting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return False
        except Exception as e:
            get_logger().error(f"Unexpected error setting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return False

    def set_tracked(
        self,
        key: str,
        value: bytes,
        ttl: Optional[int],
        registry_id: str,
        on_track_error: Callable[[str, Exception], None],
    ) -> Optional[bool]:
        """``set`` plus the key-registry write (``KeyTrackableBackend.track_key``), in one backend round trip.

        Returns:
            None when the backend cannot fuse the two: the caller falls back to ``set``, then its own
            ``track_key``, as for ``set_streaming``. Otherwise what ``set`` returns: whether the value
            was stored. A registry write that fails after a stored value goes to ``on_track_error`` with
            the key, and the value still counts as stored.
        """
        if not _fuses_tracked_write(self.backend):
            return None
        try:
            track_error = self._with_backpressure_and_timeout(self.backend._set_tracked, key, value, ttl, registry_id)  # type: ignore[attr-defined]
        except BackendError as e:
            get_logger().error(f"Backend error setting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return False
        except Exception as e:
            get_logger().error(f"Unexpected error setting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return False
        if track_error is not None:
            on_track_error(key, track_error)
        return True

    def set_streaming(self, key: str, write_payload: Callable[[BinaryIO], None], ttl: Optional[int] = None) -> Optional[bool]:
        """Stream a value to a streaming-writable backend.

        Returns:
            None when the backend doesn't implement BufferWritableBackend — the caller falls
            back to the buffered ``set``. True on success. False when the streaming attempt
            failed (logged, mirroring ``set``'s degradation contract); the caller must NOT
            retry with the buffered path — that would re-materialize the very payload this
            path exists to keep out of memory.
        """
        if not supports_streaming_write(self.backend):
            return None
        try:
            self._with_backpressure_and_timeout(self.backend.set_streaming, key, write_payload, ttl)
            return True
        except BackendError as e:
            get_logger().error(f"Backend error streaming key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return False
        except Exception as e:
            # Producer-side failure (serialization error, max_value_size budget): the backend
            # already discarded its partial write; surface the real cause, not a backend error.
            get_logger().error(f"Streaming serialization failed for key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return False

    async def set_streaming_async(
        self, key: str, write_payload: Callable[[BinaryIO], None], ttl: Optional[int] = None
    ) -> Optional[bool]:
        """Async variant of :meth:`set_streaming`: the whole stream — pyarrow serialization
        included — runs in the thread pool, keeping blocking IPC writes off the event loop."""
        if not supports_streaming_write(self.backend):
            return None
        try:
            await self._with_backpressure_and_timeout_async(self.backend.set_streaming, key, write_payload, ttl)
            return True
        except BackendError as e:
            get_logger().error(f"Backend error streaming key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return False
        except Exception as e:
            get_logger().error(f"Streaming serialization failed for key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return False

    def delete(self, key: str) -> bool:
        """Delete key from cache using backend.

        Args:
            key: Cache key to delete

        Returns:
            True if successfully deleted, False otherwise
        """
        try:
            return self._with_backpressure_and_timeout(self.backend.delete, key)
        except BackendError as e:
            get_logger().error(f"Backend error deleting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return False
        except Exception as e:
            get_logger().error(f"Unexpected error deleting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return False

    async def _with_backpressure_and_timeout_async(self, operation, *args, **kwargs):
        """Execute sync operation in thread pool with backpressure control.

        Runs sync backend operations in a thread pool to avoid blocking the
        event loop, while still applying backpressure control.
        """

        def execute_sync():
            if self.backpressure_controller:
                with self.backpressure_controller.acquire():
                    return operation(*args, **kwargs)
            return operation(*args, **kwargs)

        return await asyncio.to_thread(execute_sync)

    async def get_async(self, key: str, refresh_ttl: Optional[int] = None) -> Optional[bytes]:
        """Get value from cache asynchronously using backend.

        Runs sync backend.get() in a thread pool to avoid blocking the event loop.
        """
        try:
            # Run sync backend operation in thread pool
            value = await self._with_backpressure_and_timeout_async(self.backend.get, key)
            if value is None:
                _note_clean_l2_miss()  # a real miss on a backend that raises on failure (FillLockBackend's contract)

            # Optionally refresh TTL if value exists and refresh_ttl provided
            # Uses graceful degradation (skips if backend doesn't support TTL inspection)
            if value is not None and refresh_ttl is not None:
                await self._maybe_refresh_ttl(key, refresh_ttl)

            return value
        except BackendError as e:
            get_logger().error(f"Backend error getting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return None
        except Exception as e:
            get_logger().error(f"Unexpected error getting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return None

    async def set_async(
        self, key: str, value: Union[str, bytes], ttl: Optional[int] = None, stale_ttl: Optional[int] = None, **metadata
    ) -> bool:
        """Set value in cache asynchronously using backend.

        Runs sync backend.set() in a thread pool to avoid blocking the event loop.
        ``stale_ttl`` opens an SWR stale-grace window; silently ignored
        on backends without SWR support.
        """
        # Ensure value is bytes
        if isinstance(value, str):
            value = value.encode("utf-8")

        try:
            # Run sync backend operation in thread pool
            if stale_ttl is not None and supports_swr(self.backend):
                await self._with_backpressure_and_timeout_async(self.backend.set, key, value, ttl, stale_ttl)
            else:
                await self._with_backpressure_and_timeout_async(self.backend.set, key, value, ttl)
            return True
        except BackendError as e:
            get_logger().error(f"Backend error setting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return False
        except Exception as e:
            get_logger().error(f"Unexpected error setting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return False

    async def set_tracked_async(
        self,
        key: str,
        value: bytes,
        ttl: Optional[int],
        registry_id: str,
        on_track_error: Callable[[str, Exception], None],
    ) -> Optional[bool]:
        """Async variant of :meth:`set_tracked`: the write and its registry entry in one thread-pool hop."""
        if not _fuses_tracked_write(self.backend):
            return None
        try:
            track_error = await self._with_backpressure_and_timeout_async(
                self.backend._set_tracked,  # type: ignore[attr-defined]
                key,
                value,
                ttl,
                registry_id,
            )
        except BackendError as e:
            get_logger().error(f"Backend error setting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return False
        except Exception as e:
            get_logger().error(f"Unexpected error setting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return False
        if track_error is not None:
            on_track_error(key, track_error)
        return True

    async def delete_async(self, key: str) -> bool:
        """Delete key from cache asynchronously using backend.

        Runs sync backend.delete() in a thread pool to avoid blocking the event loop.
        """
        try:
            # Run sync backend operation in thread pool
            return await self._with_backpressure_and_timeout_async(self.backend.delete, key)
        except BackendError as e:
            get_logger().error(f"Backend error deleting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return False
        except Exception as e:
            get_logger().error(f"Unexpected error deleting key {redact_cache_key(key)}: {redact_error_for_log(e)}")
            return False
