"""One process of the cross-process lock-release tests: ``asyncio.run`` of a single decorated miss over
CachekitIO, against the loopback fake SaaS in the test, then exit.

    python _lock_release_worker.py <port> <go-file> <after>

The decorated function waits until ``<go-file>`` exists, so the test controls how long the lock is held.
``<after>`` is what ``main`` does once the call returns: ``exit`` (nothing) or ``close_async_client``.
The real client lease machinery runs; only the base URL is pointed at the fake.

Everything happens under ``__main__``: pytest's ``--doctest-modules`` imports this file.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any


def run(port: str, go: str, after: str) -> None:
    from cachekit import cache
    from cachekit.backends.cachekitio import CachekitIOBackend
    from cachekit.backends.cachekitio import client as client_module
    from cachekit.backends.cachekitio.client import close_async_client

    real_client_kwargs = client_module._client_kwargs

    def loopback_client_kwargs(*args: Any) -> dict[str, Any]:  # forwards whatever _client_kwargs takes
        return {**real_client_kwargs(*args), "base_url": f"http://127.0.0.1:{port}", "http2": False}

    client_module._client_kwargs = loopback_client_kwargs
    backend = CachekitIOBackend(api_url="https://api.cachekit.io", api_key="ck_test_lock_release")  # pragma: allowlist secret

    @cache(backend=backend, ttl=60, l1_enabled=False)
    async def compute(x: int) -> int:
        while not os.path.exists(go):
            await asyncio.sleep(0.01)
        return x * 2

    async def main() -> None:
        print(await compute(21), flush=True)
        if after == "close_async_client":
            await close_async_client()

    asyncio.run(main())


if __name__ == "__main__":
    import sys

    run(*sys.argv[1:4])
