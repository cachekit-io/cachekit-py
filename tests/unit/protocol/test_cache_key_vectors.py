"""Byte-verification of auto-mode cache keys against the protocol test vectors.

Fixture: tests/unit/protocol/fixtures/cache-keys.json, vendored from
cachekit-io/protocol @ b4ae567a402051752dabf040bd16e870657d3dec (vectors 1.3.0,
sha256 e75fb8f98d0e54fcb0d586cae72b8aa6129ed082f254539b450a47ba8e96167a).
Regenerate ONLY by re-copying from the protocol repo — never by hand.

Each vector pins the full 7-segment auto-mode key
(``ns:{ns}:func:{module}.{qualname}:args:{blake2b_256_hex}:{ic_flag}{code}``)
for a given (args, kwargs, namespace, integrity_checking, serializer_type)
tuple. The vectors were generated at top level, so the module path is
``__main__`` — reproduced here by stubbing ``__module__``/``__qualname__``
on a throwaway function, which lets the test assert the FULL key, not just
the args-hash segment.

``error_vectors`` give serializer identities that must raise, from the key
generator and from a cache configured with them. ``serializer_object_vectors``
give a serializer as an instance: a cache configured with it must hand the
backend the pinned key, observed at the backend, not handed to the generator.

A failure here is a key-stability break to triage, never a fixture to
silently regenerate: a changed auto-mode key orphans every existing cache
entry (silent 100% miss storm, billed as misses under metered pricing).
"""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from cachekit import cache
from cachekit.key_generator import CacheKeyGenerator
from cachekit.serializers.standard_serializer import StandardSerializer
from tests.unit.protocol.conftest import KeyRecordingBackend

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "cache-keys.json"
FIXTURE_SHA256 = "e75fb8f98d0e54fcb0d586cae72b8aa6129ed082f254539b450a47ba8e96167a"  # pragma: allowlist secret

VECTORS = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

# Part of the conformance claim: a fixture update that adds or removes
# vectors must be a conscious change, not a silent drift.
EXPECTED_VECTOR_COUNT = 21
EXPECTED_ERROR_VECTOR_COUNT = 4
EXPECTED_SERIALIZER_OBJECT_VECTOR_COUNT = 5
TOP_LEVEL_KEYS = {
    "version", "generator", "ci_verification", "note", "key_format", "hash_algorithm",
    "vectors", "error_vectors", "serializer_object_vectors",
}  # fmt: skip

# serializer_object_vectors' serializer_class: the SDK's own class (defined_by sdk) as (module, the optional
# package it needs), imported when a test runs so a lane without the [data]/[json] extras still collects; or
# a class this test defines with that name and the SDK's serializer interface (defined_by test).
SDK_SERIALIZER_CLASSES: dict[str, tuple[str, str | None]] = {
    "ArrowSerializer": ("cachekit.serializers.arrow_serializer", "pyarrow"),
    "AutoSerializer": ("cachekit.serializers.auto_serializer", None),
    "OrjsonSerializer": ("cachekit.serializers.orjson_serializer", "orjson"),
    "StandardSerializer": ("cachekit.serializers.standard_serializer", None),
}
TEST_SERIALIZER_CLASSES: dict[str, type] = {"auto": type("auto", (StandardSerializer,), {})}


def _serializer_class(name: str) -> type:
    if name in TEST_SERIALIZER_CLASSES:
        return TEST_SERIALIZER_CLASSES[name]
    module, requires = SDK_SERIALIZER_CLASSES[name]
    if requires is not None:
        pytest.importorskip(requires)
    return getattr(importlib.import_module(module), name)


def _stub(vector: dict[str, Any]):
    def stub(*args: Any, **kwargs: Any) -> str:
        return "computed"

    stub.__module__ = vector["function_module"]
    stub.__qualname__ = vector["function_qualname"]
    return stub


def _keys_through_cache(vector: dict[str, Any], serializer: Any) -> set[str]:
    """Every key a cache configured with ``serializer`` hands its backend for the vector's call."""
    backend = KeyRecordingBackend()
    cached = cache(
        backend=backend,
        ttl=60,
        l1_enabled=False,
        namespace=vector["namespace"],
        integrity_checking=vector["integrity_checking"],
        serializer=serializer,
    )(_stub(vector))
    cached(*vector["args"], **vector["kwargs"])
    return set(backend.keys)


def test_fixture_integrity():
    """The vendored fixture is byte-identical to the pinned protocol revision and holds only the tables driven here."""
    digest = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    assert digest == FIXTURE_SHA256, (
        f"fixtures/cache-keys.json sha256 {digest} != pinned {FIXTURE_SHA256}. "
        "If the protocol vectors were intentionally updated, refresh the pin and every version, count and name pinned."
    )
    assert set(VECTORS) == TOP_LEVEL_KEYS


def test_vector_count():
    assert VECTORS["version"] == "1.3.0"
    assert len(VECTORS["vectors"]) == EXPECTED_VECTOR_COUNT
    assert len(VECTORS["error_vectors"]) == EXPECTED_ERROR_VECTOR_COUNT
    assert len(VECTORS["serializer_object_vectors"]) == EXPECTED_SERIALIZER_OBJECT_VECTOR_COUNT


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda v: v["name"])
def test_cache_key_vectors(vector: dict[str, Any]):
    """CacheKeyGenerator reproduces every pinned auto-mode key byte-for-byte."""
    key = CacheKeyGenerator().generate_key(
        _stub(vector),
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


@pytest.mark.parametrize("vector", VECTORS["error_vectors"], ids=lambda v: v["name"])
class TestErrorVectors:
    """An empty or non-string serializer identity is an error, never a code."""

    def test_key_generation_raises(self, vector: dict[str, Any]):
        with pytest.raises((TypeError, ValueError), match="serializer_type must"):
            CacheKeyGenerator().generate_key(
                _stub(vector),
                tuple(vector["args"]),
                vector["kwargs"],
                namespace=vector["namespace"],
                integrity_checking=vector["integrity_checking"],
                serializer_type=vector["serializer_type"],
            )

    def test_configured_cache_raises(self, vector: dict[str, Any]):
        with pytest.raises((TypeError, ValueError), match=r"^(Unknown serializer: ''|serializer must be a string name)"):
            _keys_through_cache(vector, vector["serializer_type"])


@pytest.mark.parametrize("vector", VECTORS["serializer_object_vectors"], ids=lambda v: v["name"])
def test_serializer_object_vectors(vector: dict[str, Any]):
    """A cache configured with a serializer instance keys the call as pinned (the identity is the class name)."""
    assert (vector["serializer_class"] in SDK_SERIALIZER_CLASSES) == (vector["defined_by"] == "sdk")
    serializer = _serializer_class(vector["serializer_class"])()
    assert _keys_through_cache(vector, serializer) == {vector["expected_key"]}
