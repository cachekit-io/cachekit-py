"""A custom ``key=`` key carries the decorated function's identity.

Before, a ``key=`` key was ``{namespace or 'default'}:{custom_key}``: two functions whose key functions
returned the same string read, overwrote and invalidated one entry, on every decorator with a backend,
``@cache.secure`` included. The key is now ``{identity}-{namespace or 'default'}:{custom_key}``, where the
identity is the 16-hex blake3 of the decorated function's ``module.qualname``. Identity first keeps every new key
apart from every key an earlier release writes, which starts with a configured namespace.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Optional

import pytest

from cachekit import cache
from cachekit.backends.file import FileBackend
from cachekit.backends.file.config import FileBackendConfig
from cachekit.hash_utils import blake3_hash
from cachekit.l1_cache import get_l1_cache_manager
from tests.unit.test_key_registry import TrackingBackend

MASTER_KEY = "cd" * 48


@pytest.fixture(autouse=True)
def _fresh_l1() -> Iterator[None]:
    """L1 is one cache per namespace, shared by every test: start and end each test empty."""
    get_l1_cache_manager().clear_all()
    yield
    get_l1_cache_manager().clear_all()


def _file_backend(path: Path) -> FileBackend:
    path.mkdir(parents=True, exist_ok=True)
    return FileBackend(FileBackendConfig(cache_dir=str(path)))


def _identity(func: Callable[..., Any]) -> str:
    return blake3_hash(f"{func.__module__}.{func.__qualname__}", digest_size=8)


class RecordingBackend(TrackingBackend):
    """TrackingBackend that also records every read."""

    def __init__(self) -> None:
        super().__init__()
        self.reads: list[str] = []

    def get(self, key: str) -> Optional[bytes]:
        self.reads.append(key)
        return super().get(key)


def _decorator(intent: str, backend: Any, namespace: Optional[str], l1_enabled: bool) -> Callable[..., Any]:
    """One decorator per call. The key function is the same lambda shape for every function, as in the bug."""
    options: dict[str, Any] = {
        "ttl": 60,
        "backend": backend,
        "namespace": namespace,
        "key": lambda x: str(x),
        "l1_enabled": l1_enabled,
    }
    if intent == "secure":
        return cache.secure(master_key=MASTER_KEY, **options)
    return cache(**options)


_MATRIX = pytest.mark.parametrize(
    ("intent", "namespace", "l1_enabled"),
    [(i, ns, l1) for i in ("plain", "secure") for ns in (None, "team") for l1 in (True, False)],
)


@pytest.mark.unit
class TestNoSharedEntries:
    @_MATRIX
    def test_sync(self, tmp_path: Path, intent: str, namespace: Optional[str], l1_enabled: bool) -> None:
        backend = _file_backend(tmp_path)
        runs: list[str] = []

        @_decorator(intent, backend, namespace, l1_enabled)
        def k1(x: int) -> str:
            runs.append("k1")
            return "k1"

        @_decorator(intent, backend, namespace, l1_enabled)
        def k2(x: int) -> str:
            runs.append("k2")
            return "k2"

        assert (k1(1), k2(1)) == ("k1", "k2")
        assert (k1(1), k2(1)) == ("k1", "k2")
        assert runs == ["k1", "k2"]  # the second round was served, each from its own entry

    @_MATRIX
    async def test_async(self, tmp_path: Path, intent: str, namespace: Optional[str], l1_enabled: bool) -> None:
        backend = _file_backend(tmp_path)
        runs: list[str] = []

        @_decorator(intent, backend, namespace, l1_enabled)
        async def k1(x: int) -> str:
            runs.append("k1")
            return "k1"

        @_decorator(intent, backend, namespace, l1_enabled)
        async def k2(x: int) -> str:
            runs.append("k2")
            return "k2"

        assert (await k1(1), await k2(1)) == ("k1", "k2")
        assert (await k1(1), await k2(1)) == ("k1", "k2")
        assert runs == ["k1", "k2"]

    def test_two_file_backends_one_namespace(self, tmp_path: Path) -> None:
        """Different directories, one namespace: they used to collide through the shared L1."""

        @cache(ttl=60, backend=_file_backend(tmp_path / "a"), namespace="shared", key=lambda x: str(x))
        def p1(x: int) -> str:
            return "p1"

        @cache(ttl=60, backend=_file_backend(tmp_path / "b"), namespace="shared", key=lambda x: str(x))
        def p2(x: int) -> str:
            return "p2"

        assert (p1(1), p2(1)) == ("p1", "p2")
        assert (p1(1), p2(1)) == ("p1", "p2")

    def test_colons_cannot_absorb_the_identity(self) -> None:
        """F1 in namespace ``a`` keys ``<F2 id>:x``; F2 in namespace ``a:<F1 id>`` keys ``x``. Still two keys."""
        backend = RecordingBackend()

        def f1(x: int) -> str:
            return "f1"

        def f2(x: int) -> str:
            return "f2"

        id1, id2 = _identity(f1), _identity(f2)
        f1 = cache(ttl=60, backend=backend, namespace="a", key=lambda x: f"{id2}:x", l1_enabled=False)(f1)
        f2 = cache(ttl=60, backend=backend, namespace=f"a:{id1}", key=lambda x: "x", l1_enabled=False)(f2)

        assert (f1(1), f2(1)) == ("f1", "f2")
        assert set(backend.store) == {f"{id1}-a:{id2}:x", f"{id2}-a:{id1}:x"}


@pytest.mark.unit
class TestNoSharedInvalidation:
    def test_sync_invalidation_leaves_the_other_function(self, tmp_path: Path) -> None:
        backend = _file_backend(tmp_path)
        runs: list[str] = []

        @cache(ttl=60, backend=backend, key=lambda x: str(x))
        def k1(x: int) -> str:
            runs.append("k1")
            return "k1"

        @cache(ttl=60, backend=backend, key=lambda x: str(x))
        def k2(x: int) -> str:
            runs.append("k2")
            return "k2"

        k1(1), k2(1)
        k1.invalidate_cache(1)
        assert k2(1) == "k2"
        k1.cache_clear()
        assert k2(1) == "k2"
        assert runs == ["k1", "k2"]  # k2 was served both times
        assert k1(1) == "k1"
        assert runs == ["k1", "k2", "k1"]  # and k1's own entry was gone

    async def test_async_invalidation_leaves_the_other_function(self, tmp_path: Path) -> None:
        backend = _file_backend(tmp_path)
        runs: list[str] = []

        @cache(ttl=60, backend=backend, key=lambda x: str(x))
        async def k1(x: int) -> str:
            runs.append("k1")
            return "k1"

        @cache(ttl=60, backend=backend, key=lambda x: str(x))
        async def k2(x: int) -> str:
            runs.append("k2")
            return "k2"

        await k1(1), await k2(1)
        await k1.ainvalidate_cache(1)
        assert await k2(1) == "k2"
        await k1.ainvalidate_cache()
        assert await k2(1) == "k2"
        assert runs == ["k1", "k2"]
        assert await k1(1) == "k1"
        assert runs == ["k1", "k2", "k1"]

    def test_announced_invalidation_leaves_the_other_functions_l1_entry(self) -> None:
        """A custom-key invalidation is announced as the whole function; the listener evicts with ``None``."""
        backend = TrackingBackend()
        runs: list[str] = []

        @cache(ttl=60, backend=backend, key=lambda x: str(x))
        def k1(x: int) -> str:
            runs.append("k1")
            return "k1"

        @cache(ttl=60, backend=backend, key=lambda x: str(x))
        def k2(x: int) -> str:
            runs.append("k2")
            return "k2"

        assert (k1(1), k2(1)) == ("k1", "k2")
        k1._cachekit_evict(None)  # what the listener runs for k1's announcement
        backend.store.clear()  # so only L1 can serve either function now
        assert k2(1) == "k2"
        assert runs == ["k1", "k2"]  # k2's L1 entry survived k1's eviction
        assert k1(1) == "k1"
        assert runs == ["k1", "k2", "k1"]  # and k1's own was evicted


@pytest.mark.unit
class TestKeyShape:
    def test_golden_vector(self) -> None:
        """Pins the identity: a per-process salt or a hash change would move every key."""
        backend = RecordingBackend()

        def fetch_invoice(invoice_id: int) -> str:
            return "invoice"

        fetch_invoice.__module__ = "billing.invoices"
        fetch_invoice.__qualname__ = "fetch_invoice"
        wrapped = cache(ttl=60, backend=backend, namespace="billing", key=lambda i: f"inv-{i}", l1_enabled=False)(fetch_invoice)

        wrapped(42)
        assert set(backend.store) == {"f597f24f4f87a17e-billing:inv-42"}

    @pytest.mark.parametrize("namespace", [None, "default", "users", "ns", "nsapi", "a:b"])
    def test_key_is_the_identity_then_the_pre_fix_key(self, namespace: Optional[str]) -> None:
        """The key grows by exactly 16 lowercase hex plus ``-``, in front of the pre-fix key."""
        backend = RecordingBackend()

        @cache(ttl=60, backend=backend, namespace=namespace, key=lambda x: f"user:{x}", l1_enabled=False)
        def f(x: int) -> int:
            return x

        f(7)
        (stored,) = backend.store
        old_key = f"{namespace or 'default'}:user:7"
        assert stored == f"{_identity(f)}-{old_key}"

    def test_never_equals_a_generated_key(self) -> None:
        """A custom key spelling a generated key, under ``ns:users``, is not that generated key."""
        backend = RecordingBackend()

        @cache(ttl=60, backend=backend, namespace="users", l1_enabled=False)
        def generated(x: int) -> str:
            return "generated"

        generated(1)
        (generated_key,) = backend.store
        assert generated_key.startswith("ns:users:func:") and generated_key.endswith(":1s")
        spelled = generated_key.removeprefix("ns:users:")

        @cache(ttl=60, backend=backend, namespace="ns:users", key=lambda x: spelled, l1_enabled=False)
        def custom(x: int) -> str:
            return "custom"

        assert custom(1) == "custom"
        assert set(backend.store) == {generated_key, f"{_identity(custom)}-{generated_key}"}
        assert generated(1) == "generated"


@pytest.mark.unit
class TestNoLegacyKey:
    def test_pre_fix_key_is_never_read_or_written(self) -> None:
        backend = RecordingBackend()
        runs: list[int] = []

        @cache(ttl=60, backend=backend, key=lambda x: str(x), l1_enabled=False)
        def f(x: int) -> int:
            runs.append(x)
            return len(runs)

        f(1)
        (new_key,) = backend.store
        backend.store["default:1"] = backend.store.pop(new_key)  # a pre-fix process's entry, readable as f's
        backend.reads.clear()

        assert f(1) == 2  # recomputed: the old entry was not served
        assert "default:1" not in backend.reads
        assert set(backend.store) == {"default:1", new_key}

    @pytest.mark.parametrize("intent", ["plain", "secure"])
    @pytest.mark.parametrize("namespace", ["users", "a:b"])
    def test_no_key_an_earlier_release_writes_is_read(self, intent: str, namespace: str) -> None:
        """An earlier release writes ``{namespace}:{key}`` for any key a caller can shape, in this namespace or an
        enclosing one (``a`` for ``a:b``). Every key this release touches starts with the identity, so a crafted
        argument on an earlier-release process can never plant an entry this release reads."""
        backend = RecordingBackend()

        def lookup(record_id: str) -> str:
            return f"record {record_id}"

        identity = _identity(lookup)
        # A real plaintext entry, which a plain read would serve if it reached one; under @cache.secure the
        # key-prefix assertion below is what bites, since no writer of this release can seal for an old key.
        scratch = RecordingBackend()
        cache(ttl=60, backend=scratch, key=lambda: "seed", l1_enabled=False)(lambda: "planted")()
        (planted,) = scratch.store.values()
        for crafted in ("42", f"42:{identity}", f"{identity}-{namespace}:42"):
            backend.store[f"{namespace}:{crafted}"] = planted
        upgraded = _decorator(intent, backend, namespace, False)(lookup)

        assert upgraded("42") == "record 42"
        assert backend.reads and all(key.startswith(f"{identity}-") for key in backend.reads)
        assert {key for key in backend.store if not key.startswith(f"{namespace}:")} == {f"{identity}-{namespace}:42"}

    def test_no_args_invalidation_drains_tracked_pre_fix_keys(self) -> None:
        """The registry set name did not move, so the migration's early-cleanup step reaches old keys."""
        backend = TrackingBackend()

        @cache(ttl=60, backend=backend, key=lambda x: str(x), l1_enabled=False)
        def f(x: int) -> int:
            return x

        f(1)
        ((registry_id, _),) = backend.track_calls
        assert registry_id == f"ck:reg::{_identity(f)}"  # the pre-fix name: None namespace, empty segment
        backend.store["default:2"] = b"pre-fix"
        backend.sets[registry_id].add("default:2")  # tracked by a pre-fix process

        f.invalidate_cache()
        assert backend.store == {}


@pytest.mark.unit
class TestL1OnlyNeverShared:
    def test_cache_local(self) -> None:
        @cache.local(ttl=60, key=lambda x: str(x))
        def c1(x: int) -> str:
            return "c1"

        @cache.local(ttl=60, key=lambda x: str(x))
        def c2(x: int) -> str:
            return "c2"

        assert (c1(1), c2(1)) == ("c1", "c2")
        assert (c1(1), c2(1)) == ("c1", "c2")

    def test_backend_none(self) -> None:
        @cache(ttl=60, backend=None, key=lambda x: str(x))
        def l1(x: int) -> str:
            return "l1"

        @cache(ttl=60, backend=None, key=lambda x: str(x))
        def l2(x: int) -> str:
            return "l2"

        assert (l1(1), l2(1)) == ("l1", "l2")
        assert (l1(1), l2(1)) == ("l1", "l2")
