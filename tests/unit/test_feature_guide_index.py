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
# the top-level README is NOT a curated subset). Each maps to the exact link
# prefix a guide path must carry ON THAT SURFACE to resolve for a reader —
# docs/README.md links are relative to docs/, the other two to the repo root.
# A shared optional prefix would count a link that 404s on its own surface
# (e.g. docs/features/x.md written inside docs/README.md) as indexed.
INDEX_FILES = {
    "README.md": "docs/features/",
    "docs/README.md": "features/",
    "llms.txt": "docs/features/",
}


def _strip_non_rendered(text: str) -> str:
    """Remove markdown content that never renders: fenced code blocks and HTML comments.

    A link-shaped string inside either would satisfy the regexes below without
    being reachable by a reader. Backtick fences only — that is what these
    index files use. One alternation, not two passes: whichever construct
    opens first consumes the other, matching how markdown resolves the overlap.
    """
    text = re.sub(r"```.*?```|<!--.*?-->", "", text, flags=re.DOTALL)
    # An odd fence count skews the non-greedy pairing and silently un-strips a
    # block — the exact false-pass this helper exists to prevent. Fail loud.
    assert "```" not in text, "unpaired ``` fence — stripping unreliable"
    return text


def _is_linked(index_text: str, guide_name: str, prefix: str) -> bool:
    """True if the guide is reachable as a rendered link in the index text.

    ``prefix`` is the surface's exact link prefix from INDEX_FILES, anchored
    to the link target's start — so an offsite same-suffix URL or a
    wrong-prefix path that 404s on this surface does not count. A trailing
    ``#fragment`` or ``?query`` still resolves to the same file, so it counts;
    any other character after ``.md`` does not. Two link forms exist:
    - inline: ``[Name](docs/features/x.md)``
    - reference-style (README.md): ``[Name][label]`` + ``[label]: docs/features/x.md``.
      A definition whose label is never used renders as nothing, so the bare
      path substring is not enough — the label must appear as ``][label]``.
    """
    index_text = _strip_non_rendered(index_text)
    target = re.escape(f"{prefix}{guide_name}") + r"(?:[#?][^)\s]*)?"
    if re.search(rf"\]\({target}\)", index_text):
        return True
    for m in re.finditer(rf"^\[([^\]]+)\]:\s*{target}\s*$", index_text, re.MULTILINE):
        if f"][{m.group(1)}]" in index_text:
            return True
    return False


def test_is_linked_counts_rendered_links_only():
    """Link-shaped text in fenced code or HTML comments must not satisfy the guard."""
    assert _is_linked("[X](docs/features/x.md)", "x.md", "docs/features/")
    assert _is_linked("See [X][x-url].\n\n[x-url]: docs/features/x.md", "x.md", "docs/features/")
    assert not _is_linked("```\n[X](docs/features/x.md)\n```", "x.md", "docs/features/")
    assert not _is_linked("<!-- [X](docs/features/x.md) -->", "x.md", "docs/features/")
    # Definition never used renders as nothing.
    assert not _is_linked("[x-url]: docs/features/x.md", "x.md", "docs/features/")


def test_is_linked_accepts_fragment_and_query_suffixes():
    """#fragment / ?query target the same file — indexed; other suffixes are not it."""
    assert _is_linked("[X](docs/features/x.md#anchor)", "x.md", "docs/features/")
    assert _is_linked("[X](docs/features/x.md?plain=1)", "x.md", "docs/features/")
    assert _is_linked("See [X][x].\n\n[x]: docs/features/x.md#anchor", "x.md", "docs/features/")
    # A longer filename sharing the prefix is a different file.
    assert not _is_linked("[X](docs/features/x.mdx)", "x.md", "docs/features/")


def test_is_linked_requires_the_surfaces_own_prefix():
    """A link that 404s on its own surface must not count as indexed.

    These are the two realistic copy-paste-between-surfaces mistakes:
    a root-relative path inside docs/README.md and a docs-relative path
    inside the top-level README.
    """
    assert not _is_linked("[X](docs/features/x.md)", "x.md", "features/")
    assert not _is_linked("[X](features/x.md)", "x.md", "docs/features/")


@pytest.mark.parametrize(("index_file", "prefix"), sorted(INDEX_FILES.items()))
def test_every_feature_guide_is_indexed(index_file: str, prefix: str):
    """Each docs/features/*.md must be linked from every index surface."""
    guides = sorted((REPO_ROOT / "docs" / "features").glob("*.md"))
    assert guides, "docs/features/ contains no guides — glob path broken?"

    index_text = (REPO_ROOT / index_file).read_text(encoding="utf-8")
    orphans = [g.name for g in guides if not _is_linked(index_text, g.name, prefix)]
    assert not orphans, f"Feature guides missing from {index_file}: {orphans}"
