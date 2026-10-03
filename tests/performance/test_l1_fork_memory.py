"""Memory guards for the L1 at-fork hook: a forked child must not end up owning a copy of its
parent's L1 beside its own.

The hook gives every cache a fresh, empty state in the child and keeps the inherited states
referenced, so fork() itself frees nothing. Freeing them there would write to every entry's
memory and copy the parent's L1 pages into the child inside fork(). Kept for good, they would be
copied anyway, later: each page the parent rewrites stops being shared, and the child is left
with a private copy nothing can reach, beside its own L1. So the child frees them when it first
uses its L1. That copies the pages once, and the child's own L1 then fills the memory it freed.

Both guards read the child's private dirty memory (/proc/self/smaps_rollup), the deterministic
measure, on a heap where the L1 values sit between other allocations, as in an application, so
no free can shrink the heap and hide the cost.
"""

from __future__ import annotations

import subprocess
import sys
import sysconfig
import textwrap
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.performance,
    pytest.mark.slow,
    pytest.mark.skipif(not Path("/proc/self/smaps_rollup").exists(), reason="needs Linux /proc/self/smaps_rollup"),
]

_L1_MB = 16

_PARENT = textwrap.dedent(
    f"""
    import gc, os, time
    from cachekit import l1_cache
    from cachekit.l1_cache import L1CacheManager

    L1_ENTRIES = {_L1_MB} * 1024  # of 1 KiB each

    def private_dirty_kib():
        with open("/proc/self/smaps_rollup") as f:
            for line in f:
                if line.startswith("Private_Dirty:"):
                    return int(line.split()[1])
        raise RuntimeError("Private_Dirty not found")

    manager = L1CacheManager(default_max_memory_mb={_L1_MB * 2})  # held: the at-fork hook sees live managers only
    cache = manager.get_cache("fork-memory")
    elsewhere = []  # the rest of an application's heap, between L1's values: no free can shrink the heap
    for i in range(L1_ENTRIES):
        cache.put(f"key-{{i}}", os.urandom(1024), redis_ttl=600)
        elsewhere.append(os.urandom(1024))
    """
)

_AT_FORK = _PARENT + textwrap.dedent(
    """
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.write(w, f"{private_dirty_kib()} {len(cache._state.cache)}".encode())  # the hook emptied it
        os._exit(0)
    os.close(w)
    print(os.read(r, 64).decode())
    os.waitpid(pid, 0)
    """
)

_CHILD_FILLS_AFTER_PARENT_CHURN = _PARENT + textwrap.dedent(
    """
    manager.start_background_cleanup(interval_seconds=600)  # as get_l1_cache_manager() does
    # As a prefork server should: unfrozen, the child's collections write to every object the parent
    # tracks, a cost of any forked child that is not L1's, and on 3.14 about as large as this L1.
    gc.freeze()
    churned_r, churned_w = os.pipe()
    ready_r, ready_w = os.pipe()
    result_r, result_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        baseline = private_dirty_kib()  # right after fork, before anything is freed
        inherited = list(l1_cache._inherited_states)  # their entries, not the list: a state leaves it before it is freed
        cache.put("child", b"v", redis_ttl=600)  # the child uses its L1: the release starts
        deadline = time.monotonic() + 10
        while any(state.cache for state in inherited) and time.monotonic() < deadline:
            time.sleep(0.05)
        freed = not any(state.cache for state in inherited)
        released = private_dirty_kib()
        os.write(ready_w, b"x")
        os.read(churned_r, 1)  # the parent has rewritten its L1
        for i in range(L1_ENTRIES):  # the child's own L1, as large as its parent's
            cache.put(f"own-{i}", os.urandom(1024), redis_ttl=600)
        end = private_dirty_kib()
        os.write(result_w, f"{released - baseline} {end - baseline} {int(freed)}".encode())
        os._exit(0)
    os.read(ready_r, 1)
    cache.clear()  # the parent frees every entry: each page it writes stops being shared
    for i in range(1024):
        cache.put(f"again-{i}", os.urandom(1024), redis_ttl=600)
    os.write(churned_w, b"x")
    print(os.read(result_r, 64).decode())
    os.waitpid(pid, 0)
    """
)


def _run(code: str) -> list[int]:
    proc = subprocess.run(  # noqa: S603 - trusted: sys.executable + literal code
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=300
    )
    assert proc.returncode == 0, proc.stderr
    return [int(field) for field in proc.stdout.split()]


def test_forked_child_copies_nothing_at_fork() -> None:
    """fork() returns before anything is freed: a few MB of the interpreter's own writes, right after it."""
    private_kib, entries = _run(_AT_FORK)
    assert entries == 0  # the hook ran in the child
    assert private_kib / 1024 < _L1_MB / 4, f"forked child copied {private_kib / 1024:.1f} MiB of a {_L1_MB} MiB L1"


@pytest.mark.skipif(
    bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
    reason="bound calibrated for pymalloc and glibc malloc; the free-threaded build allocates with mimalloc",
)
def test_child_that_fills_its_own_l1_keeps_no_copy_of_its_parents() -> None:
    """The child's end state, measured from right after fork: it first uses its L1, its parent then
    rewrites its own, and the child fills its L1 as large as its parent's.

    Freed, the inherited states cost the child one copy of their pages, about 2.3 times the L1
    because each free also writes the allocator's bookkeeping in the values around it, and its own
    L1 then fills that memory: about 2.5 times the L1 in all. Kept, the parent's rewrite turns them
    into a private copy just as large that nothing can reach, and the child's own L1 comes on top:
    about 3.8 times. Measured on CPython 3.10 to 3.14 with glibc 2.39.
    """
    released_kib, end_kib, freed = _run(_CHILD_FILLS_AFTER_PARENT_CHURN)
    released_mib, end_mib = released_kib / 1024, end_kib / 1024
    assert end_mib < 3 * _L1_MB, (
        f"a child that filled a {_L1_MB} MiB L1 grew {end_mib:.1f} MiB since fork ({released_mib:.1f} MiB of it when "
        "it freed what it inherited): it keeps a copy of its parent's L1 beside its own"
    )
    assert freed  # the release ran to its end before the parent rewrote its L1
