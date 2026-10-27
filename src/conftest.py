"""Names the docstring examples under src/cachekit use for their keys.

Docstrings feed the generated API reference, so no example carries a literal key: each reads one of these
names instead. Test values only. markdown fences get the same secret_key from docs/conftest.py.
"""

import pytest

SECRET_KEY = "a" * 64  # 32 bytes in hex; has letters, so .upper() changes its case
OLD_SECRET_KEY = "b" * 64  # a retired key, distinct from SECRET_KEY
API_KEY = "ck_test_key"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _inject_doctest_keys(doctest_namespace: dict[str, object]) -> None:
    doctest_namespace.update(
        secret_key=SECRET_KEY,
        old_secret_key=OLD_SECRET_KEY,
        secret_key_bytes=bytes.fromhex(SECRET_KEY),
        old_secret_key_bytes=bytes.fromhex(OLD_SECRET_KEY),
        api_key=API_KEY,
    )
