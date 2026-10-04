"""A fake transport for CachekitIOBackend tests: the backend's real HTTPClient, on a pool that answers from a handler.

Everything above the socket runs as in production: the backend builds the path and headers, the client merges
its own headers in, and status handling, retries and error classification see a real urllib3 response or
exception. Only urllib3's connection pool is replaced, so nothing here checks bytes on the wire; tests that
need those run against tests/performance/loopback_saas.py.

    def handler(request: FakeRequest) -> HTTPResponse:
        return response(404) if request.method == "GET" else response(200, json={"success": True})

    backend, pool = fake_backend(handler)
    backend.get("k")
    assert pool.requests[0].path == "/v1/cache/k"

A handler raises a urllib3 exception to simulate a transport failure, as urllib3 itself does with
``retries=False``: ``ReadTimeoutError(None, url, "timed out")``, ``NewConnectionError(None, "refused")``,
``ProtocolError("Connection aborted.", ConnectionResetError())``.
"""

from __future__ import annotations

import json as jsonlib
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from urllib3 import HTTPHeaderDict, HTTPResponse

from cachekit.backends.cachekitio.backend import CachekitIOBackend
from cachekit.backends.cachekitio.client import HTTPClient

TEST_API_URL = "https://api.cachekit.io"
TEST_API_KEY = "ck_test_abc123"  # pragma: allowlist secret — fake key, test fixture


@dataclass
class FakeRequest:
    """One request as the client handed it to the pool."""

    method: str
    path: str
    headers: HTTPHeaderDict
    body: bytes | None
    # The rest of urlopen's keyword arguments (retries, redirect, pool_timeout).
    options: dict[str, Any]

    def json(self) -> Any:
        assert self.body is not None, "request has no body"
        return jsonlib.loads(self.body)


Handler = Callable[[FakeRequest], HTTPResponse]


def response(status: int = 200, body: bytes = b"", *, headers: dict[str, str] | None = None, json: Any = None) -> HTTPResponse:
    """A urllib3 response, its body already read as the client reads every one (preload_content)."""
    if json is not None:
        body = jsonlib.dumps(json).encode()
        headers = {"Content-Type": "application/json", **(headers or {})}
    return HTTPResponse(body=body, status=status, headers=headers, preload_content=True)


class FakePool:
    """Stands in for urllib3's HTTPSConnectionPool: records each request and answers from ``handler``.

    Thread-safe as far as the record goes, so concurrent tests can share one.
    """

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[FakeRequest] = []
        self.closed = False
        self._lock = threading.Lock()

    def urlopen(
        self, method: str, url: str, body: bytes | None = None, headers: dict[str, str] | None = None, **options: Any
    ) -> HTTPResponse:
        request = FakeRequest(method, url, HTTPHeaderDict(headers or {}), body, options)
        with self._lock:
            self.requests.append(request)
        return self.handler(request)

    def close(self) -> None:
        self.closed = True


def fake_client(handler: Handler, backend: CachekitIOBackend) -> tuple[HTTPClient, FakePool]:
    """A real HTTPClient for ``backend``'s config whose pool is a FakePool."""
    client = HTTPClient(backend._config)
    pool = FakePool(handler)
    client.pool = pool  # type: ignore[assignment]
    return client, pool


def fake_backend(handler: Handler, *, api_url: str = TEST_API_URL, **kwargs: Any) -> tuple[CachekitIOBackend, FakePool]:
    """A CachekitIOBackend that sends every request, sync or async, to ``handler``.

    The backend gets a lease of its own, never the process's shared one, so no other backend sees the fake.
    """
    kwargs.setdefault("api_key", TEST_API_KEY)
    backend = CachekitIOBackend(api_url=api_url, **kwargs)
    client, pool = fake_client(handler, backend)
    backend._sync_lease = SimpleNamespace(pid=os.getpid(), client=client)  # type: ignore[assignment]
    return backend, pool
