"""User journeys: each documented promise, checked against an installed cachekit with nothing configured.

Run them with ``make journeys``, which builds the wheel, installs it with its ``[data]`` extra and polars
into a fresh virtualenv, and runs this file there (see docs/CONTRIBUTING.md, "User Journeys").

This is a plain script, not a pytest suite, on purpose. pytest started from the repository loads
``tests/conftest.py``, which registers a Redis-backed provider for every test, and pyproject's
``pythonpath = ["src"]``, which imports the source tree instead of the wheel. That is the environment in
which the suite passed while a bare ``@cache`` cached nothing, a DataFrame hit came back read-only, and an
``ArrowSerializer(...)`` instance never took the fast paths the docs promised. A journey sees what a user
sees: the installed package, no environment variables, no Redis.

Exit status: 0 when every journey passes, 1 when any fails, 2 when the environment is not a clean one.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import traceback
import tracemalloc
from collections.abc import Callable
from typing import Any

# Scrub before cachekit is imported: its settings read the environment when they are first built, and match
# names case-insensitively, so redis_url configures a backend as REDIS_URL does. Each journey's interpreter
# inherits this environment.
for _var in [v for v in os.environ if v.upper().startswith("CACHEKIT_") or v.upper() == "REDIS_URL"]:
    del os.environ[_var]

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import polars as pl  # noqa: E402
import pyarrow as pa  # noqa: E402

import cachekit  # noqa: E402
from cachekit import cache  # noqa: E402
from cachekit.backends.file import FileBackend, FileBackendConfig  # noqa: E402
from cachekit.serializers import ArrowSerializer  # noqa: E402

JOURNEYS: list[tuple[str, Callable[[], str | None]]] = []
PY313 = sys.version_info >= (3, 13)
TIMEOUT_S = 300


def journey(promise: str) -> Callable[[Callable[[], str | None]], Callable[[], str | None]]:
    """Register a journey. ``promise`` states the documented behaviour it checks, in the docs' terms.

    The function raises AssertionError, with what it saw, when the promise does not hold. It may return a
    short note for the report. A journey never skips: a missing dependency fails it, because a skip is how
    the dead polars tests hid a defect.
    """

    def register(fn: Callable[[], str | None]) -> Callable[[], str | None]:
        JOURNEYS.append((promise, fn))
        return fn

    return register


# ── helpers ───────────────────────────────────────────────────────────────────────────────────────────


def file_backend() -> FileBackend:
    return FileBackend(FileBackendConfig(cache_dir=tempfile.mkdtemp(prefix="cachekit-journey-")))


def small_frame() -> pd.DataFrame:
    """One record batch: the shape every DataFrame a user caches below about 8 MiB has."""
    n = 1000
    return pd.DataFrame({"i": np.arange(n, dtype="int64"), "f": np.linspace(0, 1, n), "s": [f"x{k % 7}" for k in range(n)]})


def polars_frame() -> pl.DataFrame:
    return pl.DataFrame({"a": [1, 2, 3], "b": [1.5, 2.5, 3.5], "c": ["x", "y", "z"]})


def spy(obj: Any, method: str) -> list[Any]:
    """Record the result of every call to ``obj.method``, patched on this instance only."""
    seen: list[Any] = []
    original = getattr(obj, method)

    def recording(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        seen.append(result)
        return result

    setattr(obj, method, recording)
    return seen


def call_twice(fn: Callable[[], Any], calls: list[int]) -> Any:
    """Miss then hit; return the hit after checking the body ran once."""
    fn()
    hit = fn()
    assert len(calls) == 1, f"the function body ran {len(calls)} times for two identical calls"
    return hit


def assert_editable(df: pd.DataFrame) -> None:
    """Edit a DataFrame hit in place, the way pandas code does. A read-only column raises here."""
    try:
        df.loc[0, "i"] = -1
        df.loc[0, "s"] = "edited"
        df["f"] *= 2
    except Exception as e:
        raise AssertionError(f"editing the hit in place raised {type(e).__name__}: {e}") from e


# ── zero configuration ────────────────────────────────────────────────────────────────────────────────


@journey("bare @cache, nothing configured: the second call is a hit")
def _() -> None:
    calls: list[int] = []

    @cache
    def f(x: int) -> dict[str, int]:
        calls.append(x)
        return {"x": x}

    assert call_twice(lambda: f(1), calls) == {"x": 1}, "wrong value"


@journey("bare @cache on an async function, nothing configured: the second call is a hit")
def _() -> None:
    calls: list[int] = []

    @cache
    async def f(x: int) -> int:
        calls.append(x)
        return x * 2

    async def twice() -> tuple[int, int]:
        return await f(3), await f(3)

    assert asyncio.run(twice()) == (6, 6), "wrong value"
    assert len(calls) == 1, f"the function body ran {len(calls)} times for two identical calls"


@journey("each plaintext preset (minimal, production, dev, test), nothing configured: the second call is a hit")
def _() -> None:
    for preset in ("minimal", "production", "dev", "test"):
        calls: list[int] = []

        @getattr(cache, preset)(ttl=60)
        def f(x: int) -> int:
            calls.append(x)  # noqa: B023 - called within this iteration
            return x

        try:
            call_twice(lambda: f(1), calls)  # noqa: B023 - called within this iteration
        except AssertionError as e:
            raise AssertionError(f"@cache.{preset}: {e}") from e


@journey("bare @cache holds values by reference (README): a hit is the object the function returned")
def _() -> None:
    @cache
    def values() -> tuple[dict[str, list[int]], pd.DataFrame]:
        return {"items": [1, 2, 3]}, small_frame()

    first = values()
    assert values() is first, "a hit is not the object the first call returned"


def _invalidate_journey(label: str, make_decorator: Callable[[], Any]) -> None:
    @journey(f"invalidate_cache on {label}: after a hit, the next call recomputes")
    def _() -> None:
        calls: list[int] = []

        @make_decorator()
        def f(x: int) -> int:
            calls.append(x)
            return x

        call_twice(lambda: f(1), calls)
        f.invalidate_cache(1)
        f(1)
        assert len(calls) == 2, f"the function body ran {len(calls)} times across a hit, invalidate_cache, call"


_invalidate_journey("a bare @cache", lambda: cache)
_invalidate_journey("FileBackend", lambda: cache(backend=file_backend(), ttl=60))


# ── a backend hit is equal and editable ───────────────────────────────────────────────────────────────

# The two documented spellings of the Arrow serializer, each with and without compression where it matters.
ARROW_SPELLINGS: list[tuple[str, Callable[[], Any]]] = [
    ('serializer="arrow"', lambda: "arrow"),
    ("ArrowSerializer()", ArrowSerializer),
    ("ArrowSerializer(compression=None)", lambda: ArrowSerializer(compression=None)),
]
FRAME_SERIALIZERS: list[tuple[str, Callable[[], Any]]] = [('serializer="auto"', lambda: "auto"), *ARROW_SPELLINGS]


def _frame_journey(label: str, make_serializer: Callable[[], Any]) -> None:
    @journey(f"a DataFrame cached through {label} on FileBackend comes back equal and editable, with or without L1")
    def _() -> None:
        for l1_enabled in (True, False):
            calls: list[int] = []

            @cache(backend=file_backend(), serializer=make_serializer(), l1_enabled=l1_enabled, ttl=60)
            def f() -> pd.DataFrame:
                calls.append(1)  # noqa: B023 - called within this iteration
                return small_frame()

            hit = call_twice(f, calls)
            where = f"l1_enabled={l1_enabled}"
            assert isinstance(hit, pd.DataFrame), f"{where}: the hit is {type(hit).__name__}"
            assert hit.equals(small_frame()), f"{where}: the hit differs from the frame the function returned"
            try:
                assert_editable(hit)
            except AssertionError as e:
                raise AssertionError(f"{where}: {e}") from e


for _label, _make in FRAME_SERIALIZERS:
    _frame_journey(_label, _make)


@journey('a NumPy array cached through serializer="auto" on FileBackend comes back equal and writable')
def _() -> None:
    calls: list[int] = []

    @cache(backend=file_backend(), serializer="auto", l1_enabled=False, ttl=60)
    def f() -> np.ndarray:
        calls.append(1)
        return np.arange(10, dtype="int64")

    hit = call_twice(f, calls)
    assert np.array_equal(hit, np.arange(10)), "the hit differs from the array the function returned"
    assert hit.flags.writeable, "the hit is a read-only array"


@journey("a dict cached with the default serializer on FileBackend comes back equal and editable")
def _() -> None:
    calls: list[int] = []

    @cache(backend=file_backend(), l1_enabled=False, ttl=60)
    def f() -> dict[str, Any]:
        calls.append(1)
        return {"name": "a", "items": [1, 2, 3]}

    hit = call_twice(f, calls)
    assert hit == {"name": "a", "items": [1, 2, 3]}, f"the hit is {hit!r}"
    hit["items"].append(4)


@journey('backend=None and FileBackend: a DataFrame hit through serializer="auto" has the same type, values and editability')
def _() -> str:
    seen = {}
    for label, options in (
        ("backend=None", {"backend": None}),
        ("FileBackend", {"backend": file_backend(), "l1_enabled": False}),
    ):
        calls: list[int] = []

        @cache(serializer="auto", ttl=60, **options)
        def f() -> pd.DataFrame:
            calls.append(1)  # noqa: B023 - called within this iteration
            return small_frame()

        hit = call_twice(f, calls)
        equal = bool(hit.equals(small_frame()))
        try:
            assert_editable(hit)
            editable = True
        except AssertionError:
            editable = False
        seen[label] = (type(hit).__name__, equal, editable)
    assert seen["backend=None"] == seen["FileBackend"], f"(type, equal, editable) differs by backend: {seen}"
    return str(seen["FileBackend"])


# ── return types ──────────────────────────────────────────────────────────────────────────────────────


@journey('ArrowSerializer(return_format="polars") on FileBackend returns an equal polars DataFrame')
def _() -> None:
    calls: list[int] = []

    @cache(backend=file_backend(), serializer=ArrowSerializer(return_format="polars"), l1_enabled=False, ttl=60)
    def f() -> pl.DataFrame:
        calls.append(1)
        return polars_frame()

    hit = call_twice(f, calls)
    assert isinstance(hit, pl.DataFrame), f"the hit is {type(hit).__module__}.{type(hit).__name__}"
    assert hit.equals(polars_frame()), "the hit differs from the frame the function returned"


@journey('ArrowSerializer(return_format="arrow") on FileBackend returns an equal pyarrow.Table')
def _() -> None:
    calls: list[int] = []

    @cache(backend=file_backend(), serializer=ArrowSerializer(return_format="arrow"), l1_enabled=False, ttl=60)
    def f() -> pd.DataFrame:
        calls.append(1)
        return small_frame()

    hit = call_twice(f, calls)
    assert isinstance(hit, pa.Table), f"the hit is {type(hit).__module__}.{type(hit).__name__}"
    assert hit.to_pandas().equals(small_frame()), "the hit differs from the frame the function returned"


# ── documented fast paths (docs/serializers/arrow.md) ─────────────────────────────────────────────────

# (spelling, serializer factory, whether the docs promise the mmap read on this Python)
MMAP_CONFIGS: list[tuple[str, Callable[[], Any], bool]] = [
    *((label, make, True) for label, make in ARROW_SPELLINGS),
    (
        'ArrowSerializer(return_format="arrow", compression=None)',
        lambda: ArrowSerializer(return_format="arrow", compression=None),
        PY313,
    ),
    (
        'ArrowSerializer(return_format="polars", compression=None)',
        lambda: ArrowSerializer(return_format="polars", compression=None),
        PY313,
    ),
]


def _mmap_journey(label: str, make_serializer: Callable[[], Any], promised: bool) -> None:
    when = "memory-maps the cache file" if promised else "reads the file in (no mmap before Python 3.13)"

    @journey(f"a sync FileBackend hit through {label} {when}")
    def _() -> None:
        backend = file_backend()
        buffers = spy(backend, "get_buffer")
        calls: list[int] = []

        @cache(backend=backend, serializer=make_serializer(), l1_enabled=False, ttl=60)
        def f() -> pd.DataFrame:
            calls.append(1)
            return small_frame()

        call_twice(f, calls)
        mapped = any(b is not None for b in buffers)
        assert mapped == promised, f"mmap read engaged: {mapped}, documented: {promised} (get_buffer results: {buffers})"


for _label, _make, _promised in MMAP_CONFIGS:
    _mmap_journey(_label, _make, _promised)


def _streaming_journey(label: str, make_serializer: Callable[[], Any]) -> None:
    @journey(f"a plaintext FileBackend write through {label} streams into the cache file")
    def _() -> None:
        backend = file_backend()
        streamed = spy(backend, "set_streaming")
        calls: list[int] = []

        @cache(backend=backend, serializer=make_serializer(), l1_enabled=False, ttl=60)
        def f() -> pd.DataFrame:
            calls.append(1)
            return small_frame()

        hit = call_twice(f, calls)
        assert streamed, "the write took the buffered path: set_streaming was never called"
        assert hit.equals(small_frame()), "the streamed entry reads back different"


for _label, _make in ARROW_SPELLINGS:
    _streaming_journey(_label, _make)


if PY313:  # the docs promise zero-copy "arrow" results from Python 3.13 on; earlier, the mmap journey checks the buffered read

    @journey('an uncompressed FileBackend hit through ArrowSerializer(return_format="arrow") is zero-copy: no copy of the data')
    def _() -> str:
        calls: list[int] = []
        serializer = ArrowSerializer(return_format="arrow", compression=None)
        rows = 1_000_000  # 8 MB of float64, so a copy of the payload cannot hide in the noise

        @cache(backend=file_backend(), serializer=serializer, l1_enabled=False, ttl=60)
        def f() -> pd.DataFrame:
            calls.append(1)
            return pd.DataFrame({"price": np.arange(rows, dtype="float64")})

        f()
        # Arrow's pool sees a copy into Arrow buffers; tracemalloc sees the file read into a Python bytes.
        arrow_before = pa.total_allocated_bytes()
        tracemalloc.start()
        table = f()
        python_peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        arrow_allocated = pa.total_allocated_bytes() - arrow_before
        assert len(calls) == 1, f"the function body ran {len(calls)} times for two identical calls"
        assert table.num_rows == rows, f"the hit has {table.num_rows} rows"
        assert arrow_allocated == 0, f"the hit allocated {arrow_allocated} bytes of Arrow memory"
        assert python_peak < rows * 8 // 10, f"the hit's Python allocations peaked at {python_peak} bytes for an 8 MB payload"
        return f"Python allocation peak {python_peak} bytes for an 8 MB payload"


# ── runner ────────────────────────────────────────────────────────────────────────────────────────────


REPORTED_FAIL = 3  # a journey's own failure; any other non-zero status means its interpreter died first


def run_one(index: int) -> int:
    """Run one journey in this interpreter and print its note or failure."""
    _, fn = JOURNEYS[index]
    try:
        print(fn() or "")
        return 0
    except AssertionError as e:
        print(e)
    except KeyboardInterrupt:
        raise
    except BaseException as e:  # an unexpected error breaks the promise too; a pyo3 panic is a BaseException
        frame = traceback.extract_tb(e.__traceback__)[-1]
        print(f"{type(e).__name__}: {e} ({os.path.basename(frame.filename)}:{frame.lineno})")
    return REPORTED_FAIL


def run_isolated(index: int) -> tuple[bool, str]:
    """Run journey ``index`` in a fresh interpreter: (passed, note)."""
    try:
        proc = subprocess.run(  # noqa: S603 - this interpreter running this file
            [sys.executable, "-I", __file__, str(index)], capture_output=True, text=True, timeout=TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired:
        return False, f"no result after {TIMEOUT_S} s"
    if proc.returncode in (0, REPORTED_FAIL):
        return proc.returncode == 0, proc.stdout.strip()
    last = proc.stderr.strip().splitlines()[-1:] or [""]
    return False, f"the interpreter died with status {proc.returncode}: {last[0]}"


def redis_listening() -> list[str]:
    """Every localhost address that accepts connections on Redis's default port. scripts/journeys.sh runs a
    copy of this before its build, so a busy port fails in seconds; change the two together."""
    found = []
    for family, _, _, _, addr in socket.getaddrinfo("localhost", 6379, type=socket.SOCK_STREAM):
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(addr) == 0:
                found.append(str(addr[0]))
    return found


def main() -> int:
    """Run every journey, each in a fresh interpreter, so a user's first call meets an unused cachekit and
    no journey's cache entries, spies or crash can reach another."""
    if listening := redis_listening():
        print(
            f"journeys: something listens on localhost:6379 ({', '.join(listening)}); a user's machine has no Redis",
            file=sys.stderr,
        )
        return 2
    if not __debug__:
        print("journeys: python -O strips the asserts every journey checks with", file=sys.stderr)
        return 2
    if "site-packages" not in (cachekit.__file__ or ""):
        print(f"journeys: cachekit was imported from {cachekit.__file__}, not an installed wheel", file=sys.stderr)
        return 2
    print(f"cachekit {cachekit.__version__} from {os.path.dirname(cachekit.__file__)}")
    print(f"python {sys.version.split()[0]}, pandas {pd.__version__}, pyarrow {pa.__version__}, polars {pl.__version__}\n")
    failed = 0
    for index, (promise, _) in enumerate(JOURNEYS):
        passed, note = run_isolated(index)
        failed += not passed
        print(f"{'PASS' if passed else 'FAIL'}  {promise}" + (f"\n        {note}" if note else ""))
    print(f"\njourneys: {len(JOURNEYS) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_one(int(sys.argv[1])) if len(sys.argv) > 1 else main())
