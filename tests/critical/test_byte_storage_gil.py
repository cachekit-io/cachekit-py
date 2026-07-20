"""ByteStorage store()/retrieve() must release the GIL (cachekit-core#45).

The Rust FFI detaches from the GIL (PyO3 ``Python::detach``) for the whole
compress/hash/serialize core, so a large store() no longer freezes every other
Python thread for the full compression duration.

Proof technique: a ticker thread timestamps every ~1ms. If the GIL were held
for the whole FFI call, the ticker could only run at the call's bytecode
boundaries — never in the *interior* of the call window. We therefore assert
ticker stamps strictly inside the middle 50% of the call window; the 25%
margins on both sides dwarf any boundary-slice scheduling (GIL switch interval
is ~5ms, margins are >=25ms). Deterministic in both directions: GIL held ->
zero interior stamps possible; GIL released -> hundreds.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from cachekit._rust_serializer import ByteStorage

# Incompressible payload, large enough that store/retrieve take >=100ms even on
# fast hardware (msgpack envelope encoding dominates, ~150MB/s). 64MB keeps the
# critical suite fast and peak test memory under ~300MB.
_PAYLOAD_BYTES = 64 * 1024 * 1024
_MIN_CALL_SECONDS = 0.05  # below this the interior-window proof loses its margin


@pytest.fixture(scope="module")
def payload() -> bytes:
    return os.urandom(_PAYLOAD_BYTES)


def _stamps_during(call) -> tuple[list[float], float, float]:
    """Run *call* while a ticker thread timestamps; return (stamps, t0, t1)."""
    stamps: list[float] = []
    stop = threading.Event()

    def ticker() -> None:
        while not stop.is_set():
            stamps.append(time.monotonic())
            time.sleep(0.001)

    thread = threading.Thread(target=ticker, daemon=True)
    thread.start()
    time.sleep(0.02)  # let the ticker reach steady state
    t0 = time.monotonic()
    call()
    t1 = time.monotonic()
    stop.set()
    thread.join(timeout=5)
    return stamps, t0, t1


def _assert_gil_released(stamps: list[float], t0: float, t1: float, op: str) -> None:
    duration = t1 - t0
    assert duration >= _MIN_CALL_SECONDS, (
        f"{op} finished in {duration * 1000:.0f}ms — too fast for the interior-window "
        f"proof; bump _PAYLOAD_BYTES so the call takes >={_MIN_CALL_SECONDS * 1000:.0f}ms"
    )
    lo = t0 + duration * 0.25
    hi = t1 - duration * 0.25
    interior = [s for s in stamps if lo < s < hi]
    assert len(interior) >= 3, (
        f"{op} held the GIL: ticker made no progress inside the middle 50% of a "
        f"{duration * 1000:.0f}ms call window ({len(interior)} interior stamps)"
    )


def test_store_releases_gil(payload: bytes) -> None:
    storage = ByteStorage(None)
    stamps, t0, t1 = _stamps_during(lambda: storage.store(payload, None))
    _assert_gil_released(stamps, t0, t1, "store()")


def test_retrieve_releases_gil(payload: bytes) -> None:
    storage = ByteStorage(None)
    envelope = storage.store(payload, None)
    stamps, t0, t1 = _stamps_during(lambda: storage.retrieve(envelope))
    _assert_gil_released(stamps, t0, t1, "retrieve()")


def test_roundtrip_unchanged_by_gil_release(payload: bytes) -> None:
    """GIL release is a threading change only — bytes must round-trip identically."""
    storage = ByteStorage(None)
    data, fmt = storage.retrieve(storage.store(payload, None))
    assert data == payload
    assert fmt == "msgpack"


def test_concurrent_stores_are_correct(payload: bytes) -> None:
    """Two threads storing through one ByteStorage while detached from the GIL
    must not corrupt each other (inner metrics state is Mutex-guarded)."""
    storage = ByteStorage(None)
    chunks = [payload[: 8 * 1024 * 1024], payload[8 * 1024 * 1024 : 16 * 1024 * 1024]]
    results: dict[int, bytes] = {}

    def worker(idx: int) -> None:
        envelope = storage.store(chunks[idx], None)
        data, _ = storage.retrieve(envelope)
        results[idx] = data

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert results[0] == chunks[0]
    assert results[1] == chunks[1]
