"""Helpers for MemcachedBackend tests that stand a MagicMock in for pymemcache's HashClient."""

from __future__ import annotations

from unittest.mock import MagicMock


def mock_hash_client() -> MagicMock:
    """A MagicMock HashClient whose single-key commands land on its own public methods.

    MemcachedBackend sends single-key commands through HashClient's ``_get_client`` and
    ``_safely_run_func`` to detect a skipped send. Here ``_get_client`` returns the mock itself
    and ``_safely_run_func`` just calls the command, so tests configure and assert on
    ``mock.get``/``mock.set``/``mock.delete``/``mock.touch`` as before.
    """
    client = MagicMock()
    client._get_client.return_value = client
    client._safely_run_func.side_effect = lambda _client, func, _default, *args, **kwargs: func(*args, **kwargs)
    return client
