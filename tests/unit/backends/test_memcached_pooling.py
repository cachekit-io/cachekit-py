"""Connection-pooling tests for MemcachedBackend, over real sockets.

Mock-based tests replace pymemcache's clients, so they cannot see what happens
when two threads share one socket. These tests run the real HashClient against
an in-process fake memcached speaking the text protocol, so concurrent commands
really do interleave on the wire if the backend ever shares a connection.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator

import pytest

pytest.importorskip("pymemcache")

from cachekit.backends.errors import BackendError
from cachekit.backends.memcached.backend import MemcachedBackend
from cachekit.backends.memcached.config import MemcachedBackendConfig

HOLD_PREFIX = b"hold-"


class FakeMemcached:
    """Minimal memcached text-protocol server: one thread per connection.

    Supports get, set and delete. A ``get`` for a key starting with HOLD_PREFIX
    does not reply until ``release`` is set, which pins that client connection.
    """

    def __init__(self) -> None:
        self.store: dict[bytes, bytes] = {}
        self.lock = threading.Lock()
        self.release = threading.Event()
        self.held = threading.Semaphore(0)
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(64)
        self.port = self._srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self.release.set()
        self._srv.close()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        buf = b""

        def fill() -> None:
            nonlocal buf
            data = conn.recv(65536)
            if not data:
                raise EOFError
            buf += data

        try:
            while True:
                while b"\r\n" not in buf:
                    fill()
                line, buf = buf.split(b"\r\n", 1)
                parts = line.split()
                out = b""
                if parts and parts[0] == b"get":
                    if any(k.startswith(HOLD_PREFIX) for k in parts[1:]):
                        self.held.release()
                        self.release.wait()
                    for k in parts[1:]:
                        with self.lock:
                            v = self.store.get(k)
                        if v is not None:
                            out += b"VALUE %s 0 %d\r\n%s\r\n" % (k, len(v), v)
                    out += b"END\r\n"
                elif parts and parts[0] == b"set" and len(parts) in (5, 6):
                    n = int(parts[4])
                    while len(buf) < n + 2:
                        fill()
                    data, buf = buf[:n], buf[n + 2 :]
                    with self.lock:
                        self.store[parts[1]] = data
                    if len(parts) == 5:
                        out = b"STORED\r\n"
                elif parts and parts[0] == b"delete" and len(parts) in (2, 3):
                    with self.lock:
                        hit = self.store.pop(parts[1], None) is not None
                    if len(parts) == 2:
                        out = b"DELETED\r\n" if hit else b"NOT_FOUND\r\n"
                else:
                    out = b"ERROR\r\n"
                if out:
                    conn.sendall(out)
        except (EOFError, OSError):
            conn.close()


@pytest.fixture
def server() -> Iterator[FakeMemcached]:
    srv = FakeMemcached()
    yield srv
    srv.close()


def _backend(server: FakeMemcached, **overrides) -> MemcachedBackend:
    return MemcachedBackend(MemcachedBackendConfig(servers=[f"127.0.0.1:{server.port}"], **overrides))


def _assert_server_healthy(backend: MemcachedBackend) -> None:
    assert backend._client._failed_clients == {}
    assert backend._client._dead_clients == {}


def test_concurrent_ops_during_sweep_do_not_share_a_socket(server: FakeMemcached) -> None:
    """A per-key invalidation sweep racing get/set threads sees every key and no errors.

    With one shared socket per server, interleaved replies make ``delete`` raise or
    report a live key as absent (which untracks it while it stays in L2), and the
    socket errors mark the server failed. With a pool, every op gets its own socket.
    """
    backend = _backend(server)
    tracked = [f"tracked:{i}" for i in range(1000)]
    for key in tracked:
        backend.set(key, b"v-" + key.encode())

    stop = threading.Event()
    errors: list[str] = []

    def worker(tid: int) -> None:
        i = 0
        while not stop.is_set():
            key = f"worker:{tid}:{i % 50}"
            value = f"{tid}-{i}".encode()
            try:
                backend.set(key, value)
                got = backend.get(key)
                if got != value:
                    errors.append(f"thread {tid}: {key} -> {got!r}, expected {value!r}")
            except Exception as exc:  # noqa: BLE001 - every failure is the finding
                errors.append(f"thread {tid}: {type(exc).__name__}: {exc}")
            i += 1

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(3)]
    for t in threads:
        t.start()
    try:
        reported_absent = []
        for key in tracked:
            try:
                if not backend.delete(key):
                    reported_absent.append(key)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"sweep: {key}: {type(exc).__name__}: {exc}")
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=10)

    assert not errors, errors[:5]
    assert reported_absent == [], f"{len(reported_absent)} live keys reported absent"
    with server.lock:
        assert not any(k.startswith(b"tracked:") for k in server.store)
    _assert_server_healthy(backend)


def test_op_beyond_max_pool_size_raises_without_marking_server_failed(server: FakeMemcached) -> None:
    """pymemcache's pool does not wait: op N+1 on a server raises, and the server stays live."""
    pool_size = 2
    backend = _backend(server, max_pool_size=pool_size, timeout=10.0)

    results: list[object] = []
    holders = [threading.Thread(target=lambda i=i: results.append(backend.get(f"hold-{i}"))) for i in range(pool_size)]
    for t in holders:
        t.start()
    try:
        for _ in range(pool_size):
            assert server.held.acquire(timeout=5), "held ops never reached the server"

        with pytest.raises(BackendError) as excinfo:
            backend.get("one-too-many")
        assert isinstance(excinfo.value.original_exception, RuntimeError)
        _assert_server_healthy(backend)
    finally:
        server.release.set()
        for t in holders:
            t.join(timeout=10)

    assert results == [None] * pool_size
    # Released connections return to the pool, so the next op succeeds.
    backend.set("after", b"ok")
    assert backend.get("after") == b"ok"
    _assert_server_healthy(backend)
