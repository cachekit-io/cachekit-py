"""Guard against feature-guide index drift (LAB-1013).

Every guide in docs/features/*.md must be reachable from the repo's index
surfaces. Historically guides were "born orphaned": five of nine were listed
in no index at all because nothing checked reachability. This test is that
check — a new guide added without index links fails here with its filename.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# All three surfaces index the full guide set (deliberate call on LAB-1013;
# the top-level README is NOT a curated subset).
INDEX_FILES = ["README.md", "docs/README.md", "llms.txt"]


def _is_linked(index_text: str, guide_name: str) -> bool:
    """True if the guide is reachable as a rendered link in the index text.

    Two link forms exist across the surfaces (paths are features/x.md or
    docs/features/x.md):
    - inline: ``[Name](docs/features/x.md)``
    - reference-style (README.md): ``[Name][label]`` + ``[label]: docs/features/x.md``.
      A definition whose label is never used renders as nothing, so the bare
      path substring is not enough — the label must appear as ``][label]``.
    """
    target = re.escape(f"features/{guide_name}")
    if re.search(rf"\]\([^)]*{target}\)", index_text):
        return True
    for m in re.finditer(rf"^\[([^\]]+)\]:\s*\S*{target}\s*$", index_text, re.MULTILINE):
        if f"][{m.group(1)}]" in index_text:
            return True
    return False


@pytest.mark.parametrize("index_file", INDEX_FILES)
def test_every_feature_guide_is_indexed(index_file: str):
    """Each docs/features/*.md must be linked from every index surface."""
    guides = sorted((REPO_ROOT / "docs" / "features").glob("*.md"))
    assert guides, "docs/features/ contains no guides — glob path broken?"

    index_text = (REPO_ROOT / index_file).read_text(encoding="utf-8")
    orphans = [g.name for g in guides if not _is_linked(index_text, g.name)]
    assert not orphans, f"Feature guides missing from {index_file}: {orphans}"
