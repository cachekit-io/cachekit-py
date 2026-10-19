"""CRITICAL PATH TEST: a bare @cache with nothing configured caches in L1 and connects to nothing.

The subprocess tests run cachekit in a fresh interpreter: no conftest, no DI patching, the real default
provider, and an environment with no REDIS_URL and no CACHEKIT_* variable. The shared ``nothing_configured``
fixture opts this module out of the root Redis fixture, and the in-process test checks the same path with it.

The child refuses every outbound connection before it imports cachekit. Releases before 0.23.0 dialled Redis
at localhost:6379 with nothing configured, and every CI job serves Redis there, so a test that only checked
for a hit would pass on that defect. Refusing the connection makes it fail whatever listens locally.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import cachekit
from cachekit import cache
from tests.fixtures.zero_config import unconfigured_environ

pytestmark = [pytest.mark.critical, pytest.mark.usefixtures("nothing_configured", "no_redis_built")]

_CHILD = """
import asyncio, errno, json, logging, socket, sys, warnings

attempts = []

def refuse(self, address, *args, **kwargs):
    attempts.append(repr(address))
    raise ConnectionRefusedError(errno.ECONNREFUSED, f"outbound connection refused: {address!r}")

def refuse_ex(self, address):
    attempts.append(repr(address))
    return errno.ECONNREFUSED

socket.socket.connect = refuse
socket.socket.connect_ex = refuse_ex

logged = []

class Capture(logging.Handler):
    def emit(self, record):
        if record.name.startswith("cachekit"):
            logged.append(f"{record.levelname} {record.name}: {record.getMessage()}")

logging.getLogger().addHandler(Capture(logging.WARNING))
logging.getLogger("cachekit").setLevel(logging.WARNING)
warnings.simplefilter("always")

with warnings.catch_warnings(record=True) as warned:
    from cachekit import cache

    runs = 0

    if sys.argv[1] == "sync":

        @cache
        def square(x):
            global runs
            runs += 1
            return x * x

        results = [square(4) for _ in range(20)]
    else:

        @cache
        async def square(x):
            global runs
            runs += 1
            return x * x

        async def main():
            return [await square(4) for _ in range(20)]

        results = asyncio.run(main())

print(json.dumps({
    "runs": runs,
    "results": sorted(set(results)),
    "hits": square.cache_info().hits,
    "attempts": attempts,
    "logged": logged,
    "warned": [f"{w.category.__name__}: {w.message}" for w in warned if "cachekit" in w.filename],
}))
"""


def _run_child(tmp_path: Path, mode: str) -> dict:
    env = unconfigured_environ()
    env["PYTHONPATH"] = str(Path(cachekit.__file__).resolve().parents[1])  # the cachekit this suite tests
    proc = subprocess.run(  # noqa: S603 - trusted: sys.executable + literal code
        [sys.executable, "-c", _CHILD, mode], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, f"child failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_bare_cache_with_nothing_configured_caches_in_l1_and_connects_to_nothing(tmp_path: Path, mode: str) -> None:
    report = _run_child(tmp_path, mode)

    assert report["attempts"] == [], "cachekit opened a connection with nothing configured"
    assert report["logged"] == [], "cachekit logged a warning with nothing configured"
    assert report["warned"] == [], "cachekit warned with nothing configured"
    assert report["results"] == [16]
    assert report["runs"] == 1, "the body did not run exactly once in 20 calls"
    assert report["hits"] == 19


def test_bare_cache_in_process_with_nothing_configured(nothing_configured) -> None:
    runs = 0

    @cache
    def double(x: int) -> int:
        nonlocal runs
        runs += 1
        return 2 * x

    assert [double(3) for _ in range(20)] == [6] * 20
    assert runs == 1
    assert double.cache_info().l1_hits == 19
    nothing_configured.assert_not_called()
