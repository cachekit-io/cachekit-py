"""Regression guard for issue #205.

The markdown-docs suite runs only in the post-merge CI job (push to main), not
on PRs, so a regression here would turn `main` red instead of failing review.
This fast unit test runs on every PR and fails loudly if `docs/conftest.py`
starts setting CACHEKIT_MASTER_KEY in the process environment again.

Why it matters: an ambient master key turns encryption on globally (PR #127
auto-detect + PR #200 settings re-read), and the v0.6.0 cross-SDK rule then
rejects every plain `@cache` doc fence that uses a non-cross-SDK serializer
(serializer="auto", custom serializer instances) at decoration time.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

DOCS_CONFTEST = Path(__file__).resolve().parents[2] / "docs" / "conftest.py"


def _load_docs_conftest():
    spec = importlib.util.spec_from_file_location("docs_conftest_under_test", DOCS_CONFTEST)
    assert spec and spec.loader, f"could not load {DOCS_CONFTEST}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_docs_globals_hook_does_not_set_master_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invoking the markdown-docs globals hook must not leak CACHEKIT_MASTER_KEY."""
    monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)

    module = _load_docs_conftest()
    globals_dict = module.pytest_markdown_docs_globals()

    assert "CACHEKIT_MASTER_KEY" not in os.environ, (
        "docs/conftest.py set CACHEKIT_MASTER_KEY in the environment. This turns "
        "encryption on globally and breaks plain @cache fences using serializer='auto' "
        "or custom serializers. Pass the key explicitly (master_key=secret_key) in the "
        "@cache.secure fences instead. See issue #205."
    )
    # The key must still be available to fences that opt in explicitly.
    assert globals_dict.get("secret_key") == "a" * 64
