"""Hypothesis property-based testing strategies for security testing."""

from __future__ import annotations

from typing import Any

from hypothesis import strategies as st


class SecurityFuzzingStrategies:
    """Strategies for generating security-critical test data."""

    @staticmethod
    def tenant_ids() -> st.SearchStrategy[str]:
        """Generate valid UUID tenant IDs.

        Returns:
            Hypothesis strategy that generates UUID strings.
        """
        return st.uuids().map(str)

    @staticmethod
    def encryption_keys() -> st.SearchStrategy[bytes]:
        """Generate valid encryption keys (32+ bytes).

        Returns:
            Hypothesis strategy that generates 32-64 byte binary keys.
        """
        return st.binary(min_size=32, max_size=64)

    @staticmethod
    def cache_payloads() -> st.SearchStrategy[Any]:
        """Generate diverse cache payloads for security testing.

        Returns:
            Hypothesis strategy that generates various data types.
        """
        return st.one_of(
            st.integers(),
            st.floats(allow_nan=False, allow_infinity=False),
            st.text(),
            st.binary(),
            st.lists(st.integers(), max_size=1000),
            st.dictionaries(st.text(), st.integers(), max_size=100),
        )
