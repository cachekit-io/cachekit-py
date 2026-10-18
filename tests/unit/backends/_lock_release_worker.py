"""One process of the cross-process lock-release tests: ``asyncio.run`` of a single decorated miss over
CachekitIO, against the loopback fake SaaS in the test, then exit.

    python _lock_release_worker.py <port> <go-file> <after>

The decorated function waits until ``<go-file>`` exists, so the test controls how long the lock is held.
``<after>`` is what ``main`` does once the call returns: ``exit`` (nothing) or ``close_http_clients``.
The real client lease machinery runs; only the connection pool is pointed at the plain-HTTP fake.

Everything happens under ``__main__``: pytest's ``--doctest-modules`` imports this file.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any


def run(port: str, go: str, after: str) -> None:
    import urllib3

    from cachekit import cache
    from cachekit.backends.cachekitio import CachekitIOBackend
    from cachekit.backends.cachekitio import client as client_module
    from cachekit.backends.cachekitio.client import close_http_clients

    def loopback_pool(config: Any) -> Any:  # the shipped pool settings, on the fake's plain-HTTP port
        return urllib3.PoolManager(maxsize=config.connection_pool_size).connection_from_url(f"http://127.0.0.1:{port}")

    client_module._connection_pool = loopback_pool
    backend = CachekitIOBackend(api_url="https://api.cachekit.io", api_key="ck_test_lock_release")  # pragma: allowlist secret

    @cache(backend=backend, ttl=60, l1_enabled=False)
    async def compute(x: int) -> int:
        while not os.path.exists(go):
            await asyncio.sleep(0.01)
        return x * 2

    async def main() -> None:
        print(await compute(21), flush=True)
        if after == "close_http_clients":
            close_http_clients()

    asyncio.run(main())


if __name__ == "__main__":
    import sys

    run(*sys.argv[1:4])
