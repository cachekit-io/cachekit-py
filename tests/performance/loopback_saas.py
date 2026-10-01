"""Loopback fake of the CachekitIO data plane, for ``ft_scaling_bench.py``'s ``cachekitio`` cell.

Speaks HTTP/2 and HTTP/1.1 over TLS, chosen by ALPN as at the real edge, so the shipped client
negotiates what it would in production. One in-memory store shared by every worker thread:
GET 200 body | 404, HEAD 200 | 404, PUT 200 ``{"success":true}``, DELETE 200 | 404.
``GET /__stats`` returns each worker's CPU seconds so far, so a caller can tell when the fake is the
bottleneck. Each worker thread runs its own event loop on the shared listening socket; on a
free-threaded interpreter with the GIL off they serve in parallel. One connection is served by one
worker, so a client multiplexing everything over one HTTP/2 connection is bounded by one worker.
Bodies must fit one HTTP/2 frame and the initial flow-control window (well under 16 KiB).

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
import sys
import threading
import time

import h2.config
import h2.connection
import h2.events
import h2.exceptions
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
        return (200, _PUT_OK) if STORE.pop(path, None) is not None else (404, b"")
    return 405, b""


class _Protocol(asyncio.Protocol):
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.Transport)
        self.transport = transport
        self.h2 = transport.get_extra_info("ssl_object").selected_alpn_protocol() == "h2"
        if self.h2:
            self.conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False, header_encoding=None))
            self.conn.initiate_connection()
            self.streams: dict[int, tuple[dict[bytes, bytes], bytearray]] = {}
            transport.write(self.conn.data_to_send())
        else:
            self.h1 = h11.Connection(h11.SERVER)
            self.request: h11.Request | None = None
            self.body = bytearray()

    def data_received(self, data: bytes) -> None:
        if self.h2:
            self._h2_received(data)
        else:
            self._h1_received(data)

    def _h2_received(self, data: bytes) -> None:
        try:
            self._h2_events(data)
        except h2.exceptions.ProtocolError as exc:
            # A client that breaks the protocol (a corrupt HPACK block, a stream id out of order) lands here.
            print(f"loopback_saas: HTTP/2 protocol error from the client, connection closed: {exc!r}", file=sys.stderr)
            self.transport.write(self.conn.data_to_send())  # the GOAWAY h2 queued, so the client sees why
            self.transport.close()

    def _h2_events(self, data: bytes) -> None:
        for event in self.conn.receive_data(data):
            if isinstance(event, h2.events.RequestReceived):
                self.streams[event.stream_id] = (dict(event.headers), bytearray())  # type: ignore[arg-type]
            elif isinstance(event, h2.events.DataReceived):
                self.streams[event.stream_id][1].extend(event.data)
                self.conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
            elif isinstance(event, h2.events.StreamEnded):
                headers, body = self.streams.pop(event.stream_id)
                method = headers[b":method"]
                status, payload = handle(method, headers[b":path"], bytes(body))
                send_body = bool(payload) and method != b"HEAD"
                response = [(b":status", str(status).encode()), (b"content-length", str(len(payload)).encode())]
                self.conn.send_headers(event.stream_id, response, end_stream=not send_body)
                if send_body:
                    self.conn.send_data(event.stream_id, payload, end_stream=True)
            elif isinstance(event, h2.events.StreamReset):
                self.streams.pop(event.stream_id, None)
            elif isinstance(event, h2.events.ConnectionTerminated):
                self.transport.close()
        self.transport.write(self.conn.data_to_send())

    def _h1_received(self, data: bytes) -> None:
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
    ctx.set_alpn_protocols(["h2", "http/1.1"])
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
