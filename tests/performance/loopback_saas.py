"""Loopback fake of the CachekitIO data plane, for ``ft_scaling_bench.py``'s ``cachekitio`` cell.

Speaks HTTP/1.1 over TLS, the one protocol the shipped client offers. One in-memory store shared by every worker thread:
GET 200 body | 404, HEAD 200 | 404, PUT and DELETE 200 ``{"success":true}``.
``GET /__stats`` returns each worker's CPU seconds so far, so a caller can tell when the fake is the
bottleneck. Each worker thread runs its own event loop on the shared listening socket; on a
free-threaded interpreter with the GIL off they serve in parallel. One connection is served by one
worker.

It measures client-side contention only, never SaaS latency.

Usage: python loopback_saas.py <port, 0 for any> <certfile> <keyfile> [workers]
Prints ``ready <port>`` on stdout once it is listening.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import ssl
import threading
import time

import h11

STORE: dict[bytes, bytes] = {}
WORKERS: list[threading.Thread] = []
_PUT_OK = b'{"success":true}'


def handle(method: bytes, path: bytes, body: bytes) -> tuple[int, bytes]:
    if path == b"/__stats":
        cpu = [time.clock_gettime(time.pthread_getcpuclockid(w.ident)) for w in WORKERS if w.ident is not None]
        return 200, json.dumps({"worker_cpu_s": cpu}).encode()
    if method == b"GET":
        value = STORE.get(path)
        return (404, b"") if value is None else (200, value)
    if method == b"HEAD":
        return (200 if path in STORE else 404), b""
    if method == b"PUT":
        STORE[path] = body
        return 200, _PUT_OK
    if method == b"DELETE":
        STORE.pop(path, None)
        return 200, _PUT_OK
    return 405, b""


class _Protocol(asyncio.Protocol):
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.Transport)
        self.transport = transport
        self.h1 = h11.Connection(h11.SERVER)
        self.request: h11.Request | None = None
        self.body = bytearray()

    def data_received(self, data: bytes) -> None:
        self.h1.receive_data(data)
        while True:
            event = self.h1.next_event()
            if event is h11.NEED_DATA or event is h11.PAUSED:
                return
            if isinstance(event, h11.Request):
                self.request, self.body = event, bytearray()
            elif isinstance(event, h11.Data):
                self.body.extend(event.data)
            elif isinstance(event, h11.EndOfMessage):
                assert self.request is not None
                status, payload = handle(self.request.method, self.request.target, bytes(self.body))
                out = self.h1.send(h11.Response(status_code=status, headers=[(b"content-length", str(len(payload)))]))
                if payload and self.request.method != b"HEAD":
                    out += self.h1.send(h11.Data(data=payload))
                out += self.h1.send(h11.EndOfMessage())
                self.transport.write(out)
                self.h1.start_next_cycle()
            elif isinstance(event, h11.ConnectionClosed):
                self.transport.close()
                return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("port", type=int, help="port to listen on, 0 for any free port")
    parser.add_argument("certfile")
    parser.add_argument("keyfile")
    parser.add_argument("workers", type=int, nargs="?", default=4, help="event-loop threads (default 4)")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error(f"port must be 0-65535, got {args.port}")
    if args.workers < 1:
        parser.error(f"workers must be at least 1, got {args.workers}")
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(args.certfile, args.keyfile)
    ctx.set_alpn_protocols(["http/1.1"])
    sock = socket.create_server(("127.0.0.1", args.port), backlog=1024)
    sock.setblocking(False)

    def serve() -> None:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(loop.create_server(_Protocol, sock=sock, ssl=ctx))
        loop.run_forever()

    WORKERS.extend(threading.Thread(target=serve, daemon=True) for _ in range(args.workers))
    for thread in WORKERS:
        thread.start()
    print(f"ready {sock.getsockname()[1]}", flush=True)
    for thread in WORKERS:
        thread.join()


if __name__ == "__main__":
    main()
