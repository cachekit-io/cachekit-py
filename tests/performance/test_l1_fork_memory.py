"""Memory guard for the L1 at-fork hook: a forked child must not copy its parent's L1.

The hook gives every cache a fresh, empty state in the child. Freeing the inherited state there
would write to every entry's memory and turn the parent's L1 pages into private copies, at a cost
close to the size of a warm L1 per child. The child's private dirty memory right after fork() is
the deterministic measure: a few MB for the interpreter's own writes, independent of L1 size.
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

_CHILD = textwrap.dedent(
    f"""
    import os
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
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.write(w, f"{{private_dirty_kib()}} {{len(cache._state.cache)}}".encode())  # the hook emptied it
        os._exit(0)
    os.close(w)
    print(os.read(r, 64).decode())
    os.waitpid(pid, 0)
    """
)


def test_forked_child_shares_its_parents_l1_pages() -> None:
    proc = subprocess.run(  # noqa: S603 - trusted: sys.executable + literal code
        [sys.executable, "-c", _CHILD], capture_output=True, text=True, timeout=300
    )
    assert proc.returncode == 0, proc.stderr
    private_kib, entries = (int(field) for field in proc.stdout.split())
    assert entries == 0  # the hook ran in the child
    assert private_kib / 1024 < _L1_MB / 4, f"forked child copied {private_kib / 1024:.1f} MiB of a {_L1_MB} MiB L1"
