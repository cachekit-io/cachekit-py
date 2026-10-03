"""Memory guards for the L1 at-fork hook: a forked child must not end up owning a copy of its
parent's L1.

The hook gives every cache a fresh, empty state in the child and keeps the inherited states
referenced, so fork() itself frees nothing. Freeing them there would write to every entry's
memory and copy the parent's L1 pages into the child inside fork(). Kept for good, they would be
copied anyway, later: each page the parent rewrites stops being shared, and the child is left
with a private copy nothing can reach. So the child's cleanup thread frees them when it starts.

Both guards read the child's private dirty memory (/proc/self/smaps_rollup), the deterministic
measure, on a heap where the L1 values sit between other allocations, as in an application, so
no free can shrink the heap and hide the cost.
"""

from __future__ import annotations

import subprocess
import sys
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
    import os, time
    from cachekit import l1_cache
    from cachekit.l1_cache import L1CacheManager

    def private_dirty_kib():
        with open("/proc/self/smaps_rollup") as f:
            for line in f:
                if line.startswith("Private_Dirty:"):
                    return int(line.split()[1])
        raise RuntimeError("Private_Dirty not found")

    manager = L1CacheManager(default_max_memory_mb={_L1_MB * 2})  # held: the at-fork hook sees live managers only
    cache = manager.get_cache("fork-memory")
    elsewhere = []  # the rest of an application's heap, between L1's values: no free can shrink the heap
    for i in range({_L1_MB} * 1024):
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

_AFTER_PARENT_CHURN = _PARENT + textwrap.dedent(
    """
    manager.start_background_cleanup(interval_seconds=600)  # the child's first put starts its own
    churned_r, churned_w = os.pipe()
    ready_r, ready_w = os.pipe()
    result_r, result_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        inherited = list(l1_cache._inherited_states)  # the entries, not the list: a state leaves it before it is freed
        cache.put("child", b"v", redis_ttl=600)  # the child uses its L1: its cleanup thread starts
        deadline = time.monotonic() + 10
        while any(state.cache for state in inherited) and time.monotonic() < deadline:
            time.sleep(0.05)
        before = private_dirty_kib()
        os.write(ready_w, b"x")
        os.read(churned_r, 1)  # the parent has rewritten its L1
        os.write(result_w, f"{before} {private_dirty_kib()} {len(l1_cache._inherited_states)}".encode())
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


def test_child_keeps_no_copy_of_pages_its_parent_rewrites() -> None:
    """Once the child uses its L1, its cleanup thread frees the inherited states, so a parent that
    rewrites its L1 afterwards no longer leaves the child holding private copies of those pages."""
    before_kib, after_kib, still_inherited = _run(_AFTER_PARENT_CHURN)
    assert still_inherited == 0  # the child's cleanup thread freed them
    grown_mib = (after_kib - before_kib) / 1024
    assert grown_mib < _L1_MB / 4, f"the parent's churn left the child {grown_mib:.1f} MiB of unreachable copies"
