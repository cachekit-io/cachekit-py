"""FileBackend on-disk format against the protocol test vectors.

Fixture: tests/unit/protocol/fixtures/file-backend.json, vendored from
cachekit-io/protocol @ 63539b42f5ed328d2b54646b999e415af8338798 (vectors 1.1.0,
sha256 8d9d8c4709baf9ef3a8f2d71d21fb5a56207bc7a05c9bc7a967ac567f2604615).
Regenerate ONLY by re-copying from the protocol repo — never by hand.

spec/file-backend-format.md, "Version and flag negotiation": a nonzero reserved byte or
flag can indicate a future payload transform, so a reader that does not implement it
MUST return a miss and MUST NOT delete, rewrite, or return the payload. Every read path
is checked against that, plus a derived expired-and-flagged entry: the spec checks flags
before expiry, and its MUST NOT delete has no expiry exception.

The other vectors are read at the real wall clock, not frozen at ``reader_now_unix_seconds``:
py's expiry test is a strict ``now > expiry``, so at exactly the vector's clock
``expired_entry`` would still return its payload. That boundary is out of scope here.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cachekit.backends.file.backend import FileBackend
from cachekit.backends.file.config import FileBackendConfig

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "file-backend.json"
FIXTURE_SHA256 = "8d9d8c4709baf9ef3a8f2d71d21fb5a56207bc7a05c9bc7a967ac567f2604615"  # pragma: allowlist secret

VECTORS: dict[str, dict[str, Any]] = {v["name"]: v for v in json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["vectors"]}

_PAST_EXPIRY = 1_000_000_000  # 2001-09-09; long gone at any real wall clock


def _expired_flagged() -> dict[str, Any]:
    """``unknown_flag_preserved`` with its expiry (bytes 6-13) set in the past."""
    v = dict(VECTORS["unknown_flag_preserved"])
    raw = bytearray.fromhex(v["file_hex"])
    raw[6:14] = struct.pack(">Q", _PAST_EXPIRY)
    v["name"] = "unknown_flag_preserved_expired"
    v["file_hex"] = raw.hex()
    return v


PRESERVE = [VECTORS["unknown_flag_preserved"], VECTORS["reserved_nonzero_preserved"], _expired_flagged()]


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
    }


@pytest.mark.parametrize("vector", PRESERVE, ids=lambda v: v["name"])
@pytest.mark.parametrize("read,miss", READ_PATHS, ids=lambda p: getattr(p, "__name__", repr(p)))
async def test_miss_preserve(tmp_path: Path, vector: dict[str, Any], read: Callable[..., Any], miss: Any) -> None:
    backend, path = _place(tmp_path, vector)
    before = path.stat()

    result = read(backend, vector["key_utf8"])
    if hasattr(result, "__await__"):
        result = await result

    assert result is miss
    assert path.read_bytes() == bytes.fromhex(vector["file_hex"])  # not deleted, not rewritten
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


@pytest.mark.parametrize("name", ["permanent_ascii", "future_expiry"])
def test_return_payload(tmp_path: Path, name: str) -> None:
    vector = VECTORS[name]
    backend, _ = _place(tmp_path, vector)
    assert backend.get(vector["key_utf8"]) == bytes.fromhex(vector["payload_hex"])


def test_miss_expired(tmp_path: Path) -> None:
    vector = VECTORS["expired_entry"]
    backend, _ = _place(tmp_path, vector)
    assert backend.get(vector["key_utf8"]) is None
