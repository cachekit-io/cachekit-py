"""The miss-path lock release survives process exit (LAB-7064).

The decorator's miss path releases the CachekitIO lock in the background, so the caller does not wait on
the DELETE. These tests pin that it still lands when the decorated call is the last thing a process does:
each process is ``asyncio.run(main())`` against a loopback fake SaaS that records every lock request.

(a) The holder's DELETE /lock reaches the server before the process exits.
(b) A second process waiting on the same key's lock acquires it within about a second of that DELETE, not
    at its 5 s blocking timeout, whether the holder exits straight after the call or calls
    ``close_async_client()`` first.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from cachekit.backends.cachekitio.backend import LOCK_ID_HEADER

pytestmark = pytest.mark.unit

_WORKER = Path(__file__).with_name("_lock_release_worker.py")
_DEADLINE_S = 30.0  # hang guard only


class _FakeSaaS(ThreadingHTTPServer):
    """Entries and locks as spec/saas-api.md describes, with a log of every lock grant, refusal and release."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.mutex = threading.Lock()
        self.store: dict[str, bytes] = {}
        self.locks: dict[str, str] = {}  # key -> holder lock_id; the 30 s lease never lapses within a test
        self.events: list[tuple[float, str, str]] = []  # (monotonic time, event, lock_id)

    def record(self, event: str, lock_id: str = "") -> None:
        self.events.append((time.monotonic(), event, lock_id))

    def first(self, event: str) -> tuple[float, str, str] | None:
        with self.mutex:
            return next((e for e in self.events if e[1] == event), None)

    def wait_for(self, event: str) -> tuple[float, str, str]:
        deadline = time.monotonic() + _DEADLINE_S
        while (found := self.first(event)) is None:
            assert time.monotonic() < deadline, f"no {event} within {_DEADLINE_S}s: {self.events}"
            time.sleep(0.01)
        return found


class _Handler(BaseHTTPRequestHandler):
    server: _FakeSaaS

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — stdlib signature
        pass

    def _reply(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self) -> tuple[str, bool]:
        path = self.path.removeprefix("/v1/cache/")
        key, _, suffix = path.partition("/")
        return key, suffix == "lock"

    def do_GET(self) -> None:
        key, _ = self._route()
        with self.server.mutex:
            value = self.server.store.get(key)
        self._reply(404) if value is None else self._reply(200, value)

    def do_PUT(self) -> None:
        key, _ = self._route()
        body = self.rfile.read(int(self.headers["Content-Length"]))
        with self.server.mutex:
            self.server.store[key] = body
        self._reply(200)

    def do_POST(self) -> None:
        key, is_lock = self._route()
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        assert is_lock
        with self.server.mutex:
            if key in self.server.locks:
                self.server.record("refused")
                lock_id = None
            else:
                lock_id = self.server.locks[key] = uuid.uuid4().hex
                self.server.record("granted", lock_id)
        self._reply(200, json.dumps({"lock_id": lock_id}).encode())

    def do_DELETE(self) -> None:
        key, is_lock = self._route()
        lock_id = self.headers.get(LOCK_ID_HEADER, "")
        with self.server.mutex:
            if is_lock and self.server.locks.get(key) == lock_id:
                del self.server.locks[key]
                self.server.record("released", lock_id)
        self._reply(200)


@pytest.fixture
def saas() -> Iterator[_FakeSaaS]:
    server = _FakeSaaS()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _spawn(saas: _FakeSaaS, go: Path, after: str) -> subprocess.Popen[str]:
    argv = [sys.executable, str(_WORKER), str(saas.server_address[1]), str(go), after]
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)  # noqa: S603 — this file's worker


def _finish(proc: subprocess.Popen[str]) -> str:
    out, err = proc.communicate(timeout=_DEADLINE_S)
    assert proc.returncode == 0, err
    return out


def test_release_lands_when_the_miss_is_the_last_await(saas: _FakeSaaS, tmp_path: Path) -> None:
    go = tmp_path / "go"
    go.touch()

    assert _finish(_spawn(saas, go, "exit")).strip() == "42"

    (_, _, granted) = saas.wait_for("granted")
    released = saas.first("released")
    assert released is not None, f"the process exited holding the lock: {saas.events}"
    assert released[2] == granted


@pytest.mark.parametrize("after", ["exit", "close_async_client"])
def test_waiter_acquires_promptly_after_the_holder_exits(saas: _FakeSaaS, tmp_path: Path, after: str) -> None:
    go = tmp_path / "go"
    holder = _spawn(saas, go, after)
    (_, _, holder_lock) = saas.wait_for("granted")
    waiter = _spawn(saas, go, "exit")
    saas.wait_for("refused")  # the waiter is polling the held lock

    go.touch()
    _finish(holder)
    assert _finish(waiter).strip() == "42"  # served from the holder's entry

    with saas.mutex:
        events = list(saas.events)
    released_at = next(t for t, event, lock_id in events if event == "released" and lock_id == holder_lock)
    waiter_granted = [t for t, event, lock_id in events if event == "granted" and lock_id != holder_lock]
    assert waiter_granted, f"the waiter never got the lock, so it timed out: {events}"
    assert waiter_granted[0] - released_at < 1.0
