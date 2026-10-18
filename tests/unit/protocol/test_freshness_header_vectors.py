"""Freshness response headers against the protocol test vectors (LAB-8243).

Fixture: tests/unit/protocol/fixtures/freshness-headers.json, vendored from
cachekit-io/protocol @ f35635445a6a7461f93061aa51332e4f0d25c614 (vectors 1.0.0,
sha256 74beb975b6855f52d9dd453279873faf8048a9c3c79a8167cfda099beec5fcd3).
Regenerate ONLY by re-copying from the protocol repo — never by hand.

Row semantics live in the fixture's ``contract`` field (spec/saas-api.md § Remaining
Freshness): each row is one header's value on a GET 200, ``null`` meaning the header is
absent. urllib3 decodes header bytes as latin-1, one character per byte, which is exactly
how the fixture writes ``value`` — so each value goes onto the response unchanged.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from cachekit.backends.cachekitio.backend import FRESH_FOR_HEADER, FRESHNESS_HEADER, CachekitIOBackend
from tests.utils.cachekitio_fakes import response

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "freshness-headers.json"
FIXTURE_SHA256 = "74beb975b6855f52d9dd453279873faf8048a9c3c79a8167cfda099beec5fcd3"  # pragma: allowlist secret

FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _get_200(header: str, value: str | None) -> Any:
    return response(200, b"payload", headers=None if value is None else {header: value})


def test_fixture_integrity() -> None:
    """The vendored fixture is byte-identical to the pinned protocol revision."""
    digest = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    assert digest == FIXTURE_SHA256, (
        f"fixtures/freshness-headers.json sha256 {digest} != pinned {FIXTURE_SHA256}. "
        "If the protocol vectors were intentionally updated, refresh the pin."
    )


@pytest.mark.parametrize("vector", FIXTURE["freshness_vectors"], ids=lambda v: v["name"])
def test_freshness_vectors(vector: dict[str, Any]) -> None:
    assert CachekitIOBackend._is_stale(_get_200(FRESHNESS_HEADER, vector["value"])) is vector["stale"]


@pytest.mark.parametrize("vector", FIXTURE["fresh_for_vectors"], ids=lambda v: v["name"])
def test_fresh_for_vectors(vector: dict[str, Any]) -> None:
    # Through the response, so the header name is exercised along with _parse_fresh_for's grammar.
    assert CachekitIOBackend._fresh_for(_get_200(FRESH_FOR_HEADER, vector["value"])) == vector["fresh_for"]
