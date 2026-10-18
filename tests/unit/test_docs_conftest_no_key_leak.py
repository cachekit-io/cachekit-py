"""Regression guard for issue #205.

The markdown-docs suite runs only in the post-merge CI job (push to main), not
on PRs, so a regression here would turn `main` red instead of failing review.
This fast unit test runs on every PR and fails loudly if `docs/conftest.py`
starts setting CACHEKIT_MASTER_KEY in the process environment again.

Why it matters: with an ambient master key, every plain `@cache` doc fence that
states no encryption intent raises ConfigurationError at decoration time.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

# docs/conftest.py imports numpy/pandas at module level (its fences use them) —
# absent e.g. in the free-threaded CI lane (LAB-511).
pytest.importorskip("numpy")
pytest.importorskip("pandas")

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
        "docs/conftest.py set CACHEKIT_MASTER_KEY in the environment. Then every plain "
        "@cache fence that states no encryption intent raises ConfigurationError at "
        "decoration. Pass the key explicitly (master_key=secret_key) in the "
        "@cache.secure fences instead. See issue #205."
    )
    # The key must still be available to fences that opt in explicitly.
    assert globals_dict.get("secret_key") == "a" * 64
