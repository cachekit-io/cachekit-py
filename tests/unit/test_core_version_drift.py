"""Every hand-maintained cachekit-core version names the release the wrapper ships.

The root ``Cargo.lock`` is the source of truth. The fuzz crate resolves in its own
``[workspace]``, so its exact pin and its lock are what make the fuzzers exercise the
shipped core; the free-threading doc row declares which core the verdict covers. None of
these move with a lockfile bump unless someone moves them. ``PROBE_VERIFIED_CORE_VERSION``
is the fourth value and has its own gate in ``tests/unit/protocol/test_envelope_wire_vectors.py``.

``rust/Cargo.toml``'s caret requirement is deliberately not checked: it is a floor and
stays behind on purpose.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).parents[2]

_LOCK_ENTRY = r'name = "cachekit-core"\nversion = "([^"]+)"'
_MOVE_TOGETHER = (
    "All four cachekit-core values move together in one diff: rust/fuzz/Cargo.toml's exact pin,"
    " rust/fuzz/Cargo.lock, docs/free-threading.md's Rust extension row, and PROBE_VERIFIED_CORE_VERSION."
)


def _read_version(relpath: str, pattern: str) -> str:
    """The version ``pattern`` captures in ``relpath``; fails by file name when there is no match."""
    match = re.search(pattern, (REPO_ROOT / relpath).read_text(encoding="utf-8"), re.MULTILINE)
    if match is None:
        pytest.fail(f"{relpath}: no cachekit-core version found (pattern {pattern!r})")
    return match[1]


@pytest.mark.parametrize(
    ("relpath", "pattern"),
    [
        ("rust/fuzz/Cargo.toml", r'^\[dependencies\.cachekit_storage\]\n(?:[^\[\n].*\n)*?version = "=([^"]+)"'),
        ("rust/fuzz/Cargo.lock", _LOCK_ENTRY),
        ("docs/free-threading.md", r"^\| Rust extension \(`rust/src/`, cachekit-core ([0-9][^)]*)\) \|"),
    ],
)
def test_core_version_matches_shipped_release(relpath, pattern):
    shipped = _read_version("Cargo.lock", _LOCK_ENTRY)
    found = _read_version(relpath, pattern)
    assert found == shipped, f"{relpath} names cachekit-core {found}; Cargo.lock ships {shipped}. {_MOVE_TOGETHER}"
