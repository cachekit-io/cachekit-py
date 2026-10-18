"""Cache-key path encoding against the protocol test vectors (LAB-2880, LAB-6550, LAB-8243).

Fixture: tests/unit/protocol/fixtures/path-encoding.json, vendored from
cachekit-io/protocol @ f35635445a6a7461f93061aa51332e4f0d25c614 (vectors 1.2.0,
sha256 807af0d39ccf3f5afda65577787ac5adcd01ff6617468060161c84267fa7e0cb).
Regenerate ONLY by re-copying from the protocol repo — never by hand.

Row semantics live in the fixture's ``contract`` field (spec/saas-api.md
§ Cache-Key Path Encoding): a transmittable row conforms when the SDK's encoding
is in ``[encoded] + encoded_alternates``; a ``reject: true`` row is a reserved
segment (rule 2) the client must refuse before building the URL.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from cachekit.backends.cachekitio.backend import CachekitIOBackend
from cachekit.backends.errors import BackendError, BackendErrorType

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "path-encoding.json"
FIXTURE_SHA256 = "807af0d39ccf3f5afda65577787ac5adcd01ff6617468060161c84267fa7e0cb"  # pragma: allowlist secret

VECTORS = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["vectors"]

_TRANSMITTABLE = [v for v in VECTORS if not v.get("reject")]
_REJECT = [v for v in VECTORS if v.get("reject")]


def test_fixture_integrity() -> None:
    """The vendored fixture is byte-identical to the pinned protocol revision."""
    digest = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    assert digest == FIXTURE_SHA256, (
        f"fixtures/path-encoding.json sha256 {digest} != pinned {FIXTURE_SHA256}. "
        "If the protocol vectors were intentionally updated, refresh the pin."
    )


@pytest.mark.parametrize("vector", _TRANSMITTABLE, ids=lambda v: v["key"])
def test_transmittable_vectors(vector: dict[str, Any]) -> None:
    # Python's reference encoder is `quote(safe="")`, so it must hit `encoded` exactly,
    # not merely an alternate: that is what keeps it byte-identical to cachekit-rs.
    assert CachekitIOBackend._encode_key(vector["key"]) == vector["encoded"]


@pytest.mark.parametrize("vector", _REJECT, ids=lambda v: v["key"])
def test_reject_vectors(vector: dict[str, Any]) -> None:
    with pytest.raises(BackendError) as excinfo:
        CachekitIOBackend._encode_key(vector["key"])
    assert excinfo.value.error_type is BackendErrorType.PERMANENT
