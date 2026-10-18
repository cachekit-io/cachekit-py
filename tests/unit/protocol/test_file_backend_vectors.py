"""FileBackend on-disk format against the protocol test vectors.

Fixture: tests/unit/protocol/fixtures/file-backend.json, vendored from
cachekit-io/protocol @ 4b8fddb2120b9e3355d7fc9d593130dba8a345ac (vectors 1.2.0,
sha256 b7b0c51935a3a00ab340005ae1e091797b14938f92eecae23346bdccf0d12af4).
Regenerate ONLY by re-copying from the protocol repo — never by hand.

spec/file-backend-format.md, "Version and flag negotiation": a nonzero reserved byte or
flag can indicate a future payload transform, so a reader that does not implement it
MUST return a miss and MUST NOT delete, rewrite, or return the payload. Every read path
is checked against that, including the two expired-and-flagged entries: the spec checks flags
before expiry, and its MUST NOT delete has no expiry exception.

A vector carrying ``reader_now_unix_seconds`` is read with the clock frozen there, which equals
its expiry: the spec's entry "is expired when the reader wall clock reaches that timestamp", so
``expired_entry`` must miss at that exact instant and the expired-and-flagged entries must still
be preserved. The other vectors are read at the real wall clock.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import time_machine

from cachekit.backends.file.backend import FileBackend
from cachekit.backends.file.config import FileBackendConfig

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "file-backend.json"
FIXTURE_SHA256 = "b7b0c51935a3a00ab340005ae1e091797b14938f92eecae23346bdccf0d12af4"  # pragma: allowlist secret

VECTORS: dict[str, dict[str, Any]] = {v["name"]: v for v in json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["vectors"]}

PRESERVE = [v for v in VECTORS.values() if v["reader_action"] == "miss_preserve"]
RETURN_PAYLOAD = [v for v in VECTORS.values() if v["reader_action"] == "return_payload"]


async def _refresh_ttl(b: FileBackend, k: str) -> Any:
    return await b.refresh_ttl(k, 3600)


READ_PATHS: list[tuple[Callable[[FileBackend, str], Any], Any]] = [
    (FileBackend.get, None),
    (FileBackend.get_buffer, None),
    (FileBackend.exists, False),
    (FileBackend.get_ttl, None),
    (_refresh_ttl, False),
]


def _place(tmp_path: Path, vector: dict[str, Any]) -> tuple[FileBackend, Path]:
    """Write the vector's file_hex byte-for-byte under its filename."""
    backend = FileBackend(FileBackendConfig(cache_dir=tmp_path / "cache"))
    path = Path(backend._key_to_path(vector["key_utf8"]))
    assert path.name == vector["filename"]  # the SDK names the file the way the vector does
    path.write_bytes(bytes.fromhex(vector["file_hex"]))
    return backend, path


def _reader_clock(vector: dict[str, Any]) -> contextlib.AbstractContextManager[Any]:
    """The clock the vector is read at: frozen at its ``reader_now_unix_seconds``, else the real one."""
    if "reader_now_unix_seconds" not in vector:
        return contextlib.nullcontext()
    return time_machine.travel(vector["reader_now_unix_seconds"], tick=False)


def test_fixture_integrity() -> None:
    """The vendored fixture is byte-identical to the pinned protocol revision."""
    digest = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    assert digest == FIXTURE_SHA256, (
        f"fixtures/file-backend.json sha256 {digest} != pinned {FIXTURE_SHA256}. "
        "If the protocol vectors were intentionally updated, refresh the pin."
    )


def test_vector_actions() -> None:
    """A fixture update that adds, drops or re-classifies a vector must be a conscious change."""
    assert {n: v["reader_action"] for n, v in VECTORS.items()} == {
        "permanent_ascii": "return_payload",
        "future_expiry": "return_payload",
        "unknown_flag_preserved": "miss_preserve",
        "reserved_nonzero_preserved": "miss_preserve",
        "expired_entry": "miss_expired",
        "empty_payload": "return_payload",
        "binary_payload": "return_payload",
        "unknown_flag_high_bit": "miss_preserve",
        "reserved_one_preserved": "miss_preserve",
        "reserved_ff_preserved": "miss_preserve",
        "unknown_flag_expired": "miss_preserve",
        "reserved_nonzero_expired": "miss_preserve",
    }


@pytest.mark.parametrize("vector", PRESERVE, ids=lambda v: v["name"])
@pytest.mark.parametrize("read,miss", READ_PATHS, ids=lambda p: getattr(p, "__name__", repr(p)))
async def test_miss_preserve(tmp_path: Path, vector: dict[str, Any], read: Callable[..., Any], miss: Any) -> None:
    backend, path = _place(tmp_path, vector)
    before = path.stat()

    with _reader_clock(vector):
        result = read(backend, vector["key_utf8"])
        if hasattr(result, "__await__"):
            result = await result

    assert result is miss
    assert path.read_bytes() == bytes.fromhex(vector["file_hex"])  # not deleted, not rewritten
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


@pytest.mark.parametrize("vector", RETURN_PAYLOAD, ids=lambda v: v["name"])
def test_return_payload(tmp_path: Path, vector: dict[str, Any]) -> None:
    backend, _ = _place(tmp_path, vector)
    assert backend.get(vector["key_utf8"]) == bytes.fromhex(vector["payload_hex"])


@pytest.mark.parametrize("read,miss", READ_PATHS, ids=lambda p: getattr(p, "__name__", repr(p)))
async def test_miss_expired(tmp_path: Path, read: Callable[..., Any], miss: Any) -> None:
    vector = VECTORS["expired_entry"]
    assert vector["reader_now_unix_seconds"] == vector["expiry_unix_seconds"]  # the boundary instant
    backend, _ = _place(tmp_path, vector)

    with _reader_clock(vector):
        result = read(backend, vector["key_utf8"])
        if hasattr(result, "__await__"):
            result = await result

    assert result is miss
