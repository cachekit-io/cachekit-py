"""Byte-verification of auto-mode cache keys against the protocol test vectors.

Fixture: tests/unit/protocol/fixtures/cache-keys.json, vendored from
cachekit-io/protocol @ f0672c1cf2a3bbd2ba3f4760b6d1406a4357aab9
(sha256 4a0ae13dfa745a1a2505f2a10148db7d55f23f9b01636000cf477e805b86450e).
Regenerate ONLY by re-copying from the protocol repo — never by hand.

Each vector pins the full 7-segment auto-mode key
(``ns:{ns}:func:{module}.{qualname}:args:{blake2b_256_hex}:{ic_flag}{code}``)
for a given (args, kwargs, namespace, integrity_checking, serializer_type)
tuple. The vectors were generated at top level, so the module path is
``__main__`` — reproduced here by stubbing ``__module__``/``__qualname__``
on a throwaway function, which lets the test assert the FULL key, not just
the args-hash segment.

A failure here is a key-stability break to triage, never a fixture to
silently regenerate: a changed auto-mode key orphans every existing cache
entry (silent 100% miss storm, billed as misses under metered pricing).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from cachekit.key_generator import CacheKeyGenerator

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "cache-keys.json"
FIXTURE_SHA256 = "4a0ae13dfa745a1a2505f2a10148db7d55f23f9b01636000cf477e805b86450e"

VECTORS = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

# Part of the conformance claim: a fixture update that adds or removes
# vectors must be a conscious change, not a silent drift.
EXPECTED_VECTOR_COUNT = 10


def test_fixture_integrity():
    """The vendored fixture is byte-identical to the pinned protocol revision."""
    digest = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    assert digest == FIXTURE_SHA256, (
        f"fixtures/cache-keys.json sha256 {digest} != pinned {FIXTURE_SHA256}. "
        "If the protocol vectors were intentionally updated, refresh the pin AND the count."
    )


def test_vector_count():
    assert len(VECTORS["vectors"]) == EXPECTED_VECTOR_COUNT


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda v: v["name"])
def test_cache_key_vectors(vector: dict[str, Any]):
    """CacheKeyGenerator reproduces every pinned auto-mode key byte-for-byte."""

    def stub() -> None:  # pragma: no cover - identity carrier, never called
        pass

    stub.__module__ = vector["function_module"]
    stub.__qualname__ = vector["function_qualname"]

    key = CacheKeyGenerator().generate_key(
        stub,
        tuple(vector["args"]),
        vector["kwargs"],
        namespace=vector["namespace"],
        integrity_checking=vector["integrity_checking"],
        serializer_type=vector["serializer_type"],
    )
    assert key == vector["expected_key"], (
        f"auto-mode key drift for vector {vector['name']!r} — this breaks key "
        "stability for every deployed cache entry; triage the generator change, "
        "do NOT regenerate the vectors."
    )
