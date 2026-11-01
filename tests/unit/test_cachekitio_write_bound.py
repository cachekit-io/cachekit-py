"""The writer's own L1 copy over CachekitIO (protocol spec/saas-api.md#remaining-freshness).

API-103: once the store accepts a PUT, the decorator must not serve its L1 copy of the value after
min(local_ttl, effective_ttl, fresh_for), where fresh_for is the PUT 2xx's X-CacheKit-Fresh-For read with
the GET grammar: absent = no bound (today's behaviour), malformed or 0 = no L1 copy at all. The server
sends a Fresh-For below the stated TTL when it clamped the TTL to the plan's maximum.

API-104: a PATCH /ttl response never creates, refreshes or extends an L1 bound.

Everything runs through the backend's real client on a fake pool (tests/utils/cachekitio_fakes.py), so the
header is parsed exactly as in production. Wall-clock time is a fake clock: the ``time`` module of L1, the
decorator and the backend is swapped for a shim whose ``time()`` the test advances, so the PUT's receipt, the
L1 put and L1 expiry all read one clock.
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import threading
import time
import types
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import pytest
from urllib3 import HTTPResponse

from cachekit import cache
from cachekit import cache_handler as cache_handler_module
from cachekit import l1_cache as l1_module
from cachekit.backends.base import _WRITE_DEADLINES
from cachekit.backends.cachekitio import backend as backend_module
from cachekit.backends.cachekitio.backend import FRESH_FOR_HEADER, FRESHNESS_HEADER, CachekitIOBackend
from cachekit.cache_handler import _REACH_READS, StandardCacheHandler, _reaches_write_deadlines, _set_l2
from cachekit.decorators import wrapper as wrapper_module
from cachekit.decorators import write_behind as wb
from cachekit.l1_cache import DEFAULT_L1_TTL_SECONDS
from tests.utils.cachekitio_fakes import FakeRequest, fake_backend, response

pytestmark = pytest.mark.unit

_PREFIX = "/v1/cache/"
_GUARD_SECONDS = 10.0  # hang guard only: hitting it fails the test, it never decides an outcome


@dataclass
class _FakeSaaS:
    """Stores PUT bodies; answers each PUT 2xx with ``put_fresh_for`` (None omits the header)."""

    put_fresh_for: str | None = None
    fail_writes: bool = False
    get_fresh_for: str | None = None
    stale: bool = False  # label every hit stale, as in the entry's stale window
    store: dict[str, bytes] = field(default_factory=dict)
    ops: list[str] = field(default_factory=list)
    patched: threading.Event = field(default_factory=threading.Event)

    def respond(self, request: FakeRequest) -> HTTPResponse:
        encoded, _, suffix = request.path[len(_PREFIX) :].partition("/")
        key = unquote(encoded)
        op = f"{request.method} /{suffix}" if suffix else request.method
        self.ops.append(op)
        if op == "PUT" and self.fail_writes:
            return response(500)
        if op == "PUT":
            assert request.body is not None
            self.store[key] = request.body
            return response(200, headers=None if self.put_fresh_for is None else {FRESH_FOR_HEADER: self.put_fresh_for})
        if op == "GET":
            if key not in self.store:
                return response(404)
            if self.stale:
                return response(200, self.store[key], headers={FRESHNESS_HEADER: "stale", FRESH_FOR_HEADER: "0"})
            headers = None if self.get_fresh_for is None else {FRESH_FOR_HEADER: self.get_fresh_for}
            return response(200, self.store[key], headers=headers)
        if op == "GET /ttl":
            return response(200, json={"ttl": 1})  # under any refresh threshold: a refresh is due
        if op == "POST /lock":
            return response(200, json={"lock_id": "lock-1"})
        if op == "PATCH /ttl":
            self.patched.set()
            return response(200, json={"success": True})
        return response(200)

    def gets(self) -> int:
        return self.ops.count("GET")


class _Clock:
    """The wall clock: ``now`` is what L1, the decorator and the backend read as ``time.time()``."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    shim = types.SimpleNamespace(**{name: getattr(time, name) for name in dir(time) if not name.startswith("_")})
    shim.time = lambda: clock.now
    for module in (l1_module, wrapper_module, backend_module):
        monkeypatch.setattr(module, "time", shim)
    return clock


@pytest.fixture
def saas() -> _FakeSaaS:
    return _FakeSaaS()


@pytest.fixture
def backend(saas: _FakeSaaS) -> CachekitIOBackend:
    backend, _ = fake_backend(saas.respond)
    return backend


_namespaces = iter(range(1_000_000))


def _namespace() -> str:
    """A namespace no other test shares: L1 caches are per-namespace and process-wide."""
    return f"write-bound-{next(_namespaces)}-{time.monotonic_ns()}"


def _reported(backend: CachekitIOBackend) -> list[tuple[float, float]]:
    """What one ``backend.set`` reports through _WRITE_DEADLINES."""
    seen: list[tuple[float, float]] = []
    token = _WRITE_DEADLINES.set(seen)
    try:
        backend.set("k", b"v", ttl=60)
    finally:
        _WRITE_DEADLINES.reset(token)
    return seen


def _write(handler: Any, ttl: int | None = 60) -> tuple[bool, bool, float | None]:
    """One decorator L2 write through ``handler``: (stored, tracked, l1_until)."""
    return _set_l2(handler, "k", b"v", ttl, None, None, lambda *_: None)


class TestBackendReportsPutFreshFor:
    """``set`` reports the PUT 2xx's receipt and its header, read with the GET grammar (API-32/33)."""

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            (None, None),  # absent: the stated TTL was applied
            ("5", 5),
            ("0", 0),
            ("2592000", 2592000),  # the header's own 30-day cap, below the 90-day TTL range
            ("2592001", 0),  # over the cap: malformed
            ("7776000", 0),  # the TTL range maximum is not a valid Fresh-For
            ("+5", 0),
            ("", 0),
            ("00000005", 0),  # eight digits: the length check runs first
        ],
    )
    def test_header_grammar(
        self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock, header: str | None, expected: int | None
    ) -> None:
        saas.put_fresh_for = header
        assert _reported(backend) == [(clock.now, math.inf if expected is None else clock.now + expected)]
        assert saas.ops == ["PUT"]

    def test_timed_from_receipt(
        self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deadline is fixed when the 2xx arrives: time spent after it, parsing included, does not move it."""
        parse = CachekitIOBackend._fresh_for

        def slow_parse(response: Any) -> int | None:
            clock.advance(3)
            return parse(response)

        monkeypatch.setattr(CachekitIOBackend, "_fresh_for", staticmethod(slow_parse))
        saas.put_fresh_for = "5"
        received = clock.now
        assert _reported(backend) == [(received, received + 5)]

    def test_set_keeps_its_none_return(self, saas: _FakeSaaS, backend: CachekitIOBackend) -> None:
        saas.put_fresh_for = "5"
        assert backend.set("k", b"v", ttl=60) is None  # type: ignore[func-returns-value]


class TestWriteBound:
    """``_set_l2`` turns what the backend reported into the instant the writer's L1 copy stops serving."""

    def test_header_below_ttl(self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock) -> None:
        saas.put_fresh_for = "5"
        assert _write(StandardCacheHandler(backend)) == (True, False, clock.now + 5)
        assert _WRITE_DEADLINES.get() is None  # closed what it opened

    def test_absent_header_bounds_the_stated_ttl_from_receipt(
        self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock
    ) -> None:
        assert _write(StandardCacheHandler(backend)) == (True, False, clock.now + 60)

    def test_no_ttl_uses_the_l1_default(self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock) -> None:
        assert _write(StandardCacheHandler(backend), ttl=None) == (True, False, clock.now + DEFAULT_L1_TTL_SECONDS)

    async def test_async_write_shares_the_list(self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock) -> None:
        """``set_async`` runs the write on a worker thread, in a copy of the context: the list still reaches it."""
        saas.put_fresh_for = "7"
        seen: list[tuple[float, float]] = []
        token = _WRITE_DEADLINES.set(seen)
        try:
            assert await StandardCacheHandler(backend).set_async("k", b"v", ttl=60) is True
        finally:
            _WRITE_DEADLINES.reset(token)
        assert seen == [(clock.now, clock.now + 7)]

    def test_failed_write_reports_no_bound(self, saas: _FakeSaaS, backend: CachekitIOBackend) -> None:
        saas.fail_writes = True
        assert _write(StandardCacheHandler(backend)) == (False, False, None)

    def test_other_backends_report_none(self) -> None:
        written: list[tuple[Any, ...]] = []

        class _Plain:
            def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
                written.append((key, value, ttl))

        assert _write(StandardCacheHandler(_Plain())) == (True, False, None)  # type: ignore[arg-type]
        assert written == [("k", b"v", 60)]

    def test_set_override_still_runs(self, saas: _FakeSaaS, clock: _Clock) -> None:
        """A subclass of ``set`` keeps running on every write, and one that delegates still reports its
        write's bound: the base ``set`` reports it, not a private variant the override would bypass."""
        seen: list[str] = []

        class _Recording(CachekitIOBackend):
            def set(self, key: str, value: bytes, ttl: int | None = None, stale_ttl: int | None = None) -> None:
                seen.append(key)
                super().set(key, value, ttl, stale_ttl)

        backend, _ = fake_backend(saas.respond)
        backend.__class__ = _Recording
        saas.put_fresh_for = "5"
        assert _write(StandardCacheHandler(backend)) == (True, False, clock.now + 5)
        assert seen == ["k"]

    def test_override_that_never_reports_keeps_no_copy(self, saas: _FakeSaaS) -> None:
        """A CachekitIO ``set`` override that writes without reaching the base ``set`` reports nothing: its
        2xx's bound is unknown, so the write keeps no L1 copy (0.0) rather than assume the stated TTL."""

        class _Bypassing(CachekitIOBackend):
            def set(self, key: str, value: bytes, ttl: int | None = None, stale_ttl: int | None = None) -> None:
                self._request_sync("PUT", self._encode_key(key), body=value, headers=self._set_headers(ttl, stale_ttl))

        backend, _ = fake_backend(saas.respond)
        backend.__class__ = _Bypassing
        saas.put_fresh_for = "5"
        assert _write(StandardCacheHandler(backend)) == (True, False, 0.0)
        assert saas.ops == ["PUT"]

    def test_nested_write_restores_the_outer_list(self, saas: _FakeSaaS, clock: _Clock) -> None:
        """A write made inside another backend's ``set`` reports into its own list, and the outer write's
        list is back in place for the outer 2xx."""
        inner_saas = _FakeSaaS(put_fresh_for="2592000")
        inner, _ = fake_backend(inner_saas.respond)
        nested: list[tuple[bool, bool, float | None]] = []

        class _Nested(CachekitIOBackend):
            def set(self, key: str, value: bytes, ttl: int | None = None, stale_ttl: int | None = None) -> None:
                nested.append(_write(StandardCacheHandler(inner)))
                super().set(key, value, ttl, stale_ttl)

        outer, _ = fake_backend(saas.respond)
        outer.__class__ = _Nested
        saas.put_fresh_for = "0"
        assert _write(StandardCacheHandler(outer)) == (True, False, clock.now)
        assert nested == [(True, False, clock.now + 60)]
        assert _WRITE_DEADLINES.get() is None

    def test_reachability_is_decided_once_per_handler(self, backend: CachekitIOBackend) -> None:
        """A backend that cannot report never opens the deadline list (the miss path's instruction gate); the
        answer is cached on the handler, and found through wrappers, lists and forwarded attributes."""

        class _Holder:
            def __init__(self, *inner: object) -> None:
                self.inner = list(inner)

            def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
                pass

        class _Plain:
            def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
                pass

        assert _reaches_write_deadlines(backend)
        assert _reaches_write_deadlines(_Holder(_Plain(), backend))  # a tiered wrapper's list
        assert _reaches_write_deadlines(_Holder(_Holder(_Holder(backend))))  # three wrappers down
        assert not _reaches_write_deadlines(_Holder(_Holder(_Holder(_Holder(backend)))))  # past the depth limit
        assert not _reaches_write_deadlines(_Plain())
        handler = StandardCacheHandler(_Plain())  # type: ignore[arg-type]
        assert _write(handler) == (True, False, None)
        assert handler._write_deadlines_reachable is False

    def test_reachability_reads_slots(self, backend: CachekitIOBackend) -> None:
        """A slotted wrapper (``__slots__``, ``@dataclass(slots=True)``) has no ``__dict__``: its slots are read."""

        @dataclasses.dataclass(slots=True)
        class _SlottedDataclass:
            inner: CachekitIOBackend

        class _Private:
            __slots__ = ("__inner",)

            def __init__(self, inner: CachekitIOBackend) -> None:
                self.__inner = inner

        class _Unset:
            __slots__ = ("inner",)

        assert _reaches_write_deadlines(_SlottedDataclass(backend))
        assert _reaches_write_deadlines(_Private(backend))  # name-mangled slot
        assert not _reaches_write_deadlines(_Unset())

    def test_reachability_is_not_starved_by_a_large_sibling(self, backend: CachekitIOBackend) -> None:
        """A direct ``inner`` is found however many objects a sibling attribute holds, before or after it."""

        class _Wrapper:
            def __init__(self, inner: CachekitIOBackend, first: bool) -> None:
                if first:
                    self.inner = inner
                self.locks = [threading.Lock() for _ in range(5000)]
                if not first:
                    self.inner = inner

        assert _reaches_write_deadlines(_Wrapper(backend, first=True))
        assert _reaches_write_deadlines(_Wrapper(backend, first=False))

    def test_documented_blind_spots_keep_the_stated_ttl(self, saas: _FakeSaaS, backend: CachekitIOBackend) -> None:
        """The two shapes the docs name as out of reach: a backend held only in a closure, and one assigned
        to an attribute on first use (the search runs before the first write and is cached)."""

        class _Closure:
            def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
                backend.set(key, value, ttl)

        class _BuiltOnFirstUse:
            def __init__(self) -> None:
                self.inner: CachekitIOBackend | None = None

            def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
                if self.inner is None:
                    self.inner = backend
                self.inner.set(key, value, ttl)

        saas.put_fresh_for = "0"
        assert _write(StandardCacheHandler(_Closure())) == (True, False, None)  # type: ignore[arg-type]
        handler = StandardCacheHandler(_BuiltOnFirstUse())  # type: ignore[arg-type]
        assert _write(handler) == (True, False, None)
        assert _write(handler) == (True, False, None)  # still: the answer was cached at the first write

    def test_reachability_through_containers(self, backend: CachekitIOBackend) -> None:
        """Routers, shard rings and pools: dict values, sets and nested containers, without spending depth."""

        class _Holder:
            def __init__(self, held: object) -> None:
                self.held = held

        assert _reaches_write_deadlines(_Holder({"eu": object(), "us": backend}))  # router
        assert _reaches_write_deadlines(_Holder([(17, object()), (42, backend)]))  # (hash, backend) ring
        assert _reaches_write_deadlines(_Holder({backend}))
        assert _reaches_write_deadlines(_Holder({"tier": [[{"x": backend}]]}))  # nested, one wrapper level
        assert _reaches_write_deadlines(_Holder([object()] * 1_000_000))  # budget spent: fails open, quickly

    def test_an_ordinary_backend_answers_false_without_failing_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A plain backend with dicts, lists, sets and slots in its attributes is searched to a clean False:
        a search that raised would answer True, opening the list on every write of a non-reporting backend."""
        failed: list[str] = []
        monkeypatch.setattr(cache_handler_module.get_logger(), "debug", lambda message, *a, **k: failed.append(message))

        class _Plain:
            __slots__ = ("store", "pool", "tags", "__dict__")

            def __init__(self) -> None:
                self.store = {"k": b"v", "n": {"nested": [object()]}}
                self.pool = [object(), (object(), {"x": object()})]
                self.tags = {"a", "b"}
                self.extra = frozenset({1})

        assert _reaches_write_deadlines(_Plain()) is False
        assert failed == []

    def test_a_cyclic_container_ends(self) -> None:
        ring: list[object] = []
        ring.append(ring)

        class _Holder:
            def __init__(self) -> None:
                self.ring = ring

        assert _reaches_write_deadlines(_Holder()) is False  # returns, and finds nothing

    def test_one_counter_bounds_any_container_shape(self) -> None:
        """Every value read counts, leaves included, and a shared container is read once: shapes that grow
        super-linearly when re-read finish at once and fail open."""

        class _Wide:
            def __init__(self) -> None:
                row = [0] * 10**6
                self.grid = [row] * 10**6
                self.nested = [[[[0] * 100] * 100] * 100] * 100

        started = time.monotonic()
        assert _reaches_write_deadlines(_Wide())
        assert time.monotonic() - started < 1.0

    def test_a_cycle_beside_the_backend_ends(self, backend: CachekitIOBackend) -> None:
        root: dict[str, object] = {}
        root["children"] = [{"parent": root}]

        class _Wrapper:
            def __init__(self) -> None:
                self.config = root
                self.inner = backend

        assert _reaches_write_deadlines(_Wrapper())

    def test_a_full_bucket_does_not_starve_a_route(self, backend: CachekitIOBackend) -> None:
        """Containers at one nesting level are read round-robin: a 4,096-item bucket before a router does
        not use up the budget before the router's backend is read."""

        class _Wrapper:
            def __init__(self) -> None:
                self.buckets = [[object() for _ in range(_REACH_READS)]]
                self.routes = {"us": [backend]}

        class _Root:
            def __init__(self) -> None:
                self.wrapper = _Wrapper()

        assert _reaches_write_deadlines(_Wrapper())
        assert _reaches_write_deadlines(_Root())

    def test_a_wide_sibling_does_not_starve_another_wrapper(self, backend: CachekitIOBackend) -> None:
        """Each object at a wrapper level gets a fair share of the budget."""

        class _Wide:
            def __init__(self) -> None:
                self.items = [object() for _ in range(10 * _REACH_READS)]

        class _Holder:
            def __init__(self, inner: object) -> None:
                self.inner = inner

        class _Root:
            def __init__(self) -> None:
                self.a = _Wide()
                self.b = _Holder(backend)

        assert _reaches_write_deadlines(_Root())

    def test_properties_named_like_storage_never_run(self, backend: CachekitIOBackend) -> None:
        """A property named ``__dict__`` (a lazy proxy) or overriding a slot name is skipped, not called."""
        ran: list[str] = []

        class _LazyProxy:
            @property
            def __dict__(self) -> dict[str, object]:  # type: ignore[override]
                ran.append("__dict__")
                return {"inner": backend}

        class _Base:
            __slots__ = ("computed",)

        class _Overriding(_Base):
            __slots__ = ()

            @property
            def computed(self) -> object:  # type: ignore[override]
                ran.append("computed")
                return backend

        class _Holder:
            def __init__(self) -> None:
                self.lazy = _LazyProxy()
                self.overriding = _Overriding()
                self.hooked = _Hooked()

        class _Meta(type):
            def __getattribute__(cls, name: str) -> Any:
                ran.append(f"meta:{name}")
                return super().__getattribute__(name)

        class _Hooked(metaclass=_Meta):
            def __init__(self) -> None:
                self.value = 1

        ran.clear()
        holder = _Holder()
        ran.clear()  # building the objects above may touch the metaclass; the search must not
        assert _reaches_write_deadlines(holder) is False
        assert ran == []

    def test_forwarding_proxy_is_reached(self, backend: CachekitIOBackend) -> None:
        """The backend lives only in a closure: the proxy is found through the root's forwarded attribute."""

        class _Proxy:
            def __getattr__(self, name: str) -> Any:
                return getattr(backend, name)

        assert _reaches_write_deadlines(_Proxy())

    def test_search_runs_no_user_code_below_the_root(self, backend: CachekitIOBackend) -> None:
        """A lazy object is never evaluated, a list subclass's ``__iter__`` never runs, and a property never
        fires: nothing below the root raises into the write."""
        ran: list[str] = []

        class _Lazy:
            def __getattr__(self, name: str) -> Any:
                ran.append(name)
                raise RuntimeError("factory failed")

            @property
            def __class__(self) -> type:  # type: ignore[override]
                ran.append("__class__")
                raise RuntimeError("factory failed")

        class _List(list):  # type: ignore[type-arg]
            def __iter__(self) -> Any:
                ran.append("__iter__")
                raise RuntimeError("iteration failed")

        class _Holder:
            def __init__(self) -> None:
                self.lazy = _Lazy()
                self.items = _List([object(), backend])

        assert _reaches_write_deadlines(_Holder())
        assert ran == []

    def test_a_search_that_raises_opens_the_list(self, backend: CachekitIOBackend, monkeypatch: pytest.MonkeyPatch) -> None:
        """Opening the list is always safe, so a search that fails answers True."""

        def broken(obj: object) -> list[object]:
            raise RuntimeError("unexpected")

        monkeypatch.setattr(cache_handler_module, "_instance_values", broken)
        assert _reaches_write_deadlines(object())

    def test_a_none_backend_or_odd_module_neither_raises_nor_logs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        logged: list[str] = []
        monkeypatch.setattr(cache_handler_module.get_logger(), "info", lambda message, *a, **k: logged.append(message))
        assert _reaches_write_deadlines(None) is False
        assert logged == []
        odd = type("Odd", (), {})
        odd.__module__ = None  # type: ignore[assignment]
        assert _reaches_write_deadlines(odd()) is False
        assert len(logged) == 1

    def test_budget_exhaustion_opens_the_list(self, backend: CachekitIOBackend) -> None:
        """A wide first level used to answer False and drop the bound; now the walk fails open."""

        class _Striped:
            def __init__(self, inner: object) -> None:
                self.locks = {i: threading.Lock() for i in range(5000)}
                self.inner = inner

        assert _reaches_write_deadlines(_Striped(backend))
        assert _reaches_write_deadlines(_Striped(object()))  # nothing to find, but too wide to be sure

    def test_composing_wrapper_keeps_the_bound(self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock) -> None:
        """A wrapper that composes a CachekitIO backend without forwarding its attributes (a metrics class)
        still reaches the base ``set`` on the same thread: the list is open around every write."""

        class _Metrics:
            def __init__(self, inner: CachekitIOBackend) -> None:
                self.inner = inner
                self.writes = 0

            def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
                self.writes += 1
                self.inner.set(key, value, ttl)

        saas.put_fresh_for = "0"
        assert _write(StandardCacheHandler(_Metrics(backend))) == (True, False, clock.now)  # type: ignore[arg-type]

    def test_override_raising_after_its_2xx_keeps_the_bound(self, saas: _FakeSaaS, clock: _Clock) -> None:
        """The write failed as far as the handler knows, but a 2xx was received: its bound still applies."""

        class _Raising(CachekitIOBackend):
            def set(self, key: str, value: bytes, ttl: int | None = None, stale_ttl: int | None = None) -> None:
                super().set(key, value, ttl, stale_ttl)
                raise RuntimeError("audit hook failed")

        backend, _ = fake_backend(saas.respond)
        backend.__class__ = _Raising
        saas.put_fresh_for = "5"
        assert _write(StandardCacheHandler(backend)) == (False, False, clock.now + 5)

    def test_forwarding_proxy_that_bypasses_set_keeps_no_copy(self, saas: _FakeSaaS, backend: CachekitIOBackend) -> None:
        """The capability is read off the instance, so a proxy that forwards attributes forwards it: a write
        that then never reaches the base ``set`` has an unknown bound, and keeps no copy (0.0)."""

        class _Proxy:
            def __init__(self, inner: CachekitIOBackend) -> None:
                self.inner = inner

            def __getattr__(self, name: str) -> Any:
                return getattr(self.inner, name)

            def set(self, key: str, value: bytes, ttl: int | None = None, stale_ttl: int | None = None) -> None:
                inner = self.inner
                inner._request_sync("PUT", inner._encode_key(key), body=value, headers=inner._set_headers(ttl, stale_ttl))

        saas.put_fresh_for = "5"
        assert _write(StandardCacheHandler(_Proxy(backend))) == (True, False, 0.0)  # type: ignore[arg-type]
        assert saas.ops == ["PUT"]


def _decorated(
    backend: CachekitIOBackend, *, is_async: bool, ttl: int | None = 60, **kwargs: Any
) -> tuple[Callable[[], Any], list[int]]:
    """A decorated function counting its executions, run synchronously either way."""
    calls: list[int] = []
    namespace = _namespace()
    if is_async:

        @cache(backend=backend, ttl=ttl, namespace=namespace, **kwargs)
        async def compute_async() -> int:
            calls.append(1)
            return len(calls)

        return (lambda: asyncio.run(compute_async())), calls

    @cache(backend=backend, ttl=ttl, namespace=namespace, **kwargs)
    def compute() -> int:
        calls.append(1)
        return len(calls)

    return compute, calls


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
class TestWriterCopyBound:
    """API-103 on the miss path: the L1 copy of a stored value lives min(local_ttl, effective_ttl, fresh_for).

    Each test counts the entry GETs after the miss: the async miss also re-reads under its fill lock.
    """

    def test_header_below_ttl_bounds_the_copy(
        self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock, is_async: bool
    ) -> None:
        saas.put_fresh_for = "5"  # the plan maximum clamped ttl=60 down to 5 s
        compute, calls = _decorated(backend, is_async=is_async)
        assert compute() == 1  # miss: PUT, then the bounded L1 copy
        gets = saas.gets()
        clock.advance(3.9)  # L1 drops an entry ttl_buffer_seconds (1 s) early: it lives until +4
        assert compute() == 1
        assert saas.gets() == gets  # served from L1
        clock.advance(1.2)  # +5.1: past fresh_for, the copy is gone
        assert compute() == 1  # from L2, not recomputed
        assert saas.gets() == gets + 1
        assert len(calls) == 1

    def test_absent_header_keeps_the_stated_ttl(
        self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock, is_async: bool
    ) -> None:
        compute, calls = _decorated(backend, is_async=is_async)
        assert compute() == 1
        gets = saas.gets()
        clock.advance(58.5)  # still inside min(local_ttl, effective_ttl) = 60, less the 1 s buffer
        assert compute() == 1
        assert saas.gets() == gets
        clock.advance(1.0)  # +59.5: past it
        assert compute() == 1
        assert saas.gets() == gets + 1
        assert len(calls) == 1

    def test_header_above_ttl_never_extends(
        self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock, is_async: bool
    ) -> None:
        saas.put_fresh_for = "2592000"
        compute, _ = _decorated(backend, is_async=is_async)
        assert compute() == 1
        gets = saas.gets()
        clock.advance(59.5)
        assert compute() == 1
        assert saas.gets() == gets + 1  # the 60 s copy expired: Fresh-For never lengthens the bound

    def test_no_ttl_is_bounded_by_the_header(
        self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock, is_async: bool
    ) -> None:
        """No TTL: the server stores the plan maximum and says so; L1 keeps min(default 300 s, fresh_for)."""
        saas.put_fresh_for = "10"
        compute, _ = _decorated(backend, is_async=is_async, ttl=None)
        assert compute() == 1
        gets = saas.gets()
        clock.advance(8.5)
        assert compute() == 1
        assert saas.gets() == gets
        clock.advance(1.0)
        assert compute() == 1
        assert saas.gets() == gets + 1

    def test_time_after_receipt_shortens_the_copy(
        self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock, monkeypatch: pytest.MonkeyPatch, is_async: bool
    ) -> None:
        """The bound runs from the 2xx's receipt, not from the L1 put: a stall between the two (here 2.2 s,
        on the async path the hop back to a busy event loop) is spent from the copy, not added to it."""
        parse = CachekitIOBackend._fresh_for

        def stalled_parse(response: Any) -> int | None:
            clock.advance(2.2)
            return parse(response)

        monkeypatch.setattr(CachekitIOBackend, "_fresh_for", staticmethod(stalled_parse))
        saas.put_fresh_for = "2"
        compute, calls = _decorated(backend, is_async=is_async, distributed_lock=False)
        assert compute() == 1
        gets = saas.gets()
        assert compute() == 1  # the receipt is already 2.2 s ago: no copy may serve
        assert saas.gets() == gets + 1
        assert len(calls) == 1

    def test_absent_header_times_the_stated_ttl_from_receipt(
        self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock, monkeypatch: pytest.MonkeyPatch, is_async: bool
    ) -> None:
        """No Fresh-For (an older server): the stated TTL bounds the copy, still timed from the 2xx's receipt."""
        parse = CachekitIOBackend._fresh_for

        def stalled_parse(response: Any) -> int | None:
            clock.advance(2.2)
            return parse(response)

        monkeypatch.setattr(CachekitIOBackend, "_fresh_for", staticmethod(stalled_parse))
        compute, calls = _decorated(backend, is_async=is_async, ttl=2, distributed_lock=False)
        assert compute() == 1
        gets = saas.gets()
        assert compute() == 1  # the receipt is already 2.2 s ago, past ttl=2: no copy may serve
        assert saas.gets() == gets + 1
        assert len(calls) == 1

    def test_handler_set_override_is_not_bypassed(
        self, saas: _FakeSaaS, backend: CachekitIOBackend, monkeypatch: pytest.MonkeyPatch, is_async: bool
    ) -> None:
        """The miss writes through the handler's own set / set_async, so a patch of either still decides."""
        calls: list[str] = []

        def refusing(self: StandardCacheHandler, *args: Any, **kwargs: Any) -> bool:
            calls.append("set")
            return False

        async def refusing_async(self: StandardCacheHandler, *args: Any, **kwargs: Any) -> bool:
            calls.append("set_async")
            return False

        if is_async:
            monkeypatch.setattr(StandardCacheHandler, "set_async", refusing_async)
        else:
            monkeypatch.setattr(StandardCacheHandler, "set", refusing)
        compute, _ = _decorated(backend, is_async=is_async, distributed_lock=False)
        assert compute() == 1
        assert calls
        assert "PUT" not in saas.ops

    def test_composing_wrapper_keeps_a_zero_bound(self, saas: _FakeSaaS, backend: CachekitIOBackend, is_async: bool) -> None:
        """A composing wrapper (no attribute forwarding) over CachekitIO: a 0 Fresh-For keeps no copy."""

        class _Metrics:
            def __init__(self, inner: CachekitIOBackend) -> None:
                self.inner = inner

            def get(self, key: str) -> bytes | None:
                return self.inner.get(key)

            def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
                self.inner.set(key, value, ttl)

            def delete(self, key: str) -> bool:
                return self.inner.delete(key)

            def exists(self, key: str) -> bool:
                return self.inner.exists(key)

        saas.put_fresh_for = "0"
        compute, calls = _decorated(_Metrics(backend), is_async=is_async, distributed_lock=False)  # type: ignore[arg-type]
        assert compute() == 1
        gets = saas.gets()
        assert compute() == 1
        assert saas.gets() == gets + 1
        assert len(calls) == 1

    @pytest.mark.parametrize("header", ["0", "garbage", "+5", "3000000"])
    def test_malformed_or_zero_keeps_no_copy(
        self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock, is_async: bool, header: str
    ) -> None:
        saas.put_fresh_for = header
        compute, calls = _decorated(backend, is_async=is_async)
        assert compute() == 1
        gets = saas.gets()
        assert compute() == 1  # no clock movement: an L1 copy would have served this
        assert saas.gets() == gets + 1
        assert len(calls) == 1


class TestWriteBehindCopyBound:
    """A write-behind miss puts its L1 copy before the PUT (API-36/37). Once the 2xx lands, a Fresh-For that
    would end the copy sooner drops it; a value the copy already honours keeps it. A put that comes after the
    2xx is bounded by it from the start."""

    @pytest.fixture(autouse=True)
    def _writers(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[wb.WriteBehind]]:
        created: list[wb.WriteBehind] = []
        init = wb.WriteBehind.__init__

        def recording_init(self: wb.WriteBehind, *args: Any, **kwargs: Any) -> None:
            init(self, *args, **kwargs)
            created.append(self)

        monkeypatch.setattr(wb.WriteBehind, "__init__", recording_init)
        self.created = created
        yield created

    def _drain(self) -> None:
        for writer in self.created:
            assert writer.close(time.monotonic() + _GUARD_SECONDS) == [], "a deferred write never finished"

    def _land_before_put(self, monkeypatch: pytest.MonkeyPatch, then: Callable[[], None] = lambda: None) -> None:
        """Hold the miss after it hands its write to the writer until that write has landed, then run
        ``then``: the miss's L1 put comes after the 2xx, and must see its bound."""
        submit = wb.WriteBehind.submit

        def submit_and_land(writer: wb.WriteBehind, *args: Any, **kwargs: Any) -> bool:
            queued = submit(writer, *args, **kwargs)
            self._drain()
            then()
            return queued

        monkeypatch.setattr(wb.WriteBehind, "submit", submit_and_land)

    @pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
    @pytest.mark.parametrize(
        ("header", "copy_kept"),
        [
            (None, True),
            ("59", True),  # floor(fresh_until - now) of an unclamped ttl=60: the 1 s buffer already covers it
            ("5", False),  # clamped by the plan maximum
            ("0", False),
            ("garbage", False),
        ],
    )
    def test_landed_write_drops_a_copy_it_would_outlive(
        self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock, header: str | None, copy_kept: bool, is_async: bool
    ) -> None:
        """The usual order: the miss puts its copy for ttl, then the write lands and drops it if its bound is shorter."""
        saas.put_fresh_for = header
        # The async locked miss path never defers (its write must land inside the lock).
        compute, _ = _decorated(backend, is_async=is_async, write_behind=True, distributed_lock=False)
        assert compute() == 1
        self._drain()
        assert saas.ops == ["GET", "PUT"]
        assert compute() == 1
        assert saas.gets() == (1 if copy_kept else 2)

    @pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
    @pytest.mark.parametrize(
        ("header", "bound"),
        [(None, 60), ("59", 59), ("5", 5), ("0", 0), ("garbage", 0)],
    )
    def test_put_after_landing_is_bounded(
        self,
        saas: _FakeSaaS,
        backend: CachekitIOBackend,
        clock: _Clock,
        monkeypatch: pytest.MonkeyPatch,
        header: str | None,
        bound: int,
        is_async: bool,
    ) -> None:
        """A write that lands before the miss's put bounds the put itself: a copy with no time left is never
        published, not even until a later drop, and a clamped one serves only inside its bound."""
        self._land_before_put(monkeypatch)
        original_put = l1_module.L1Cache.put
        visible: list[bool] = []

        def observed_put(self_: l1_module.L1Cache, key: str, value: bytes, **kwargs: Any) -> None:
            original_put(self_, key, value, **kwargs)
            visible.append(self_.get(key)[0])  # what a concurrent reader sees right after the put

        monkeypatch.setattr(l1_module.L1Cache, "put", observed_put)
        saas.put_fresh_for = header
        compute, _ = _decorated(backend, is_async=is_async, write_behind=True, distributed_lock=False)
        assert compute() == 1
        assert visible == [bound > 1]
        gets = saas.gets()
        clock.advance(max(bound - 1.5, 0))  # inside the bound, less the 1 s buffer
        assert compute() == 1
        assert saas.gets() == (gets + 1 if bound <= 1 else gets)
        clock.advance(1.0)  # past it
        assert compute() == 1
        assert saas.gets() > gets

    @pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
    def test_put_long_after_landing_keeps_the_receipt_bound(
        self,
        saas: _FakeSaaS,
        backend: CachekitIOBackend,
        clock: _Clock,
        monkeypatch: pytest.MonkeyPatch,
        is_async: bool,
    ) -> None:
        """A copy put 10 s after its write's 2xx serves to receipt + 59 (less the buffer), not put + 59 or
        put + 60: the bound is an instant taken at the receipt."""
        self._land_before_put(monkeypatch, then=lambda: clock.advance(10))
        saas.put_fresh_for = "59"
        received = clock.now
        compute, _ = _decorated(backend, is_async=is_async, write_behind=True, distributed_lock=False)
        assert compute() == 1
        gets = saas.gets()
        clock.now = received + 57.5
        assert compute() == 1
        assert saas.gets() == gets
        clock.now = received + 58.5  # past receipt + 59 less the buffer
        assert compute() == 1
        assert saas.gets() == gets + 1

    @pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
    @pytest.mark.parametrize("lands_first", [False, True], ids=["put-first", "write-lands-first"])
    @pytest.mark.parametrize("raiser", ["backend", "handler"])
    def test_write_failing_after_its_2xx_keeps_no_copy(
        self,
        saas: _FakeSaaS,
        backend: CachekitIOBackend,
        clock: _Clock,
        monkeypatch: pytest.MonkeyPatch,
        raiser: str,
        lands_first: bool,
        is_async: bool,
    ) -> None:
        """A deferred write whose PUT got Fresh-For 0 and which then raised: in the backend's ``set`` (the
        handler catches it, so the write is not stored but its bound was reported), or in a patched handler
        ``set`` (so ``_set_l2`` raises and the bound is lost). Either way no copy serves."""
        if raiser == "backend":

            class _Raising(CachekitIOBackend):
                def set(self, key: str, value: bytes, ttl: int | None = None, stale_ttl: int | None = None) -> None:
                    super().set(key, value, ttl, stale_ttl)
                    raise RuntimeError("audit hook failed")

            backend.__class__ = _Raising
        else:
            original = StandardCacheHandler.set

            def raising(self_: StandardCacheHandler, *args: Any, **kwargs: Any) -> bool:
                original(self_, *args, **kwargs)
                raise RuntimeError("audit hook failed")

            monkeypatch.setattr(StandardCacheHandler, "set", raising)
        if lands_first:
            self._land_before_put(monkeypatch)
        saas.put_fresh_for = "0"
        compute, _ = _decorated(backend, is_async=is_async, write_behind=True, distributed_lock=False)
        assert compute() == 1
        self._drain()
        assert saas.ops.count("PUT") == 1
        gets = saas.gets()
        clock.advance(30)
        assert compute() == 1
        assert saas.gets() == gets + 1  # no L1 copy served


class TestPatchTtlNeverExtendsL1:
    """API-104: refresh_ttl_on_get's background PATCH /ttl leaves every L1 bound as it was."""

    async def test_refresh_leaves_the_backfill_bound(self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock) -> None:
        namespace = _namespace()

        @cache(backend=backend, ttl=60, namespace=namespace, refresh_ttl_on_get=True)
        async def compute() -> int:
            return 1

        assert await compute() == 1  # miss + PUT
        l1 = l1_module.get_l1_cache(namespace)
        l1.invalidate_many(list(l1._state.cache))  # drop L1 only: the next read is an L2 hit
        saas.get_fresh_for = "3"  # 3 s of freshness left: the backfill copy lives until +2 (1 s buffer)
        assert await compute() == 1  # L2 hit -> bounded backfill, and a refresh is due
        assert await asyncio.to_thread(saas.patched.wait, _GUARD_SECONDS), "no PATCH /ttl was sent"
        await asyncio.sleep(0)  # let the refresh task finish after its response
        clock.advance(2.5)  # past the backfill bound; a PATCH-derived 60 s bound would still serve L1
        gets = saas.gets()
        assert await compute() == 1
        assert saas.gets() == gets + 1


def _wait_for(predicate: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + _GUARD_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
class TestRevalidationCopyBound:
    """API-103 on the SWR revalidation write: its L1 refresh is bounded by the revalidation PUT's 2xx."""

    @pytest.mark.parametrize(("header", "l1_ttl"), [(None, 60), ("5", 5), ("0", 0)])
    def test_revalidated_copy_is_bounded(
        self,
        saas: _FakeSaaS,
        backend: CachekitIOBackend,
        clock: _Clock,
        monkeypatch: pytest.MonkeyPatch,
        is_async: bool,
        header: str | None,
        l1_ttl: int,
    ) -> None:
        namespace = _namespace()
        calls: list[int] = []
        puts: list[Any] = []  # lifetime of each L1 put, from the put
        put_seen = threading.Event()
        original_put = l1_module.L1Cache.put

        def spy(self: l1_module.L1Cache, key: str, value: bytes, redis_ttl: Any = None, expires_at: Any = None) -> None:
            original_put(self, key, value, redis_ttl=redis_ttl, expires_at=expires_at)
            puts.append(redis_ttl if expires_at is None else expires_at - clock.now)
            put_seen.set()

        if is_async:

            @cache(backend=backend, ttl=60, stale_ttl=60, namespace=namespace)
            async def compute_async() -> int:
                calls.append(1)
                return len(calls)

            def compute() -> Any:
                async def call_and_settle() -> int:
                    result = await compute_async()
                    if saas.stale:  # a revalidation runs on this loop: hold it open until its L1 put
                        await asyncio.to_thread(put_seen.wait, _GUARD_SECONDS)
                    return result

                return asyncio.run(call_and_settle())
        else:

            @cache(backend=backend, ttl=60, stale_ttl=60, namespace=namespace)
            def compute() -> int:
                calls.append(1)
                return len(calls)

        assert compute() == 1  # miss + PUT (no header: a 60 s copy)
        l1 = l1_module.get_l1_cache(namespace)
        l1.invalidate_many(list(l1._state.cache))  # the next read reaches L2, which labels it stale
        saas.stale = True
        saas.put_fresh_for = header
        monkeypatch.setattr(l1_module.L1Cache, "put", spy)
        assert compute() == 1  # stale served; the revalidation recomputes and writes in the background
        assert put_seen.wait(_GUARD_SECONDS), "the revalidation never refreshed L1"
        assert len(calls) == 2
        assert saas.ops.count("PUT") == 2
        assert puts == [l1_ttl]  # min(local_ttl, effective_ttl, fresh_for)
        assert len(l1._state.cache) == (1 if l1_ttl else 0)  # 0: no copy kept


class TestL1DeclinedPutDropsOlderEntry:
    """A put L1 declines because the value would already be expired (a 0 Fresh-For) drops the older value
    under the key, as an oversized put does: the newer write must not leave the previous one serving."""

    def test_zero_ttl_put_drops_the_older_entry(self, clock: _Clock) -> None:
        l1 = l1_module.L1Cache(namespace=_namespace())
        l1.put("k", b"old", redis_ttl=60)
        assert l1.get("k") == (True, b"old")
        l1.put("k", b"new", redis_ttl=0)
        assert l1.get("k") == (False, None)

    def test_declined_put_runs_the_fork_take_over_first(self, clock: _Clock) -> None:
        """The removal touches L1's state, so a child forked without hooks must take it over first: the
        lock it would otherwise wait on can be one the fork orphaned."""
        order: list[str] = []
        l1 = l1_module.L1Cache(namespace=_namespace(), before_store=lambda: order.append("take-over"))
        original = l1._on_current_state

        def recording(*args: Any, **kwargs: Any) -> None:
            order.append("state")
            original(*args, **kwargs)

        l1._on_current_state = recording  # type: ignore[method-assign]
        l1.put("k", b"new", redis_ttl=0)
        assert order == ["take-over", "state"]
